# ruff: noqa: F811 - the fixtures imported from test_launch
"""``levi automatic plan | run --mode | runs | stop | resume | scene-answer |
attach`` (T-CL-09) in both reset modes: the command line is a thin shell over
the launch core and the command channel. Operator commands need typed
confirmation at a terminal and are refused without one (no bypass). The
digest the command line prints is the launch core's (``ENTRY_POINTS``: the
API of T-CL-11 adds itself to the same assertion)."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_aeri_cli import as_json, call
from test_launch import aeri_home, calls, fake_systemd, request  # noqa: F401
from test_runner import Served, committed, state_of, wait_until

from levi.automatic import cli, control, launch, modes

ROOT = Path(__file__).resolve().parents[2]


def mode_job(tmp_path, reset_mode, name="r-cmd", attested=False):
    """A job that waits for a person on its first scene check in each mode
    (``--scenes`` gives the decision): the reset policy is told to ask a
    person when the scene is unknown; the human-assisted mode asks anyway."""
    from test_launch import write_job

    path = write_job(tmp_path, name=name, strategy=reset_mode)
    text = path.read_text()
    if reset_mode == modes.SINGLE:
        text = text.replace("on_unknown: reset", "on_unknown: wait_human")
    elif attested:
        text = text.replace(
            "on_unknown: reset", "on_unknown: reset\n  scene_check: operator_attested"
        )
    path.write_text(text)
    return path


def waiting_scenes(reset_mode) -> str:
    """Two scene checks that send the run to a person."""
    one = "unknown" if reset_mode == modes.SINGLE else "reset_required"
    return f"{one},{one}"


# --- one digest for every entry ------------------------------------------------------------------


def cli_digest(job, mode, episodes, capsys):
    argv = ["plan", "--config", str(job)]
    if mode:
        argv += ["--mode", mode]
    if episodes is not None:
        argv += ["--episodes", str(episodes)]
    return as_json(capsys, *argv)[1]["plan_sha256"]


def core_digest(job, mode, episodes, capsys):
    overrides = {} if episodes is None else {"episodes": episodes}
    return launch.plan(
        launch.LaunchRequest(
            job_path=str(job), execution_mode=mode, overrides=overrides
        )
    ).plan_sha256


# T-CL-11 appends the API's ``POST /api/levi/automatic/plan`` here.
ENTRY_POINTS = [cli_digest, core_digest]


@pytest.mark.mode_matrix("cli:plan")
@pytest.mark.parametrize("mode", [None, "dry_run", "shadow", "autonomous"])
@pytest.mark.parametrize("episodes", [None, 5])
def test_every_entry_computes_the_same_digest(
    tmp_path, reset_mode, mode, episodes, capsys
):
    job = mode_job(tmp_path, reset_mode)
    found = {entry(job, mode, episodes, capsys) for entry in ENTRY_POINTS}
    assert len(found) == 1 and None not in found
    if mode is None and episodes is None:
        # The job's own mode, no override: validate's digest.
        assert found == {cli.load_job(job)["plan_sha256"]}


@pytest.mark.mode_matrix("cli:plan")
def test_plan_says_whether_it_launches(tmp_path, reset_mode, capsys):
    job = mode_job(tmp_path, reset_mode)
    code, found = as_json(capsys, "plan", "--config", str(job), "--mode", "dry_run")
    assert code == 0 and found["launchable"] and found["launch_token"]
    code, found = as_json(capsys, "plan", "--config", str(job), "--mode", "assisted")
    assert code == 2 and "E_NO_ROBOT_ADAPTER" in found["refusals"]
    human = reset_mode == modes.HUMAN
    assert ("E_SCENE_PROVIDER_MISSING" in found["refusals"]) == human
    code, text = call(capsys, "plan", "--config", str(job))
    assert code == 2 and "launchable: no" in text and "E_NO_ROBOT_ADAPTER" in text


# --- run --mode -------------------------------------------------------------------------------------


@pytest.mark.mode_matrix("cli:run")
@pytest.mark.parametrize("mode", ["shadow", "assisted", "autonomous"])
def test_run_refuses_every_real_mode(tmp_path, reset_mode, mode, capsys, fake_systemd):
    job = mode_job(tmp_path, reset_mode)
    for hosting in ("--detach", "--foreground"):
        code, found = as_json(
            capsys, "run", "--config", str(job), "--mode", mode, hosting
        )
        assert code == 2 and "E_NO_ROBOT_ADAPTER" in found["error"]
    assert calls(fake_systemd) == [] and not (tmp_path / "aeri-home" / "runs").exists()


def test_run_mode_needs_one_hosting_and_no_dry_run_flag(
    job_single, capsys, fake_systemd
):
    for argv in (
        ["--mode", "dry_run"],
        ["--mode", "dry_run", "--dry-run", "--detach"],
    ):
        code, _ = as_json(capsys, "run", "--config", str(job_single), *argv)
        assert code == 2
    assert calls(fake_systemd) == []


@pytest.fixture
def job_single(tmp_path):
    return mode_job(tmp_path, modes.SINGLE)


def test_run_refuses_a_plan_other_than_the_expected_one(
    job_single, capsys, fake_systemd
):
    code, found = as_json(
        capsys,
        "run",
        "--config",
        str(job_single),
        "--mode",
        "dry_run",
        "--detach",
        "--wait",
        "0",
        "--expect-plan",
        "0" * 64,
    )
    assert code == 2 and "E_PLAN_CHANGED" in found["error"]
    assert calls(fake_systemd) == []


@pytest.mark.mode_matrix("cli:run", "cli:runs")
def test_run_detach_starts_a_unit_and_runs_lists_it(
    tmp_path, reset_mode, capsys, fake_systemd
):
    job = mode_job(tmp_path, reset_mode)
    digest = as_json(capsys, "plan", "--config", str(job), "--mode", "dry_run")[1][
        "plan_sha256"
    ]
    code, found = as_json(
        capsys,
        "run",
        "--config",
        str(job),
        "--mode",
        "dry_run",
        "--detach",
        "--wait",
        "0",
        "--expect-plan",
        digest,
    )
    assert code == 0 and found["unit"] == "levi-aeri-r-cmd"
    (argv,) = calls(fake_systemd)
    assert argv[argv.index("--plan-sha256") + 1] == digest
    code, listed = as_json(capsys, "runs")
    assert [r["run_id"] for r in listed["runs"]] == ["r-cmd"]
    assert not listed["runs"][0]["runner_alive"]


@pytest.mark.mode_matrix("cli:run")
def test_run_foreground_runs_here(tmp_path, reset_mode, capsys):
    job = mode_job(tmp_path, reset_mode)
    code, found = as_json(
        capsys, "run", "--config", str(job), "--mode", "dry_run", "--foreground"
    )
    assert code == 0 and found["final_state"] == "COMPLETED"
    assert journal_state(found["run_dir"]) == "COMPLETED"


def journal_state(run_dir):
    return state_of(Path(run_dir))


# --- operator commands ----------------------------------------------------------------------------


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def terminal(monkeypatch):
    """A terminal whose operator types ``answers`` in order."""
    typed = []

    def answer(*lines):
        typed.extend(lines)

    def read():
        if not typed:
            raise EOFError
        return typed.pop(0)

    monkeypatch.setattr(sys, "stdin", Terminal())
    monkeypatch.setattr("builtins.input", read)
    return answer


def waiting(tmp_path, reset_mode, capsys, fake_systemd, attested=False):
    """A launched dry run (runner in a thread here) waiting for a person."""
    job = mode_job(tmp_path, reset_mode, attested=attested)
    code, found = as_json(
        capsys,
        "run",
        "--config",
        str(job),
        "--mode",
        "dry_run",
        "--detach",
        "--wait",
        "0",
        "--scenes",
        waiting_scenes(reset_mode),
    )
    assert code == 0
    run_dir = Path(found["run_dir"])
    served = Served(run_dir, found["plan_sha256"])
    wait_until(lambda: state_of(run_dir) == "WAIT_HUMAN")
    wait_until(
        lambda: launch.runner_alive(json.loads((run_dir / "runner.json").read_text()))
    )
    return run_dir, served


@pytest.mark.mode_matrix("cli:resume", "cli:stop")
def test_resume_and_stop_with_typed_confirmation(
    tmp_path, reset_mode, capsys, fake_systemd, terminal
):
    run_dir, served = waiting(tmp_path, reset_mode, capsys, fake_systemd)
    inbox = run_dir / "control" / "inbox"
    # A wrong confirmation writes nothing.
    terminal("yes", "yes", "resume r-other")
    code, found = as_json(capsys, "resume", "--run", "r-cmd")
    assert code == 2 and "not confirmed" in found["error"]
    assert not any(inbox.iterdir())
    terminal("yes", "no", "resume r-cmd")
    assert as_json(capsys, "resume", "--run", "r-cmd")[0] == 2
    assert not any(inbox.iterdir())
    # Both confirmations and the typed command: one resume.
    from levi.automatic.journal import Journal

    seq = str(len(Journal.read(run_dir).events))
    again = ["--command-id", "resume-a", "--principal", "op-2", "--expected-seq", seq]
    terminal("yes", "yes", "resume r-cmd")
    code, found = as_json(capsys, "resume", "--run", "r-cmd", *again)
    assert code == 0 and found["result"]["code"] == "resumed"
    # The same command again (a second click): the first result, no second
    # resume, although the run waits for a person again.
    wait_until(lambda: state_of(run_dir) == "WAIT_HUMAN")
    terminal("yes", "yes", "resume r-cmd")
    code, found = as_json(capsys, "resume", "--run-dir", str(run_dir), *again)
    assert found["queued"] == "already_queued" and found["result"]["code"] == "resumed"
    assert len([c for c in committed(run_dir) if c[3] == "resume-a"]) == 1
    # The same id for another command is refused.
    terminal("yes", "yes", "resume r-cmd")
    code, found = as_json(
        capsys, "resume", "--run", "r-cmd", "--command-id", "resume-a"
    )
    assert code == 2 and "command_used" in found["error"]
    # Stop.
    terminal("stop wrong")
    assert as_json(capsys, "stop", "--run", "r-cmd")[0] == 2
    terminal("stop r-cmd")
    code, found = as_json(capsys, "stop", "--run", "r-cmd")
    assert code == 0 and found["result"]["code"] == "completed"
    assert served.join().state == "COMPLETED"
    audit = [
        json.loads(x)
        for x in (tmp_path / "aeri-home" / "control.jsonl").read_text().splitlines()
    ]
    issued = [x for x in audit if x["phase"] == "issued"]
    assert [x["kind"] for x in issued] == ["resume", "resume", "stop"]
    assert issued[0]["principal_id"] == "op-2"


@pytest.mark.mode_matrix("cli:resume", "cli:stop", "cli:scene-answer")
def test_operator_commands_refuse_without_a_terminal(
    tmp_path, reset_mode, capsys, fake_systemd
):
    run_dir, served = waiting(tmp_path, reset_mode, capsys, fake_systemd)
    for argv in (
        ["stop", "--run", "r-cmd"],
        ["resume", "--run", "r-cmd"],
        [
            "scene-answer",
            "--run",
            "r-cmd",
            "--request-id",
            "q1",
            "--predicates",
            "a=true",
        ],
    ):
        code, found = as_json(capsys, *argv)
        assert code == 2 and "terminal" in found["error"]
    assert not any((run_dir / "control" / "inbox").iterdir())
    # The same through the real command, its standard input not a terminal.
    for argv in (["stop", "--run", "r-cmd"], ["resume", "--run", "r-cmd"]):
        done = subprocess.run(
            [sys.executable, "-m", "levi.automatic.cli", *argv],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join(
                    [str(ROOT), os.environ.get("PYTHONPATH", "")]
                ),
            },
            check=False,
        )
        assert done.returncode == 2 and "terminal" in done.stdout
    assert not any((run_dir / "control" / "inbox").iterdir())
    control.write_command(run_dir, control.command("stop", "stop-x", "op-1"))
    served.join()


@pytest.mark.mode_matrix("cli:scene-answer")
def test_scene_answer_reaches_only_a_person_check(
    tmp_path, reset_mode, capsys, fake_systemd, terminal
):
    human = reset_mode == modes.HUMAN
    run_dir, served = waiting(
        tmp_path, reset_mode, capsys, fake_systemd, attested=human
    )
    terminal("answer wrong")
    argv = [
        "scene-answer",
        "--run",
        "r-cmd",
        "--request-id",
        "r-cmd.forward.0001:scene9",
        "--predicates",
        "object_at_source=true,gripper_open=null",
    ]
    assert as_json(capsys, *argv)[0] == 2
    terminal("answer r-cmd.forward.0001:scene9")
    code, found = as_json(capsys, *argv)
    # The scripted person of a dry run already answered and closed its
    # question: a late answer is unsolicited. A machine provider asks nobody.
    assert code == 2
    assert found["result"]["code"] == ("unsolicited" if human else "not_supported")
    code, found = as_json(capsys, *argv[:-1], "object_at_source=maybe")
    assert code == 2 and "predicates" in found["error"]
    control.write_command(run_dir, control.command("stop", "stop-x", "op-1"))
    served.join()


@pytest.mark.mode_matrix("cli:stop", "cli:resume")
def test_commands_need_a_live_runner(
    tmp_path, reset_mode, capsys, fake_systemd, terminal
):
    job = mode_job(tmp_path, reset_mode)
    code, found = as_json(
        capsys,
        "run",
        "--config",
        str(job),
        "--mode",
        "dry_run",
        "--detach",
        "--wait",
        "0",
    )
    terminal("stop r-cmd")
    code, found = as_json(capsys, "stop", "--run", "r-cmd")
    assert code == 2 and "E_NO_RUNNER" in found["error"]
    code, found = as_json(capsys, "stop", "--run", "nope")
    assert code == 2 and "index" in found["error"]
    code, found = as_json(capsys, "resume", "--run-dir", str(tmp_path))
    assert code == 2 and "not a launched run" in found["error"]
    code, found = as_json(capsys, "stop", "--run", "r-cmd", "--principal", "Jane Doe")
    assert code == 2 and "opaque" in found["error"]


@pytest.mark.mode_matrix("cli:attach")
def test_attach_through_the_command_line(tmp_path, reset_mode, capsys, fake_systemd):
    job = mode_job(tmp_path, reset_mode)
    code, found = as_json(
        capsys,
        "run",
        "--config",
        str(job),
        "--mode",
        "dry_run",
        "--detach",
        "--wait",
        "0",
        "--scenes",
        waiting_scenes(reset_mode),
    )
    run_dir = Path(found["run_dir"])
    # The runner ran, then went away while the run waited for a person.
    from levi.automatic import runner

    gone = runner.serve(run_dir, found["plan_sha256"], deadline_s=0.2)
    assert gone.state == "WAIT_HUMAN"
    code, found = as_json(capsys, "attach", "--run", "r-cmd")
    assert code == 2  # --detach or --foreground
    code, found = as_json(capsys, "attach", "--run", "r-cmd", "--detach", "--wait", "0")
    assert code == 0 and found["unit"] == "levi-aeri-r-cmd"
    argv = calls(fake_systemd)[-1]
    assert argv[-1] == "--attach" and "--run-dir" in argv
    # A completed run is not attached.
    other = mode_job(tmp_path, reset_mode, name="r-done")
    code, found = as_json(
        capsys, "run", "--config", str(other), "--mode", "dry_run", "--foreground"
    )
    assert code == 0 and found["final_state"] == "COMPLETED"
    code, found = as_json(capsys, "attach", "--run", "r-done", "--foreground")
    assert code == 2 and "E_RUN_COMPLETED" in found["error"]
