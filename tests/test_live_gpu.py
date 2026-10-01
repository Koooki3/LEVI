"""The live service's GPU rules: when vLLM may start or wake, when the model
may work, when it must sleep, and its own server lifecycle -- with fake probes
and a fake serve script, never a real GPU or a real model."""

import json
import os
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from live_helpers import Rollouts

from levi.live import cli, controller, fakevlm, gpumgr
from levi.live import config as live_config

PROJECT = Path(__file__).resolve().parents[1]


# --- pure pieces ---------------------------------------------------------------


def table(rows):
    head = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
    lines = [
        f"   {i}: 00000000:{port:04X} 00000000:0000 {state} 00000000:00000000 00:00000000 00000000  1000        0 {i}"
        for i, (port, state) in enumerate(rows)
    ]
    return head + "\n".join(lines) + "\n"


def test_listening_ports_come_from_the_socket_table_not_a_connection(tmp_path):
    tcp = tmp_path / "tcp"
    tcp.write_text(table([(8000, "0A"), (5000, "01"), (8100, "0A"), (22, "0A")]))
    missing = tmp_path / "nope"
    ports = gpumgr.listening_ports(tables=(str(tcp), str(missing)))
    assert ports == {8000, 8100, 22}  # 5000 is ESTABLISHED, not listening


def test_the_policy_probe_never_opens_a_socket(monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("the GPU probe must not connect to anything")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    gpumgr.listening_ports()  # reads /proc only


def test_vram_is_read_with_a_query_only_nvidia_smi_call():
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "32607, 11033\n", "")

    assert gpumgr.vram(runner=runner) == {
        "total_mib": 32607,
        "used_mib": 11033,
        "free_mib": 21574,
    }
    assert calls[0][:2] == ["nvidia-smi", "--query-gpu=memory.total,memory.used"]
    assert gpumgr.vram(runner=lambda *a, **k: (_ for _ in ()).throw(OSError())) is None


def cfg_for(mode="timeshare", **over):
    c = live_config.Config()
    c.gpu.mode = mode
    c.gpu.settle_s = 20.0
    for key, value in over.items():
        setattr(c.vllm, key, value)
    return c


def decide(c, mode, **kw):
    base = {"free_mib": 26000, "need": 24517, "since_policy_change_s": None}
    base.update(kw)
    return gpumgr.decide(c, mode, **base)


def test_the_memory_a_start_needs_follows_the_measured_profile():
    c = cfg_for()
    profile = c.vllm_profile()
    # 0.72 of vLLM's total (nvidia-smi's less 500 MiB) plus the margin.
    assert gpumgr.need_mib(c, profile, 32607) == int(0.72 * 32107) + 1100


def test_a_start_needs_room_and_a_settled_policy_server_not_an_idle_evaluation():
    c = cfg_for()
    assert decide(c, "timeshare").allowed
    assert decide(c, "timeshare", free_mib=12000).code == "vram"
    assert decide(c, "timeshare", free_mib=None).code == "vram"
    assert decide(c, "timeshare", since_policy_change_s=5).code == "settling"
    assert decide(c, "timeshare", since_policy_change_s=25).allowed
    assert decide(c, "manual").code == "manual"


def session(state, crashed=False, group="g", task="t", updated_at=None):
    class S:
        pass

    s = S()
    s.state, s.crashed, s.group, s.task_folder = state, crashed, group, task
    s.updated_at = updated_at
    s.path = f"/sessions/{group}__{task}.json"
    s.reset_wait_s = None
    return s


def test_the_gate_closes_only_while_the_policy_infers():
    c = cfg_for()
    gate = gpumgr.gate
    assert not gate(c, "timeshare", {("g", "t"): session("running")}, True).open
    assert (
        gate(c, "timeshare", {("g", "t"): session("running")}, True).code
        == "policy_inferring"
    )
    for state in ("homing", "waiting_reset", "standby", "fault"):
        assert gate(c, "timeshare", {("g", "t"): session(state)}, True).open, state
    # A crashed client is not inferring.
    assert gate(
        c, "timeshare", {("g", "t"): session("running", crashed=True)}, True
    ).open
    # A policy server nobody tells us about may be in use.
    unknown = gate(c, "timeshare", {}, True)
    assert not unknown.open and unknown.code == "unknown_client"
    assert gate(c, "timeshare", {}, False).open  # no policy server, no evaluation
    # Coexist and manual never close it.
    for mode in ("coexist", "manual"):
        assert gate(c, mode, {("g", "t"): session("running")}, True).open
    # One of several tasks running is enough.
    many = {("g", "a"): session("waiting_reset"), ("g", "b"): session("running")}
    assert not gate(c, "timeshare", many, True).open


def test_vllm_sleeps_when_its_memory_is_wanted():
    c = cfg_for()
    ok = {"free_mib": 1400, "policy_mib": 7685}  # the measured shared peak
    assert gpumgr.should_sleep(c, "timeshare", **ok) is None
    assert gpumgr.should_sleep(c, "timeshare", free_mib=1400, policy_mib=None) is None
    code, _ = gpumgr.should_sleep(c, "timeshare", free_mib=1400, policy_mib=11863)
    assert code == "policy_large"  # the old .35 setting
    code, _ = gpumgr.should_sleep(c, "timeshare", free_mib=300, policy_mib=7685)
    assert code == "vram"
    assert gpumgr.should_sleep(c, "manual", free_mib=0, policy_mib=99999) is None


def test_the_policy_server_s_memory_is_found_through_its_listening_socket():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]

        def runner(argv, **kwargs):
            if "--query-compute-apps=pid,process_name,used_memory" in argv:
                out = f"{os.getpid()}, python3, 7685\n99999999, other, 500\n"
            else:
                out = ""
            return subprocess.CompletedProcess(argv, 0, out, "")

        assert gpumgr.policy_vram_mib([port], runner=runner) == 7685
        assert (
            gpumgr.policy_vram_mib([port + 1], runner=runner) is None
        )  # nobody listens there


def test_one_profile_for_every_mode_and_auto_is_timeshare():
    c = live_config.Config()
    assert c.effective_gpu_mode() == "timeshare"
    profile = c.vllm_profile()
    assert profile["gpu_memory_utilization"] == 0.72 and profile["sleep_mode"]
    args = gpumgr.launch_args(c, profile)
    assert "--enable-sleep-mode" in args and "4096" in args
    assert "--max-num-batched-tokens" in args and '{"image":128,"video":0}' in args
    c.vllm.sleep_mode = False
    assert "--enable-sleep-mode" not in gpumgr.launch_args(c, c.vllm_profile())


