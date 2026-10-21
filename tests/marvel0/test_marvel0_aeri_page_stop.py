"""``scripts/aeri_page_stop.py``: the Ctrl+C of the hosted terminal D from outside
the page (for when the page is down). Real processes: the target is a Python
child that records the signals it gets. The script runs as ``python -I`` from a
folder with nothing of LEVI in it."""

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import scaled

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "aeri_page_stop.py"
NAME = "aeri-20261021-ext-r01"
TARGET = r"""
import os, signal, sys, time
log = sys.argv[1]
def note(signum, frame):
    open(log, "a").write(f"{signum}\n")
signal.signal(signal.SIGINT, note)
open(log + ".up", "w").write("up")
while True:
    time.sleep(0.05)
"""


def identity(pid):
    stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    return {"start_ticks": stat[19], "boot": boot}


@pytest.fixture
def world(tmp_path):
    procs = []

    def spawn(session=False, extra=(), program=None):
        log = tmp_path / f"signals{len(procs)}.log"
        proc = subprocess.Popen(
            [program or sys.executable, "-c", TARGET, str(log), *extra],
            start_new_session=session,
        )
        procs.append(proc)
        deadline = time.monotonic() + scaled(10)
        while not Path(str(log) + ".up").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        return proc, log

    class World:
        pass

    w = World()
    w.tmp = tmp_path
    w.spawn = spawn
    w.record_path = (
        tmp_path
        / "ws"
        / "outputs"
        / "LEVI"
        / "workbench"
        / "marvel0"
        / "aeri"
        / "record.json"
    )
    w.record_path.parent.mkdir(parents=True)
    w.run_dir = tmp_path / "rollout" / ".aeri" / "runs" / NAME
    w.run_dir.mkdir(parents=True)

    def write(leader=None, runner=None, **record):
        leader = leader or procs[0]
        body = {
            "name": NAME,
            "phase": "started",
            "leader": {"pid": leader.pid, "identity": identity(leader.pid)},
            "runner_json": str(w.run_dir / "runner.json"),
            **record,
        }
        w.record_path.write_text(json.dumps(body))
        if runner is not None:
            (w.run_dir / "runner.json").write_text(json.dumps(runner))

    w.write = write
    w.run = lambda *args: subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--record", str(w.record_path), *args],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=scaled(20),
        check=False,
    )

    def signals(log):
        time.sleep(0.3)
        return Path(log).read_text().split() if Path(log).exists() else []

    w.signals = signals
    yield w
    for proc in procs:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.wait(5)


def runner_of(proc, **over):
    return {"pid": proc.pid, "identity": identity(proc.pid), "state": "running", **over}


def test_one_sigint_goes_to_the_pid_in_the_heartbeat_and_the_record_remembers(world):
    proc, log = world.spawn()
    world.write(runner=runner_of(proc))
    done = world.run()
    assert done.returncode == 0, done.stderr
    assert f"SIGINT sent to terminal D (pid {proc.pid})" in done.stdout
    assert "does not go home" in done.stdout
    assert world.signals(log) == [str(int(signal.SIGINT))]
    rec = json.loads(world.record_path.read_text())
    assert (
        rec["stop_sent_epoch"] > 0 and rec["name"] == NAME and rec["phase"] == "started"
    )
    assert oct(os.stat(world.record_path).st_mode & 0o777) == "0o600"


def test_a_second_sigint_is_refused_unless_asked_for(world):
    proc, log = world.spawn()
    world.write(runner=runner_of(proc))
    assert world.run().returncode == 0
    again = world.run()
    assert again.returncode == 1 and "already sent" in again.stderr
    assert world.signals(log) == [str(int(signal.SIGINT))], "nothing more was sent"
    forced = world.run("--again")
    assert forced.returncode == 0
    assert world.signals(log) == [str(int(signal.SIGINT))] * 2


def test_a_dry_run_sends_nothing_and_changes_nothing(world):
    proc, log = world.spawn()
    world.write(runner=runner_of(proc))
    before = world.record_path.read_text()
    done = world.run("--dry-run")
    assert done.returncode == 0 and "would send SIGINT" in done.stdout
    assert world.signals(log) == [] and world.record_path.read_text() == before


