"""``levi/marvel0/hostchild.py`` in AERI mode (``"mode": "aeri"``), with real
processes under real ptys. The client is a few lines of Python; nothing of the
robot is touched.

What the page depends on, and what these tests pin down:

* terminal D gets **three ttys**: fd 0 is the controlling terminal (keys and
  termios), fd 1 and 2 are the slave of an output pty of the launcher's own
  (window 50 x 200), because the console refuses to run unless stdin and
  stdout are terminals;
* the launcher **drains** that pty into ``out_log`` (private, appended, never
  through a link) so a core that stopped reading, hung or died never blocks the
  client's output (a stalled ``print`` would stall its heartbeat thread, and
  with it Ctrl+C and the safe shutdown), and a full disk drops bytes and goes on;
* a hang-up is **SIGINT first**, SIGKILL after ``hangup_grace_s``; a client that
  ends on SIGINT within the grace is not killed afterwards; the launcher's own
  dead log never keeps it from signalling;
* the exit record and the ``terminal D exited rc=`` event line are written.

The E mode (no ``mode`` key) is covered by ``test_marvel0_run_live.py`` and is
not changed here.
"""

import json
import os
import signal
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from conftest import scaled

from levi.marvel0 import hostchild, run

pytestmark = pytest.mark.skipif(
    not os.path.exists("/dev/ptmx"), reason="needs a pseudo-terminal"
)
HOSTCHILD = str(Path(run.__file__).with_name("hostchild.py"))

# What the core does: owns the input pty, starts the launcher in a session of
# its own, and (as a separate process, so a test can kill it) holds the master.
CORE = textwrap.dedent(
    """
    import os, subprocess, sys, time
    hostchild, spec, log = sys.argv[1:4]
    master, slave = os.openpty()
    out = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    child = subprocess.Popen(
        [sys.executable, "-I", hostchild, spec],
        stdin=slave, stdout=out, stderr=out, start_new_session=True, close_fds=True,
    )
    os.close(slave)
    print(child.pid, flush=True)
    time.sleep(600)
    """
)


def write_spec(tmp_path, code, **extra) -> Path:
    spec = {
        "mode": "aeri",
        "argv": [sys.executable, "-c", textwrap.dedent(code)],
        "cwd": str(tmp_path),
        "exit_file": str(tmp_path / "exit.json"),
        "out_log": str(tmp_path / "screen.log"),
        **extra,
    }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec))
    return path


class Launch:
    """The launcher with the test as the core: the input pty's master is ours."""

    def __init__(self, tmp_path, code, **extra):
        self.tmp = tmp_path
        self.spec = write_spec(tmp_path, code, **extra)
        self.master, slave = os.openpty()
        self.log = (tmp_path / "launcher.log").open("w")
        self.proc = subprocess.Popen(
            [sys.executable, "-I", HOSTCHILD, str(self.spec)],
            stdin=slave,
            stdout=self.log,
            stderr=self.log,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave)

    def close(self):
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.close(self.master)
        except OSError:
            pass
        self.log.close()

    def screen(self) -> str:
        path = self.tmp / "screen.log"
        return path.read_text(errors="replace") if path.exists() else ""

    def exit_record(self) -> dict:
        return json.loads((self.tmp / "exit.json").read_text())


@pytest.fixture
def launch(tmp_path):
    made = []

    def make(code, **extra):
        item = Launch(tmp_path, code, **extra)
        made.append(item)
        return item

    yield make
    for item in made:
        item.close()


def wait_for(predicate, timeout=10.0, step=0.02):
    deadline = time.monotonic() + scaled(timeout)
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def gone(pid, timeout=8.0) -> bool:
    return wait_for(lambda: run.proc_state(pid) in (None, "Z"), timeout)


# --- the three ttys ---------------------------------------------------------------------------


