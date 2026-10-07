"""`levi serve` restarts a core that crashed, never one that was stopped (levi/watch.py, agent/core.py:crashed)."""

import json
import os
import subprocess
import sys

from levi.agent import core
from levi.watch import CoreWatch, git_head, runtime_changed, web_exit_status


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def watch(crashed_values, **kw):
    clock = Clock()
    calls = {"restart": 0, "log": []}
    it = iter(crashed_values)
    w = CoreWatch(
        lambda: next(it, False),
        lambda: calls.__setitem__("restart", calls["restart"] + 1),
        calls["log"].append,
        clock=clock,
        **kw,
    )
    return w, clock, calls


def test_a_crashed_core_is_restarted_once_per_look():
    w, clock, calls = watch([True, False])
    assert w.tick() is True and calls["restart"] == 1
    assert "restarting" in calls["log"][0]
    clock.now += 6
    assert w.tick() is False and calls["restart"] == 1  # it is back: nothing to do


def test_looks_are_spaced_by_the_interval():
    w, clock, calls = watch([True, True, True])
    w.tick()
    clock.now += 1  # too soon: not even asked
    assert w.tick() is False and calls["restart"] == 1


def test_a_core_that_cannot_stay_up_is_given_up_on_within_the_window_then_retried_later():
    w, clock, calls = watch([True] * 20, limit=3, window=100.0)
    for _ in range(10):
        w.tick()
        clock.now += 6
    assert calls["restart"] == 3 and w.gave_up
    assert (
        sum("no more restarts" in line for line in calls["log"]) == 1
    )  # said once, not every look
    clock.now += 100  # the window has passed: it may try again
    assert w.tick() is True and calls["restart"] == 4


def test_a_failing_restart_is_logged_and_retried_within_the_cap():
    clock = Clock()
    log = []

    def boom():
        raise RuntimeError("Core failed to start")

    w = CoreWatch(lambda: True, boom, log.append, clock=clock, limit=2)
    assert w.tick() is False
    clock.now += 6
    assert w.tick() is False
    clock.now += 6
    w.tick()
    assert sum("did not restart" in line for line in log) == 2 and any(
        "no more restarts" in line for line in log
    )


def _core_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "directory", lambda: tmp_path)


def test_crashed_is_true_only_for_an_instance_file_with_a_dead_process(
    monkeypatch, tmp_path
):
    _core_dir(monkeypatch, tmp_path)
    assert core.crashed() is False  # no file: never started or stopped on purpose
    (tmp_path / "instance.json").write_text(json.dumps({"pid": os.getpid()}))
    assert core.crashed() is False  # alive
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    (tmp_path / "instance.json").write_text(json.dumps({"pid": dead.pid}))
    assert core.crashed() is True
    (tmp_path / "instance.json").write_text("not json")
    assert core.crashed() is False


def test_a_killed_child_that_is_still_a_zombie_counts_as_crashed(monkeypatch, tmp_path):
    """The real case: the core is a child of `levi serve`; after kill -9 it stays defunct until reaped."""
    import signal
    import time

    _core_dir(monkeypatch, tmp_path)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    (tmp_path / "instance.json").write_text(json.dumps({"pid": child.pid}))
    assert core.crashed() is False
    os.kill(child.pid, signal.SIGKILL)
    for _ in range(50):  # until the kernel has made it a zombie
        time.sleep(0.05)
        if core.crashed():
            break
    assert core.crashed() is True


def test_sigterm_removes_the_instance_file_then_dies_by_the_signal(
    monkeypatch, tmp_path
):
    """A stopped core (levi stop, the UI, kill) must not look like a crashed one."""
    import signal

    (tmp_path / "instance.json").write_text("{}")
    (tmp_path / "api.sock").write_text("")
    sent = []
    monkeypatch.setattr(
        signal,
        "signal",
        lambda signum, handler: sent.append(("signal", signum, handler)),
    )
    monkeypatch.setattr(
        os, "kill", lambda pid, signum: sent.append(("kill", pid, signum))
    )
    core.clean_exit_handler(tmp_path)(signal.SIGTERM, None)
    assert (
        not (tmp_path / "instance.json").exists()
        and not (tmp_path / "api.sock").exists()
    )
    assert sent == [
        ("signal", signal.SIGTERM, signal.SIG_DFL),
        ("kill", os.getpid(), signal.SIGTERM),
    ]


def test_a_real_core_stopped_with_sigterm_leaves_no_instance_file(tmp_path):
    """End to end on a throw-away child: the handler is what `serve()` installs before uvicorn runs."""
    import signal
    import textwrap
    import time

    script = textwrap.dedent(
        f"""
        import signal, time
        from pathlib import Path
        from levi.agent import core
        root = Path({str(tmp_path)!r})
        (root / "instance.json").write_text("{{}}")
        signal.signal(signal.SIGTERM, core.clean_exit_handler(root))
        print("ready", flush=True)
        time.sleep(60)
        """
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True
    )
    assert child.stdout.readline().strip() == "ready"
    child.send_signal(signal.SIGTERM)
    assert (
        child.wait(timeout=20) == -signal.SIGTERM
    )  # still reported as killed by the signal
    time.sleep(0.05)
    assert not (tmp_path / "instance.json").exists()


