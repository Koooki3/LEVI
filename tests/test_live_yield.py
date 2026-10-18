"""The background labelling's yield rules (``[gpu.yield]``): when it gives the
GPU way, that it never starts or wakes the model meanwhile, and that it
carries on only after every reason has stayed gone. Fake machine, fake vLLM,
no robot, no real GPU."""

# ruff: noqa: F811

import fcntl
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from test_live_gpu import ctl, fresh, live, serve, step, up, wait_for  # noqa: F401

from levi.live import config as live_config
from levi.live import controller, gating, gpumgr, jsonio


@pytest.fixture(autouse=True)
def rules_on(live):
    """The shared fixture of test_live_gpu.py switches the rules off; here
    they are the subject."""
    live[0].gpu.yield_.enabled = True


def session(state, group="g", task="t", crashed=False):
    return SimpleNamespace(state=state, group=group, task_folder=task, crashed=crashed)


def reasons(config=None, **kwargs):
    config = config or live_config.Config()
    args = {"ports": set(), "sessions": {}, "apps": [], "check_gpu": True}
    args.update(kwargs)
    return gpumgr.yield_reasons(config, **args)


def codes(found):
    return [r.code for r in found]


# --- the pure rules -------------------------------------------------------------


def test_nothing_going_on_is_no_reason():
    assert reasons() == []


def test_a_listening_policy_port_or_an_unfinished_session_is_a_deployment():
    assert codes(reasons(ports={8000})) == ["deployment"]
    assert reasons(ports={8001}) == []  # not a configured policy port
    for state in ("standby", "running", "homing", "waiting_reset", "fault"):
        found = reasons(sessions={"a": session(state)})
        assert codes(found) == ["deployment"] and state in found[0].text
    # Ended sessions (their files are kept) and a crashed client are not.
    for state in ("stopped", "finished"):
        assert reasons(sessions={"a": session(state)}) == []
    assert reasons(sessions={"a": session("running", crashed=True)}) == []


def test_quiet_states_are_a_reason_on_their_own():
    c = live_config.Config()
    c.gpu.quiet_states = ["homing"]
    found = reasons(c, sessions={"a": session("homing")})
    assert set(codes(found)) == {"deployment", "robot_quiet"}
    assert reasons(sessions={"a": session("homing")})[0].code == "deployment"


def test_other_processes_on_the_gpu_over_the_limit_are_a_reason_ours_are_not():
    apps = [
        {"pid": 10, "name": "vllm", "memory_mib": 22000},
        {"pid": 20, "name": "train", "memory_mib": 1500},
    ]
    assert reasons(apps=apps, ours={10}) == []  # under 2000 MiB of others
    apps[1]["memory_mib"] = 2500
    found = reasons(apps=apps, ours={10})
    assert codes(found) == ["foreign_gpu"] and "train(20)" in found[0].text
    assert codes(reasons(apps=apps)) == ["foreign_gpu"]  # ours not named: all count
    # A process whose memory cannot be read counts (fail closed).
    assert codes(reasons(apps=[{"pid": 5, "name": "x", "memory_mib": None}])) == [
        "foreign_gpu"
    ]
    c = live_config.Config()
    c.gpu.yield_.foreign_gpu_mib = 0  # switched off: not even read
    assert reasons(c, apps=None) == []
    c.gpu.yield_.foreign_gpu_mib = 3000
    assert reasons(c, apps=apps, ours={10}) == []


def test_somebody_else_holding_the_gpu_lock_is_a_reason():
    c = live_config.Config()
    assert reasons(c, lock_pids=[123]) == []  # no lock file configured
    c.gpu.lock_file = "/tmp/x.lock"
    assert codes(reasons(c, lock_pids=[123])) == ["foreign_lock"]
    assert reasons(c, lock_pids=[]) == []
    c.gpu.yield_.foreign_lock = False
    assert reasons(c, lock_pids=[123]) == []


def test_what_cannot_be_read_is_a_reason_fail_closed():
    c = live_config.Config()
    c.gpu.lock_file = "/tmp/x.lock"
    found = reasons(c, ports=None, ports_error="OSError", apps=None, lock_error=True)
    assert codes(found) == ["probe_error"] * 3
    # The GPU readings are left out when nothing is queued (check_gpu False)...
    assert reasons(c, check_gpu=False, apps=None, lock_error=True) == []
    # ...the socket table never is.
    assert codes(reasons(c, check_gpu=False, ports=None)) == ["probe_error"]


def test_the_machine_probes_never_connect_and_failures_raise(tmp_path):
    table = tmp_path / "tcp"
    with pytest.raises(OSError):
        gpumgr.listening_ports_strict((str(table), str(table)))
    table.write_text("header\n")
    assert (
        gpumgr.listening_ports_strict((str(table), str(tmp_path / "missing6"))) == set()
    )

    def bad(*a, **k):
        raise FileNotFoundError("nvidia-smi")

    with pytest.raises(FileNotFoundError):
        gpumgr.compute_apps(bad)

    def good(*a, **k):
        return SimpleNamespace(stdout="10, /usr/bin/python3, 2048\n11, x, [N/A]\n")

    rows = gpumgr.compute_apps(good)
    assert rows[0] == {"pid": 10, "name": "python3", "memory_mib": 2048}
    assert rows[1]["memory_mib"] is None


