"""fr3_run with the binding's ``policy_swap`` (T-C-SWITCH) on the fake FR3
modules of test_fr3_bridge plus a fake systemd, ``/proc`` and ``nvidia-smi``.
No unit tool runs, no GPU is queried and no socket is opened."""

import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from fr3_swap_fakes import Proc, Smi, Time, Units, refuse_unit_tools
from test_fr3_bridge import rig

from levi.automatic import fr3_policy_swap as S
from levi.automatic import fr3_run as R
from levi.automatic import state_machine as sm
from levi.automatic.journal import Journal

RUN = "real-test"
OLD_CHECK_KEYS = {
    "plan_sha256",
    "binding_sha256",
    "reset_strategy",
    "hardware_verified",
    "event_source",
    "signal_events",
}


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    refuse_unit_tools(monkeypatch)
    monkeypatch.setattr(R, "ARM_LOCK_FILE", str(tmp_path / "physical-arm.lock"))


def swap_block(tmp_path, job, binding, roles=("forward", "reset"), **changes):
    deployments = job["plan"]["deployments"]
    services = {
        role: {
            "unit": "levi-aeri-policy-" + role,
            "argv": [
                sys.executable,
                "serve.py",
                "--port",
                str(binding["policies"][role]["port"]),
                "--policy.config",
                deployments[role]["config"],
                "--policy.dir",
                deployments[role]["checkpoint_dir"],
            ],
            "working_directory": str(tmp_path),
            "mem_fraction": 0.22,
        }
        for role in roles
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


class Swap:
    """The bridge rig in policy-reset mode with a policy_swap binding."""

    def __init__(
        self,
        tmp_path,
        monkeypatch,
        *,
        scenes=("reset_required", "ready"),
        leaky_fence=False,
        mode="single_reset_policy",
        **swap_changes,
    ):
        self.r = rig(tmp_path, monkeypatch, mode, scenes=scenes)
        self.r.binding["policy_swap"] = swap_block(
            tmp_path,
            self.r.job,
            self.r.binding,
            roles=tuple(self.r.binding["policies"]),
            **swap_changes,
        )
        self.extra_hooks = []
        # The real fr3_run objects, to act on them the way the hardware would.
        self.robots, self.orchs = [], []
        robots, orchs = self.robots, self.orchs

        class Robot(R.bridge.Robot):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                robots.append(self)

        class Orch(R.Orchestrator):
            @classmethod
            def create(cls, *a, **k):
                made = super().create(*a, **k)
                orchs.append(made)
                return made

        monkeypatch.setattr(R.bridge, "Robot", Robot)
        monkeypatch.setattr(R, "Orchestrator", Orch)
        self.time = Time()
        self.proc = Proc(tmp_path / "proc")
        self.gpu = Smi()
        self.violations = []
        self.fences = []
        self.units = Units(self.proc, self.gpu, hook=self.hook)
        self.time.tick = self.units.tick
        self.io = NS(
            systemd=self.units,
            proc_root=self.proc.root,
            cgroup_root=self.proc.cgroup_root,
            gpu=S.Gpu(self.gpu),
            clock=self.time.clock,
            sleep=self.time.sleep,
            poll_s=0.01,
        )
        fences = self.fences

        class Fence(sm.MotionFence):
            def __init__(self):
                super().__init__()
                fences.append(self)

            def revoke(self):
                if not leaky_fence:  # leaky: a token outlives its transaction
                    super().revoke()

        monkeypatch.setattr(R.sm, "MotionFence", Fence)
        client = self.r.modules[0]
        original = client.websocket_client_policy.WebsocketClientPolicy

        def connect(host, port):
            # The openpi client connects in its constructor: only after the
            # passive check saw our unit listen on that port.
            if port not in self.proc.sockets.values():
                self.violations.append(("connected_before_listening", port))
            self.r.calls.append(("connect", port))
            return original(host, port)

        client.websocket_client_policy = NS(WebsocketClientPolicy=connect)

    def hook(self, argv):
        policy_ports = [p for p in self.proc.sockets.values() if p in (8000, 8001)]
        if len(policy_ports) > 1:
            self.violations.append(("two_policy_services", argv))
        if self.fences and self.fences[-1].current is not None:
            self.violations.append(("motion_token_during_switch", argv))
        for extra in self.extra_hooks:
            extra(argv)

    def execute(self, job=None):
        return R.execute(
            job or self.r.job,
            self.r.binding,
            load_modules=lambda b: self.r.modules,
            swap_io=self.io,
        )

    @property
    def run_dir(self):
        return Path(self.r.job["rollout_root"]) / ".aeri/runs" / RUN

    @property
    def log(self):
        return S.read_log(S.log_path(self.r.job["rollout_root"], RUN))[0]

    def starts_any(self):
        return [c for c in self.units.calls if c[0] == "systemd-run"]

    def unit_calls(self):
        out = []
        for call in self.units.calls:
            if call[0] == "systemd-run":
                out.append(
                    ("start", next(a for a in call if a.startswith("--unit="))[7:])
                )
            elif call[2] == "stop":
                out.append(("stop", call[-1].removesuffix(".service")))
        return out


def write(path, value):
    path.write_text(json.dumps(value))
    return path


# --- binding and --check ---------------------------------------------------------------


def test_old_binding_check_output_is_byte_for_byte_unchanged(
    tmp_path, monkeypatch, capsys
):
    r = rig(tmp_path, monkeypatch, "single_reset_policy")
    path = write(tmp_path / "binding.json", r.binding)
    value, digest = R.load_binding(path, r.job)
    assert value == r.binding
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(R.cli, "load_job", lambda p: r.job)
    job_path = tmp_path / "job.yaml"
    job_path.write_text("fake parser")
    assert R.main(["--config", str(job_path), "--binding", str(path), "--check"]) == 0
    expected = {
        "plan_sha256": r.job["plan_sha256"],
        "binding_sha256": digest,
        "reset_strategy": "single_reset_policy",
        "hardware_verified": False,
        "event_source": "files",
        "signal_events": None,
    }
    assert capsys.readouterr().out == json.dumps(expected, indent=2) + "\n"
    assert set(expected) == OLD_CHECK_KEYS
    assert r.calls == []


def test_check_validates_policy_swap_without_running_or_connecting(
    tmp_path, monkeypatch, capsys
):
    s = Swap(tmp_path, monkeypatch)
    path = write(tmp_path / "binding.json", s.r.binding)
    monkeypatch.setattr(R.cli, "load_job", lambda p: s.r.job)
    monkeypatch.setattr(R, "execute", lambda *a, **k: pytest.fail("executed"))
    job_path = tmp_path / "job.yaml"
    job_path.write_text("fake parser")
    assert R.main(["--config", str(job_path), "--binding", str(path), "--check"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) == OLD_CHECK_KEYS | {"policy_swap"}
    assert out["policy_swap"]["units"]["reset"] == "levi-aeri-policy-reset"
    assert out["policy_swap"]["services_started_by_check"] is False
    assert s.units.calls == [] and s.gpu.calls == [] and s.r.calls == []
    # Every field is explicit: a missing one is refused by --check.
    del s.r.binding["policy_swap"]["still_tolerance_rad"]
    write(path, s.r.binding)
    assert R.main(["--config", str(job_path), "--binding", str(path), "--check"]) == 2
    assert "policy_swap requires exactly" in capsys.readouterr().err


def test_policy_swap_needs_the_policy_reset_mode(tmp_path, monkeypatch):
    s = Swap(tmp_path, monkeypatch)
    (tmp_path / "human").mkdir()
    human = rig(tmp_path / "human", monkeypatch)
    binding = dict(human.binding, policy_swap=s.r.binding["policy_swap"])
    with pytest.raises(ValueError, match="policy_swap needs the forward and reset"):
        R.load_binding(write(tmp_path / "b.json", binding), human.job)


# --- execution ---------------------------------------------------------------------------


def test_execute_runs_one_policy_service_at_a_time(tmp_path, monkeypatch):
    s = Swap(tmp_path, monkeypatch)
    assert s.execute() == "COMPLETED"
    assert s.violations == []
    assert s.unit_calls() == [
        ("start", "levi-aeri-policy-forward"),
        ("stop", "levi-aeri-policy-forward"),
        ("start", "levi-aeri-policy-reset"),
        ("stop", "levi-aeri-policy-reset"),
        ("start", "levi-aeri-policy-forward"),
        ("stop", "levi-aeri-policy-forward"),  # the run's end
    ]
    assert s.units.units == {} and s.proc.sockets == {}
    events = Journal.read(s.run_dir).events
    assert sm.check_journal(events) == []
    roles = [
        e.episode_role for e in events if e.record == "committed" and e.episode_result
    ]
    assert roles == ["reset", "forward"]
    # The forward identity is checked before any robot I/O, as before.
    calls = s.r.calls
    assert calls.index(next(c for c in calls if c[0] == "env")) > calls.index(
        ("metadata", "forward")
    )
    log = s.log
    assert [r["service_state"] for r in log if r["record"] == "committed"] == [
        "forward",
        "reset",
        "forward",
        "none",
    ]
    assert S.pending(s.r.job["rollout_root"]) == []


def test_a_switch_failure_locks_the_run_without_motion(tmp_path, monkeypatch):
    s = Swap(tmp_path, monkeypatch)
    client = s.r.modules[0]
    connect = client.websocket_client_policy.WebsocketClientPolicy

    def wrong_reset(host, port):
        made = connect(host, port)
        if port == 8001:
            made.get_server_metadata = lambda: {
                "config": "cfg",
                "checkpoint_dir": "/models/other",
            }
        return made

    client.websocket_client_policy = NS(WebsocketClientPolicy=wrong_reset)
    assert s.execute() == "FAULT_LOCKED"
    events = Journal.read(s.run_dir).events
    committed = [
        (e.from_state, e.to_state, e.reason) for e in events if e.record == "committed"
    ]
    assert committed[-1] == ("VERIFY_INITIAL", "FAULT_LOCKED", "policy_error")
    assert not any(
        e.to_state == "RESET_ACTIVE" and e.record == "committed" for e in events
    )
    assert ("route", "pose") not in s.r.calls  # nothing moved
    assert s.units.units == {} and s.violations == []
    aborted = [r for r in s.log if r["record"] == "aborted"]
    assert aborted[0]["code"] == "handshake_refused"
    assert aborted[0]["service_state"] == "none"
    assert S.pending(s.r.job["rollout_root"]) == []  # cleaned up and confirmed


class PowerLoss(BaseException):
    pass


def test_interrupted_switch_attaches_as_fault_locked(tmp_path, monkeypatch):
    from aeri_harness import fake

    from levi.automatic.adapters import events as ev
    from levi.automatic.orchestrator import Orchestrator

    s = Swap(tmp_path, monkeypatch)
    run = s.units.run
    started = []

    def dying(argv):
        if started or (
            argv[0] == "systemd-run" and "--unit=levi-aeri-policy-reset" in argv
        ):
            started.append(argv)
            raise PowerLoss("power lost during the switch")
        return run(argv)

    s.units.run = dying
    with pytest.raises(PowerLoss):
        s.execute()
    assert S.open_transaction(s.log)["to_role"] == "reset"
    # Attach: the journal's recovery runs; nothing is replayed.
    job = s.r.job
    cfg = replace(
        job["config"], specs={**job["config"].specs, **job["contract"].spec()}
    )
    clock = s.r.clock  # the run's own clock domain, later than its journal
    fence = sm.MotionFence()
    orch = Orchestrator.restore(
        s.run_dir,
        cfg,
        plan=job["plan"],
        robot=fake.FakeRobot(clock, fence.check),
        policy=fake.FakePolicy(clock),
        recorder=fake.FakeRecorder(),
        events=ev.FakeEventStream({}, clock, cfg.run_id),
        verifier=ev.FakeGoalVerifier([], clock, cfg.run_id),
        scene=ev.FakeSceneAssessor([], clock, cfg.run_id),
        clock=clock,
        fence=fence,
    )
    try:
        assert orch.state == "FAULT_LOCKED"
        assert orch.recovery.reason == "recovery_ambiguous"
        notes = [e for e in orch.journal.events if e.record == "note"]
        assert any("policy_steps" in str(e.note) for e in notes)
    finally:
        orch.close()
    found = S.pending(job["rollout_root"])
    assert [Path(p).name for p, _ in found] == [RUN + ".jsonl"]


def test_unclean_earlier_switch_needs_the_extended_confirmation(tmp_path, monkeypatch):
    s = Swap(tmp_path, monkeypatch)
    old = S.SwapLog.create(
        S.log_path(s.r.job["rollout_root"], "older-run"), run_id="older-run", units={}
    )
    old.append({"record": "prepared", "txn": "older-run:swap:0001", "to_role": "reset"})
    path = write(tmp_path / "binding.json", s.r.binding)
    job_path = tmp_path / "job.yaml"
    job_path.write_text("fake parser")
    monkeypatch.setattr(R.cli, "load_job", lambda p: s.r.job)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    argv = [
        "--config",
        str(job_path),
        "--binding",
        str(path),
        "--execute",
        "--expect-plan",
        s.r.job["plan_sha256"],
        "--expect-binding",
        digest,
    ]
    executed = []
    monkeypatch.setattr(R, "execute", lambda job, binding, **k: executed.append(job))
    monkeypatch.setattr("builtins.input", lambda prompt: "execute " + RUN)
    assert R.main(argv) == 2 and executed == []
    monkeypatch.setattr(
        "builtins.input", lambda prompt: "execute " + RUN + " after policy switch check"
    )
    R.main(argv)
    assert executed[0]["swap_acknowledged"] == [str(old.path)]


def test_execute_acknowledges_only_the_logs_the_operator_saw(tmp_path, monkeypatch):
    s = Swap(tmp_path, monkeypatch)
    old = S.SwapLog.create(
        S.log_path(s.r.job["rollout_root"], "older-run"), run_id="older-run", units={}
    )
    old.append({"record": "committed", "txn": "x", "service_state": "forward"})
    with pytest.raises(ValueError, match="policy switch logs changed"):
        s.execute()
    assert s.units.calls == [] and s.r.calls == []
    assert s.execute(dict(s.r.job, swap_acknowledged=[str(old.path)])) == "COMPLETED"
    assert S.read_log(old.path)[0][-1]["record"] == "acknowledged"
    assert S.read_log(old.path)[0][-1]["by_run"] == RUN
    assert S.pending(s.r.job["rollout_root"]) == []


def test_a_refused_start_does_not_use_up_the_run_id(tmp_path, monkeypatch):
    s = Swap(tmp_path, monkeypatch)
    s.gpu.other_mib = 6000  # someone else's job holds the memory
    with pytest.raises(S.SwapError, match="gpu_budget"):
        s.execute()
    assert s.units.calls and not s.starts_any()
    assert not s.run_dir.exists() and ("env",) not in [c[:1] for c in s.r.calls]
    s.gpu.other_mib = 0
    assert s.execute() == "COMPLETED"
    assert S.pending(s.r.job["rollout_root"]) == []


# --- the arm side of fr3_run.assembled (RobotSide wiring) -----------------------------


def switch_refusals(s):
    return [r for r in s.log if r["record"] in ("refused", "aborted")]


def reset_starts(s):
    return [c for c in s.starts_any() if "--unit=levi-aeri-policy-reset" in c]


def test_wiring_a_switch_reads_the_actual_arm_pose(tmp_path, monkeypatch):
    s = Swap(tmp_path, monkeypatch)
    _, health, env_module, *rest = s.r.modules
    real_env = env_module.RobotEnv
    envs = []

    def make_env(**kw):
        envs.append(real_env(**kw))
        return envs[-1]

    s.r.modules = (s.r.modules[0], health, NS(RobotEnv=make_env), *rest)
    create = R.Orchestrator.create

    def drifted(*a, **k):
        # The arm leaves home after the start gate passed (a start away from
        # home is refused earlier, by the home-gate start check).
        made = create(*a, **k)
        envs[-1].pose[0] += 0.02  # 20 mm from home, tolerance 5 mm
        return made

    monkeypatch.setattr(R.Orchestrator, "create", drifted)
    assert s.execute() == "FAULT_LOCKED"
    assert switch_refusals(s)[0]["code"] == "arm_not_home"
    assert reset_starts(s) == []
    assert ("route", "pose") not in s.r.calls


def test_wiring_a_switch_sees_a_token_on_the_run_fence(tmp_path, monkeypatch):
    s = Swap(tmp_path, monkeypatch, leaky_fence=True)
    assert s.execute() == "FAULT_LOCKED"
    # The reset's home token was never revoked: the switch back must refuse.
    refusal = switch_refusals(s)[0]
    assert refusal["code"] == "motion_token_present"
    assert refusal["to_role"] == "forward"


def test_wiring_a_switch_counts_the_robots_motion_steps(tmp_path, monkeypatch):
    s = Swap(tmp_path, monkeypatch)
    client = s.r.modules[0]
    query = client._query_policy
    queries = []

    def moving_warmup(*a):
        queries.append(a)
        if len(queries) == 2:  # the reset warm-up (1 is the start-up warm-up)
            s.robots[-1].steps += 1
        return query(*a)

    client._query_policy = moving_warmup
    assert s.execute() == "FAULT_LOCKED"
    assert switch_refusals(s)[0]["code"] == "motion_during_switch"
    assert s.units.units == {}


@pytest.mark.parametrize("how", ["signal", "operator_command"])
def test_wiring_a_stop_ends_a_switch_wait(tmp_path, monkeypatch, how):
    s = Swap(tmp_path, monkeypatch, start_timeout_s=1)

    def stop_when_reset_starts(argv):
        if argv[0] == "systemd-run" and "--unit=levi-aeri-policy-reset" in argv:
            s.units.silent = True  # it never listens: the switch waits
            if how == "signal":
                s.robots[-1].stop_event.set()
            else:
                s.orchs[-1].stop("page-stop-1")

    s.extra_hooks.append(stop_when_reset_starts)
    assert s.execute() == "FAULT_LOCKED"
    assert switch_refusals(s)[0]["code"] == "operator_stop"
    assert s.units.units == {}


# --- one start-up warm-up (int/aeri-flow's and the switcher's are not stacked) --------


def test_a_swap_run_warms_the_forward_service_once_and_before_the_session(
    tmp_path, monkeypatch
):
    s = Swap(tmp_path, monkeypatch)
    client = s.r.modules[0]
    query = client._query_policy
    events = []

    def spy(*a):
        events.append("query")
        return query(*a)

    client._query_policy = spy
    create = R.Orchestrator.create

    def created(*a, **k):
        events.append("session")
        return create(*a, **k)

    monkeypatch.setattr(R.Orchestrator, "create", created)

    def at_reset_start(argv):
        if argv[0] == "systemd-run" and "--unit=levi-aeri-policy-reset" in argv:
            events.append("switch")

    s.extra_hooks.append(at_reset_start)
    assert s.execute() == "COMPLETED"
    # Exactly one query before the session exists and none more before the first
    # switch (the later queries are the switches' warm-ups and the episodes').
    assert events[:3] == ["query", "session", "switch"]
    assert [r["role"] for r in s.log if r["record"] == "warm"] == ["forward"]


def test_a_swap_binding_refuses_the_external_warmup_budget(tmp_path, monkeypatch):
    s = Swap(tmp_path, monkeypatch)
    s.r.binding["policy_warmup_timeout_s"] = 10.0
    path = tmp_path / "binding-both.json"
    path.write_text(json.dumps(s.r.binding))
    with pytest.raises(ValueError, match="does not apply with policy_swap"):
        R.load_binding(path, s.r.job)
    assert not s.r.calls


# --- guard: a motion token during the start-up warm-up (ensure("forward")) ------------


@pytest.mark.parametrize("when", ["before_warmup", "during_warmup"])
def test_a_token_on_the_fence_refuses_the_start_up_warmup(tmp_path, monkeypatch, when):
    # Guard test for the two _no_token checks in PolicySwitcher._warm, which the
    # start-up path (warm_forward_at_start -> ensure) relies on alone: _switch
    # has its own fence check, the start-up path has none.
    s = Swap(tmp_path, monkeypatch)
    client = s.r.modules[0]
    token = object()

    def put_token():
        s.fences[-1]._current = token  # a token somebody left or issued

    query = client._query_policy
    if when == "before_warmup":
        warm = S.PolicySwitcher._warm

        def warming(self, role, policy_client):
            put_token()  # present when the warm-up starts ...
            return warm(self, role, policy_client)

        def querying(*a):
            s.fences[-1]._current = None  # ... gone again by its end
            return query(*a)

        monkeypatch.setattr(S.PolicySwitcher, "_warm", warming)
        client._query_policy = querying
    else:

        def querying(*a):
            put_token()  # the token appears while the warm-up query runs
            return query(*a)

        client._query_policy = querying
    with pytest.raises(
        ValueError, match="policy warm-up refused: motion_token_present"
    ):
        s.execute()
    assert "motion_token_present" in [
        r.get("code") for r in s.log if r["record"] in ("refused", "aborted")
    ]
    assert s.units.units == {} and s.proc.sockets == {}  # the unit this run started
    assert ("route", "pose") not in s.r.calls