def test_the_shared_gpu_lock_is_the_same_flock_others_use(tmp_path):
    path = tmp_path / "gpu.lock"
    a, b = gpumgr.GpuLock(path), gpumgr.GpuLock(path)
    assert a.acquire() and a.held
    assert not b.acquire()
    # Another agent's `flock` on the file also has to wait.
    shell = subprocess.run(
        ["flock", "-n", str(path), "true"], capture_output=True, check=False
    )
    assert shell.returncode != 0
    a.release()
    assert b.acquire()
    b.release()
    assert gpumgr.GpuLock("").acquire()  # no lock file configured: nothing to take


# --- the vLLM lifecycle with a fake serve script -----------------------------------------


@pytest.fixture
def serve(tmp_path, monkeypatch):
    """A serve.sh look-alike that starts the fake model server detached and
    writes the pid file, exactly as tools/vllm/serve.sh does. It records the
    environment vLLM would have been launched with."""
    monkeypatch.setenv("PYTHONPATH", str(PROJECT))
    pid_dir = tmp_path / "vllm-logs"
    pid_dir.mkdir()
    script = tmp_path / "serve-fake.sh"
    script.write_text(
        f"""#!/usr/bin/env bash
if [[ "${{1:-}}" == "--stop" ]]; then
  PID=$(cat "{pid_dir}/vllm_$2.pid"); kill -TERM -- "-$PID" 2>/dev/null; rm -f "{pid_dir}/vllm_$2.pid"; exit 0
fi
echo "dev=${{VLLM_SERVER_DEV_MODE:-}} util=${{GPU_UTIL:-}} args=$*" > "{pid_dir}/launch.txt"
setsid nohup "{sys.executable}" -m levi.live.fakevlm --port "$PORT" >/dev/null 2>&1 < /dev/null &
echo $! > "{pid_dir}/vllm_$PORT.pid"
"""
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script, pid_dir


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live(tmp_path, serve, demo_template):
    script, pid_dir = serve
    c = live_config.Config()
    c.service.workspace = str(tmp_path / "ws")
    c.service.home = str(tmp_path / "home")
    c.watch.roots = [str(tmp_path / "rollouts")]
    c.watch.backlog = "process"
    c.watch.settle_s = 0.0
    c.gpu.mode = "timeshare"
    c.gpu.lock_file = str(tmp_path / "gpu.lock")
    c.gpu.settle_s = 20.0
    c.vllm.script = str(script)
    c.vllm.stop_script = str(script)
    c.vllm.pid_dir = str(pid_dir)
    c.vllm.port = free_port()
    c.vllm.idle_timeout_s = 30.0
    cli.prepare(c)
    rollouts = Rollouts(tmp_path / "rollouts", demo_template)
    return c, rollouts


def wait_for(predicate, seconds=20):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_vllm_starts_sleeps_wakes_and_stops_only_what_it_started(live, serve):
    c, _ = live
    vllm = gpumgr.Vllm(c)
    profile = c.vllm_profile()
    assert vllm.start(profile) and vllm.state == "starting" and vllm.mine()
    launched = (serve[1] / "launch.txt").read_text()
    assert (
        "dev=1" in launched
        and "util=0.72" in launched
        and "--enable-sleep-mode" in launched
    )
    assert wait_for(lambda: vllm.poll() == "ready")
    pid = vllm._pid()
    assert vllm.sleep() and vllm.state == "asleep" and gpumgr.is_sleeping(c.vllm.port)
    assert vllm.poll() == "asleep" and vllm.mine()
    # A restarted supervisor finds it asleep and takes it back.
    again = gpumgr.Vllm(c)
    assert again.state == "asleep" and again.mine()
    assert (
        again.wake() and again.state == "ready" and not gpumgr.is_sleeping(c.vllm.port)
    )
    assert vllm.stop() and vllm.state == "stopped"
    assert wait_for(lambda: gpumgr.identity(pid) is None)
    assert not gpumgr.healthy(c.vllm.port)
    assert not c.live_dir.joinpath("vllm.json").exists()


def test_without_sleep_mode_there_is_no_sleeping(live):
    c, _ = live
    c.vllm.sleep_mode = False
    vllm = gpumgr.Vllm(c)
    assert vllm.start(c.vllm_profile())
    assert wait_for(lambda: vllm.poll() == "ready")
    assert not vllm.sleep() and vllm.state == "ready"
    vllm.stop()


def test_a_server_it_did_not_start_is_never_stopped_or_put_to_sleep(live):
    c, _ = live
    server, fake, port = fakevlm.serve(0)
    try:
        c.vllm.port = port
        vllm = gpumgr.Vllm(c)
        assert vllm.external() and not vllm.mine()
        assert not vllm.sleep() and not fake.sleeping
        assert vllm.stop()  # nothing of ours to stop
        assert gpumgr.healthy(port)
    finally:
        server.shutdown()


# --- the controller's GPU decisions ----------------------------------------------------------


class Machine:
    """Fake probes: what the kernel and nvidia-smi would say."""

    def __init__(self):
        self.ports = set()
        self.free = 26000
        self.policy_mib = None

    def probes(self):
        return controller.Probes(
            vram=lambda: {
                "total_mib": 32607,
                "used_mib": 32607 - self.free,
                "free_mib": self.free,
            },
            ports=lambda: set(self.ports),
            holders=list,
            policy_vram=lambda ports: self.policy_mib,
        )


@pytest.fixture
def ctl(live):
    c, rollouts = live
    machine = Machine()
    spawned = []
    ctl = controller.Controller(c, probes=machine.probes(), log=lambda *a: None)
    ctl._spawn = lambda name: spawned.append(name)
    ctl.machine, ctl.spawned, ctl.rollouts = machine, spawned, rollouts
    yield ctl
    ctl.shutdown()


def step(ctl, now):
    ctl.tick(now)
    return ctl.decision.code


def fresh(ctl):
    ctl._vram = (0.0, None)
    ctl._policy_mib = (0.0, None)


def up(ctl, t):
    """Start vLLM and wait until it answers (the gate is open: a cold start
    never happens while the policy infers)."""
    assert step(ctl, t) == "ok"
    assert wait_for(lambda: ctl.vllm.poll() == "ready")
    ctl.tick(t + 0.5)


def test_vllm_starts_beside_a_running_evaluation_and_the_gate_holds_the_work(ctl):
    ctl.rollouts.write(0)
    ctl.machine.ports = {8000}
    ctl.machine.policy_mib = 7685
    t = time.time()
    ctl.rollouts.session("standby")  # no cold start once an evaluation is under way
    up(ctl, t)
    assert ctl.vllm.mine() and ctl.lock.held
    ctl.spawned.clear()
    ctl.rollouts.session("running")
    ctl.tick(t + 1)
    # The policy infers: the model is resident and ready, but does no work.
    assert not ctl.gate.open and ctl.gate.code == "policy_inferring"
    assert ctl.spawned == [] and ctl.state == "gpu_wait"
    gate = json.loads((ctl.config.live_dir / "gate.json").read_text())
    assert gate["open"] is False and gate["code"] == "policy_inferring"
    # Homing between episodes: the gate opens, the worker starts.
    ctl.rollouts.session("homing")
    ctl.tick(t + 2)
    assert ctl.gate.open and ctl.spawned == ["pi05_fake__stack_the_plates"]
    assert json.loads((ctl.config.live_dir / "gate.json").read_text())["open"] is True
    status = ctl.status(t + 2)["gpu"]
    assert status["gate"]["open"] and status["vllm_state"] == "ready"


def test_a_policy_server_still_loading_delays_the_start(ctl):
    ctl.rollouts.write(0)
    t = time.time()
    ctl.tick(t)  # no policy server yet
    ctl.machine.ports = {8000}
    ctl.machine.free = 26000
    fresh(ctl)
    assert step(ctl, t + 1) in ("settling", "ok")
    assert ctl.policy_changed_at == t + 1
    if (
        ctl.decision.code == "ok"
    ):  # pragma: no cover - the first tick may already have started it
        return
    assert step(ctl, t + 5) == "settling"
    fresh(ctl)
    assert step(ctl, t + 30) == "ok"


def test_not_enough_memory_or_a_busy_lock_blocks_the_start(ctl):
    ctl.rollouts.write(0)
    t = time.time()
    ctl.machine.free = 12000  # the old .35 policy server is running
    assert step(ctl, t) == "insufficient_vram" and not ctl.vllm.mine()
    assert "12000 MiB" in ctl.decision.reason
    ctl.machine.free = 26000
    fresh(ctl)
    other = gpumgr.GpuLock(ctl.config.gpu.lock_file)
    assert other.acquire()
    assert step(ctl, t + 1) == "lock" and not ctl.vllm.mine()
    other.release()
    assert step(ctl, t + 2) == "ok" and ctl.vllm.mine()


def test_a_larger_policy_server_puts_vllm_to_sleep_and_it_wakes_when_the_policy_goes(
    ctl,
):
    ctl.rollouts.write(0)
    t = time.time()
    up(ctl, t)
    # A worker is busy when a policy server that holds 11.8 GB comes up.
    worker = subprocess.Popen(["sleep", "60"], start_new_session=True)
    ctl.worker, ctl.worker_dataset = worker, "pi05_fake__stack_the_plates"
    ctl.machine.ports, ctl.machine.policy_mib = {8000}, 11863
    fresh(ctl)
    ctl.tick(t + 2)
    assert worker.poll() is not None  # the worker was stopped first
    assert ctl.vllm.state == "asleep" and gpumgr.is_sleeping(ctl.config.vllm.port)
    assert ctl.lock.held  # asleep still holds a little of the card
    assert any("goes to sleep" in e["text"] for e in ctl.events)
    # Work is waiting, but the policy server is still big: it stays asleep.
    fresh(ctl)
    assert step(ctl, t + 4) == "policy_large" and ctl.vllm.state == "asleep"
    # The evaluation ended and the policy server exited: wake and go on.
    ctl.machine.ports, ctl.machine.policy_mib, ctl.machine.free = set(), None, 26000
    fresh(ctl)
    ctl.tick(t + 10)
    assert ctl.vllm.state == "ready" and not gpumgr.is_sleeping(ctl.config.vllm.port)
    assert any("woke up" in e["text"] for e in ctl.events)


def test_vllm_sleeps_when_free_memory_runs_out_and_wakes_only_with_room(ctl):
    ctl.rollouts.write(0)
    t = time.time()
    up(ctl, t)
    ctl.machine.free = 300  # someone took the rest of the card
    fresh(ctl)
    ctl.tick(t + 2)
    assert ctl.vllm.state == "asleep"
    ctl.machine.free = 3000  # not enough to wake (it needs about 22 GB)
    fresh(ctl)
    assert step(ctl, t + 4) == "vram" and ctl.vllm.state == "asleep"
    ctl.machine.free = 26000
    fresh(ctl)
    ctl.tick(t + 6)
    assert ctl.vllm.state == "ready"


def finish_work(ctl):
    from levi.live import mirror

    state = mirror.load_state(ctl.config, "pi05_fake__stack_the_plates")
    state["demos"] = {"demo_0000": {"state": "done"}}
    mirror.jsonio.write(mirror.state_path(ctl.config, state["name"]), state)
    ctl.tasks = []


def test_an_idle_vllm_sleeps_during_an_evaluation_and_stops_after_it(ctl):
    c = ctl.config
    ctl.rollouts.write(0)
    t = time.time()
    ctl.machine.ports, ctl.machine.policy_mib = {8000}, 7685
    ctl.rollouts.session("standby")
    up(ctl, t)
    finish_work(ctl)
    ctl.tick(t + 2)
    assert ctl.vllm.state == "ready"  # idle, but not for long enough yet
    ctl.tick(t + 2 + c.vllm.idle_timeout_s + 1)
    assert ctl.vllm.state == "asleep" and ctl.lock.held  # the policy server is still up
    # The evaluation is over: nothing is live any more, so it is released.
    ctl.machine.ports, ctl.machine.policy_mib = set(), None
    ctl.rollouts.session("finished")
    ctl.tick(t + 200)
    assert ctl.vllm.state == "asleep"
    ctl.tick(t + 200 + c.vllm.idle_timeout_s + 1)
    assert not ctl.vllm.mine() and not ctl.lock.held and ctl.state == "idle"


def test_an_idle_vllm_with_no_evaluation_is_stopped_not_put_to_sleep(ctl):
    ctl.rollouts.write(0)
    t = time.time()
    up(ctl, t)
    finish_work(ctl)
    ctl.tick(t + 2)
    ctl.tick(t + 2 + ctl.config.vllm.idle_timeout_s + 1)
    assert not ctl.vllm.mine() and not ctl.lock.held


def test_a_worker_that_does_not_stand_down_is_stopped_but_one_that_does_is_left(ctl):
    from levi.live import jsonio

    ctl.rollouts.write(0)
    t = time.time()
    ctl.machine.ports, ctl.machine.policy_mib = {8000}, 7685
    ctl.rollouts.session("standby")
    up(ctl, t)
    worker = subprocess.Popen(["sleep", "60"], start_new_session=True)
    ctl.worker, ctl.worker_dataset = worker, "pi05_fake__stack_the_plates"
    ctl.rollouts.session("running")
    ctl.tick(t + 2)
    assert not ctl.gate.open and worker.poll() is None  # grace period
    # It reports that it has stood down: left alone however long the gate is shut.
    jsonio.write(
        ctl.config.live_dir / "worker.json",
        {"pid": worker.pid, "phase": "gated", "updated_at": t + 3},
    )
    ctl.rollouts.session("running")
    ctl.tick(t + 2 + controller.GATE_GRACE_S + 1)
    assert worker.poll() is None
    # One that ignores the gate is stopped.
    jsonio.write(
        ctl.config.live_dir / "worker.json",
        {"pid": worker.pid, "phase": "temporal", "updated_at": t + 3},
    )
    ctl.tick(t + 2 + controller.GATE_GRACE_S + 2)
    assert worker.poll() is not None
    assert any("did not stand down" in e["text"] for e in ctl.events)


def test_a_vllm_someone_else_started_is_used_only_when_adopted(ctl):
    server, _fake, port = fakevlm.serve(0)
    try:
        ctl.config.vllm.port = port
        ctl.vllm = gpumgr.Vllm(ctl.config)
        ctl.rollouts.write(0)
        assert step(ctl, time.time()) == "external_busy" and not ctl.spawned
        ctl.config.vllm.adopt_external = True
        ctl.tick(time.time() + 1)
        assert ctl.spawned  # used as is
        ctl.shutdown()
        assert gpumgr.healthy(port)  # and left running
    finally:
        server.shutdown()


def bad_vllm(
    tmp_path,
    ctl,
    message="ValueError: To serve at least one request ... 1.82 GiB KV cache is needed",
):
    """A serve script whose vLLM dies at once and says why in its log."""
    bad = tmp_path / "bad.sh"
    log = Path(ctl.config.vllm.pid_dir) / f"vllm_{ctl.config.vllm.port}.log"
    bad.write_text(
        f"""#!/usr/bin/env bash
echo "INFO starting" > "{log}"
echo "{message}" >> "{log}"
sleep 0 &
echo $! > "{ctl.config.vllm.pid_dir}/vllm_{ctl.config.vllm.port}.pid"
"""
    )
    bad.chmod(0o755)
    ctl.config.vllm.script = str(bad)
    ctl.vllm = gpumgr.Vllm(ctl.config)


def test_a_failing_vllm_start_backs_off_then_stops_and_names_the_reason(ctl, tmp_path):
    bad_vllm(tmp_path, ctl)
    ctl.rollouts.write(0)
    t = time.time()
    starts = lambda: sum("starting vLLM" in e["text"] for e in ctl.events)
    step(ctl, t)
    wait_for(lambda: ctl.vllm.poll() == "error", 5)
    ctl.tick(t + 1)
    assert starts() == 1 and ctl.start_failures == 1
    assert "KV cache" in ctl.vllm.error and "KV cache" in ctl.status()["last_error"]
    for n in range(2, 55):  # the first wait is 60 s
        ctl.tick(t + n)
    assert starts() == 1 and ctl.decision.code == "backoff"
    # Second try after 60 s, third after another 120 s (doubling), then no more.
    ctl.tick(t + 62)
    wait_for(lambda: ctl.vllm.poll() == "error", 5)
    ctl.tick(t + 63)
    assert starts() == 2 and ctl.start_failures == 2
    ctl.tick(t + 63 + 60)
    assert starts() == 2  # now 120 s
    ctl.tick(t + 63 + 125)
    wait_for(lambda: ctl.vllm.poll() == "error", 5)
    ctl.tick(t + 63 + 126)
    assert starts() == 3 and ctl.attention and ctl.attention["code"] == "vllm_failed"
    for n in (400, 2000, 90000):
        ctl.tick(t + n)
    assert starts() == 3  # asking for a person, not retrying
    assert ctl.decision.code == "needs_attention" and "KV cache" in ctl.decision.reason
    status = ctl.status(t + 90001)
    assert status["attention"]["code"] == "vllm_failed"
    # Labelling is paused but the service still takes sessions: the evaluation
    # client only needs somewhere that is receiving its rollouts.
    assert status["accepts_sessions"] and ctl.state == "gpu_wait"
    # `levi live resume` clears it.
    (ctl.config.live_dir / "resume.json").write_text("{}")
    ctl.config.vllm.script = ctl.config.vllm.stop_script  # any script that works
    ctl.tick(t + 90002)
    assert ctl.attention is None and ctl.start_failures == 0


def test_the_launch_script_failing_is_reported_with_vllm_s_own_reason(ctl, tmp_path):
    bad = tmp_path / "bad2.sh"
    bad.write_text("#!/usr/bin/env bash\nexit 7\n")
    bad.chmod(0o755)
    ctl.config.vllm.script = str(bad)
    ctl.vllm = gpumgr.Vllm(ctl.config)
    ctl.rollouts.write(0)
    step(ctl, time.time())
    assert ctl.start_failures == 1 and "exited 7" in ctl.vllm.error


def test_no_cold_start_while_an_evaluation_is_unfinished(ctl):
    """A cold start is 45-70 s of heavy GPU load and its effect on the policy's
    latency is unmeasured: not while a session is running, homing or waiting
    for the reset. It happens at standby, after the end, or with no session."""
    ctl.rollouts.write(0)
    ctl.machine.ports, ctl.machine.policy_mib = {8000}, 7685
    t = time.time()
    ctl.rollouts.session("running")
    assert step(ctl, t) == "gate_closed" and not ctl.vllm.mine()
    for n, state in enumerate(("homing", "waiting_reset"), 1):
        ctl.rollouts.session(state)
        assert step(ctl, t + n) == "evaluation_active", state
        assert not ctl.vllm.mine()
    assert "evaluation" in ctl.decision.reason
    ctl.rollouts.session("standby")
    up(ctl, t + 5)
    assert ctl.vllm.mine()


@pytest.mark.parametrize("state", ["finished", "stopped", "crashed", "fault"])
def test_a_cold_start_is_allowed_once_the_evaluation_is_over_or_faulted(ctl, state):
    ctl.rollouts.write(0)
    ctl.machine.ports = {8000}
    ctl.machine.policy_mib = 7685
    ctl.rollouts.session(state)
    t = time.time()
    # A fault keeps the client alive (it witnesses); an ended session is
    # compared with the policy server's age, so make that older.
    ctl.tick(t - 100)
    ctl.policy_changed_at = t - 200
    up(ctl, t)
    assert ctl.vllm.mine()


def test_a_wake_needs_an_open_gate_and_no_imminent_episode(ctl):
    ctl.rollouts.write(0)
    ctl.machine.ports, ctl.machine.policy_mib = {8000}, 7685
    t = time.time()
    ctl.rollouts.session("standby")
    up(ctl, t)
    ctl.sleep_vllm("test", "for the test")
    assert ctl.vllm.state == "asleep"
    # Work arrives while the policy infers: stay asleep.
    ctl.rollouts.write(1)
    ctl.rollouts.session("running")
    assert step(ctl, t + 2) == "gate_closed" and ctl.vllm.state == "asleep"
    # The reset is nearly over (episode_imminent): still asleep.
    ctl.rollouts.session("waiting_reset")
    paths = [x.path for x in ctl.sessions.values()]
    ctl.sessions = {}  # (the next scan finds it again)
    ctl._waiting_since = {path: t + 3 - 7.5 for path in paths}  # of 10 s
    ctl.tick(t + 4)
    assert ctl.gate.code == "episode_imminent"
    assert ctl.decision.code == "gate_closed" and ctl.vllm.state == "asleep"
    # Early in the reset the gate is open: it wakes.
    ctl._waiting_since = {k: t + 4 for k in ctl._waiting_since}
    ctl.tick(t + 4.5)
    assert ctl.gate.open and ctl.vllm.state == "ready"


def test_prewarm_brings_vllm_up_with_no_work_and_keeps_it(ctl):
    ctl.config.vllm.prewarm = True
    ctl.config.vllm.idle_timeout_s = 5
    ctl.machine.ports, ctl.machine.policy_mib = {8000}, 7685
    ctl.rollouts.session("standby")
    t = time.time()
    assert step(ctl, t) == "ok"  # no demo at all: started anyway
    assert wait_for(lambda: ctl.vllm.poll() == "ready")
    ctl.tick(t + 1)
    # Idle past the timeout: asleep, never stopped.
    for n in range(2, 40):
        ctl.tick(t + n)
    assert ctl.vllm.mine() and ctl.vllm.state == "asleep"
    # An evaluation running does not stop or restart it.
    ctl.rollouts.session("running")
    ctl.tick(t + 41)
    assert ctl.vllm.mine()


def test_prewarm_waits_for_the_evaluation_to_be_between_runs(ctl):
    ctl.config.vllm.prewarm = True
    ctl.machine.ports, ctl.machine.policy_mib = {8000}, 7685
    t = time.time()
    ctl.rollouts.session("waiting_reset")
    assert step(ctl, t) == "evaluation_active" and not ctl.vllm.mine()
    ctl.rollouts.session("standby")
    up(ctl, t + 1)


def test_the_prewarm_flag_and_key_reach_the_config():
    assert live_config.Config().vllm.prewarm is False
    assert live_config.from_dict({"vllm": {"prewarm": True}}).vllm.prewarm is True
    assert "prewarm = true" in live_config.render(
        live_config.from_dict({"vllm": {"prewarm": True}})
    )
    args = cli.build_parser().parse_args(["start", "--prewarm", "--no-core"])
    assert cli.resolve_config(args).vllm.prewarm is True
    assert "--prewarm" in cli._forward(args)


def test_the_budget_follows_the_memory_free_at_start_in_both_orders():
    c = cfg_for()
    plan = lambda free, policy: gpumgr.plan_budget(
        c, free_mib=free, total_mib=32607, policy_up=policy
    )
    # Policy server first (7685 MiB held): the budget the measurement says works.
    first = plan(32607 - 7685, True)
    assert (
        first.ok and 0.74 <= first.utilization <= 0.747 and first.max_model_len == 49152
    )
    # The pre-check and the launch use the same number: free covers need.
    assert first.need_mib <= 32607 - 7685
    # vLLM first (nothing else on the card): the configured 0.72, which leaves
    # room for a policy server that starts later.
    alone = plan(32500, False)
    assert (
        alone.ok
        and alone.utilization == pytest.approx(0.7195)
        and alone.max_model_len == 49152
    )
    # A bigger policy server (the old .35): nothing starts, the numbers say why.
    big = plan(32607 - 11863, True)
    assert not big.ok and big.code == "insufficient_vram"
    assert "20744 MiB" in big.reason and "32768" in big.reason
    # In between the context steps down rather than failing.
    mid = plan(24200, True)
    assert mid.ok and mid.max_model_len == 40960 and mid.utilization >= 0.719
    # Never below the floor, never above the cap.
    assert all(
        plan(f, True).utilization <= 0.747
        for f in range(20000, 32000, 500)
        if plan(f, True).ok
    )


def test_the_controller_starts_vllm_with_the_planned_budget_and_context(ctl, serve):
    ctl.rollouts.write(0)
    ctl.machine.ports, ctl.machine.policy_mib = {8000}, 7685
    ctl.rollouts.session("standby")
    ctl.machine.free = 24200  # a policy server already loaded, little room
    t = time.time()
    assert step(ctl, t) == "ok"
    launched = (serve[1] / "launch.txt").read_text()
    assert "util=0.719" in launched and "--max-model-len 40960" in launched
    assert ctl.vllm.profile["max_model_len"] == 40960
    assert ctl.provider_spec()["context_tokens"] == 40960
    assert any("budget 0.719" in e["text"] and "40960" in e["text"] for e in ctl.events)
    ctl.vllm.stop()


def launched_util(serve):
    return (serve[1] / "launch.txt").read_text().split("util=")[1].split()[0]


def test_the_budget_counts_the_policy_only_once_its_memory_is_really_held(ctl, serve):
    """A listening port is not a loaded policy server: only what it holds
    (``policy_mib``) says whether the card is already shared."""
    ctl.rollouts.write(0)
    ctl.rollouts.session("standby")
    ctl.machine.ports, ctl.machine.free = {8000}, 26000
    t = time.time()
    # Held: 7685 MiB. vLLM takes what is free (up to the cap).
    ctl.machine.policy_mib = 7685
    assert step(ctl, t) == "ok"
    assert launched_util(serve) == "0.7465"
    ctl.vllm.stop()


def test_a_policy_server_whose_memory_is_unknown_gets_the_conservative_budget(
    ctl, serve
):
    ctl.rollouts.write(0)
    ctl.rollouts.session("standby")
    ctl.machine.ports, ctl.machine.free = {8000}, 26000
    ctl.machine.policy_mib = None  # nvidia-smi could not say
    assert step(ctl, time.time()) == "ok"
    assert launched_util(serve) == "0.7195"  # as if alone: room for it to grow
    ctl.vllm.stop()


def test_a_listening_policy_server_that_holds_almost_nothing_is_still_loading(
    ctl, serve
):
    ctl.rollouts.write(0)
    ctl.rollouts.session("standby")
    ctl.machine.ports, ctl.machine.free = {8000}, 26000
    ctl.machine.policy_mib = 1500  # listening, weights not on the card yet
    t = time.time()
    assert step(ctl, t) == "settling" and not ctl.vllm.mine()
    assert "still loading" in ctl.decision.reason
    assert step(ctl, t + 60) == "settling"
    # It loads: counted as held from then on.
    ctl.machine.policy_mib, ctl.machine.free = 7685, 24200
    fresh(ctl)
    assert step(ctl, t + 70) == "ok"
    assert launched_util(serve) == "0.719"
    ctl.vllm.stop()


def test_one_that_stays_small_is_given_up_waiting_for(ctl, serve):
    """Something else listens on the port (or a policy that never loads):
    after ``gpu.policy_load_wait_s`` the budget is the conservative one."""
    ctl.rollouts.write(0)
    ctl.rollouts.session("standby")
    ctl.machine.ports, ctl.machine.free = {8000}, 26000
    ctl.machine.policy_mib = 1500
    t = time.time()
    assert step(ctl, t) == "settling"
    assert step(ctl, t + ctl.config.gpu.policy_load_wait_s + 1) == "ok"
    assert launched_util(serve) == "0.7195"
    ctl.vllm.stop()


def test_yesterday_s_finished_session_does_not_vouch_for_a_policy_server():
    """Session files are never deleted. One that ended before the policy server
    appeared says nothing about the server now listening (another client, a
    --no-record run...): the gate stays closed."""
    c = cfg_for()
    old = {("g", "t"): session("finished", updated_at=50.0)}
    closed = gpumgr.gate(c, "timeshare", old, True, 100.0)
    assert not closed.open and closed.code == "unknown_client"
    # Without knowing when the server appeared it is no witness either.
    assert not gpumgr.gate(c, "timeshare", old, True, None).open
    # One that ended after the server appeared did use it: the server is idle.
    fresh_end = {("g", "t"): session("finished", updated_at=150.0)}
    assert gpumgr.gate(c, "timeshare", fresh_end, True, 100.0).open
    for state in ("stopped", "crashed"):
        assert not gpumgr.gate(
            c, "timeshare", {("g", "t"): session(state, updated_at=50.0)}, True, 100.0
        ).open
    # A live session (not ended) always vouches.
    assert gpumgr.gate(
        c, "timeshare", {("g", "t"): session("standby")}, True, 100.0
    ).open
    # No policy server: nothing to protect, an old file or none is fine.
    assert gpumgr.gate(c, "timeshare", old, False, 100.0).open


# --- the GPU lock follows the vLLM process ---------------------------------------------


def started_controller(live, machine=None):
    c, rollouts = live
    machine = machine or Machine()
    ctl = controller.Controller(c, probes=machine.probes(), log=lambda *a: None)
    ctl._spawn = lambda name: None
    ctl.machine, ctl.rollouts = machine, rollouts
    return ctl


def test_the_lock_stays_with_vllm_when_the_supervisor_dies(live):
    c, _ = live
    first = started_controller(live)
    first.rollouts.write(0)
    first.machine.ports, first.machine.policy_mib = {8000}, 7685
    first.rollouts.session("standby")
    t = time.time()
    up(first, t)
    assert first.lock.held
    pid = first.vllm._pid()
    # kill -9 of the supervisor: the kernel closes its descriptors, nothing
    # else is cleaned up. vLLM keeps the descriptor it was started with.
    first.lock.release()
    other = gpumgr.GpuLock(c.gpu.lock_file)
    assert not other.acquire()  # still locked: no lock-less orphan
    # A restarted supervisor takes the running vLLM back without error.
    second = started_controller(live, first.machine)
    assert second.vllm.mine() and not any(e["level"] == "error" for e in second.events)
    assert second.status()["gpu"]["lock_held"]
    assert second.vllm.holds_lock(c.gpu.lock_file)
    assert second._stop_vllm()
    assert wait_for(lambda: gpumgr.identity(pid) is None)
    assert other.acquire()  # released with the process
    other.release()


def test_a_vllm_that_would_run_without_the_lock_is_not_taken_over(live):
    c, _ = live
    vllm = gpumgr.Vllm(c)
    assert vllm.start(c.vllm_profile())  # started without the lock descriptor
    assert wait_for(lambda: vllm.poll() == "ready")
    holder = gpumgr.GpuLock(c.gpu.lock_file)
    assert holder.acquire()  # somebody else took the lock meanwhile
    ctl = started_controller(live)
    errors = [e["text"] for e in ctl.events if e["level"] == "error"]
    assert errors and "another agent holds the GPU lock" in errors[0]
    assert not ctl.vllm.mine()  # treated as someone else's server, never stopped
    assert gpumgr.healthy(c.vllm.port)
    holder.release()
    gpumgr.Vllm.stop(vllm)  # the test's own cleanup (the record is gone: use pid)
    pid = vllm._pid()
    if pid:
        os.killpg(pid, signal.SIGTERM)
    assert wait_for(lambda: not gpumgr.healthy(c.vllm.port))


def test_nobody_holding_the_lock_lets_the_new_supervisor_take_it(live):
    c, _ = live
    vllm = gpumgr.Vllm(c)
    assert vllm.start(c.vllm_profile())
    assert wait_for(lambda: vllm.poll() == "ready")
    ctl = started_controller(live)
    assert ctl.vllm.mine() and ctl.lock.held
    assert ctl._stop_vllm()


def test_the_lock_is_kept_while_vllm_will_not_die(ctl):
    ctl.rollouts.write(0)
    up(ctl, time.time())
    assert ctl.lock.held
    real = ctl.vllm.stop
    ctl.vllm.stop = lambda: False  # "vLLM did not stop"
    ctl.preempt("test", "forcing a stop that fails")
    assert ctl.lock.held  # the process is still on the GPU
    ctl.vllm.stop = real
    assert ctl._stop_vllm() and not ctl.lock.held


def test_doctor_reports_an_orphan_vllm_when_no_service_runs(live):
    from levi.live import cli, jsonio

    c, _ = live
    sleeper = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        jsonio.write(
            c.live_dir / "vllm.json",
            {
                "pid": sleeper.pid,
                "identity": gpumgr.identity(sleeper.pid),
                "port": c.vllm.port,
            },
        )
        report = cli.diagnose(c)
        assert report["orphan_vllm"] == sleeper.pid
        assert any("orphan vLLM" in w and "--stop" in w for w in report["warnings"])
    finally:
        sleeper.kill()
        sleeper.wait()


# --- the gate closes before the episode, not after it -------------------------------


def waiting(reset_wait_s=10.0, group="g", task="t"):
    s = session("waiting_reset", group=group, task=task)
    s.reset_wait_s = reset_wait_s
    return s


def test_the_gate_closes_a_few_seconds_before_the_next_episode_starts():
    c = cfg_for()
    s = waiting(10.0)
    sessions = {("r", "g", "t"): s}
    began = {
        s.path: 100.0
    }  # waiting for the reset since t=100; the episode starts at 110
    gate = lambda now: gpumgr.gate(
        c, "timeshare", sessions, True, 50.0, now=now, waiting_since=began
    )
    assert gate(100.0).open and gate(106.9).open  # 3 s of lead: closes at 107
    shut = gate(107.1)
    assert not shut.open and shut.code == "episode_imminent"
    assert "starts in" in shut.reason
    assert not gate(110.0).open and not gate(114.9).open  # the client may be late
    assert gate(115.5).open  # still waiting long after: nothing is coming
    # No reset time known, or not waiting: the lead does not apply.
    s.reset_wait_s = None
    assert gate(108.0).open
    s.reset_wait_s = 10.0
    s.state = "homing"
    assert gate(108.0).open


def test_the_controller_learns_when_a_session_began_waiting_and_closes_ahead(ctl):
    ctl.rollouts.write(0)
    ctl.machine.ports, ctl.machine.policy_mib = {8000}, 7685
    t = time.time()
    ctl.rollouts.session("standby")
    up(ctl, t)
    ctl.rollouts.session("waiting_reset")  # reset_wait_s is 10
    ctl.tick(t + 0.1)
    ctl.spawned.clear()
    assert ctl.gate.open
    ctl.tick(t + 6.0)
    assert ctl.gate.open
    ctl.tick(t + 7.5)
    assert not ctl.gate.open and ctl.gate.code == "episode_imminent"
    assert json.loads((ctl.config.live_dir / "gate.json").read_text())["open"] is False
    assert ctl.status(t + 7.5)["sessions"][0]["reset_wait_s"] == 10.0


# --- what counts as the person having acted --------------------------------------------


@pytest.mark.parametrize(
    "change, run_status, acted",
    [
        ({"status": "draft"}, "waiting_for_review", False),
        ({"status": "approved"}, "waiting_for_review", False),
        ({"status": "committed"}, "succeeded", True),
        ({"status": "rejected"}, "waiting_for_review", True),
        # The person cancelled or discarded the run instead: nothing to wait for.
        ({"status": "draft"}, "cancelled", True),
        ({"status": "draft"}, "failed", True),
        ({"status": "draft"}, "succeeded", True),
        ({"status": "draft"}, "partially_succeeded", True),
        # The draft is gone (deleted, archived with its run): same.
        (None, "waiting_for_review", True),
    ],
)
def test_a_draft_wait_ends_when_the_run_or_the_draft_is_no_longer_there(
    ctl, monkeypatch, change, run_status, acted
):
    monkeypatch.setattr(controller, "peek_record", lambda *a: change)
    monkeypatch.setattr(
        controller, "peek_run", lambda *a: {"status": run_status, "plan": {}}
    )
    ctl.awaiting["d"] = {"kind": "changes", "run_id": "r1", "changeset": "c1", "at": 0}
    assert ctl._human_acted("d", {}) is acted
    assert ("d" not in ctl.awaiting) is acted


def test_an_unreadable_store_is_not_taken_for_an_answer(ctl, monkeypatch):
    monkeypatch.setattr(controller, "peek_record", lambda *a: None)
    monkeypatch.setattr(controller, "peek_run", lambda *a: None)
    ctl.awaiting["d"] = {"kind": "changes", "run_id": "r1", "changeset": "c1", "at": 0}
    assert ctl._human_acted("d", {}) is False


def test_a_restarted_supervisor_reads_the_wait_for_a_person_and_starts_no_vllm(
    ctl, monkeypatch
):
    """The wait lives in the dataset state: after a restart the dataset is not
    queued (so vLLM is not cold-started for a worker that would only find the
    plan unapproved) until the person has acted."""
    from levi.live import jsonio, mirror

    ctl.rollouts.write(0)
    ctl.rollouts.session("standby")
    t = time.time()
    ctl.tick(t)  # the mirror creates the dataset state
    ctl.preempt("test", "the old supervisor is gone")
    (name,) = mirror.list_states(ctl.config)
    jsonio.update(
        mirror.state_path(ctl.config, name),
        lambda v: v.update(awaiting={"kind": "plan", "run_id": "r1", "at": t}),
        default=dict,
    )
    monkeypatch.setattr(
        controller,
        "peek_run",
        lambda *a: {"status": "planned", "plan": {"approval": None}},
    )
    again = controller.Controller(
        ctl.config, probes=ctl.machine.probes(), log=lambda *a: None
    )
    spawned = []
    again._spawn = spawned.append
    ctl.machine.ports = {8000}
    ctl.machine.policy_mib = 7685
    try:
        assert again.awaiting[name]["kind"] == "plan"
        for n in range(3):
            again.tick(t + 10 + n)
        assert spawned == [] and not again.vllm.mine()
        assert not any("starting vLLM" in e["text"] for e in again.events)
        assert again.status(t + 20)["datasets"][name]["state"] == "awaiting_approval"
        # The person approves: the wait is forgotten (here and on disk) and the
        # dataset is worked on.
        monkeypatch.setattr(
            controller,
            "peek_run",
            lambda *a: {"status": "planned", "plan": {"approval": {"by": "me"}}},
        )
        again.awaiting[name]["at"] = 0
        again.tick(t + 30)
        assert name not in again.awaiting
        assert "awaiting" not in mirror.load_state(ctl.config, name)
    finally:
        again.shutdown()


# --- labelling_paused: why nothing is being labelled, for the client --------------------


def paused(ctl, now):
    return ctl.status(now)["labelling_paused"]


def test_labelling_paused_is_null_when_all_is_well(ctl):
    ctl.rollouts.write(0)
    ctl.rollouts.session("standby")
    t = time.time()
    up(ctl, t)
    assert paused(ctl, t + 1) is None
    status = ctl.status(t + 1)
    assert status["accepts_sessions"] and "labelling_paused" in status


def test_labelling_paused_names_a_vllm_that_needs_a_person(ctl):
    ctl.rollouts.write(0)
    ctl.rollouts.session("standby")
    t = time.time()
    ctl.attention = {"code": "vllm_failed", "reason": "KV cache too small", "since": t}
    ctl.tick(t + 5)
    p = paused(ctl, t + 5)
    assert p["code"] == "vllm_failed" and p["since"] == t
    assert "KV cache" in p["reason"] and "resume" in p["reason"]
    assert ctl.status(t + 5)["accepts_sessions"]  # unchanged: sessions are welcome


def test_labelling_paused_for_too_little_vram_until_there_is_room(ctl):
    ctl.rollouts.write(0)
    ctl.rollouts.session("standby")
    ctl.machine.ports, ctl.machine.policy_mib = {8000}, 11863
    ctl.machine.free = 32607 - 11863
    t = time.time()
    assert step(ctl, t) == "insufficient_vram"
    first = paused(ctl, t)
    assert first["code"] == "insufficient_vram" and "MiB" in first["reason"]
    fresh(ctl)
    ctl.tick(t + 30)
    assert paused(ctl, t + 30)["since"] == first["since"]  # one pause, not many
    ctl.machine.ports, ctl.machine.policy_mib, ctl.machine.free = set(), None, 26000
    fresh(ctl)
    ctl.tick(t + 40)  # (the policy server going settles for gpu.settle_s)
    up(ctl, t + 70)
    assert paused(ctl, t + 71) is None


def test_labelling_paused_while_vllm_start_fails_and_backs_off(ctl, tmp_path):
    bad_vllm(tmp_path, ctl)
    ctl.rollouts.write(0)
    t = time.time()
    step(ctl, t)
    wait_for(lambda: ctl.vllm.poll() == "error", 5)
    ctl.tick(t + 1)
    p = paused(ctl, t + 1)
    assert p["code"] == "vllm_error" and "KV cache" in p["reason"]


def test_labelling_paused_when_an_unknown_client_keeps_the_gate_shut(ctl):
    ctl.rollouts.write(0)
    ctl.machine.ports, ctl.machine.policy_mib = {8000}, 7685
    pause = ctl.config.gpu.unknown_client_pause_s
    t = time.time()
    ctl.tick(t)
    assert ctl.gate.code == "unknown_client" and paused(ctl, t) is None
    ctl.tick(t + pause - 5)
    assert paused(ctl, t + pause - 5) is None
    ctl.tick(t + pause + 5)
    p = paused(ctl, t + pause + 5)
    assert p["code"] == "unknown_client" and "policy" in p["reason"].lower()
    assert p["since"] == pytest.approx(t, abs=1)
    # A session shows up (it vouches for the server): the pause is over.
    ctl.rollouts.session("standby")
    ctl.tick(t + pause + 10)
    assert paused(ctl, t + pause + 10) is None


# --- the client says when the wait for the reset began ----------------------------------


def set_session_field(ctl, **fields):
    folder = ctl.rollouts.root / ".eval_sessions"
    (path,) = list(folder.glob("*.json"))
    data = json.loads(path.read_text())
    data.update(fields)
    path.write_text(json.dumps(data))


def test_the_gate_uses_the_client_s_own_start_of_the_wait_not_a_guess(ctl):
    from levi.live import sessions

    ctl.rollouts.write(0)
    ctl.machine.ports, ctl.machine.policy_mib = {8000}, 7685
    t = time.time()
    ctl.rollouts.session("waiting_reset")  # reset_wait_s is 10
    set_session_field(ctl, waiting_reset_since=t - 8.0)
    (s,) = sessions.read_sessions([str(ctl.rollouts.root)], t).values()
    assert s.waiting_reset_since == pytest.approx(t - 8.0)
    assert s.public()["waiting_reset_since"] == pytest.approx(t - 8.0)
    # The supervisor sees the session for the first time only now, but the
    # reset began 8 s ago: the next episode is 2 s away, the gate is shut.
    ctl.tick(t)
    assert ctl.gate.code == "episode_imminent" and not ctl.gate.open
    assert ctl.status(t)["sessions"][0]["waiting_reset_since"] == pytest.approx(t - 8)


def test_without_the_field_the_first_sighting_stays_the_estimate(ctl):
    ctl.rollouts.write(0)
    ctl.machine.ports, ctl.machine.policy_mib = {8000}, 7685
    t = time.time()
    ctl.rollouts.session("waiting_reset")
    ctl.tick(t)
    assert ctl.gate.open  # an older client: the wait is taken to begin now
    set_session_field(ctl, waiting_reset_since=None)
    assert ctl.status(t)["sessions"][0]["waiting_reset_since"] == pytest.approx(t)


def test_a_nonsense_start_of_the_wait_is_ignored(ctl):
    ctl.rollouts.write(0)
    ctl.machine.ports, ctl.machine.policy_mib = {8000}, 7685
    t = time.time()
    ctl.rollouts.session("waiting_reset")
    set_session_field(ctl, waiting_reset_since="soon")
    ctl.tick(t)
    assert ctl.gate.open
    set_session_field(ctl, waiting_reset_since=t + 3600)  # from the future
    ctl.tick(t + 1)
    assert ctl.status(t + 1)["sessions"][0]["waiting_reset_since"] <= t + 1


def test_the_supervisor_ticks_at_least_once_a_second_while_an_evaluation_is_on(ctl):
    t = time.time()
    ctl.rollouts.write(0)
    ctl.rollouts.session("standby")
    assert ctl.tick(t) > 1.0  # a standing session: no need
    ctl.rollouts.session("waiting_reset")
    assert ctl.tick(t + 5) <= 1.0
    ctl.rollouts.session("running")
    assert ctl.tick(t + 6) <= 1.0
    ctl.rollouts.session("finished")
    assert ctl.tick(t + 7) > 1.0