def test_flock_holders_reads_the_table_and_does_not_take_the_lock(tmp_path):
    lock = tmp_path / "gpu.lock"
    lock.write_text("")
    st = lock.stat()
    dev = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
    table = tmp_path / "locks"
    table.write_text(
        f"1: FLOCK  ADVISORY  WRITE 4242 {dev} 0 EOF\n"
        f"2: POSIX  ADVISORY  WRITE 99 {dev} 0 EOF\n"
        "3: FLOCK  ADVISORY  WRITE 7 ff:ff:1 0 EOF\n"
    )
    assert gpumgr.flock_holders(lock, locks=str(table)) == [4242]
    table.write_text("3: FLOCK  ADVISORY  WRITE 7 ff:ff:1 0 EOF\n")
    assert gpumgr.flock_holders(lock, locks=str(table)) == []
    # The inode matches but not the device: it cannot be told from the table.
    table.write_text(f"1: FLOCK  ADVISORY  WRITE 4242 ff:ee:{st.st_ino} 0 EOF\n")
    assert gpumgr.flock_holders(lock, locks=str(table)) is None
    assert gpumgr.flock_holders(lock, locks=str(tmp_path / "nope")) is None
    assert gpumgr.flock_holders(tmp_path / "absent", locks=str(table)) == []


def test_flock_busy_sees_a_holder_and_lets_go_of_its_own_probe(tmp_path):
    lock = tmp_path / "gpu.lock"
    assert not gpumgr.flock_busy(lock)  # no file
    with lock.open("a") as other:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert gpumgr.flock_busy(lock)
    assert not gpumgr.flock_busy(lock)
    # The probe held nothing back: someone else can take it right away.
    with lock.open("a") as again:
        fcntl.flock(again, fcntl.LOCK_EX | fcntl.LOCK_NB)


# --- over time ------------------------------------------------------------------------


def test_it_yields_at_once_and_resumes_only_after_every_reason_stayed_gone():
    state = gpumgr.YieldState(60)
    why = [gpumgr.Reason("deployment", "port 8000")]
    assert not state.update(0, []) and not state.active
    assert state.update(10, why) and state.active and state.since == 10
    assert not state.update(11, why)
    assert state.since == 10  # since the first reason, not the latest reading
    state.update(20, [])  # gone
    assert state.active and state.reasons[0].code == "resuming"
    assert "60" in state.text()
    state.update(40, [])
    assert "40" in state.text()
    state.update(79, [])
    assert state.active
    # It comes back before the time is up: the clock starts again.
    state.update(80, why)
    assert state.reasons[0].code == "deployment" and state.public()["resume_at"] is None
    state.update(90, [])
    state.update(149, [])
    assert state.active
    assert state.update(150, []) and not state.active
    assert state.public() == {
        "active": False,
        "reasons": [],
        "since": None,
        "resume_at": None,
    }
    quick = gpumgr.YieldState(0)
    quick.update(0, why)
    assert quick.update(1, []) and not quick.active


# --- the supervisor -------------------------------------------------------------------


def add_apps(ctl, rows):
    ctl.probes.apps = lambda: rows
    ctl._apps = (0.0, None, "")


def test_a_policy_server_stops_the_start_and_the_gate_file_says_yield(ctl):
    ctl.rollouts.write(0)
    t = time.time()
    ctl.machine.ports = {8000}
    assert step(ctl, t) == "yield"
    assert ctl.vllm.state == "stopped" and not ctl.vllm.mine()
    gate = jsonio.read(ctl.config.live_dir / "gate.json")
    assert gate["open"] is False and gate["code"] == "yield"
    assert (
        gate["yield"]["active"] and gate["yield"]["reasons"][0]["code"] == "deployment"
    )
    assert gating.closed(gate, worker=True)
    status = ctl.status(t)
    assert status["yield"]["active"] and status["yield"]["since"] == t
    assert status["queue"][0]["state"] == "yielding"
    assert ctl.spawned == []


def test_it_resumes_after_resume_after_s_and_not_before(ctl):
    ctl.rollouts.write(0)
    t = time.time()
    ctl.machine.ports = {8000}
    step(ctl, t)
    ctl.machine.ports = set()
    ctl.tick(t + 100)
    assert ctl.yield_state.active
    assert "resuming" in ctl.status(t + 100)["queue"][0]["reason"]
    assert step(ctl, t + 100 + 30) == "yield"
    assert ctl.vllm.state == "stopped"
    # 60 s of quiet: the model may start (the policy-settle wait is long over).
    assert step(ctl, t + 100 + 61) == "ok"
    assert not ctl.yield_state.active
    assert wait_for(lambda: ctl.vllm.poll() == "ready")
    ctl.tick(t + 100 + 62)
    assert ctl.spawned == ["pi05_fake__stack_the_plates"]