def test_the_client_has_the_keyboard_on_fd0_and_a_screen_of_its_own_on_fd1_and_fd2(
    launch, tmp_path
):
    item = launch(
        """
        import json, os, sys
        info = {
            "isatty": [os.isatty(i) for i in (0, 1, 2)],
            "names": [os.ttyname(i) for i in (0, 1, 2)],
            "size": list(os.get_terminal_size(1)),
            "ctty": os.tcgetpgrp(0) == os.getpgrp(),
        }
        with open("/dev/tty") as tty:  # the controlling terminal opens
            info["dev_tty"] = tty.isatty()
        open("info.json", "w").write(json.dumps(info))
        """
    )
    assert item.proc.wait(scaled(20)) == 0
    info = json.loads((tmp_path / "info.json").read_text())
    assert info["isatty"] == [True, True, True]
    keyboard, out, err = info["names"]
    assert out == err and out != keyboard
    assert info["dev_tty"] is True, "the session has a controlling terminal"
    assert info["ctty"] is True
    assert info["size"] == [200, 50], "columns, lines of the output pty"
    assert item.exit_record()["code"] == 0


def test_the_screen_goes_to_out_log_and_nothing_of_it_to_the_launchers_log(
    launch, tmp_path
):
    item = launch(
        """
        import sys
        print("on stdout")
        print("on stderr", file=sys.stderr)
        sys.stdout.write("no newline yet")
        """
    )
    assert item.proc.wait(scaled(20)) == 0
    text = item.screen()
    assert "on stdout" in text and "on stderr" in text and "no newline yet" in text
    launcher = (tmp_path / "launcher.log").read_text()
    assert "on stdout" not in launcher and "on stderr" not in launcher
    assert all(
        line.startswith("hostchild:") for line in launcher.splitlines() if line.strip()
    )


def test_the_keys_reach_the_client_through_the_input_pty(launch, tmp_path):
    item = launch(
        """
        import os, sys, termios, tty
        tty.setcbreak(0)
        print("ready", flush=True)
        got = os.read(0, 1)
        print("key", got, flush=True)
        """
    )
    assert wait_for(lambda: "ready" in item.screen())
    os.write(item.master, b"y")
    assert item.proc.wait(scaled(20)) == 0
    assert "key b'y'" in item.screen()


def test_an_input_call_prompts_on_the_output_pty(launch, tmp_path):
    """``input()`` writes its prompt to stderr: the output pty, so the page's
    screen reader sees ``Type execute <id>: `` (the unfinished line) there."""
    item = launch(
        """
        typed = input("Type execute r1: ")
        print("typed", repr(typed), flush=True)
        """
    )
    assert wait_for(lambda: "Type execute r1: " in item.screen())
    assert "typed" not in item.screen()
    os.write(item.master, b"execute r1\r")
    assert item.proc.wait(scaled(20)) == 0
    assert "typed 'execute r1'" in item.screen()


# --- the output never blocks the client -------------------------------------------------------


def test_a_client_that_prints_a_lot_is_not_blocked_by_a_core_that_never_reads(
    launch, tmp_path
):
    """Eight megabytes through a pty whose master nobody of the core holds: only
    the launcher's drain keeps the client moving."""
    item = launch(
        """
        import sys
        chunk = "x" * 1023 + "\\n"
        for _ in range(8 * 1024):
            sys.stdout.write(chunk)
        sys.stdout.flush()
        print("done", flush=True)
        """
    )
    assert item.proc.wait(scaled(60)) == 0
    text = (tmp_path / "screen.log").read_bytes()
    assert text.endswith(b"done\r\n")
    assert len(text) > 8 * 1024 * 1024


def test_a_full_disk_drops_the_bytes_and_never_stops_the_client(launch, tmp_path):
    item = launch(
        """
        import sys
        for _ in range(2048):
            sys.stdout.write("y" * 1023 + "\\n")
        print("done", flush=True)
        """,
        out_log="/dev/full",
    )
    assert item.proc.wait(scaled(60)) == 0
    assert item.exit_record()["code"] == 0


