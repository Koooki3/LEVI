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