def test_a_sleeping_model_is_not_woken_while_yielding(ctl):
    ctl.rollouts.write(0)
    t = time.time()
    up(ctl, t)
    ctl.spawned.clear()
    assert ctl.vllm.sleep() and ctl.vllm.state == "asleep"
    ctl.rollouts.session("standby")
    ctl.tick(t + 1)
    assert ctl.decision.code == "yield" and ctl.vllm.state == "asleep"
    ctl.tick(t + 30)
    assert ctl.vllm.state == "asleep" and ctl.spawned == []
    ctl.rollouts.session("finished")
    ctl.tick(t + 31)  # the reason is gone; the quiet period starts
    assert ctl.vllm.state == "asleep"
    ctl.tick(t + 31 + 61)
    assert ctl.vllm.state == "ready"


def test_other_processes_on_the_gpu_make_it_yield_a_small_one_does_not(ctl):
    ctl.rollouts.write(0)
    t = time.time()
    add_apps(ctl, [{"pid": 999, "name": "train.py", "memory_mib": 9000}])
    assert step(ctl, t) == "yield"
    assert ctl.yield_state.reasons[0].code == "foreign_gpu"
    assert ctl.vllm.state == "stopped"
    add_apps(ctl, [{"pid": 999, "name": "tiny", "memory_mib": 300}])
    ctl.tick(t + 10)
    assert ctl.yield_state.reasons[0].code == "resuming"
    assert step(ctl, t + 80) == "ok"


def test_somebody_else_holding_the_gpu_lock_makes_it_yield(ctl):
    ctl.rollouts.write(0)
    t = time.time()
    # Another process (not this one, whose own locks are ours) holds it.
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl,sys,time;"
                "f=open(sys.argv[1],'a');fcntl.flock(f,fcntl.LOCK_EX);"
                "print('held',flush=True);time.sleep(60)"
            ),
            ctl.config.gpu.lock_file,
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        assert step(ctl, t) == "yield"
        assert ctl.yield_state.reasons[0].code == "foreign_lock"
        assert str(holder.pid) in ctl.yield_state.reasons[0].text
        assert ctl.vllm.state == "stopped"
    finally:
        holder.kill()
        holder.wait()
    ctl.tick(t + 1)
    assert step(ctl, t + 70) == "ok"


def test_unreadable_machine_state_is_treated_as_in_use(ctl):
    ctl.rollouts.write(0)
    t = time.time()

    def broken():
        raise OSError("no table")

    ctl.probes.ports_strict = broken
    assert step(ctl, t) == "yield"
    assert ctl.yield_state.reasons[0].code == "probe_error"
    assert ctl.vllm.state == "stopped"
    ctl.probes.ports_strict = lambda: set()

    def smi():
        raise subprocess.CalledProcessError(9, "nvidia-smi")

    ctl.probes.apps = smi
    ctl._apps = (0.0, None, "")
    ctl.tick(t + 70)
    assert ctl.yield_state.active and ctl.yield_state.reasons[0].code == "probe_error"
    assert ctl.vllm.state == "stopped"
    assert any("labelling yields" in e["text"] for e in ctl.events)


def test_a_batch_in_flight_is_cut_within_a_second_and_stopped(ctl):
    ctl.rollouts.write(0)
    t = time.time()
    up(ctl, t)
    worker = subprocess.Popen(["sleep", "60"], start_new_session=True)
    ctl.worker, ctl.worker_dataset = worker, "pi05_fake__stack_the_plates"
    try:
        ctl.machine.ports = {8000}  # a policy server comes up, no session yet
        started = time.monotonic()
        wait = ctl.tick(t + 1)
        gate = jsonio.read(ctl.config.live_dir / "gate.json")
        # The worker reads gate.json every 0.2 s; the supervisor looks again
        # within 0.25 s while a batch runs.
        assert gating.closed(gate, worker=True)
        assert wait <= ctl.config.service.gate_poll_s
        assert time.monotonic() - started < 1.0
        assert worker.poll() is None  # first it may cancel its request itself
        ctl.tick(t + 1 + controller.YIELD_STOP_GRACE_S + 0.1)
        assert worker.poll() is not None  # then it is stopped, its runs pause
        assert ctl.worker is None
        assert ctl.vllm.state == "ready"  # (the model is not killed for it)
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait()


def test_with_the_rules_off_the_old_behaviour_is_unchanged(ctl):
    ctl.config.gpu.yield_.enabled = False
    ctl.rollouts.write(0)
    t = time.time()
    ctl.machine.ports = {8000}
    ctl.machine.policy_mib = 7685
    ctl.rollouts.session("standby")
    up(ctl, t)
    assert not ctl.yield_state.active
    assert "yield" not in (jsonio.read(ctl.config.live_dir / "gate.json") or {}).get(
        "code", ""
    )
    assert ctl.status(t)["yield"]["enabled"] is False