def test_a_pid_that_is_not_the_same_process_is_never_signalled(world):
    proc, _log = world.spawn(session=True)
    bad = runner_of(proc)
    bad["identity"] = {**bad["identity"], "start_ticks": "1"}
    world.write(runner=bad, leader=proc)
    # the launcher's identity is the recorded one, so the group is the fallback
    done = world.run()
    assert done.returncode == 0 and "process group" in done.stdout
    # and when the launcher's identity does not fit either, nothing is sent
    rec = json.loads(world.record_path.read_text())
    rec.pop("stop_sent_epoch")
    rec["leader"]["identity"]["start_ticks"] = "1"
    world.record_path.write_text(json.dumps(rec))
    done = world.run()
    assert done.returncode == 1 and "not running" in done.stdout


def test_without_a_heartbeat_the_launchers_group_gets_it(world):
    proc, log = world.spawn(session=True)  # a session of its own: leader of its group
    world.write(leader=proc)  # (no runner.json at all)
    done = world.run()
    assert done.returncode == 0 and f"process group {proc.pid}" in done.stdout
    assert world.signals(log) == [str(int(signal.SIGINT))]


def test_a_process_that_is_gone_is_not_signalled(world):
    proc, _log = world.spawn()
    world.write(runner=runner_of(proc))
    proc.kill()
    proc.wait(5)
    done = world.run()
    assert done.returncode == 1 and "not running" in done.stdout


@pytest.mark.parametrize("phase", ["ended", "never", None])
def test_a_run_that_is_not_running_is_left_alone(world, phase):
    proc, log = world.spawn()
    world.write(runner=runner_of(proc), phase=phase)
    done = world.run()
    assert done.returncode == 1 and world.signals(log) == []


def test_a_record_that_is_not_ours_to_trust_is_refused(world):
    proc, log = world.spawn()
    world.record_path.write_text("{not json")
    assert world.run().returncode == 2
    world.record_path.write_text(json.dumps({"name": "../etc", "phase": "started"}))
    assert world.run().returncode == 2
    world.write(runner=runner_of(proc))
    link = world.tmp / "link.json"
    link.symlink_to(world.record_path)
    done = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--record", str(link)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert done.returncode == 2
    missing = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--record", str(world.tmp / "nope.json")],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert missing.returncode == 1
    assert world.signals(log) == []


def test_a_heartbeat_path_outside_the_run_folder_or_of_another_run_is_ignored(world):
    proc, _log = world.spawn(session=True)
    other = world.tmp / "elsewhere" / "runner.json"
    other.parent.mkdir()
    other.write_text(json.dumps(runner_of(proc)))
    world.write(leader=proc, runner_json=str(other))
    done = world.run()
    assert "process group" in done.stdout, "the heartbeat path was not trusted"
    rec = json.loads(world.record_path.read_text())
    rec.pop("stop_sent_epoch")
    rec["runner_json"] = str(
        world.run_dir.parent / "aeri-20261021-ext-r02" / "runner.json"
    )
    world.record_path.write_text(json.dumps(rec))
    assert "process group" in world.run().stdout


def test_the_script_is_stdlib_only_and_opens_no_socket(tmp_path):
    source = SCRIPT.read_text()
    imports = set(
        re.findall(
            r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)", source, re.MULTILINE
        )
    )
    assert imports <= {
        "argparse",
        "errno",
        "fcntl",
        "json",
        "os",
        "re",
        "signal",
        "stat",
        "sys",
        "time",
        "pathlib",
    }, imports
    assert "levi" not in imports
    assert "import socket" not in source and "socket." not in source
    assert (
        "SIGKILL" not in source and "SIGSTOP" not in source and "SIGTERM" not in source
    )
    assert "TIOCSTI" not in source and "pty" not in imports
    done = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=20,
        check=False,
    )
    assert done.returncode == 0 and "SIGINT" in done.stdout