def test_the_out_log_is_private_appended_and_never_followed_through_a_link(
    launch, tmp_path
):
    log = tmp_path / "screen.log"
    log.write_text("earlier\n")
    os.chmod(log, 0o644)
    item = launch("print('later')")
    assert item.proc.wait(scaled(20)) == 0
    assert log.read_text().startswith("earlier\n") and "later" in log.read_text()
    # a log that is a link is not opened: the client does not run
    target = tmp_path / "elsewhere"
    target.write_text("keep")
    link = tmp_path / "linked.log"
    link.symlink_to(target)
    marker = tmp_path / "ran"
    second = Launch(
        tmp_path, f"open({str(marker)!r}, 'w').write('ran')", out_log=str(link)
    )
    try:
        assert second.proc.wait(scaled(20)) == run.EXIT_SPAWN
        assert not marker.exists()
        assert target.read_text() == "keep"
        assert second.exit_record()["code"] == run.EXIT_SPAWN
    finally:
        second.close()


def test_a_new_out_log_is_created_private(launch, tmp_path):
    item = launch("print('x')")
    assert item.proc.wait(scaled(20)) == 0
    assert stat.S_IMODE(os.stat(tmp_path / "screen.log").st_mode) == 0o600


def test_a_drain_thread_that_died_is_started_again(monkeypatch):
    calls = []

    class Output:
        def __init__(self):
            self.up = False

        def alive(self):
            return self.up

        def start(self):
            calls.append("start")
            self.up = True

    class Child:
        def __init__(self):
            self.n = 0

        def wait(self, timeout=None):
            self.n += 1
            if self.n < 3:
                raise subprocess.TimeoutExpired("c", timeout)
            return 7

    assert hostchild._wait_aeri(Child(), Output()) == 7
    assert calls == ["start"], "restarted once, then it lived"


# --- the exit ---------------------------------------------------------------------------------


def test_the_exit_is_recorded_and_the_event_line_written(launch, tmp_path):
    events = tmp_path / "terminal_d.events"
    item = launch("raise SystemExit(3)", events_file=str(events))
    assert item.proc.wait(scaled(20)) == 3
    record = item.exit_record()
    assert (record["code"], record["signal"]) == (3, None)
    assert stat.S_IMODE(os.stat(tmp_path / "exit.json").st_mode) == 0o600
    assert events.read_text().strip().endswith("terminal D exited rc=3")
    assert stat.S_IMODE(os.stat(events).st_mode) == 0o600


def test_a_client_that_dies_of_a_signal_is_recorded_as_such(launch, tmp_path):
    events = tmp_path / "terminal_d.events"
    item = launch(
        "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
        events_file=str(events),
    )
    assert item.proc.wait(scaled(20)) == 128 + signal.SIGTERM
    record = item.exit_record()
    assert (record["code"], record["signal"]) == (None, int(signal.SIGTERM))
    assert events.read_text().strip().endswith(f"rc={128 + signal.SIGTERM}")