def test_a_long_foreign_use_is_reported_as_paused_a_deployment_is_not(ctl):
    ctl.rollouts.write(0)
    t = time.time()
    ctl.machine.ports = {8000}
    ctl.tick(t)
    ctl.tick(t + ctl.config.gpu.blocked_pause_s + 5)
    assert ctl.status(t)["labelling_paused"] is None  # a deployment is normal
    ctl.machine.ports = set()
    add_apps(ctl, [{"pid": 999, "name": "train", "memory_mib": 9000}])
    ctl.tick(t + 1000)
    assert ctl.yield_state.active
    ctl.tick(t + 1000 + ctl.config.gpu.blocked_pause_s + 5)
    paused = ctl.status(t + 2000)["labelling_paused"]
    assert paused and paused["code"] == "gpu_in_use"


def test_an_online_judgement_does_not_wake_the_model_beside_someone_else(ctl):
    ctl.rollouts.write(0)
    t = time.time()
    up(ctl, t)
    assert ctl.vllm.sleep()
    add_apps(ctl, [{"pid": 999, "name": "train", "memory_mib": 9000}])
    ctl.tick(t + 1)
    assert ctl.yield_state.active
    ok, code, why = ctl._online_wake(t + 2, "timeshare")
    assert ok is False and code == "no_room" and "yield" in why
    assert ctl.vllm.state == "asleep"


def test_the_idle_service_never_asks_nvidia_smi(ctl):
    calls = []
    ctl.probes.apps = lambda: calls.append(1) or []
    t = time.time()
    ctl.tick(t)  # nothing to label
    assert calls == []


def test_a_cold_start_in_progress_is_given_up_when_a_reason_appears(ctl):
    ctl.rollouts.write(0)
    t = time.time()
    assert step(ctl, t) == "ok"  # the start begins
    assert ctl.vllm.mine() and ctl.vllm.state == "starting"
    ctl.vllm.poll = lambda: "starting"  # (the fake server answers within a second)
    ctl.machine.ports = {8000}
    assert step(ctl, t + 1) == "yield"
    assert not ctl.vllm.mine() and ctl.vllm.state == "stopped"
    assert not ctl.lock.held


def test_a_persons_run_is_told_why_when_the_labelling_yields():
    gate = {
        "open": False,
        "code": "yield",
        "reason": "labelling yields: x",
        "updated_at": time.time(),
    }
    from levi.live import gating as g

    assert "yielding the GPU" in g._message(gate)
    assert "continues by itself" in g._message(gate, run=True)
    assert "inferring" in g._message({**gate, "code": "policy_inferring"})


@pytest.mark.parametrize("error", [PermissionError("denied"), OSError("I/O")])
def test_flock_busy_open_errors_raise_instead_of_claiming_idle(
    tmp_path, monkeypatch, error
):
    def fail(*args, **kwargs):
        raise error

    with monkeypatch.context() as patch:
        patch.setattr(gpumgr.os, "open", fail)
        with pytest.raises(type(error), match=str(error)):
            gpumgr.flock_busy(tmp_path / "gpu.lock")


@pytest.mark.parametrize("error", [PermissionError("denied"), OSError("I/O")])
def test_flock_busy_probe_errors_raise_and_close_fd(tmp_path, monkeypatch, error):
    lock = tmp_path / "gpu.lock"
    lock.write_text("")
    closed = []
    real_close = os.close

    def fail(*args, **kwargs):
        raise error

    def close(fd):
        closed.append(fd)
        real_close(fd)

    with monkeypatch.context() as patch:
        patch.setattr(gpumgr.fcntl, "flock", fail)
        patch.setattr(gpumgr.os, "close", close)
        with pytest.raises(type(error), match=str(error)):
            gpumgr.flock_busy(lock)
    assert len(closed) == 1
    with pytest.raises(OSError):
        os.fstat(closed[0])


def broken_lock_probe(monkeypatch, ctl, error):
    monkeypatch.setattr(gpumgr, "flock_holders", lambda *a, **k: None)
    monkeypatch.setattr(ctl.vllm, "holds_lock", lambda *a, **k: False)
    ctl.lock.release()

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(gpumgr, "flock_busy", fail)


@pytest.mark.parametrize("error", [PermissionError("denied"), OSError("I/O")])
def test_lock_fallback_error_prevents_model_start(ctl, monkeypatch, error):
    ctl.rollouts.write(0)
    t = time.time()
    broken_lock_probe(monkeypatch, ctl, error)
    found = ctl._yield_reasons(t, True)
    assert any(r.code == "probe_error" and "lock" in r.text for r in found)
    assert step(ctl, t) == "yield"
    assert ctl.vllm.state == "stopped" and ctl.spawned == []
    gate = jsonio.read(ctl.config.live_dir / "gate.json")
    assert gating.closed(gate, worker=True)
    assert gate["yield"]["reasons"][0]["code"] == "probe_error"


