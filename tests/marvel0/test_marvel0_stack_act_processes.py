"""L2 of the stack actions: the page's real code driving real child processes.

``systemd-run`` and ``systemctl`` are mapped onto ``subprocess.Popen`` by
``fake_stack/process_host.py`` (the stop signal, the time to stop and
``SendSIGKILL`` are honoured from the properties the page passed), and the
programs are the stand-ins of ``fake_stack/``: they print the real ready lines,
write a fake health file, and one of them listens on the unprotected loopback
port 5056. Modes make them slow, never ready, crash, ignore SIGINT, print a
line twice or leave one out. Nothing but a stand-in is ever run (the host
checks every argv), nothing touches a robot, and no protected port is used.

These tests start processes and wait for them; they take a few seconds each."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fake_stack import fake_specs, process_host
from stack_act_machine import ActMachine

from levi.marvel0 import net, record, stack, stack_act, stack_units

PORT = 5056
FAKE_ENV = fake_specs.ENV_FILE


def standin_table():
    """The kernel's socket table, cut down to the one port the stand-in C
    listens on. These tests must give the same answer whatever else the
    machine is doing, a real robot server on :5000 included (R2 5): nothing of
    any other port reaches the page."""
    table = net.read()
    return net.Table(
        table.ok,
        tuple(s for s in table.socks if PORT in (s.local_port, s.remote_port)),
    )


class ProcessMachine(ActMachine):
    """The fake machine of L1 with the processes real: the clock is the
    wall clock, the arm's sockets follow terminal A's process and the socket
    table is the kernel's."""

    def __init__(self, tmp_path, **spec_options):
        # (the base class builds its hosts at once: the processes come first)
        self.processes = process_host.ProcessHost(
            wrapper=stack_units.WRAPPER if spec_options.get("via_wrapper") else None
        )
        super().__init__(tmp_path)
        qos = tmp_path / "qos.py"
        qos.write_text(f"x\n{stack_units.QOS_OK}\n")
        self.specs = fake_specs.fake_specs(
            self.health_path,
            port=PORT,
            qos_file=qos,
            alive_file=tmp_path / "b.alive",
            **spec_options,
        )
        self.sup = stack.Stack(self.host())
        self.act = stack_act.Act(
            self.sup, self.act_host(), setup=lambda: (self.loaded, self.specs)
        )
        self.sup.act = self.act
        self.slept.clear()

    @property
    def t(self):
        return time.time()

    @t.setter
    def t(self, value):
        pass

    @property
    def sockets(self):
        proc = self.processes.units.get(self.specs.units["a"].unit)
        alive = proc is not None and proc.alive()
        return {"state": "active" if alive else "inactive", "detail": "fake"}

    @sockets.setter
    def sockets(self, value):
        pass

    def host(self):
        built = super().host()
        import dataclasses

        return dataclasses.replace(
            built,
            run=stack.guarded(self.processes.run),
            net_table=standin_table,
            now=time.time,
            monotonic=time.monotonic,
        )

    def act_host(self):
        built = super().act_host()
        built.sleep = lambda s: None  # (the release polls are bounded by count)
        return built

    def run(self, argv, timeout_s):  # the base class's fake systemd is not used
        return self.processes.run(argv, timeout_s)

    # -- waiting
    def wait_for(self, key, state, timeout=15.0):
        end = time.monotonic() + timeout
        last = None
        while time.monotonic() < end:
            last = self.terminal(key)
            if last["state"] == state:
                return last
            time.sleep(0.05)
        raise AssertionError(f"{key} never became {state}; last: {last}")

    def ready_for_a(self, aged_s=None):
        self.no_manager()
        self.act.marker_op("touch", self.body("marker_touch", "mk-touch-000001"))
        time.sleep(self.specs.marker_wait_s + 0.1)
        self.preflight_a()
        self.no_manager()

    def log_text(self, key):
        return Path(record.read_record(self.state_root / key)["log"]).read_text()


@pytest.fixture
def make(tmp_path):
    made = []

    def build(**options):
        m = ProcessMachine(tmp_path / f"m{len(made)}", **options)
        made.append(m)
        return m

    yield build
    for m in made:
        m.processes.reap()
    assert (
        subprocess.run(
            ["pgrep", "-f", str(fake_specs.HERE / "fake_")],
            capture_output=True,
            check=False,
        ).returncode
        == 1
    ), "a stand-in program was left running"


def start(m, key):
    return m.act.start(key, m.start_body(key))