def test_a_client_that_cannot_start_is_reported_and_its_output_closed(tmp_path):
    master, slave = os.openpty()
    spec = write_spec(tmp_path, "pass")
    data = json.loads(spec.read_text())
    data["argv"] = ["/nonexistent/client"]
    spec.write_text(json.dumps(data))
    proc = subprocess.Popen(
        [sys.executable, "-I", HOSTCHILD, str(spec)],
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    os.close(slave)
    try:
        assert proc.wait(scaled(20)) == run.EXIT_SPAWN
        assert (
            json.loads((tmp_path / "exit.json").read_text())["code"] == run.EXIT_SPAWN
        )
    finally:
        os.close(master)


@pytest.mark.parametrize(
    "change",
    [
        {"mode": "other"},
        {"out_log": None},
        {"out_log": ""},
        {"hangup_grace_s": 0},
        {"hangup_grace_s": 31},
        {"hangup_grace_s": True},
        {"events_file": 3},
    ],
)
def test_a_spec_that_is_not_what_the_core_writes_is_refused(tmp_path, change):
    spec = write_spec(tmp_path, "open('ran', 'w').write('x')")
    data = json.loads(spec.read_text())
    data.update(change)
    spec.write_text(json.dumps(data))
    master, slave = os.openpty()
    try:
        done = subprocess.run(
            [sys.executable, "-I", HOSTCHILD, str(spec)],
            stdin=slave,
            capture_output=True,
            text=True,
            timeout=scaled(20),
            check=False,
            start_new_session=True,
        )
    finally:
        os.close(master)
        os.close(slave)
    assert done.returncode == run.EXIT_SPAWN
    assert not (tmp_path / "ran").exists()


def test_without_a_controlling_terminal_nothing_runs(tmp_path):
    spec = write_spec(tmp_path, "open('ran', 'w').write('x')")
    done = subprocess.run(
        [sys.executable, "-I", HOSTCHILD, str(spec)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=scaled(20),
        check=False,
    )
    assert done.returncode == run.EXIT_NO_TTY
    assert not (tmp_path / "ran").exists()
    assert not (tmp_path / "screen.log").exists(), "no output pty was made either"


def test_the_launcher_ignores_the_signals_meant_for_the_client(launch, tmp_path):
    item = launch("import time; time.sleep(30)")
    assert wait_for(lambda: item.proc.poll() is None)
    time.sleep(0.3)
    for sig in (signal.SIGINT, signal.SIGTERM):
        os.kill(item.proc.pid, sig)
    time.sleep(0.3)
    assert item.proc.poll() is None


# --- the hang-up: SIGINT, the grace, SIGKILL --------------------------------------------------

# A client that records every signal it receives (with the time) and what it
# does about SIGINT: ``ends`` (wraps up and exits 0 after ``wrap`` seconds) or
# ``ignores`` (the terminal D of a hung shutdown).
WATCHER = textwrap.dedent(
    """
    import os, signal, sys, time
    mode, wrap = sys.argv[1], float(sys.argv[2])
    log = open("signals.log", "a", buffering=1)
    def note(name):
        log.write(f"{name} {time.monotonic():.3f}\\n")
    def on_int(signum, frame):
        note("INT")
        if mode == "ends":
            time.sleep(wrap)
            note("WRAPPED")
            os._exit(0)
    def on_hup(signum, frame):
        note("HUP")
    signal.signal(signal.SIGINT, on_int)
    signal.signal(signal.SIGHUP, on_hup)
    signal.signal(signal.SIGTERM, lambda *a: note("TERM"))
    open("up", "w").write("up")
    while True:
        time.sleep(0.05)
    """
)


def launch_with_a_core(tmp_path, mode, wrap=0.0, grace=0.6, **extra):
    """The launcher under a pty whose master a separate core process holds."""
    spec = tmp_path / "core-spec.json"
    spec.write_text(
        json.dumps(
            {
                "mode": "aeri",
                "argv": [sys.executable, "-c", WATCHER, mode, str(wrap)],
                "cwd": str(tmp_path),
                "exit_file": str(tmp_path / "exit.json"),
                "out_log": str(tmp_path / "screen.log"),
                "events_file": str(tmp_path / "terminal_d.events"),
                "hangup_grace_s": grace,
                **extra,
            }
        )
    )
    core = subprocess.Popen(
        [sys.executable, "-c", CORE, HOSTCHILD, str(spec), str(tmp_path / "core.log")],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    launcher = int(core.stdout.readline())
    assert wait_for(lambda: (tmp_path / "up").exists(), 15)
    return core, launcher


def signals(tmp_path) -> list:
    path = tmp_path / "signals.log"
    if not path.exists():
        return []
    return [line.split() for line in path.read_text().splitlines() if line.strip()]


def kill_all(core, launcher, tmp_path):
    core.kill()
    core.wait()
    try:
        os.killpg(launcher, signal.SIGKILL)
    except OSError:
        pass


def test_a_hangup_is_sigint_first_and_sigkill_only_after_the_grace(tmp_path):
    core, launcher = launch_with_a_core(tmp_path, "ignores", grace=0.6)
    try:
        t0 = time.monotonic()
        core.kill()  # the master closes: the kernel hangs the terminal up
        core.wait()
        assert gone(launcher, 8), "the launcher is killed with its group"
        elapsed = time.monotonic() - t0
        seen = signals(tmp_path)
        names = [name for name, _ in seen]
        assert names and names[0] == "INT", names
        assert "HUP" not in names, "the kernel hangs up the session leader only"
        assert elapsed >= 0.5, "the grace was waited for"
    finally:
        kill_all(core, launcher, tmp_path)


def test_a_client_that_wraps_up_within_the_grace_is_not_killed(tmp_path):
    core, launcher = launch_with_a_core(tmp_path, "ends", wrap=0.4, grace=3.0)
    try:
        core.kill()
        core.wait()
        assert gone(launcher, 10)
        names = [name for name, _ in signals(tmp_path)]
        assert names[:2] == ["INT", "WRAPPED"], names
        record = json.loads((tmp_path / "exit.json").read_text())
        assert record["code"] == 0, "the exit was recorded: the launcher was not killed"
        assert (tmp_path / "terminal_d.events").read_text().strip().endswith("rc=0")
    finally:
        kill_all(core, launcher, tmp_path)


def test_a_client_that_wraps_up_too_slowly_is_killed_at_the_end_of_the_grace(tmp_path):
    core, launcher = launch_with_a_core(tmp_path, "ends", wrap=30.0, grace=0.6)
    try:
        core.kill()
        core.wait()
        assert gone(launcher, 10)
        names = [name for name, _ in signals(tmp_path)]
        assert names == ["INT"], "it began to wrap up and was cut off"
        assert not (tmp_path / "exit.json").exists(), (
            "a killed launcher records nothing"
        )
    finally:
        kill_all(core, launcher, tmp_path)


def test_a_dead_log_of_the_launcher_never_keeps_it_from_signalling(tmp_path):
    """The launcher's own stderr was the terminal, now hung up: writing to it
    fails (EIO). The hang-up handler must still send SIGINT and, later, the
    kill."""
    log_master, log_slave = os.openpty()
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps(
            {
                "mode": "aeri",
                "argv": [sys.executable, "-c", WATCHER, "ignores", "0"],
                "cwd": str(tmp_path),
                "exit_file": str(tmp_path / "exit.json"),
                "out_log": str(tmp_path / "screen.log"),
                "hangup_grace_s": 0.6,
            }
        )
    )
    key_master, key_slave = os.openpty()
    proc = subprocess.Popen(
        [sys.executable, "-I", HOSTCHILD, str(spec)],
        stdin=key_slave,
        stdout=log_slave,
        stderr=log_slave,
        start_new_session=True,
        close_fds=True,
    )
    os.close(key_slave)
    os.close(log_slave)
    try:
        assert wait_for(lambda: (tmp_path / "up").exists(), 15)
        os.close(log_master)  # the launcher's log is dead from now on
        os.close(key_master)  # the keyboard hangs up
        assert gone(proc.pid, 8)
        assert [name for name, _ in signals(tmp_path)][:1] == ["INT"]
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass


def test_say_never_raises(monkeypatch):
    class Dead:
        def write(self, text):
            raise OSError(5, "Input/output error")

        def flush(self):
            raise OSError(5, "Input/output error")

    monkeypatch.setattr(sys, "stderr", Dead())
    hostchild._say("anything")  # (a handler that raised would escape into main)


def test_the_hangup_handlers_are_installed_grace_timer_first(monkeypatch, tmp_path):
    """A hang-up that arrives at once must find SIGALRM's handler in place."""
    order = []
    real = signal.signal

    def spy(signum, handler):
        order.append(signum)
        return real(signum, handler)

    spec = write_spec(tmp_path, "pass")
    monkeypatch.setattr(signal, "signal", spy)
    monkeypatch.setattr(hostchild, "_controlling_tty", lambda: False)
    saved = {n: real(n, signal.SIG_DFL) for n in (signal.SIGINT, signal.SIGALRM)}
    try:
        assert hostchild.main(["hostchild.py", str(spec)]) == run.EXIT_NO_TTY
    finally:
        for n, handler in saved.items():
            real(n, handler)
        for n in (signal.SIGTERM, signal.SIGTSTP, signal.SIGQUIT, signal.SIGHUP):
            real(n, signal.SIG_DFL)
    assert order.index(signal.SIGALRM) < order.index(signal.SIGHUP)