@pytest.mark.parametrize("error", [PermissionError("denied"), OSError("I/O")])
@pytest.mark.parametrize("asleep", [False, True])
def test_lock_fallback_error_closes_gate_for_existing_model(
    ctl, monkeypatch, error, asleep
):
    ctl.rollouts.write(0)
    t = time.time()
    up(ctl, t)
    ctl.spawned.clear()
    if asleep:
        assert ctl.vllm.sleep()
    before = ctl.vllm.state
    broken_lock_probe(monkeypatch, ctl, error)
    ctl.tick(t + 1)
    assert ctl.decision.code == "yield"
    assert ctl.yield_state.reasons[0].code == "probe_error"
    assert ctl.vllm.state == before
    assert ctl.spawned == []
    assert gating.closed(jsonio.read(ctl.config.live_dir / "gate.json"), worker=True)


@pytest.mark.parametrize("error", [PermissionError("denied"), OSError("I/O")])
def test_lock_fallback_error_closes_inflight_worker_gate(ctl, monkeypatch, error):
    ctl.rollouts.write(0)
    t = time.time()
    up(ctl, t)
    worker = subprocess.Popen(["sleep", "60"], start_new_session=True)
    ctl.worker, ctl.worker_dataset = worker, "pi05_fake__stack_the_plates"
    try:
        broken_lock_probe(monkeypatch, ctl, error)
        wait = ctl.tick(t + 1)
        gate = jsonio.read(ctl.config.live_dir / "gate.json")
        assert gating.closed(gate, worker=True)
        assert gate["yield"]["reasons"][0]["code"] == "probe_error"
        assert wait <= ctl.config.service.gate_poll_s
        assert worker.poll() is None
        ctl.tick(t + 1 + controller.YIELD_STOP_GRACE_S + 0.1)
        assert worker.poll() is not None and ctl.worker is None
        assert ctl.vllm.state == "ready"
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait()


def test_unknown_owned_group_yields_on_actual_tick_and_retains_lock(ctl, monkeypatch):
    ctl.rollouts.write(0)
    ctl.rollouts.session("standby")
    ctl.config.gpu.yield_.enabled = False
    up(ctl, time.time())
    ctl.config.gpu.yield_.enabled = True
    real_scan = gpumgr.os.scandir

    def scan(path):
        if str(path) == "/proc":
            raise PermissionError("/proc unreadable")
        return real_scan(path)

    with monkeypatch.context() as probes:
        probes.setattr(gpumgr.os, "scandir", scan)
        ctl.tick(time.time() + 5)
        assert ctl.yield_state.active
        assert any(r.code == "probe_error" for r in ctl.yield_state.reasons)
        assert ctl.lock.held
        # The existing yield path may sleep instead of stopping. An explicit
        # stop must still retain ownership when the same group probe is unknown.
        assert not ctl._stop_vllm()
        assert ctl.vllm.leaving()
        assert ctl.vllm.record_path.exists()
        gate = jsonio.read(ctl.config.live_dir / "gate.json")
        assert not gate["open"] and gating.closed(gate, worker=True)


# --- the policy server's own GPU memory is not "someone else" (L2-ONLINE-NOROOM) ----------

POLICY_APP = {"pid": 841772, "name": "python", "memory_mib": 7660}
FOREIGN_APP = {"pid": 999, "name": "train", "memory_mib": 9000}


def test_the_policy_servers_own_processes_are_not_foreign_gpu():
    # 7660 MiB > foreign_gpu_mib (2000), but it is the evaluated policy.
    assert reasons(apps=[POLICY_APP], policy_pids={841772}) == []
    found = reasons(apps=[POLICY_APP, FOREIGN_APP], policy_pids={841772})
    assert codes(found) == ["foreign_gpu"] and "train(999)" in found[0].text
    assert "841772" not in found[0].text and "7660" not in found[0].text
    # Not named as the policy server's: it still counts.
    assert codes(reasons(apps=[POLICY_APP], policy_pids={1})) == ["foreign_gpu"]


def asleep_vllm(ctl, t):
    ctl.rollouts.write(0)
    up(ctl, t)
    assert ctl.vllm.sleep() and ctl.vllm.state == "asleep"
    ctl.machine.ports = {8000}
    ctl.machine.policy_mib = POLICY_APP["memory_mib"]
    ctl.probes.policy_pids = lambda ports, apps, **kw: {
        a["pid"] for a in apps if a["pid"] == POLICY_APP["pid"]
    }
    fresh(ctl)


def test_an_online_judgement_wakes_the_model_beside_the_policy_server(ctl):
    t = time.time()
    asleep_vllm(ctl, t)
    add_apps(ctl, [POLICY_APP])
    ctl.tick(t + 1)
    # The deployment is a reason to yield the labelling, not to refuse this.
    assert ctl.yield_state.active
    assert {r.code for r in ctl.yield_state.reasons} == {"deployment"}
    fresh(ctl)
    assert ctl._online_wake(t + 2, "timeshare") is None
    assert ctl.vllm.state == "ready"


