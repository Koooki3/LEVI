"""The live service's GPU rules: when vLLM may start or wake, when the model
may work, when it must sleep, and its own server lifecycle -- with fake probes
and a fake serve script, never a real GPU or a real model."""

import json
import os
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
    # 0.72 of vLLM's total (nvidia-smi's less 500 MiB) plus the growth after
    # the first requests.
    assert gpumgr.need_mib(c, profile, 32607) == int(0.72 * 32107) + 1400
    with_policy_free = 32607 - 7685  # the policy server at .22
    assert with_policy_free >= gpumgr.need_mib(c, profile, 32607)
    with_big_policy_free = 32607 - 11863  # the old .35
    assert with_big_policy_free < gpumgr.need_mib(c, profile, 32607)


def test_a_start_needs_room_and_a_settled_policy_server_not_an_idle_evaluation():
    c = cfg_for()
    assert decide(c, "timeshare").allowed
    assert decide(c, "timeshare", free_mib=12000).code == "vram"
    assert decide(c, "timeshare", free_mib=None).code == "vram"
    assert decide(c, "timeshare", since_policy_change_s=5).code == "settling"
    assert decide(c, "timeshare", since_policy_change_s=25).allowed
    assert decide(c, "manual").code == "manual"


def session(state, crashed=False, group="g", task="t"):
    class S:
        pass

    s = S()
    s.state, s.crashed, s.group, s.task_folder = state, crashed, group, task
    return s


def test_the_gate_closes_only_while_the_policy_infers():
    c = cfg_for()
    gate = gpumgr.gate
    assert not gate(c, "timeshare", {("g", "t"): session("running")}, True).open
    assert (
        gate(c, "timeshare", {("g", "t"): session("running")}, True).code
        == "policy_inferring"
    )
    for state in ("homing", "waiting_reset", "standby", "fault", "stopped", "finished"):
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
    """Start vLLM and wait until it answers."""
    assert step(ctl, t) == "ok"
    assert wait_for(lambda: ctl.vllm.poll() == "ready")
    ctl.tick(t + 0.5)


def test_vllm_starts_beside_a_running_evaluation_and_the_gate_holds_the_work(ctl):
    ctl.rollouts.write(0)
    ctl.machine.ports = {8000}
    ctl.machine.policy_mib = 7685
    t = time.time()
    ctl.rollouts.session("running")
    up(ctl, t)
    assert ctl.vllm.mine() and ctl.lock.held
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
    assert step(ctl, t) == "vram" and not ctl.vllm.mine()
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
    up(ctl, t)
    finish_work(ctl)
    ctl.tick(t + 2)
    assert ctl.vllm.state == "ready"  # idle, but not for long enough yet
    ctl.tick(t + 2 + c.vllm.idle_timeout_s + 1)
    assert ctl.vllm.state == "asleep" and ctl.lock.held  # the policy server is still up
    # The evaluation is over: nothing is live any more, so it is released.
    ctl.machine.ports, ctl.machine.policy_mib = set(), None
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
    ctl.rollouts.session("homing")
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


def test_a_failed_vllm_start_is_waited_out_not_retried_in_a_loop(ctl, tmp_path):
    bad = tmp_path / "bad.sh"
    bad.write_text("#!/usr/bin/env bash\nexit 7\n")
    bad.chmod(0o755)
    ctl.config.vllm.script = str(bad)
    ctl.vllm = gpumgr.Vllm(ctl.config)
    ctl.rollouts.write(0)
    t = time.time()
    step(ctl, t)
    assert ctl.vllm.state == "error"
    starts = sum("starting vLLM" in e["text"] for e in ctl.events)
    for n in range(1, 20):
        ctl.tick(t + n)
    assert (
        sum("starting vLLM" in e["text"] for e in ctl.events) == starts
    )  # still waiting
    ctl.tick(t + controller.ERROR_WAIT_S + 5)
    ctl.tick(t + controller.ERROR_WAIT_S + 6)
    assert sum("starting vLLM" in e["text"] for e in ctl.events) == starts + 1
    assert ctl.status()["last_error"]
