"""Policy service switching for the FR3 bridge (fr3_policy_swap.py, T-C-SWITCH)
on a fake ``systemd-run``/``systemctl``, a fake ``/proc`` tree, a fake
``nvidia-smi`` and a fake arm. No unit tool or GPU query is ever executed and
no socket is opened (tests/conftest.py refuses protected ports)."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from fr3_swap_fakes import HOME, Proc, Smi, Time, Units, refuse_unit_tools

from levi.automatic import fr3_policy_swap as S
from levi.automatic import state_machine as sm


@pytest.fixture(autouse=True)
def no_unit_tools(monkeypatch):
    refuse_unit_tools(monkeypatch)


DEPLOYMENTS = {
    "forward": {"config": "cfg_fwd", "checkpoint_dir": "/models/forward"},
    "reset": {"config": "cfg_reset", "checkpoint_dir": "/models/reset"},
}
SAFETY = {"home_pose": HOME, "home_tolerance_mm": 5}


def block(tmp_path, *, ports=(8000, 8001), **changes):
    services = {}
    for (role, deployment), port in zip(DEPLOYMENTS.items(), ports, strict=True):
        services[role] = {
            "unit": "levi-aeri-policy-" + role,
            "argv": [
                sys.executable,
                "serve.py",
                "--port",
                str(port),
                "--policy.config",
                deployment["config"],
                "--policy.dir",
                deployment["checkpoint_dir"],
            ],
            "working_directory": str(tmp_path),
            "mem_fraction": 0.22,
        }
    value = {
        "schema": S.SCHEMA,
        "services": services,
        "vision_mem_fraction": 0.72,
        "max_total_fraction": 0.96,
        "gpu_margin_mib": 512,
        "stop_timeout_s": 20,
        "start_timeout_s": 60,
        "handshake_timeout_s": 10,
        "warmup_timeout_s": 10,
        "start_retries": 1,
        "still_window_ms": 200,
        "still_tolerance_mm": 1.0,
        "still_tolerance_rad": 0.01,
    }
    value.update(changes)
    return value


def policies(ports=(8000, 8001)):
    return {
        role: {"host": "127.0.0.1", "port": port}
        for role, port in zip(("forward", "reset"), ports, strict=True)
    }


def parse(tmp_path, value, ports=(8000, 8001)):
    return S.parse(
        value, policies=policies(ports), deployments=DEPLOYMENTS, safety=SAFETY
    )


class World:
    """One switcher over the fakes, a fake arm and a fence."""

    def __init__(self, tmp_path, *, ports=(8000, 8001), **changes):
        self.tmp = tmp_path
        self.time = Time()
        self.proc = Proc(tmp_path / "proc")
        self.gpu = Smi()
        self.fence = sm.MotionFence()
        self.steps = 0
        self.pose = list(HOME)
        self.cancel = False
        self.handshakes = []
        self.identity = {}  # role -> metadata override
        self.hook_errors = []
        self.units = Units(self.proc, self.gpu, hook=self.check_invariants)
        self.time.tick = self.units.tick
        self.cfg = parse(tmp_path, block(tmp_path, ports=ports, **changes), ports)
        self.log_path = S.log_path(tmp_path / "rollouts", "run-1")
        self.log = S.SwapLog.create(self.log_path, run_id="run-1", units={})
        self.switcher = self.make(self.log)
        self.warmups = []

    def make(self, log):
        return S.PolicySwitcher(
            self.cfg,
            log=log,
            handshake=self.handshake,
            run_id="run-1",
            systemd=self.units,
            proc_root=self.proc.root,
            cgroup_root=self.proc.cgroup_root,
            gpu=S.Gpu(self.gpu),
            clock=self.time.clock,
            sleep=self.time.sleep,
            poll_s=0.01,
        )

    def bind(self, switcher=None):
        (switcher or self.switcher).bind_robot(
            S.RobotSide(
                pose=lambda: list(self.pose),
                home_pose=HOME,
                home_tolerance_mm=5,
                fence=self.fence,
                hold=lambda: None,
                motion_count=lambda: self.steps,
                warmup=self.warmup,
                cancelled=lambda: self.cancel,
            )
        )

    def handshake(self, role):
        self.handshakes.append(role)
        expected = {
            "config": DEPLOYMENTS[role]["config"],
            "checkpoint_dir": DEPLOYMENTS[role]["checkpoint_dir"],
        }
        metadata = {**expected, **self.identity.get(role, {})}
        if metadata != expected:
            raise ValueError("policy deployment identity mismatch")
        return NS(role=role)

    def warmup(self, role, client):
        self.warmups.append(role)
        return [[0.0] * 7] * 4

    def check_invariants(self, argv):
        listeners = [
            port for port in self.proc.sockets.values() if port in (8000, 8001)
        ]
        if self.fence.current is not None:
            self.hook_errors.append(("token", argv))
        if len(listeners) > 1:
            self.hook_errors.append(("two_policy_services", argv))

    def starts(self):
        return [c for c in self.units.calls if c[0] == "systemd-run"]

    def records(self):
        return [r["record"] for r in S.read_log(self.log_path)[0]]


# --- the binding block --------------------------------------------------------------


def test_parse_accepts_the_complete_block_and_builds_a_fixed_argv(tmp_path):
    cfg = parse(tmp_path, block(tmp_path))
    service = cfg.services["reset"]
    assert service.unit == "levi-aeri-policy-reset" and service.port == 8001
    argv = S.start_argv(cfg, service)
    assert argv[:2] == ["systemd-run", "--user"]
    assert "--setenv=XLA_PYTHON_CLIENT_MEM_FRACTION=0.22" in argv
    assert f"--working-directory={tmp_path}" in argv
    assert argv[argv.index("--") + 1 :] == list(service.argv)
    summary = S.summary(cfg)
    assert summary["units"] == {
        "forward": "levi-aeri-policy-forward",
        "reset": "levi-aeri-policy-reset",
    }
    assert summary["services_started_by_check"] is False
    # One port for both roles is fine: only one service ever runs.
    assert parse(tmp_path, block(tmp_path, ports=(8000, 8000)), (8000, 8000))


@pytest.mark.parametrize("key", sorted(S.KEYS))
def test_parse_requires_every_field_explicitly(tmp_path, key):
    value = block(tmp_path)
    del value[key]
    with pytest.raises(ValueError, match="requires exactly"):
        parse(tmp_path, value)
    value = block(tmp_path)
    value[key] = None
    with pytest.raises((ValueError, TypeError)):
        parse(tmp_path, value)


@pytest.mark.parametrize("key", sorted(S.SERVICE_KEYS))
def test_parse_requires_every_service_field(tmp_path, key):
    value = block(tmp_path)
    del value["services"]["reset"][key]
    with pytest.raises(ValueError, match="requires exactly"):
        parse(tmp_path, value)


@pytest.mark.parametrize(
    "change,message",
    [
        (lambda v: v.update(extra=1), "requires exactly"),
        (lambda v: v.update(schema="other"), "requires exactly"),
        (lambda v: v["services"].pop("reset"), "forward and reset"),
        (
            lambda v: v["services"]["reset"].update(unit="levi-live"),
            "levi-aeri-policy",
        ),
        (
            lambda v: v["services"]["reset"].update(unit="levi-aeri-policy-forward"),
            "distinct",
        ),
        (
            lambda v: v["services"]["reset"]["argv"].append("$HOME"),
            "no expansion",
        ),
        (
            lambda v: v["services"]["reset"]["argv"].append("50%"),
            "no expansion",
        ),
        (
            lambda v: v["services"]["reset"]["argv"].__setitem__(0, "python"),
            "absolute executable",
        ),
        (
            lambda v: v["services"]["reset"]["argv"].__setitem__(3, "8000"),
            "--port 8001",
        ),
        (
            lambda v: v["services"]["reset"]["argv"].__setitem__(5, "cfg_other"),
            "frozen reset config",
        ),
        (
            lambda v: v["services"]["reset"]["argv"].__setitem__(7, "models/reset"),
            "absolute frozen reset checkpoint_dir",
        ),
        (
            lambda v: v["services"]["reset"].update(working_directory="rel"),
            "working_directory",
        ),
        (
            lambda v: v["services"]["reset"].update(mem_fraction=0.6),
            "mem_fraction",
        ),
        (lambda v: v["services"]["reset"].update(mem_fraction=True), "mem_fraction"),
        (lambda v: v.update(vision_mem_fraction=0.8), "exceeds max_total_fraction"),
        (lambda v: v.update(max_total_fraction=1.2), "max_total_fraction"),
        (lambda v: v.update(start_retries=2), "0 or 1"),
        (lambda v: v.update(start_retries=True), "0 or 1"),
        (lambda v: v.update(stop_timeout_s=0), "stop_timeout_s"),
        (lambda v: v.update(start_timeout_s=float("inf")), "start_timeout_s"),
        (lambda v: v.update(still_window_ms=10), "still_window_ms"),
        (lambda v: v.update(still_tolerance_mm=6), "still_tolerance_mm"),
        (lambda v: v.update(still_tolerance_rad=0.2), "still_tolerance_rad"),
        (lambda v: v.update(gpu_margin_mib=-1), "gpu_margin_mib"),
    ],
)
def test_parse_refuses_unreviewed_values(tmp_path, change, message):
    value = block(tmp_path)
    change(value)
    with pytest.raises(ValueError, match=message):
        parse(tmp_path, value)


# --- the switch ---------------------------------------------------------------------


def test_switch_stops_old_then_starts_new_with_no_motion_and_one_service(tmp_path):
    w = World(tmp_path)
    client = w.switcher.startup("forward")
    assert client.role == "forward" and w.switcher.active == "forward"
    w.bind()
    assert w.switcher.ensure("forward") is None  # warm-up only
    assert w.warmups == ["forward"]
    forward_pid = w.units.units["levi-aeri-policy-forward"]
    client = w.switcher.ensure("reset")
    assert client.role == "reset" and w.switcher.active == "reset"
    assert w.hook_errors == []
    assert forward_pid not in w.proc.alive() and forward_pid not in w.gpu.pids
    order = [c[2] if c[0] == "systemctl" else "run" for c in w.units.calls]
    stop = order.index("stop")
    assert order.index("run", stop) > stop  # stop before the new start
    assert w.steps == 0 and w.fence.current is None
    assert w.handshakes == ["forward", "reset"]
    assert w.records() == [
        "header",
        "prepared",
        "step",
        "step",
        "step",
        "committed",
        "warm",
        "prepared",
        "step",
        "step",
        "step",
        "step",
        "step",
        "committed",
    ]
    committed = S.read_log(w.log_path)[0][-1]
    assert committed["service_state"] == "reset"


def test_switch_transaction_is_idempotent(tmp_path):
    w = World(tmp_path)
    w.switcher.startup("forward")
    w.bind()
    w.switcher.ensure("reset")
    calls, records = len(w.starts()), len(w.records())
    assert w.switcher.ensure("reset") is None
    assert w.switcher.ensure("reset") is None
    assert len(w.starts()) == calls and len(w.records()) == records
    assert not [c for c in w.units.calls[-4:] if c[2:3] == ["stop"]]


def test_port_not_released_refuses_and_latches(tmp_path):
    w = World(tmp_path)
    w.switcher.startup("forward")
    w.bind()
    w.switcher.ensure("forward")
    w.units.linger = 10_000  # the old service never lets go of its port
    starts = len(w.starts())
    with pytest.raises(S.SwapError, match="stop_timeout"):
        w.switcher.ensure("reset")
    assert len(w.starts()) == starts  # the new service never started
    last = S.read_log(w.log_path)[0][-1]
    assert last["record"] == "aborted" and last["service_state"] == "unknown"
    with pytest.raises(S.SwapError, match="latched"):
        w.switcher.ensure("reset")
    assert len(w.starts()) == starts


def test_gpu_memory_not_released_refuses(tmp_path):
    w = World(tmp_path)
    w.switcher.startup("forward")
    w.bind()
    w.switcher.ensure("forward")
    w.gpu.hold_after_exit = 10_000  # nvidia-smi still lists the old process
    with pytest.raises(S.SwapError, match="GPU memory"):
        w.switcher.ensure("reset")
    assert not any("levi-aeri-policy-reset" in " ".join(c) for c in w.starts())


def test_gpu_budget_refuses_when_others_hold_memory(tmp_path):
    w = World(tmp_path)
    w.switcher.startup("forward")
    w.bind()
    w.switcher.ensure("forward")
    w.gpu.other_mib = 3000  # someone else's job
    with pytest.raises(S.SwapError, match="gpu_budget"):
        w.switcher.ensure("reset")
    last = S.read_log(w.log_path)[0][-1]
    assert last["service_state"] == "none"  # known: nothing runs
    assert len(w.starts()) == 1


def test_new_service_with_wrong_metadata_is_refused_and_stopped(tmp_path):
    w = World(tmp_path)
    w.switcher.startup("forward")
    w.bind()
    w.identity["reset"] = {"checkpoint_dir": "/models/other"}
    with pytest.raises(S.SwapError, match="handshake_refused"):
        w.switcher.ensure("reset")
    assert "levi-aeri-policy-reset" not in w.units.units  # stopped again
    assert w.proc.sockets == {}
    last = S.read_log(w.log_path)[0][-1]
    assert last["code"] == "handshake_refused" and last["service_state"] == "none"
    assert w.warmups == []  # the refused service was never queried


def test_listener_with_other_command_line_is_not_ready(tmp_path):
    w = World(tmp_path)
    w.switcher.startup("forward")
    w.bind()
    w.units.argv_of = lambda argv: [
        a.replace("/models/reset", "/models/x") for a in argv
    ]
    with pytest.raises(S.SwapError, match="not_our_service"):
        w.switcher.ensure("reset")
    assert w.handshakes == ["forward"]  # no client was built


def test_foreign_listener_on_the_new_port_is_refused_before_start(tmp_path):
    w = World(tmp_path)
    w.switcher.startup("forward")
    w.bind()
    stranger = w.proc.spawn(["python", "-m", "http.server"], "0::/user.slice/x.scope")
    w.proc.listen(stranger, 8001)
    with pytest.raises(S.SwapError, match="port_taken"):
        w.switcher.ensure("reset")
    assert len(w.starts()) == 1
    assert stranger in w.proc.alive()  # someone else's process is not touched


@pytest.mark.parametrize(
    "retries,failures,ok", [(1, 1, True), (0, 1, False), (1, 2, False)]
)
def test_start_is_retried_at_most_once(tmp_path, retries, failures, ok):
    w = World(tmp_path, start_retries=retries)
    w.switcher.startup("forward")
    w.bind()
    w.units.exit_early = failures
    if ok:
        assert w.switcher.ensure("reset").role == "reset"
    else:
        with pytest.raises(S.SwapError, match="exited_before_ready"):
            w.switcher.ensure("reset")
    reset_starts = [c for c in w.starts() if "--unit=levi-aeri-policy-reset" in c]
    # The first start plus at most one retry, never a third.
    assert len(reset_starts) == min(failures, retries) + 1 <= 2


def test_start_timeout_stops_the_unit(tmp_path):
    w = World(tmp_path)
    w.switcher.startup("forward")
    w.bind()
    w.units.silent = True  # active, never listens
    with pytest.raises(S.SwapError, match="start_timeout"):
        w.switcher.ensure("reset")
    assert "levi-aeri-policy-reset" not in w.units.units
    assert len([c for c in w.starts() if "--unit=levi-aeri-policy-reset" in c]) == 1


def test_handshake_timeout_is_a_fault(tmp_path):
    import threading

    w = World(tmp_path)
    w.switcher.startup("forward")
    w.bind()
    blocked = threading.Event()
    w.switcher.handshake = lambda role: blocked.wait(5)
    w.time.step = 1.0
    try:
        with pytest.raises(S.SwapError, match="handshake_timeout"):
            w.switcher.ensure("reset")
    finally:
        blocked.set()
    assert "levi-aeri-policy-reset" not in w.units.units


@pytest.mark.parametrize("problem", ["token", "away", "moving"])
def test_switch_needs_no_token_and_a_still_arm_at_home(tmp_path, problem):
    w = World(tmp_path)
    w.switcher.startup("forward")
    w.bind()
    if problem == "token":
        w.fence.issue(sm.MotionToken("run-1", "tx", 1, "policy_steps", "e", 1, 10**18))
    elif problem == "away":
        w.pose[0] += 0.05
    else:
        reads = iter([list(HOME), [HOME[0] + 0.01, *HOME[1:]]])
        w.switcher.robot.pose = lambda: next(reads)
    calls = len(w.units.calls)
    with pytest.raises(S.SwapError):
        w.switcher.ensure("reset")
    assert len(w.units.calls) == calls  # no service was touched
    last = S.read_log(w.log_path)[0][-1]
    assert last["record"] == "refused" and last["service_state"] == "forward"
    with pytest.raises(S.SwapError, match="latched"):
        w.switcher.ensure("reset")


def test_motion_during_the_switch_is_detected(tmp_path):
    w = World(tmp_path)
    w.switcher.startup("forward")
    w.bind()

    def warmup(role, client):
        w.steps += 1  # something moved the arm while services changed
        return [[0.0] * 7]

    w.switcher.robot.warmup = warmup
    with pytest.raises(S.SwapError, match="motion_during_switch"):
        w.switcher.ensure("reset")
    assert "levi-aeri-policy-reset" not in w.units.units


def test_operator_stop_during_start_aborts_and_stops_the_new_unit(tmp_path):
    w = World(tmp_path)
    w.switcher.startup("forward")
    w.bind()
    w.units.silent = True

    def stop_once_started():
        if "levi-aeri-policy-reset" in w.units.units:
            w.cancel = True

    w.time.tick = lambda: (w.units.tick(), stop_once_started())
    with pytest.raises(S.SwapError, match="operator_stop: while the policy service"):
        w.switcher.ensure("reset")
    assert w.units.units == {}
    last = S.read_log(w.log_path)[0][-1]
    assert last["code"] == "operator_stop" and last["service_state"] == "none"


@pytest.mark.parametrize("problem", ["unit", "port"])
def test_startup_assumes_nothing(tmp_path, problem):
    w = World(tmp_path)
    if problem == "unit":
        w.units.units["levi-aeri-policy-reset"] = w.proc.spawn(
            ["x"], "0::/user.slice/levi-aeri-policy-reset.service"
        )
        match = "systemctl --user stop levi-aeri-policy-reset.service"
    else:
        w.proc.listen(w.proc.spawn(["x"], "0::/x.scope"), 8000)
        match = "port_taken"
    with pytest.raises(S.SwapError, match=match):
        w.switcher.startup("forward")
    assert w.starts() == []
    assert S.unclean(*S.read_log(w.log_path)) is None  # nothing was started
    leftover = dict(w.units.units)
    w.switcher.shutdown()  # the run's end stops only what this run started
    assert w.units.units == leftover
    assert not [c for c in w.units.calls if c[2:3] == ["stop"]]


def test_unreadable_gpu_is_never_free(tmp_path):
    w = World(tmp_path)
    w.gpu.broken = True
    with pytest.raises(S.SwapError, match="gpu_unreadable"):
        w.switcher.startup("forward")
    assert w.starts() == []


def test_shutdown_stops_own_units_and_leaves_a_clean_log(tmp_path):
    w = World(tmp_path)
    w.switcher.startup("forward")
    w.bind()
    w.switcher.ensure("reset")
    w.switcher.shutdown()
    assert w.units.units == {} and w.proc.sockets == {}
    assert S.unclean(*S.read_log(w.log_path)) is None
    assert S.pending(tmp_path / "rollouts") == []


class PowerLoss(BaseException):
    pass


def test_interrupted_switch_latches_a_new_switcher_and_needs_acknowledgement(
    tmp_path,
):
    w = World(tmp_path)
    w.switcher.startup("forward")
    w.bind()
    w.units.crash = PowerLoss("power")
    # The crash is outside our control; the log keeps the open transaction
    # because the process "dies" without running any cleanup.
    w.switcher._fail = lambda *a, **k: None
    with pytest.raises(PowerLoss):
        w.switcher.ensure("reset")
    records, torn = S.read_log(w.log_path)
    assert S.open_transaction(records)["to_role"] == "reset"
    assert "never ended" in S.unclean(records, torn)
    calls = len(w.units.calls)
    again = w.make(S.SwapLog(w.log_path))
    w.bind(again)
    with pytest.raises(S.SwapError, match="latched"):
        again.ensure("reset")
    with pytest.raises(S.SwapError, match="latched"):
        again.startup("forward")
    assert len(w.units.calls) == calls
    found = S.pending(tmp_path / "rollouts")
    assert [Path(p) for p, _ in found] == [w.log_path]
    S.acknowledge([p for p, _ in found], by_run="run-2")
    assert S.pending(tmp_path / "rollouts") == []


def test_torn_log_line_needs_acknowledgement(tmp_path):
    w = World(tmp_path)
    w.switcher.startup("forward")
    with open(w.log_path, "a") as handle:
        handle.write('{"record": "prep')
    assert S.pending(tmp_path / "rollouts")[0][1] == "torn log line"


def test_switching_policy_maps_any_switch_failure_to_a_fault(tmp_path):
    from levi.automatic import fr3_bridge as B

    w = World(tmp_path)
    w.switcher.startup("forward")
    w.bind()
    w.units.linger = 10_000

    class Client:
        def get_server_metadata(self):
            return {"config": "cfg_fwd", "checkpoint_dir": "/models/forward"}

    module = NS(_check_server=lambda *a: None, _check_prompt=lambda *a: None)
    policy = S.SwitchingPolicy(
        w.switcher,
        {"forward": Client()},
        DEPLOYMENTS,
        {"forward": "f", "reset": "r"},
        NS(raw=None),
        module,
        NS(),
        None,
    )
    assert isinstance(policy, B.Policy)
    handle = policy.acquire("forward", policy_epoch=1)
    assert handle["role"] == "forward"
    handle = policy.acquire("reset", policy_epoch=2)
    assert handle["kind"] == "unavailable" and handle["fault"] is True
    assert handle["code"] == "policy_switch_fault"


def test_log_lines_are_json_with_wall_time(tmp_path):
    w = World(tmp_path)
    w.switcher.startup("forward")
    lines = w.log_path.read_text().splitlines()
    assert all("wall_ns" in json.loads(line) for line in lines)
    with pytest.raises(FileExistsError):
        S.SwapLog.create(w.log_path, run_id="run-1", units={})


# --- the orchestrator's side ---------------------------------------------------------


def test_orchestrator_locks_on_a_switch_fault_instead_of_waiting(tmp_path, request):
    from aeri_harness import build, committed, config, fake

    class SwitchFault(fake.FakePolicy):
        def acquire(self, role, *, policy_epoch):
            return {
                "kind": "unavailable",
                "code": "policy_switch_fault",
                "detail": "stop_timeout: port 8000 still listening",
                "fault": True,
            }

    clock = fake.FakeClock()
    r = build(tmp_path, cfg=config(episodes=1), clock=clock, policy=SwitchFault(clock))
    # aeri_harness is imported here, after conftest looked for the fake: close
    # the recorder threads ourselves.
    request.addfinalizer(r.recorder.close)
    assert r.orch.run() == "FAULT_LOCKED"
    assert committed(r.orch)[-1] == ("VERIFY_INITIAL", "FAULT_LOCKED", "policy_error")
    assert r.robot.motions == []
    assert "policy_switch_fault" in {code for code, _ in r.orch.notes}
    # A plain unavailable policy still waits for a person (unchanged).
    plain = build(
        tmp_path / "plain", cfg=config(episodes=1), policy={"acquire_fails": True}
    )
    request.addfinalizer(plain.recorder.close)
    assert plain.orch.run() == "WAIT_HUMAN"
    assert committed(plain.orch)[-1] == (
        "VERIFY_INITIAL",
        "WAIT_HUMAN",
        "policy_error",
    )


def test_orchestrator_tells_whether_an_operator_stop_is_pending(tmp_path):
    from aeri_harness import build, config

    r = build(tmp_path, cfg=config(episodes=1))
    assert r.orch.stop_requested() is False
    r.orch.stop("stop-1")
    assert r.orch.stop_requested() is True
    assert r.orch.run() == "WAIT_HUMAN"  # the stop took effect before a start
    assert r.orch.stop_requested() is False


def test_an_open_transaction_stays_open_when_a_later_one_ends():
    records = [
        {"record": "header"},
        {"record": "prepared", "txn": "r:swap:0002", "to_role": "reset"},
        {"record": "prepared", "txn": "r:swap:0003", "to_role": None},
        {"record": "aborted", "txn": "r:swap:0003", "service_state": "unknown"},
    ]
    assert S.open_transaction(records)["txn"] == "r:swap:0002"
    records.append(
        {"record": "committed", "txn": "r:swap:0003", "service_state": "none"}
    )
    assert S.open_transaction(records)["txn"] == "r:swap:0002"
    assert "never ended" in S.unclean(records)


def test_a_clean_log_of_a_refused_start_can_be_reused_by_the_same_run(tmp_path):
    w = World(tmp_path)
    w.gpu.other_mib = 6000
    with pytest.raises(S.SwapError, match="gpu_budget"):
        w.switcher.startup("forward")
    assert S.unclean(*S.read_log(w.log_path)) is None
    again = S.SwapLog.open_for_run(w.log_path, run_id="run-1", units={})
    w.gpu.other_mib = 0
    switcher = w.make(again)
    assert switcher.latched is None
    assert switcher.startup("forward").role == "forward"
    txns = [r["txn"] for r in S.read_log(w.log_path)[0] if r["record"] == "prepared"]
    assert len(txns) == len(set(txns)) == 2


def test_an_unclean_log_is_not_reused(tmp_path):
    w = World(tmp_path)
    w.switcher.startup("forward")  # a service runs: the log is not clean
    with pytest.raises(ValueError, match="not clean"):
        S.SwapLog.open_for_run(w.log_path, run_id="run-1", units={})


def test_acknowledgement_is_a_boundary_for_old_problems_only(tmp_path):
    w = World(tmp_path)
    w.switcher.startup("forward")
    with open(w.log_path, "a") as handle:
        handle.write('{"record": "prep\n')
    assert S.unclean(*S.read_log(w.log_path)) == "torn log line"
    S.acknowledge([w.log_path], by_run="run-2")
    assert S.unclean(*S.read_log(w.log_path)) is None
    reopened = S.SwapLog.open_for_run(w.log_path, run_id="run-1", units={})
    assert w.make(reopened).latched is None
    with open(w.log_path, "a") as handle:
        handle.write("garbage after the acknowledgement\n")
    assert S.unclean(*S.read_log(w.log_path)) == "torn log line"


def stops_of(w, unit):
    return [
        c for c in w.units.calls if c[2:3] == ["stop"] and c[-1] == unit + ".service"
    ]


@pytest.mark.parametrize("problem", ["unit_active", "port_taken"])
def test_a_refused_new_service_that_this_switch_did_not_start_is_never_stopped(
    tmp_path, problem
):
    w = World(tmp_path)
    w.switcher.startup("forward")
    w.bind()
    if problem == "unit_active":
        # Someone (an operator, an earlier run) runs a unit with that name.
        theirs = w.proc.spawn(
            ["x"], "0::/user.slice/app.slice/levi-aeri-policy-reset.service", 8001
        )
        w.units.units["levi-aeri-policy-reset"] = theirs
    else:
        theirs = w.proc.spawn(["python", "-m", "http.server"], "0::/x.scope", 8001)
    with pytest.raises(S.SwapError, match=problem):
        w.switcher.ensure("reset")
    assert stops_of(w, "levi-aeri-policy-reset") == []
    assert theirs in w.proc.alive()
    if problem == "unit_active":
        assert w.units.units["levi-aeri-policy-reset"] == theirs
    last = S.read_log(w.log_path)[0][-1]
    assert last["record"] == "aborted" and last["service_state"] == "none"


def test_a_unit_this_run_started_earlier_is_not_stopped_when_someone_restarted_it(
    tmp_path,
):
    w = World(tmp_path)
    w.switcher.startup("forward")
    w.bind()
    w.switcher.ensure("reset")
    w.switcher.ensure("forward")  # this run stopped its reset unit again
    before = len(stops_of(w, "levi-aeri-policy-reset"))
    theirs = w.proc.spawn(
        ["x"], "0::/user.slice/app.slice/levi-aeri-policy-reset.service", 8001
    )
    w.units.units["levi-aeri-policy-reset"] = theirs
    with pytest.raises(S.SwapError, match="unit_active"):
        w.switcher.ensure("reset")
    assert len(stops_of(w, "levi-aeri-policy-reset")) == before
    assert theirs in w.proc.alive()


def test_an_unreadable_arm_before_a_switch_is_a_logged_latched_refusal(tmp_path):
    w = World(tmp_path)
    w.switcher.startup("forward")
    w.bind()

    def stale():
        raise ValueError("robot state explicitly stale")

    w.switcher.robot.pose = stale
    calls = len(w.units.calls)
    with pytest.raises(S.SwapError, match="adapter_error"):
        w.switcher.ensure("reset")
    assert len(w.units.calls) == calls  # no service was touched
    last = S.read_log(w.log_path)[0][-1]
    assert last["record"] == "refused" and last["code"] == "adapter_error"
    assert last["service_state"] == "forward"
    assert w.switcher.latched and "stale" in w.switcher.latched


def test_an_arm_error_after_the_new_start_stops_it_and_reports_a_swap_error(tmp_path):
    w = World(tmp_path)
    w.switcher.startup("forward")
    w.bind()
    reads = iter([list(HOME), list(HOME)])

    def pose():
        try:
            return next(reads)
        except StopIteration:
            raise RuntimeError("camera read failed") from None

    w.switcher.robot.pose = pose
    with pytest.raises(S.SwapError, match="adapter_error"):
        w.switcher.ensure("reset")
    assert w.units.units == {}
    last = S.read_log(w.log_path)[0][-1]
    assert last["record"] == "aborted" and last["service_state"] == "none"
    assert w.switcher.latched


def test_the_switcher_latches_even_when_the_log_cannot_be_written(tmp_path):
    w = World(tmp_path)
    w.switcher.startup("forward")
    w.bind()
    w.pose[0] += 0.05  # not at home: refused

    def disk_full(record):
        raise OSError(28, "No space left on device")

    w.switcher.log.append = disk_full
    with pytest.raises(OSError):
        w.switcher.ensure("reset")
    assert w.switcher.latched
    w.pose[0] -= 0.05
    with pytest.raises(S.SwapError, match="latched"):
        w.switcher.ensure("reset")


# --- review S3: checks that had no test ---------------------------------------------


def test_an_arm_moved_during_the_switch_is_refused(tmp_path):
    w = World(tmp_path)
    w.switcher.startup("forward")
    w.bind()

    def warmup(role, client):
        w.pose[0] += 0.002  # 2 mm: within the home tolerance, not still
        return [[0.0] * 7]

    w.switcher.robot.warmup = warmup
    with pytest.raises(S.SwapError, match="arm_moved"):
        w.switcher.ensure("reset")
    assert w.units.units == {} and w.switcher.latched


def test_a_service_that_died_is_a_lost_service(tmp_path):
    w = World(tmp_path)
    w.switcher.startup("forward")
    w.bind()
    w.switcher.ensure("forward")
    pid = w.units.units.pop("levi-aeri-policy-forward")
    w.proc.kill(pid)  # the server crashed between episodes
    starts = len(w.starts())
    with pytest.raises(S.SwapError, match="service_lost"):
        w.switcher.ensure("forward")
    last = S.read_log(w.log_path)[0][-1]
    assert last["record"] == "fault" and last["service_state"] == "unknown"
    assert w.switcher.latched and len(w.starts()) == starts
    w.switcher.shutdown()
    assert S.read_log(w.log_path)[0][-1]["service_state"] == "none"


def test_a_foreign_listener_on_the_old_port_during_the_stop_is_refused(tmp_path):
    w = World(tmp_path)
    w.switcher.startup("forward")
    w.bind()
    taken = []

    def grab_port(unit):
        if unit == "levi-aeri-policy-forward" and not taken:
            taken.append(w.proc.spawn(["nc", "-l", "8000"], "0::/x.scope", 8000))

    w.units.after_stop = grab_port
    with pytest.raises(S.SwapError, match="foreign_listener"):
        w.switcher.ensure("reset")
    assert taken[0] in w.proc.alive()  # someone else's process is not touched
    assert stops_of(w, "levi-aeri-policy-reset") == [] and len(w.starts()) == 1
    last = S.read_log(w.log_path)[0][-1]
    assert last["record"] == "aborted" and last["service_state"] == "unknown"


@pytest.mark.parametrize(
    "actions",
    [[[float("nan")] * 7], [[0.0] * 6], [], [[True] * 7], "chunk", [[0.0] * 7, None]],
)
def test_warm_up_output_must_be_finite_k_by_7(tmp_path, actions):
    w = World(tmp_path)
    w.switcher.startup("forward")
    w.bind()
    w.switcher.robot.warmup = lambda role, client: actions
    with pytest.raises(S.SwapError, match="warmup_invalid"):
        w.switcher.ensure("reset")
    assert w.units.units == {}


@pytest.mark.parametrize("phase", ["handshake", "warmup"])
def test_a_stop_during_handshake_or_warm_up_ends_the_switch(tmp_path, phase):
    import threading

    w = World(tmp_path)
    w.switcher.startup("forward")
    w.bind()
    release = threading.Event()

    def blocking(*a):
        w.cancel = True  # the operator stops while the call hangs
        release.wait(5)
        return [[0.0] * 7]

    if phase == "handshake":
        w.switcher.handshake = blocking
    else:
        w.switcher.robot.warmup = blocking
    try:
        with pytest.raises(S.SwapError, match=f"operator_stop: {phase}"):
            w.switcher.ensure("reset")
    finally:
        release.set()
    assert w.units.units == {}


@pytest.mark.parametrize("fails,ok", [(1, True), (2, False)])
def test_systemd_run_exiting_non_zero_is_retried_at_most_once(tmp_path, fails, ok):
    w = World(tmp_path)
    w.switcher.startup("forward")
    w.bind()
    w.units.run_fails = fails
    if ok:
        assert w.switcher.ensure("reset").role == "reset"
    else:
        with pytest.raises(S.SwapError, match="start_failed"):
            w.switcher.ensure("reset")
        assert w.units.units == {}
    assert len([c for c in w.starts() if "--unit=levi-aeri-policy-reset" in c]) == 2


def test_every_log_line_is_fsynced(tmp_path, monkeypatch):
    w = World(tmp_path)
    synced = []
    real = S.os.fsync
    monkeypatch.setattr(S.os, "fsync", lambda fd: (synced.append(fd), real(fd)))
    before = len(S.read_log(w.log_path)[0])
    w.switcher.startup("forward")
    assert len(synced) == len(S.read_log(w.log_path)[0]) - before > 0


def test_unit_pids_reads_the_units_cgroup_procs_not_the_proc_listing(
    tmp_path, monkeypatch
):
    # Root AGENTS.md execution boundary (user 2026-10-14): never list /proc.
    w = World(tmp_path)
    w.switcher.startup("forward")
    stranger = w.proc.spawn(["python", "-m", "http.server"], "0::/user.slice/x.scope")
    seen = []
    real = Path.iterdir

    def iterdir(self):
        if Path(self) == Path(w.proc.root):
            seen.append(self)
        return real(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    own = w.units.units["levi-aeri-policy-forward"]
    assert w.switcher.unit_pids("levi-aeri-policy-forward") == [own]
    assert w.switcher.unit_pids("levi-aeri-policy-reset") == []
    assert seen == []
    assert stranger not in w.switcher.unit_pids("levi-aeri-policy-forward")


def test_unit_pids_fails_closed_when_the_cgroup_cannot_be_read(tmp_path):
    w = World(tmp_path)
    w.switcher.startup("forward")
    real = w.units.run

    def broken(argv):
        if "ControlGroup" in argv:
            return 1, ""
        return real(argv)

    w.units.run = broken
    with pytest.raises(S.SwapError, match="proc_unreadable"):
        w.switcher.unit_pids("levi-aeri-policy-forward")


def test_a_named_cgroup_that_cannot_be_read_fails_closed(tmp_path):
    w = World(tmp_path)
    w.switcher.startup("forward")
    w.proc.control_group = lambda unit: "/gone/" + unit + ".service"
    with pytest.raises(S.SwapError, match="proc_unreadable"):
        w.switcher.unit_pids("levi-aeri-policy-forward")


def test_a_readable_foreign_listener_is_not_ours(tmp_path):
    # Its /proc entry is readable in the fake, but it is not in our unit.
    w = World(tmp_path)
    stranger = w.proc.spawn(["nc"], "0::/user.slice/x.scope", 8000)
    assert w.switcher._foreign(8000, "levi-aeri-policy-forward")
    with pytest.raises(S.SwapError) as caught:
        w.switcher.startup("forward")
    assert str(stranger) not in str(caught.value)
    assert stranger in w.proc.alive()


def test_an_unreadable_cgroup_after_the_stop_times_out_and_starts_nothing(tmp_path):
    # _released: the old unit's cgroup cannot be read after the stop. Its
    # processes are not proven gone, so the switch waits until stop_timeout
    # and never starts the new unit.
    w = World(tmp_path)
    w.switcher.startup("forward")
    w.bind()
    real = w.units.run
    stopped = []

    def run(argv):
        if stopped and "ControlGroup" in argv:
            return 1, ""
        return real(argv)

    w.units.run = run
    w.units.after_stop = stopped.append
    with pytest.raises(S.SwapError) as caught:
        w.switcher.ensure("reset")
    assert caught.value.code == "stop_timeout"
    assert "cgroup unreadable" in str(caught.value)
    assert [c for c in w.starts() if "--unit=levi-aeri-policy-reset" in c] == []


def test_a_lingering_listener_with_an_unreadable_cgroup_is_owner_unknown(tmp_path):
    w = World(tmp_path)
    w.switcher.startup("forward")
    w.bind()
    w.units.linger = 1000
    real = w.units.run
    stopped = []

    def run(argv):
        if stopped and "ControlGroup" in argv:
            return 1, ""
        return real(argv)

    w.units.run = run
    w.units.after_stop = stopped.append
    with pytest.raises(S.SwapError) as caught:
        w.switcher.ensure("reset")
    assert caught.value.code == "foreign_listener"
    assert "owner unknown: levi-aeri-policy-forward cgroup unreadable" in str(
        caught.value
    )
    assert "another process" not in str(caught.value)


def test_an_argv_with_whitespace_is_refused(tmp_path):
    value = block(tmp_path)
    value["services"]["forward"]["argv"].append("--note=a b")
    with pytest.raises(ValueError, match="whitespace"):
        parse(tmp_path, value)


def test_start_argv_never_delegates_the_cgroup(tmp_path):
    # Ownership reads only the unit's top-level cgroup.procs (no sub-cgroups).
    w = World(tmp_path)
    for service in w.cfg.services.values():
        assert not any("Delegate" in part for part in S.start_argv(w.cfg, service))