def test_the_default_place_of_the_record_is_the_product_workspace(tmp_path):
    base = tmp_path / "ws"
    done = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--workspace", str(base)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert done.returncode == 1 and "has not hosted a run" in done.stderr
    from levi.marvel0 import aeri_run

    assert Path(*aeri_run.REL) == Path("marvel0/aeri")
    rel = Path("outputs/LEVI/workbench") / Path(*aeri_run.REL) / "record.json"
    from levi.workspace import Workspace

    assert Workspace.at(base).state / Path(*aeri_run.REL) / "record.json" == base / rel


# --- the record is written before the signal goes out ---------------------------------------


def patched_kill(script, monkeypatch, kill):
    """The script's own view of ``os`` with another ``kill`` (the real ``os`` is
    left alone: the fixture kills its processes with it)."""
    import types

    own = types.SimpleNamespace(**vars(os))
    own.kill = kill
    monkeypatch.setattr(script, "os", own)


def load_script():
    import importlib.util

    spec = importlib.util.spec_from_file_location("aeri_page_stop_order", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_record_says_sent_before_the_signal_is_sent(world, monkeypatch):
    """A page that looks at the record between the two must find that a Stop was
    sent (it then offers none of its own): the record is written first."""
    proc, _log = world.spawn()
    world.write(runner=runner_of(proc))
    script = load_script()
    seen = []

    def spy(pid, sig):
        seen.append(json.loads(world.record_path.read_text()).get("stop_sent_epoch"))

    patched_kill(script, monkeypatch, spy)
    assert script.main(["--record", str(world.record_path)]) == 0
    assert len(seen) == 1 and seen[0] is not None and seen[0] > 0


def test_a_signal_that_cannot_be_sent_puts_the_record_back(world, monkeypatch, capsys):
    proc, _log = world.spawn()
    world.write(runner=runner_of(proc), stop_sent_epoch=None)
    script = load_script()

    def refuse(pid, sig):
        raise PermissionError(1, "Operation not permitted")

    patched_kill(script, monkeypatch, refuse)
    assert script.main(["--record", str(world.record_path)]) == 2
    assert "could not be sent" in capsys.readouterr().err
    assert json.loads(world.record_path.read_text())["stop_sent_epoch"] is None
    # a Stop that had been sent before keeps its time when the second fails
    world.write(runner=runner_of(proc), stop_sent_epoch=1234.5)
    assert script.main(["--record", str(world.record_path), "--again"]) == 2
    assert json.loads(world.record_path.read_text())["stop_sent_epoch"] == 1234.5


def test_a_record_that_cannot_be_written_never_keeps_the_signal_from_going_out(
    world, monkeypatch, capsys
):
    proc, log = world.spawn()
    world.write(runner=runner_of(proc))
    script = load_script()

    def broken(path, when, **kw):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(script, "mark_sent", broken)
    assert script.main(["--record", str(world.record_path)]) == 0
    assert "will not know of this Stop" in capsys.readouterr().err
    assert world.signals(log) == [str(int(signal.SIGINT))]


# --- the check and the write are one step under the record's lock (N13) ------------------------


def test_a_stop_the_page_sent_after_the_script_read_the_record_is_not_doubled(
    world, monkeypatch, capsys
):
    """Safety review 2 N13: the script read ``stop_sent_epoch`` (unset), the page's
    Stop wrote it and signalled, then the script went on and signalled again. The
    check is now repeated inside the lock, where the write is."""
    proc, log = world.spawn()
    world.write(runner=runner_of(proc))
    script = load_script()
    stale = json.loads(world.record_path.read_text())
    reads = {"n": 0}
    real = script.read_json

    def read_json(path, limit=1 << 18):
        reads["n"] += 1
        if reads["n"] == 1:  # what main saw first, before the page's Stop
            return stale
        return real(path, limit)

    monkeypatch.setattr(script, "read_json", read_json)
    # ... and then the page wrote its Stop
    page = json.loads(world.record_path.read_text())
    page["stop_sent_epoch"] = 4321.0
    world.record_path.write_text(json.dumps(page))
    assert script.main(["--record", str(world.record_path)]) == 1
    assert "already sent" in capsys.readouterr().err
    assert world.signals(log) == [], "nothing was signalled"
    assert json.loads(world.record_path.read_text())["stop_sent_epoch"] == 4321.0


def test_again_is_still_allowed_after_a_stop_was_sent(world):
    proc, log = world.spawn()
    world.write(runner=runner_of(proc), stop_sent_epoch=1234.5)
    assert world.run().returncode == 1
    assert world.run("--again").returncode == 0
    assert world.signals(log) == [str(int(signal.SIGINT))]
    assert json.loads(world.record_path.read_text())["stop_sent_epoch"] > 1234.5


def test_a_record_lock_someone_holds_does_not_keep_the_signal_back(
    world, monkeypatch, capsys
):
    import fcntl

    proc, log = world.spawn()
    world.write(runner=runner_of(proc))
    script = load_script()
    monkeypatch.setattr(script, "LOCK_WAIT_S", 0.3)
    held = os.open(str(world.record_path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        started = time.monotonic()
        assert script.main(["--record", str(world.record_path)]) == 0
        assert time.monotonic() - started < 5
    finally:
        os.close(held)
    assert "will not know of this Stop" in capsys.readouterr().err
    assert world.signals(log) == [str(int(signal.SIGINT))]


# --- the record cannot be read: terminal D by its systemd scope (N8) ------------------------------

SCOPE = "levi-aeri-termd-marvel0-20261022T101500-a1b2c3.scope"


def fake_cgroup(world, pids, unit=SCOPE, parent="app.slice", uid=None, name="cgroup"):
    """A cgroup tree like the user manager's, with one scope listing ``pids``."""
    uid = os.getuid() if uid is None else uid
    root = world.tmp / name
    folder = root / "user.slice" / f"user-{uid}.slice" / f"user@{uid}.service"
    scope = folder / parent / unit
    scope.mkdir(parents=True, exist_ok=True)
    (scope / "cgroup.procs").write_text("".join(f"{pid}\n" for pid in pids))
    return root


def run_scope(world, root, *args):
    return world.run("--scope", "--cgroup-root", str(root), *args)


D_WORDS = ("scripts/aeri.py", "execute", "--config", "job.yaml")


def test_an_unreadable_record_is_refused_and_the_scope_route_is_named(world):
    _proc, log = world.spawn()
    world.record_path.write_text("{not json")
    done = world.run()
    assert done.returncode == 2 and "cannot be read" in done.stderr
    assert "--scope" in done.stderr, "the way on is named"
    assert world.signals(log) == []


def test_the_scope_route_signals_the_d_process_found_by_its_command_line(world):
    d, d_log = world.spawn(extra=D_WORDS)
    other, other_log = world.spawn(extra=("something", "else"))
    root = fake_cgroup(world, [other.pid, d.pid])
    world.record_path.write_text("{not json")
    done = run_scope(world, root)
    assert done.returncode == 0, done.stderr
    assert f"pid {d.pid}" in done.stdout and SCOPE in done.stdout
    assert "page will not know" in done.stdout
    assert world.signals(d_log) == [str(int(signal.SIGINT))]
    assert world.signals(other_log) == [], "nothing else in the scope was touched"
    assert world.record_path.read_text() == "{not json", "and the record is left alone"


def test_the_scope_route_does_not_signal_uv_which_has_the_same_words(world):
    """``uv run ... python ... aeri.py execute ...``: uv's command line holds the
    words of D's. Only the python process is terminal D."""
    d, d_log = world.spawn(extra=D_WORDS)
    bin_dir = world.tmp / "bin"
    bin_dir.mkdir()
    (bin_dir / "uv").symlink_to(os.path.realpath(sys.executable))
    uv, uv_log = world.spawn(extra=D_WORDS, program=str(bin_dir / "uv"))
    root = fake_cgroup(world, [uv.pid, d.pid])
    world.record_path.write_text("{not json")
    done = run_scope(world, root)
    assert done.returncode == 0, done.stderr
    assert f"pid {d.pid}" in done.stdout
    assert world.signals(d_log) == [str(int(signal.SIGINT))]
    assert world.signals(uv_log) == []


def test_the_scope_route_sends_to_the_group_when_d_is_not_there_yet(world):
    launcher, log = world.spawn(session=True, extra=("hostchild.py", "spec.json"))
    root = fake_cgroup(world, [launcher.pid])
    world.record_path.write_text("{not json")
    done = run_scope(world, root)
    assert done.returncode == 0, done.stderr
    assert "process group" in done.stdout
    assert world.signals(log) == [str(int(signal.SIGINT))]


def test_the_scope_route_does_not_send_to_a_launcher_that_is_not_a_group_leader(world):
    launcher, log = world.spawn(session=False, extra=("hostchild.py", "spec.json"))
    root = fake_cgroup(world, [launcher.pid])
    world.record_path.write_text("{not json")
    done = run_scope(world, root)
    assert done.returncode == 1 and world.signals(log) == []


def test_the_scope_route_refuses_when_two_processes_look_like_d(world):
    one, log1 = world.spawn(extra=D_WORDS)
    two, log2 = world.spawn(extra=D_WORDS)
    root = fake_cgroup(world, [one.pid, two.pid])
    world.record_path.write_text("{not json")
    done = run_scope(world, root)
    assert done.returncode == 2 and "more than one" in done.stderr
    assert world.signals(log1) == [] and world.signals(log2) == []


def test_the_scope_route_refuses_two_scopes_and_ignores_names_and_places_of_others(
    world,
):
    d, log = world.spawn(extra=D_WORDS)
    root = fake_cgroup(world, [d.pid])
    other = "levi-aeri-termd-marvel0-20261022T111500-d4e5f6.scope"
    fake_cgroup(world, [d.pid], unit=other)
    world.record_path.write_text("{not json")
    done = run_scope(world, root)
    assert done.returncode == 2 and "more than one scope" in done.stderr
    assert world.signals(log) == []
    # not our unit names, not our user's subtree
    wrong_name = fake_cgroup(
        world, [d.pid], unit="levi-aeri-termd-marvel0-x.scope", name="c2"
    )
    assert run_scope(world, wrong_name).returncode == 1
    wrong_user = fake_cgroup(world, [d.pid], uid=os.getuid() + 1, name="c3")
    assert run_scope(world, wrong_user).returncode == 1
    outside = world.tmp / "c4" / "other.slice" / SCOPE
    outside.mkdir(parents=True)
    (outside / "cgroup.procs").write_text(f"{d.pid}\n")
    assert run_scope(world, world.tmp / "c4").returncode == 1
    assert world.signals(log) == []


def test_the_scope_route_is_only_for_a_record_that_cannot_be_read(world):
    d, log = world.spawn(extra=D_WORDS)
    root = fake_cgroup(world, [d.pid])
    world.write(runner=runner_of(d))
    done = run_scope(world, root)
    assert done.returncode == 2 and "record can be read" in done.stderr
    assert world.signals(log) == []
    # no record at all is "the page has not hosted a run here", not a reason
    world.record_path.unlink()
    assert run_scope(world, root).returncode == 1
    assert world.signals(log) == []


def test_the_scope_route_dry_run_sends_nothing(world):
    d, log = world.spawn(extra=D_WORDS)
    root = fake_cgroup(world, [d.pid])
    world.record_path.write_text("{not json")
    done = run_scope(world, root, "--dry-run")
    assert (
        done.returncode == 0
        and f"would send SIGINT to terminal D (pid {d.pid})" in (done.stdout)
    )
    assert world.signals(log) == []


def test_the_scope_route_skips_a_pid_that_is_gone_or_not_a_number(world):
    d, log = world.spawn(extra=D_WORDS)
    root = fake_cgroup(world, [d.pid, 4_194_300, "x"])
    world.record_path.write_text("{not json")
    assert run_scope(world, root).returncode == 0
    assert world.signals(log) == [str(int(signal.SIGINT))]


def test_the_scope_route_runs_no_program_and_opens_no_socket():
    """It reads the cgroup files of the user's own scope and ``/proc``; it does
    not ask ``systemctl`` (a bus connection) and starts nothing."""
    source = SCRIPT.read_text()
    assert "import subprocess" not in source and "import socket" not in source
    for call in ("os.system", "os.popen", "os.spawn", "os.exec", "Popen("):
        assert call not in source, call