def test_a_veto_holds_the_restart_back_logs_once_per_reason_and_costs_no_restart():
    clock = Clock()
    log, restarts = [], []
    reason = ["job workers run"]
    w = CoreWatch(
        lambda: True,
        lambda: restarts.append(1),
        log.append,
        veto=lambda: reason[0],
        clock=clock,
        limit=2,
    )
    for _ in range(4):
        assert w.tick() is False
        clock.now += 6
    assert restarts == [] and len(log) == 1 and "job workers run" in log[0]
    reason[0] = None  # the person dealt with it: the cap is untouched, it restarts now
    assert w.tick() is True and restarts == [1]


def test_web_exit_status_is_zero_only_for_a_stop_on_purpose():
    import signal

    assert web_exit_status(0) == 0
    assert web_exit_status(-signal.SIGTERM) == 0
    assert web_exit_status(-signal.SIGINT) == 0
    assert web_exit_status(-signal.SIGHUP) == 0
    assert (
        web_exit_status(-signal.SIGKILL) == 1
    )  # killed (OOM): a supervisor must restart it
    assert web_exit_status(1) == 1 and web_exit_status(137) == 137


def test_runtime_changed_sees_only_runtime_paths_since_the_start_commit(tmp_path):
    def git(*args):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    (tmp_path / "levi").mkdir()
    (tmp_path / "levi/a.py").write_text("1")
    (tmp_path / "README.md").write_text("1")
    git("add", ".")
    git("commit", "-q", "-m", "one")
    start = git_head(tmp_path)
    assert start and runtime_changed(tmp_path, start) == []
    (tmp_path / "README.md").write_text("2")
    git("commit", "-q", "-am", "docs only")
    assert runtime_changed(tmp_path, start) == []
    (tmp_path / "levi/a.py").write_text("2")
    git("commit", "-q", "-am", "code")
    assert runtime_changed(tmp_path, start) == ["levi/a.py"]
    (tmp_path / "levi/a.py").write_text(
        "3"
    )  # not committed: work in progress does not count
    assert runtime_changed(tmp_path, start) == ["levi/a.py"]
    git("checkout", "--", "levi/a.py")
    now = git_head(tmp_path)
    (tmp_path / "levi/a.py").write_text("4")  # edited after the start, never committed
    assert runtime_changed(tmp_path, now) == []
    assert runtime_changed(tmp_path, None) == []
    assert git_head(tmp_path / "missing") is None


def test_orphaned_workers_are_running_workers_whose_owner_is_gone(monkeypatch):
    from levi import children

    rows = [
        {"running": True, "owner_running": False},
        {"running": True, "owner_running": True},
        {"running": False, "owner_running": False},
    ]
    monkeypatch.setattr(children, "listed", lambda: rows)
    assert core.orphaned_workers() == [rows[0]]


def test_a_stop_request_marker_keeps_a_dead_core_from_counting_as_crashed(
    monkeypatch, tmp_path
):
    _core_dir(monkeypatch, tmp_path)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    (tmp_path / "instance.json").write_text(json.dumps({"pid": dead.pid}))
    assert core.crashed() is True
    core.mark_stop(dead.pid + 1)  # a marker for another core does not count
    assert core.crashed() is True
    core.mark_stop(dead.pid)
    assert core.crashed() is False


def _free_port():
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _answers(port):
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/levi/health", timeout=2
        ):
            return True
    except OSError:
        return False


def _wait_for(check, seconds=60):
    import time

    end = time.time() + seconds
    while time.time() < end:
        if check():
            return True
        time.sleep(0.2)
    return False


def test_a_real_core_process_stopped_leaves_nothing_and_killed_counts_as_crashed(
    monkeypatch,
):
    """`python -m levi.agent.core` itself: SIGTERM is a stop, SIGKILL is a crash."""
    import shutil
    import signal
    import tempfile
    from pathlib import Path

    workspace = Path(
        tempfile.mkdtemp(prefix="lvcw", dir="/tmp")
    )  # short: a Unix socket path is limited to ~107 bytes
    try:
        env = {
            # no LEVI_* of the test run (other tests leave paths of their own there), only this workspace
            **{k: v for k, v in os.environ.items() if not k.startswith("LEVI_")},
            "LEVI_WORKSPACE": str(workspace),
            "LEVI_CORE_PORT": str(_free_port()),
            "LEVI_CPU_ONLY": "1",
        }
        monkeypatch.setenv("LEVI_WORKSPACE", str(workspace))
        monkeypatch.setattr(core, "directory", lambda: _instance_dir(workspace))

        def start():
            child = subprocess.Popen(
                [sys.executable, "-m", "levi.agent.core"],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=(workspace / "core.err").open("ab"),
            )
            up = _wait_for(
                lambda: (_instance_dir(workspace) / "instance.json").is_file(), 20
            )
            assert up, (workspace / "core.err").read_text()[-2000:]
            served = _wait_for(
                lambda: _answers(env["LEVI_CORE_PORT"]), 30
            )  # uvicorn is serving
            assert served, (workspace / "core.err").read_text()[-2000:]
            return child

        child = start()
        assert core.crashed() is False
        child.send_signal(signal.SIGTERM)
        assert child.wait(timeout=60) == -signal.SIGTERM
        assert not (_instance_dir(workspace) / "instance.json").exists()
        assert core.crashed() is False  # stopped on purpose

        child = start()
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=60)
        assert (_instance_dir(workspace) / "instance.json").exists()
        assert core.crashed() is True
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def _instance_dir(workspace):
    found = list(workspace.glob("**/agent/core"))
    return found[0] if found else workspace / "agent" / "core"