@pytest.mark.parametrize(
    "held, word",
    [(3000, "loading"), (None, "unknown"), (5999, "loading"), (6000, None)],
)
def test_an_online_wake_waits_for_a_policy_server_that_is_not_loaded(ctl, held, word):
    # The exemption removes the foreign_gpu that used to stop this by accident
    # while the policy server was still preallocating: the batch path waits
    # for it (``policy_loaded``), so must this one.
    t = time.time()
    asleep_vllm(ctl, t)
    ctl.machine.policy_mib = held
    add_apps(ctl, [dict(POLICY_APP, memory_mib=held or 3000)])
    ctl.tick(t + 1)
    assert "foreign_gpu" not in {r.code for r in ctl.yield_state.reasons}
    fresh(ctl)
    answer = ctl._online_wake(t + 2, "timeshare")
    if word is None:
        assert answer is None and ctl.vllm.state == "ready"
        return
    ok, code, why = answer
    assert (ok, code) == (False, "no_room")
    assert "policy server" in why and word in why
    assert ctl.vllm.state == "asleep"


def only_extra_listening(ctl, t, held):
    """Nothing on policy_ports, a reset policy on 8001 holding ``held`` MiB."""
    asleep_vllm(ctl, t)
    ctl.config.gpu.policy_extra_ports = [8001]
    ctl.machine.ports, ctl.machine.policy_mib = {8001}, held
    add_apps(ctl, [dict(POLICY_APP, memory_mib=held or 3000)])
    ctl.tick(t + 1)
    assert not ctl.policy_up  # 8001 is not a deployment
    assert "foreign_gpu" not in {r.code for r in ctl.yield_state.reasons}
    if ctl.vllm.state != "asleep":  # the batch path woke it: put it back
        assert ctl.vllm.sleep()
    assert ctl.vllm.state == "asleep"
    fresh(ctl)


@pytest.mark.parametrize(
    "held, word", [(3000, "loading"), (None, "unknown"), (7660, None)]
)
def test_an_online_wake_waits_for_a_loading_policy_on_an_extra_port(ctl, held, word):
    t = time.time()
    only_extra_listening(ctl, t, held)
    answer = ctl._online_wake(t + 2, "timeshare")
    if word is None:
        assert answer is None and ctl.vllm.state == "ready"
        return
    ok, code, why = answer
    assert (ok, code) == (False, "no_room") and word in why
    assert "8001" in why and ctl.vllm.state == "asleep"


def test_each_policy_port_group_is_judged_on_its_own_memory(ctl):
    t = time.time()
    asleep_vllm(ctl, t)
    ctl.config.gpu.policy_extra_ports = [8001]
    ctl.machine.ports = {8000, 8001}
    held = {8000: 7660, 8001: 3000}
    ctl.probes.policy_vram = lambda ports: held[int(ports[0])]
    add_apps(ctl, [POLICY_APP])
    ctl.tick(t + 1)
    fresh(ctl)
    ok, code, why = ctl._online_wake(t + 2, "timeshare")
    assert (ok, code) == (False, "no_room") and "8001" in why
    held[8001] = 7660  # the reset policy has loaded
    fresh(ctl)
    assert ctl._online_wake(t + 3, "timeshare") is None


def test_an_online_wake_asks_nvidia_smi_once_for_every_policy_port(ctl, monkeypatch):
    t = time.time()
    asleep_vllm(ctl, t)
    ctl.config.gpu.policy_extra_ports = [8001, 8002]
    ctl.machine.ports = {8000, 8001, 8002}
    add_apps(ctl, [POLICY_APP])
    ctl.tick(t + 1)
    if ctl.vllm.state != "asleep":
        assert ctl.vllm.sleep()
    fresh(ctl)
    calls = []

    # The real memory lookup over a fake socket table and one fake nvidia-smi.
    class OurUnit:
        """Our policy unit holds the listening socket (tests/test_live_noroom_own.py
        reads a fake kernel tree; here only the call count matters)."""

        def listening_inodes(self, ports):
            return {"1"}

        def policy_units(self):
            return ["levi-aeri-policy-test"]

        def servers(self, units, inodes):
            return {units[0]: {POLICY_APP["pid"]}}

    monkeypatch.setattr(gpumgr, "OwnUnits", OurUnit)
    monkeypatch.setattr(
        gpumgr,
        "gpu_holders",
        lambda runner=None: calls.append(1) or [dict(POLICY_APP, memory_mib=7000)],
    )
    ctl.probes.policy_vram = gpumgr.policy_vram_mib
    assert ctl._online_wake(t + 2, "timeshare") is None
    assert len(calls) == 1


