"""The command channel (T-CL-08, levi/automatic/control.py): commands are
written whole or not at all, never replaced, checked before use; the same
command twice is one command; a command id is never reused for another
command. Crash consistency: a writer killed mid-write (SIGKILL) leaves no
half command, and two processes writing the same resume at once queue it
once."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from levi.automatic import control, launch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


@pytest.fixture(autouse=True)
def aeri_home(tmp_path, monkeypatch):
    monkeypatch.setenv(launch.HOME_ENV, str(tmp_path / "aeri-home"))


@pytest.fixture
def run_dir(tmp_path):
    folder = tmp_path / "run"
    folder.mkdir()
    (folder / launch.LAUNCH_RECORD).write_text("{}")
    return folder


def resume(command_id="resume-1", seq=7, **over):
    return control.command(
        "resume",
        command_id,
        "op-1",
        expected_seq=seq,
        environment_handled=over.get("env", True),
        health_rechecked=over.get("health", True),
    )


def test_a_command_is_queued_once(run_dir):
    assert control.write_command(run_dir, resume()) == "queued"
    # The same command again (another moment, a second click): one file.
    assert control.write_command(run_dir, resume()) == "already_queued"
    inbox = run_dir / "control" / "inbox"
    assert [p.name for p in inbox.iterdir()] == ["resume-1.json"]
    for folder in ("control", "control/inbox", "control/results"):
        assert (run_dir / folder).stat().st_mode & 0o777 == 0o700
    assert (inbox / "resume-1.json").stat().st_mode & 0o777 == 0o600


def test_a_used_command_id_is_refused_for_another_command(run_dir):
    control.write_command(run_dir, resume())
    with pytest.raises(control.CommandError) as caught:
        control.write_command(run_dir, resume(seq=8))
    assert caught.value.code == "command_used"
    with pytest.raises(control.CommandError):
        control.write_command(run_dir, control.command("stop", "resume-1", "op-1"))
    assert (
        json.loads((run_dir / "control/inbox/resume-1.json").read_text())[
            "expected_seq"
        ]
        == 7
    )


def test_only_a_launched_run_takes_commands(tmp_path):
    with pytest.raises(control.CommandError) as caught:
        control.write_command(tmp_path, resume())
    assert caught.value.code == "E_NOT_LAUNCHED"
    assert not (tmp_path / "control").exists()


@pytest.mark.parametrize(
    "change",
    [
        {"command_id": "../escape"},
        {"command_id": "a/b"},
        {"command_id": ""},
        {"principal_id": "Jane Doe <jane@example.org>"},
        {"kind": "home"},
        {"schema": "levi.aeri.command.v2"},
        {"expected_seq": -1},
        {"expected_seq": True},
        {"environment_handled": "yes"},
        {"issued_wall_ns": 1.5},
        {"extra": 1},
    ],
)
def test_bad_commands_are_refused(change):
    value = {**resume(), **change}
    with pytest.raises(control.CommandError) as caught:
        control.validate(value)
    assert caught.value.code == "invalid"


@pytest.mark.parametrize(
    "predicates",
    [{}, {"a": 1}, {"a": "true"}, {"../x": True}, {f"p{i}": True for i in range(65)}],
)
def test_bad_scene_answers_are_refused(predicates):
    with pytest.raises(control.CommandError):
        control.command(
            "scene_answer", "a-1", "op", request_id="r1", predicates=predicates
        )


def test_strict_json(tmp_path):
    for text in ('{"a": 1, "a": 2}', '{"a": NaN}', "[1", '{"a": Infinity}'):
        with pytest.raises(control.CommandError):
            control._strict(text.encode())


def test_files_are_read_without_following_links(tmp_path):
    target = tmp_path / "secret.json"
    target.write_text('{"x": 1}')
    link = tmp_path / "c.json"
    link.symlink_to(target)
    with pytest.raises(OSError):
        control.read_file(link)
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (control.MAX_BYTES + 1))
    with pytest.raises(control.CommandError):
        control.read_file(big)


def test_a_symlinked_control_folder_is_refused(run_dir, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (run_dir / "control").symlink_to(elsewhere)
    with pytest.raises(control.CommandError):
        control.ensure_folders(run_dir)


def test_results_are_read_by_id_only(run_dir):
    with pytest.raises(control.CommandError):
        control.read_result(run_dir, "../x")
    assert control.read_result(run_dir, "resume-1") is None


# --- crash consistency ---------------------------------------------------------------------------

CHILD = """
import os, signal, sys
from levi.automatic import control
run_dir, point, command_id = sys.argv[1:4]
def crash(at):
    if at == point:
        os.kill(os.getpid(), signal.SIGKILL)
control._crash = crash
value = control.command(
    "resume", command_id, "op-1", expected_seq=7,
    environment_handled=True, health_rechecked=True,
)
print(control.write_command(run_dir, value), flush=True)
"""


def child_env():
    return {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(ROOT), os.environ.get("PYTHONPATH", "")]),
    }


@pytest.mark.parametrize("point", ["written", "linked"])
def test_a_writer_killed_mid_write_leaves_no_half_command(run_dir, point):
    done = subprocess.run(
        [sys.executable, "-c", CHILD, str(run_dir), point, "resume-1"],
        env=child_env(),
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == -9
    inbox = run_dir / "control" / "inbox"
    visible = sorted(p.name for p in inbox.iterdir() if not p.name.startswith("."))
    if point == "written":
        # Killed before the link: nothing a runner reads (a hidden
        # temporary file at most, which every reader skips).
        assert visible == []
        assert control.write_command(run_dir, resume()) == "queued"
    else:
        # Killed after the link: the whole command, readable as it was.
        assert visible == ["resume-1.json"]
        assert (
            control.validate(control._strict((inbox / "resume-1.json").read_bytes()))[
                "expected_seq"
            ]
            == 7
        )
        assert control.write_command(run_dir, resume()) == "already_queued"


WRITER = """
import sys, time
from levi.automatic import control
run_dir, start, seq = sys.argv[1], float(sys.argv[2]), int(sys.argv[3])
while time.time() < start:
    time.sleep(0.001)
value = control.command(
    "resume", "resume-twice", "op-1", expected_seq=seq,
    environment_handled=True, health_rechecked=True,
)
try:
    print(control.write_command(run_dir, value))
except control.CommandError as exc:
    print(exc.code)
"""


@pytest.mark.parametrize("same", [True, False])
def test_two_processes_write_one_command_id_at_once(run_dir, same):
    start = time.time() + 1.0
    writers = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                WRITER,
                str(run_dir),
                str(start),
                str(7 if same else 7 + n),
            ],
            env=child_env(),
            stdout=subprocess.PIPE,
            text=True,
        )
        for n in range(2)
    ]
    outs = sorted(p.communicate(timeout=60)[0].strip() for p in writers)
    if same:
        assert outs == ["already_queued", "queued"]
    else:
        assert outs == ["command_used", "queued"]
    files = [
        p for p in (run_dir / "control/inbox").iterdir() if not p.name.startswith(".")
    ]
    assert [p.name for p in files] == ["resume-twice.json"]