# --- the happy path ---------------------------------------------------------------------


def test_a_starts_prints_its_lines_and_stops_on_sigint(make):
    m = make()
    m.ready_for_a()
    start(m, "a")
    t = m.wait_for("a", "ready")
    assert [r["key"] for r in t["readiness"]["seen"]] == [
        "fifo",
        "spawners",
        "health_hardware_active",
    ]
    assert (
        "Successful set up FIFO RT scheduling policy with priority 98."
        in m.log_text("a")
    )
    unit = m.specs.units["a"].unit
    proc = m.processes.units[unit]
    assert m.sockets["state"] == "active"
    answer = m.act.stop("a", m.stop_body("a"))
    assert answer["done"] == "stopping"
    assert proc.signals == ["SIGINT"] and proc.popen.returncode == 0
    assert unit not in m.processes.units
    assert m.terminal("a")["state"] == "idle"
    assert not m.marker_path.exists()  # released: ours, unit gone, arm quiet


def test_c_listens_where_the_page_looks_and_stops_leaving_the_arm_held(make):
    m = make()
    m.ready_for_a()
    start(m, "a")
    m.wait_for("a", "ready")
    start(m, "b")
    m.wait_for("b", "ready")
    m.act.preflight_c({"request_id": "pre-c-00000001"})
    assert m.doc()["checks"]["c_preflight"]["ok"] is True
    start(m, "c")
    t = m.wait_for("c", "ready")
    assert [r["key"] for r in t["readiness"]["seen"]][-2:] == [
        "health_controller_active",
        "listening",
    ]
    assert net.listeners(standin_table(), PORT)
    procs = {k: m.processes.units[m.specs.units[k].unit] for k in "abc"}
    answer = m.act.stop("c", m.stop_body("c"))
    assert answer["note"] is None  # (said once C is stopped: the document's stop_note)
    assert m.terminal("c")["stop_note"] == "c.stop_arm_still_held"
    assert not net.listeners(standin_table(), PORT)
    health = m.sup.view()["health"]
    assert health["controller_active"] is True  # C's exit does not stop the controller
    assert m.terminal("a")["can_stop"] is True
    m.act.stop("a", m.stop_body("a", "stop-a-000001"))
    assert m.terminal("a")["state"] == "idle"
    m.act.stop("b", m.stop_body("b", "stop-b-000001"))
    assert m.terminal("b")["state"] == "idle"
    # Each was told once, with the unit's own stop signal, and left on its own.
    assert {k: (p.signals, p.popen.returncode) for k, p in procs.items()} == {
        k: (["SIGINT"], 0) for k in "abc"
    }


def test_a_slow_start_is_starting_with_lines_missing_until_it_is_ready(make):
    m = make(a="slow")
    m.ready_for_a()
    start(m, "a")
    t = m.wait_for("a", "starting")
    assert t["readiness"]["missing"] and t["readiness"]["waited_s"] >= 0
    m.wait_for("a", "ready", timeout=10)


# --- what goes wrong -----------------------------------------------------------------------


def test_a_unit_that_is_never_ready_is_failed_after_its_timeout_and_can_be_stopped(
    make,
):
    m = make(a="never-ready", ready_timeout_s=1.0)
    m.ready_for_a()
    start(m, "a")
    t = m.wait_for("a", "failed", timeout=8)
    assert t["failure"] == "ready_timeout"
    assert t["can_start"] is False and t["can_stop"] is True
    assert m.processes.units[
        m.specs.units["a"].unit
    ].alive()  # the page did not kill it
    m.act.stop("a", m.stop_body("a"))
    assert m.terminal("a")["state"] == "idle"
    # The record is closed; another start needs a new marker (the page's own
    # was removed once the arm was quiet) and a new FCI check.
    blockers = m.blockers("a")
    assert "record_open" not in blockers and "gate.marker_set" in blockers


@pytest.mark.parametrize(
    "mode, code", [("crash-after-ready", "failed"), ("crash-clean", "dead")]
)
def test_a_crash_after_ready_is_failed_or_dead_and_stops_by_acknowledging(
    make, mode, code
):
    m = make(a=mode)
    m.ready_for_a()
    start(m, "a")
    m.wait_for("a", code, timeout=10)
    assert "record_open" in m.blockers("a")
    answer = m.act.stop("a", m.stop_body("a"))
    assert answer["done"] == "acknowledged"
    assert m.terminal("a")["state"] == "idle"