def test_an_online_wake_reads_the_policy_memory_now_not_the_cache(ctl):
    t = time.time()
    asleep_vllm(ctl, t)
    add_apps(ctl, [dict(POLICY_APP, memory_mib=3000)])
    ctl.tick(t + 1)
    # The last reading (a moment ago) was a loaded server; a new one has
    # started since and holds 3000 MiB.
    ctl.machine.policy_mib = 3000
    ctl._policy_mib = (t + 1.9, 7660)
    ctl._vram = (0.0, None)
    ok, code, why = ctl._online_wake(t + 2, "timeshare")
    assert (ok, code) == (False, "no_room") and "loading" in why
    # And the other way: a stale None is not believed either.
    ctl.machine.policy_mib = 7660
    ctl._policy_mib = (t + 2.9, None)
    assert ctl._online_wake(t + 3, "timeshare") is None


def test_an_online_wake_without_a_policy_server_is_not_held_back(ctl):
    t = time.time()
    asleep_vllm(ctl, t)
    ctl.machine.ports, ctl.machine.policy_mib = set(), None
    add_apps(ctl, [])
    ctl.tick(t + 1)
    fresh(ctl)
    assert ctl._online_wake(t + 2, "timeshare") is None


def test_a_real_foreign_process_still_refuses_beside_the_policy_server(ctl):
    t = time.time()
    asleep_vllm(ctl, t)
    add_apps(ctl, [POLICY_APP, FOREIGN_APP])
    ctl.tick(t + 1)
    ok, code, why = ctl._online_wake(t + 2, "timeshare")
    assert (ok, code) == (False, "no_room") and "foreign_gpu_mib" in why
    assert "train(999)" in why and ctl.vllm.state == "asleep"


def test_too_little_free_vram_is_no_room_and_says_so_not_a_foreign_process(ctl):
    t = time.time()
    asleep_vllm(ctl, t)
    add_apps(ctl, [POLICY_APP])
    ctl.machine.free = 3000
    ctl.tick(t + 1)
    fresh(ctl)
    ok, code, why = ctl._online_wake(t + 2, "timeshare")
    assert (ok, code) == (False, "no_room")
    assert "VRAM free" in why and "needed" in why and "other processes" not in why
    assert ctl.vllm.state == "asleep"


def test_extra_policy_ports_are_asked_for_with_the_policy_ports(ctl):
    t = time.time()
    asleep_vllm(ctl, t)
    ctl.config.gpu.policy_extra_ports = [8001]
    asked = []

    def policy_pids(ports, apps, **kw):
        asked.append((list(ports), kw))
        return set()

    ctl.probes.policy_pids = policy_pids
    add_apps(ctl, [POLICY_APP])
    ctl.tick(t + 1)
    assert asked and asked[-1][0] == [8000, 8001]
    # The budget comes from the configuration, not from the probe; ownership
    # comes only from our own units (no folder rule).
    assert asked[-1][1] == {"budget_mib": ctl.config.gpu.policy_budget_mib}
    ctl.config.gpu.policy_budget_mib = 9000
    ctl._apps = (0.0, None, "")
    ctl.tick(t + 2)
    assert asked[-1][1] == {"budget_mib": 9000}


@pytest.mark.parametrize(
    "error", [OSError("proc gone"), PermissionError("no"), KeyError("x")]
)
def test_a_failing_policy_probe_keeps_the_yield_and_the_refusal(ctl, error):
    t = time.time()
    asleep_vllm(ctl, t)

    def broken(ports, apps, **kw):
        raise error

    ctl.probes.policy_pids = broken
    add_apps(ctl, [POLICY_APP])
    ctl.tick(t + 1)
    assert "foreign_gpu" in {r.code for r in ctl.yield_state.reasons}
    ok, code, why = ctl._online_wake(t + 2, "timeshare")
    assert (ok, code) == (False, "no_room") and "foreign_gpu_mib" in why
    assert ctl.vllm.state == "asleep"


def test_an_unreadable_gpu_process_table_keeps_the_yield(ctl):
    t = time.time()
    asleep_vllm(ctl, t)

    def broken():
        raise OSError("nvidia-smi")

    ctl.probes.apps = broken
    ctl._apps = (0.0, None, "")
    ctl.tick(t + 1)
    assert "probe_error" in {r.code for r in ctl.yield_state.reasons}
    ok, code, _ = ctl._online_wake(t + 2, "timeshare")
    assert (ok, code) == (False, "no_room")


def test_policy_extra_ports_default_off_and_are_validated(tmp_path):
    import tomllib

    default = live_config.Config()
    assert default.gpu.policy_extra_ports == []
    assert "policy_extra_ports" not in tomllib.loads(live_config.render(default))["gpu"]
    c = live_config.Config()
    c.gpu.policy_extra_ports = [8001]
    path = tmp_path / "effective.toml"
    path.write_text(live_config.render(c))
    assert live_config.load(path).gpu.policy_extra_ports == [8001]
    for bad in ([5000], [0], [70000]):
        c.gpu.policy_extra_ports = bad
        with pytest.raises(ValueError, match="policy_extra_ports"):
            c.validate()