def test_a_unit_that_ignores_sigint_stays_failed_and_is_never_killed_by_the_page(make):
    m = make(a="ignore-sigint", stop_timeout_s=1)
    m.ready_for_a()
    start(m, "a")
    m.wait_for("a", "ready")
    m.act.stop("a", m.stop_body("a"))
    op = m.doc()["operation"]
    assert (op["state"], op["code"]) == ("failed", "stop_failed")
    proc = m.processes.units[m.specs.units["a"].unit]
    assert proc.alive() and proc.signals == ["SIGINT"]
    t = m.terminal("a")
    assert t["state"] == "failed" and t["unit_state"] == "failed"
    # (the test, not the page, ends it)
    os.killpg(proc.popen.pid, signal.SIGKILL)


def test_a_stop_pressed_again_on_a_unit_that_ignores_sigint_sends_sigint_not_a_kill(
    make,
):
    m = make(a="ignore-sigint", stop_timeout_s=1)
    m.ready_for_a()
    start(m, "a")
    m.wait_for("a", "ready")
    m.act.stop("a", m.stop_body("a"))
    proc = m.processes.units[m.specs.units["a"].unit]
    try:
        with pytest.raises(stack.Refuse) as caught:
            m.act.stop("a", m.stop_body("a", "stop-a-again1"))
        assert caught.value.code == "stop_left_processes"
        # a second Ctrl+C, once; the program ignores it; nobody killed it
        assert proc.alive() and proc.signals == ["SIGINT", "SIGINT"]
        t = m.terminal("a")
        assert t["state"] == "failed" and t["owned"] and t["can_stop"]
        assert not [c for c in m.processes.commands if "reset-failed" in c]
    finally:
        os.killpg(proc.popen.pid, signal.SIGKILL)  # (the test, not the page)


def test_a_line_printed_twice_is_a_second_process_not_a_second_ready(make):
    m = make(a="duplicate-text")
    m.ready_for_a()
    start(m, "a")
    t = m.wait_for("a", "failed")
    assert t["failure"] == "duplicate_text"


def test_a_unit_without_the_realtime_line_is_never_ready(make):
    m = make(a="no-rt-line", ready_timeout_s=1.0)
    m.ready_for_a()
    start(m, "a")
    t = m.wait_for("a", "failed", timeout=8)
    assert t["failure"] == "ready_timeout"
    assert [r["key"] for r in t["readiness"]["missing"]] == ["fifo"]


def test_a_failed_realtime_request_is_named(make):
    m = make(a="rt-failed")
    m.ready_for_a()
    start(m, "a")
    t = m.wait_for("a", "failed")
    assert t["failure"] == "fifo_failed"


def test_a_half_started_gripper_stack_is_not_ready_and_blocks_the_gripper_test(make):
    m = make(b="half-started", ready_timeout_s=1.5)
    m.ready_for_a()
    start(m, "a")
    m.wait_for("a", "ready")
    start(m, "b")
    t = m.wait_for("b", "failed", timeout=8)
    assert t["failure"] == "ready_timeout"
    assert [r["key"] for r in t["readiness"]["seen"]] == [
        "driver",
        "controller_joint_state_broadcaster_robotiq",
    ]
    assert "gripper_b_not_ready" in [
        b["code"] for b in m.doc()["gripper_test"]["blockers"]
    ]


def test_c_that_runs_without_the_impedance_controller_is_failed(make):
    m = make(c="deferred")
    m.ready_for_a()
    start(m, "a")
    m.wait_for("a", "ready")
    start(m, "b")
    m.wait_for("b", "ready")
    m.act.preflight_c({"request_id": "pre-c-00000001"})
    start(m, "c")
    t = m.wait_for("c", "failed")
    assert t["failure"] == "activation_deferred"
    assert "stop_c_first" in [b["code"] for b in m.terminal("a")["stop_blockers"]]


def test_c_on_a_port_that_is_taken_names_it(make):
    import socket

    taken = socket.socket()
    taken.bind(("127.0.0.1", PORT))
    taken.listen(1)
    try:
        m = make()
        m.ready_for_a()
        start(m, "a")
        m.wait_for("a", "ready")
        start(m, "b")
        m.wait_for("b", "ready")
        m.act.preflight_c({"request_id": "pre-c-00000001"})
        # The port is taken: the gate says so before anything starts.
        assert "port_taken" in m.blockers("c")
        with pytest.raises(stack.Refuse):
            start(m, "c")
    finally:
        taken.close()


# --- the one-shot checks -------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode, ok, code",
    [
        ("ok", True, "reached"),
        ("stalled", False, "stalled"),
        # (waits for the server, then ends by itself with status 1 and no
        # result: not 0 and not timeout's 124, so nothing says it stopped)
        ("silent", False, "timeout_killed"),
    ],
)
def test_the_gripper_test_runs_a_real_command_and_reads_what_it_printed(
    make, mode, ok, code
):
    m = make(gripper=mode)
    m.ready_for_a()
    start(m, "a")
    m.wait_for("a", "ready")
    start(m, "b")
    m.wait_for("b", "ready")
    m.act.gripper(
        {
            **m.body("gripper_close", "grip-close-0001"),
            "action": "close",
            "attest": {"hands_clear": True},
        }
    )
    last = m.doc()["gripper_test"]["last"]
    assert (last["ok"], last["code"]) == (ok, code)


def probe_limit(monkeypatch, seconds=3):
    """The backstop behind the command's own bound, short for a test."""
    monkeypatch.setattr(stack_units, "PROBE_RUNTIME_MAX_S", seconds)


def test_the_probe_of_the_gripper_stack_reads_a_real_command(make, monkeypatch):
    """No gripper stack: the graph lists the monitor's node and none of the
    gripper's. One started by hand (here: the stand-in's marker file): its nodes
    are listed. (The slow question, the controller manager, is the next tests'.)"""
    m = make()
    m.no_manager()
    m.preflight_a()
    assert m.terminal("b")["state"] == "idle"
    m2 = make()
    m2.no_manager()
    (m2.tmp / "b.alive").write_text("")
    m2.preflight_a()
    assert m2.terminal("b")["state"] == "foreign_running"
    assert m2.processes.oneshot_timeouts == []


def test_a_probe_that_hangs_is_ended_by_the_backstop_and_its_warning_is_the_answer(
    make, monkeypatch
):
    probe_limit(monkeypatch)
    # (the graph proves nothing, so the controller manager is asked: nothing but
    # RuntimeMaxSec ends that command)
    m = make(controllers="hang", nodes="blind")
    m.no_manager()
    started = time.monotonic()
    m.preflight_a()
    assert time.monotonic() - started < 15
    assert len(m.processes.oneshot_timeouts) == 1
    t = m.terminal("b")
    assert (t["state"], t["reason"]) == ("idle", "b.not_running")


def test_a_probe_that_is_ended_before_it_can_say_is_no_answer_not_absent(
    make, monkeypatch
):
    probe_limit(monkeypatch)
    m = make(controllers="silent", nodes="blind")  # ended before the 10 s warning
    m.no_manager()
    m.preflight_a()
    t = m.terminal("b")
    assert (t["state"], t["reason"]) == ("unknown", "b.probe_no_answer")
    assert "gate.layers_known" in m.blockers("a")


def test_a_manager_with_no_controller_loaded_is_a_gripper_stack_that_exists(
    make, monkeypatch
):
    probe_limit(monkeypatch)
    m = make(controllers="none")
    m.no_manager()
    (m.tmp / "b.alive").write_text("")
    m.preflight_a()
    assert m.terminal("b")["state"] == "foreign_running"


# --- the commands' bounds, with the real `timeout` ---------------------------------------

SIGNAL_CHILD = (
    "import signal, sys, time\n"
    "def h(sig, frm):\n"
    "    print('Goal canceled.', flush=True); sys.exit(0)\n"
    "if sys.argv[1] == 'cancel': signal.signal(signal.SIGINT, h)\n"
    "else: signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
    "time.sleep(30)\n"
)