@pytest.mark.parametrize(
    "port, what",
    [
        (8100, "vllm.port"),
        (7882, "online.port"),
        (7880, "service.ui_port"),
        (7881, "service.core_port"),
        (5000, "5000"),
        (5001, "5001"),
        (5100, "5100"),
        (7470, "7470"),
        (7860, "7860"),
        (7861, "7861"),
    ],
)
def test_a_policy_extra_port_may_not_be_another_servers_port(port, what):
    c = live_config.Config()
    c.gpu.policy_extra_ports = [8001, port]
    with pytest.raises(ValueError, match="policy_extra_ports") as found:
        c.validate()
    assert what in str(found.value)
    c.gpu.policy_extra_ports = [8001, 8002]
    c.validate()


def test_the_doctor_lists_the_extra_policy_ports_and_who_is_exempt():
    c = live_config.Config()

    def keys():
        return [
            x
            for x in live_config.checks(c, watching=False)
            if x["key"] == "gpu.policy_extra_ports"
        ]

    assert keys() == []  # nothing set: nothing to list
    c.gpu.policy_extra_ports = [8001]
    (found,) = keys()
    assert found["level"] == "ok"
    for word in ("8001", "levi-aeri-policy-", "8500", "foreign_gpu"):
        assert word in found["message"]


def test_the_doctor_lists_who_is_exempt_with_the_default_configuration(
    ctl, monkeypatch, capsys
):
    from types import SimpleNamespace

    from levi.live import cli

    c = ctl.config
    assert c.gpu.policy_extra_ports == []  # the default
    seen = {}

    def exempt(ports, apps, **kw):
        seen.update(ports=list(ports), apps=apps, **kw)
        return {841772}

    monkeypatch.setattr(cli.gpumgr, "vram", dict)
    monkeypatch.setattr(cli.gpumgr, "gpu_holders", lambda *a, **k: [POLICY_APP])
    monkeypatch.setattr(cli.gpumgr, "listening_ports", lambda *a, **k: {8000})
    monkeypatch.setattr(cli.gpumgr, "policy_gpu_pids", exempt)
    report = cli.diagnose(c)
    assert report["policy_exempt"] == {"pids": [841772], "memory_mib": 7660}
    assert seen["ports"] == [8000] and "roots" not in seen
    assert seen["budget_mib"] == c.gpu.policy_budget_mib
    monkeypatch.setattr(cli, "resolve_config", lambda args: c)
    cli.cmd_doctor(SimpleNamespace(json=False))
    out = capsys.readouterr().out
    assert "foreign_gpu" in out and "841772" in out and "7660" in out

    # An unreadable answer is said, not hidden.
    def broken(*a, **k):
        raise OSError("proc")

    monkeypatch.setattr(cli.gpumgr, "policy_gpu_pids", broken)
    assert cli.diagnose(c)["policy_exempt"] is None


def test_doctor_status_and_gate_name_the_extra_policy_ports(ctl, monkeypatch):
    from levi.live import cli

    c = ctl.config
    c.gpu.policy_extra_ports = [8001, 8002]
    monkeypatch.setattr(cli.gpumgr, "vram", dict)
    monkeypatch.setattr(cli.gpumgr, "gpu_holders", lambda *a, **k: [])
    monkeypatch.setattr(cli.gpumgr, "listening_ports", lambda *a, **k: {8001, 9})
    report = cli.diagnose(c)
    assert report["policy_extra_ports_listening"] == [8001]
    assert report["policy_ports_listening"] == []
    t = time.time()
    ctl.rollouts.write(0)
    ctl.tick(t)
    gate = jsonio.read(c.live_dir / "gate.json")
    assert gate["policy_extra_ports"] == [8001, 8002]
    # They are listed, but a listener there is still not a deployment and does
    # not close the stale-gate fallback (that is policy_ports' job).
    assert gate["policy_ports"] == [8000]


def test_the_policy_budget_is_validated_and_there_is_no_folder_rule():
    c = live_config.Config()
    assert c.gpu.policy_budget_mib == 8500  # the existing setting, reused
    assert not hasattr(c.gpu, "policy_roots")
    for bad in (-1, "8500", True):
        c.gpu.policy_budget_mib = bad
        with pytest.raises(ValueError, match="policy_budget_mib"):
            c.validate()
    c.gpu.policy_budget_mib = 0
    c.validate()


@pytest.mark.parametrize("doc", ["docs/LIVE.md", "docs/LIVE.zh-CN.md"])
def test_policy_extra_ports_are_documented(doc):
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / doc).read_text(encoding="utf-8")
    assert "`gpu.policy_extra_ports" in text
    # Who is exempt: our own levi-aeri-policy-* units, by their cgroup.procs;
    # the folder rule is gone.
    assert "levi-aeri-policy-" in text and "cgroup.procs" in text
    assert "policy_roots" not in text