def run_bounded(mode):
    done = subprocess.run(
        [
            "timeout",
            "-s",
            "INT",
            "-k",
            "1",
            "1",
            sys.executable,
            "-c",
            SIGNAL_CHILD,
            mode,
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    return done.returncode, done.stdout


def test_timeout_with_sigint_lets_the_program_cancel_and_says_124():
    # The behaviour the gripper judge relies on, with coreutils' own `timeout`:
    # a program that handles SIGINT (send_goal cancels its goal) is asked to
    # stop, and the status is still 124.
    code, text = run_bounded("cancel")
    assert (code, text.strip()) == (124, "Goal canceled.")


def test_timeout_kills_a_program_that_ignores_sigint_and_says_137():
    code, text = run_bounded("ignore")
    # (`timeout` kills itself with the same signal: a shell sees 137, python -9.
    # The status the page gets is that of `systemd-run --wait`, 255 by the
    # review's reading of systemd, which the rehearsal measures: the judge takes
    # every status but 0 and 124, without a result, as "may still be moving".)
    assert code in (137, -9) and text == ""
    from levi.marvel0 import gripper

    for status in (code, 255):
        assert gripper.judge(status, text)["code"] == "timeout_killed", status


def test_the_gripper_test_that_hangs_is_ended_by_the_backstop_and_says_so(make):
    m = make(gripper="hang")
    m.ready_for_a()
    start(m, "a")
    m.wait_for("a", "ready")
    start(m, "b")
    m.wait_for("b", "ready")
    from levi.marvel0 import gripper

    gripper_limit = gripper.RUNTIME_MAX_S
    try:
        gripper.RUNTIME_MAX_S = 2  # (the backstop, short for a test)
        m.act.gripper(
            {
                **m.body("gripper_open", "grip-open-hang1"),
                "action": "open",
                "attest": {"hands_clear": True},
            }
        )
    finally:
        gripper.RUNTIME_MAX_S = gripper_limit
    last = m.doc()["gripper_test"]["last"]
    # nothing was printed and the unit was ended by the backstop (the stand-in
    # says 1 for it, the real systemd-run something else; the page must not
    # depend on which): not a cancel it saw, so the gripper may still be moving
    assert (last["ok"], last["code"]) == (False, "timeout_killed")
    assert len(m.processes.oneshot_timeouts) == 1


# --- behind the wrapper ----------------------------------------------------------------------


def test_behind_the_wrapper_the_unit_gets_the_rigs_environment_and_quiet_logs(make):
    m = make(via_wrapper=True)
    m.ready_for_a()
    start(m, "a")
    m.wait_for("a", "ready")
    text = m.log_text("a")
    assert "FAKE_ROS_SOURCED=1 RCUTILS_COLORIZED_OUTPUT=0" in text
    assert "[fr3_env]" not in text  # the environment file's own echo is dropped
    proc = m.processes.units[m.specs.units["a"].unit]
    m.act.stop("a", m.stop_body("a"))
    assert proc.signals == ["SIGINT"] and proc.popen.returncode == 0


def run_wrapper(*args, env_file=None):
    argv = ["/bin/bash", str(stack_units.WRAPPER), *args]
    return subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)


def test_the_wrapper_sources_then_execs_with_the_arguments_untouched():
    done = run_wrapper(
        str(FAKE_ENV),
        "--",
        sys.executable,
        "-c",
        "import os,sys;print(os.environ['FAKE_ROS_SOURCED'],os.environ['RCUTILS_COLORIZED_OUTPUT'],"
        "os.environ['PYTHONUNBUFFERED'],os.environ['FAKE_ENV_ARGC'],sys.argv[1:]);raise SystemExit(7)",
        "a b",
        "$HOME",
        "--flag=1",
    )
    assert done.returncode == 7  # the command's own code
    # (the environment file saw no positional parameters; the command saw all three)
    assert done.stdout.strip() == "1 0 1 0 ['a b', '$HOME', '--flag=1']"
    assert "[fr3_env]" not in done.stdout


def test_the_wrapper_replaces_itself_so_the_unit_s_main_process_is_the_command():
    done = run_wrapper(str(FAKE_ENV), "--", "sh", "-c", "echo $$")
    assert done.returncode == 0 and int(done.stdout.strip()) > 1
    proc = subprocess.Popen(
        [
            "/bin/bash",
            str(stack_units.WRAPPER),
            str(FAKE_ENV),
            "--",
            "sh",
            "-c",
            "echo $$; sleep 5",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert int(proc.stdout.readline()) == proc.pid
    finally:
        proc.kill()
        proc.wait()


def test_the_wrapper_does_not_run_the_command_without_its_environment(tmp_path):
    marker = tmp_path / "ran"
    command = [sys.executable, "-c", f"open({str(marker)!r}, 'w')"]
    for env in (tmp_path / "absent.sh", tmp_path):
        done = run_wrapper(str(env), "--", *command)
        assert done.returncode == 78 and not marker.exists()
    broken = tmp_path / "broken.sh"
    broken.write_text("false\n")
    assert run_wrapper(str(broken), "--", *command).returncode == 78
    assert not marker.exists()


@pytest.mark.parametrize("args", [(), ("a",), ("a", "b"), ("a", "b", "c"), ("a", "--")])
def test_the_wrapper_refuses_wrong_arguments(args):
    done = run_wrapper(*args)
    assert done.returncode == 64 and "usage" in done.stderr
