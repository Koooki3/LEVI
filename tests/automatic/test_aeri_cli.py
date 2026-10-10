"""``levi automatic`` (T-C-16, levi/automatic/cli.py): doctor, validate,
run --dry-run, status, report. A dry run drives the in-process fakes only:
no robot, no port, no motion authority over anything real."""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from levi.automatic import cli

HERE = Path(__file__).resolve().parent
CONTRACT = """initial_state:
  id: stack-plates-initial
  version: "1"
  predicates:
    required: [object_at_source, gripper_open]
  observations:
    require_visible_evidence: true
    min_evidence_refs: 1
"""


def job_text(root, **over):
    values = {
        "name": "r-cli",
        "episodes": 2,
        "strategy": "single_reset_policy",
        "forward_folder": "stack__r-cli",
        "reset_folder": "reset_stack__r-cli",
        "extra": "",
    }
    values.update(over)
    return f"""schema_version: levi.aeri.job.v1
experiment:
  name: {values["name"]}
  episodes: {values["episodes"]}
  random_seed: 42
  execution_mode: shadow
policies:
  forward:
    max_steps: 30
  reset:
    max_steps: 20
task:
  instruction: stack the plates
  reset_instruction: "Reset: stack the plates"
  initial_state_spec: initial-state.yaml
termination:
  min_steps: 5
  settle_steps: 3
  cooldown_steps: 5
reset:
  strategy: {values["strategy"]}
  max_attempts: 1
  on_unknown: reset
recording:
  rollout_root: {root}
  group: aeri
  forward_folder: {values["forward_folder"]}
  reset_folder: {values["reset_folder"]}
{values["extra"]}"""


@pytest.fixture
def job(tmp_path):
    root = tmp_path / "rollouts"
    root.mkdir()
    (tmp_path / "initial-state.yaml").write_text(CONTRACT)
    path = tmp_path / "automatic-eval.yaml"
    path.write_text(job_text(root))
    return path


def call(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr().out
    return code, out


def as_json(capsys, *argv):
    code, out = call(capsys, *argv, "--json")
    return code, json.loads(out)


# --- validate ----------------------------------------------------------------------------------


def test_validate_prints_a_stable_plan(job, capsys):
    code, found = as_json(capsys, "validate", "--config", str(job))
    assert code == 0 and found["ok"]
    plan = found["plan"]
    assert plan["contract"]["id"] == "stack-plates-initial"
    assert plan["run"]["reset_strategy"] == "single_reset_policy"
    assert plan["run"]["forward_max_steps"] == 30
    again = as_json(capsys, "validate", "--config", str(job))[1]
    assert again["plan"]["plan_sha256"] == plan["plan_sha256"]
    job.write_text(job.read_text().replace("episodes: 2", "episodes: 3"))
    changed = as_json(capsys, "validate", "--config", str(job))[1]
    assert changed["plan"]["plan_sha256"] != plan["plan_sha256"]


@pytest.mark.parametrize(
    "over",
    [
        {"extra": "robot:\n  adapter: fr3\n"},  # a section v1 does not read
        {"strategy": "atomic_skill_sequence"},
        {"reset_folder": "stack__r-cli"},  # one folder for both roles
        {"name": "r.cli"},  # dots belong to episode ids
        {"episodes": -1},
        {"extra": "termination:\n  min_steps: 3\n"},  # a duplicate section
    ],
)
def test_validate_refuses_a_bad_job(job, capsys, over):
    job.write_text(job_text(job.parent / "rollouts", **over))
    code, found = as_json(capsys, "validate", "--config", str(job))
    assert code == 2 and not found["ok"] and found["error"]


def test_validate_refuses_flow_mappings_and_a_bad_contract(job, capsys):
    job.write_text(
        job.read_text().replace(
            "  forward:\n    max_steps: 30", "  forward: {max_steps: 30}"
        )
    )
    assert as_json(capsys, "validate", "--config", str(job))[0] == 2
    (job.parent / "initial-state.yaml").write_text("initial_state:\n\tid: x\n")
    job.write_text(job_text(job.parent / "rollouts"))
    code, found = as_json(capsys, "validate", "--config", str(job))
    assert code == 2 and "initial_state_spec" in found["error"]


# --- run ---------------------------------------------------------------------------------------------


def test_a_real_run_is_refused(job, capsys):
    code, found = as_json(capsys, "run", "--config", str(job))
    assert code == 2 and "--dry-run" in found["error"]
    assert list((job.parent / "rollouts").iterdir()) == []


def test_a_dry_run_drives_the_fakes_only_and_leaves_nothing(
    job, capsys, monkeypatch, tmp_path
):
    entered = []
    real = cli.no_network

    def spy():
        entered.append(True)
        return real()

    monkeypatch.setattr(cli, "no_network", spy)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", None)
    code, found = as_json(capsys, "run", "--config", str(job), "--dry-run")
    assert code == 0 and found["dry_run"] and found["state"] == "COMPLETED"
    # The only robot was the in-process fake; nothing real had authority.
    assert found["robot"]["kind"] == "FakeRobot" and found["robot"]["motions"] > 0
    assert found["robot"]["refused"] == 0
    assert entered == [True]
    # The job's rollout root is never written; the temporary folder is gone.
    assert list((job.parent / "rollouts").iterdir()) == []
    assert list((tmp_path / "tmp").iterdir()) == []
    assert found["metrics"]["autonomous"]["forward_episodes"] == 2


def test_no_network_refuses_every_connection_and_restores():
    before = socket.socket.connect, socket.create_connection
    with cli.no_network():
        with pytest.raises(cli.NetworkRefused):
            socket.create_connection(("127.0.0.1", 9))
        with socket.socket() as s, pytest.raises(cli.NetworkRefused):
            s.connect(("127.0.0.1", 9))
    assert (socket.socket.connect, socket.create_connection) == before


def test_a_kept_dry_run_can_be_inspected_and_reported(job, capsys, tmp_path):
    keep = tmp_path / "kept"
    code, found = as_json(
        capsys,
        "run",
        "--config",
        str(job),
        "--dry-run",
        "--keep",
        str(keep),
        "--scenes",
        "reset_required",
    )
    assert code == 0 and found["kept"] == str(keep)
    assert found["metrics"]["reset"]["resets"] == 1
    run_dir = keep / ".aeri" / "runs" / "r-cli"
    journal = run_dir / "state_journal.jsonl"
    before = journal.read_bytes()
    code, status = as_json(capsys, "status", "--run-dir", str(run_dir))
    assert code == 0 and status["state"] == "COMPLETED" and status["corrupt"] is None
    assert status["rollouts"]["complete"] == 3
    code, out = call(capsys, "report", "--run-dir", str(run_dir), "--config", str(job))
    assert code == 0 and "# AERI run report (COMPLETED)" in out
    assert "not ground truth" in out and "| resets | 1 |" in out
    code, out = call(capsys, "report", "--run-dir", str(run_dir), "--format", "json")
    assert json.loads(out)["automation"]["interventions"] == 0
    assert journal.read_bytes() == before
    # A second dry run never writes into a folder that holds one.
    code, found = as_json(
        capsys, "run", "--config", str(job), "--dry-run", "--keep", str(keep)
    )
    assert code == 2


def test_status_is_read_only_even_on_a_torn_or_corrupt_journal(job, capsys, tmp_path):
    keep = tmp_path / "kept"
    assert (
        as_json(capsys, "run", "--config", str(job), "--dry-run", "--keep", str(keep))[
            0
        ]
        == 0
    )
    run_dir = keep / ".aeri" / "runs" / "r-cli"
    journal = run_dir / "state_journal.jsonl"
    journal.write_bytes(journal.read_bytes() + b'{"torn')
    torn = journal.read_bytes()
    code, status = as_json(capsys, "status", "--run-dir", str(run_dir))
    assert code == 0 and status["torn_bytes"] == 6 and status["state"] == "COMPLETED"
    assert journal.read_bytes() == torn and not (run_dir / "torn").exists()
    lines = journal.read_bytes().split(b"\n")
    lines[1] = lines[1].replace(b'"record":', b'"recorx":', 1)
    journal.write_bytes(b"\n".join(lines))
    code, status = as_json(capsys, "status", "--run-dir", str(run_dir))
    assert status["state"] == "FAULT_LOCKED" and status["corrupt"]
    assert as_json(capsys, "status", "--run-dir", str(tmp_path))[0] == 2


# --- doctor and help -----------------------------------------------------------------------------------


def test_doctor_reads_only_and_says_a_real_run_is_not_available(job, capsys):
    before = sorted(p.name for p in job.parent.rglob("*"))
    code, found = as_json(capsys, "doctor", "--config", str(job))
    assert code == 0 and found["ok"]
    checks = {c["check"]: c for c in found["checks"]}
    assert checks["contract snapshots"]["ok"] and checks["job file"]["ok"]
    assert not checks["real robot adapter"]["ok"]
    assert not checks["real robot adapter"]["required"]
    assert checks["rollout root"]["ok"]
    assert sorted(p.name for p in job.parent.rglob("*")) == before
    job.write_text("schema_version: nope\n")
    assert as_json(capsys, "doctor", "--config", str(job))[0] == 1


CJK = re.compile(r"[一-鿿]")


def test_every_help_is_bilingual():
    parser = cli.build_parser()
    assert CJK.search(parser.format_help()) and "dry-run" in parser.format_help()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert set(sub.choices) == {"doctor", "validate", "run", "status", "report"}
    for name, command in sub.choices.items():
        for action in command._actions:
            if (
                action.help
                and action.help != argparse.SUPPRESS
                and action.dest != "help"
            ):
                assert " / " in action.help and CJK.search(action.help), (
                    name,
                    action.dest,
                )


def test_the_module_runs_as_a_command(job):
    proc = subprocess.run(
        [sys.executable, "-m", "levi.automatic.cli", "validate", "--config", str(job)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=HERE.parents[1],
        env={**os.environ, "PYTHONPATH": str(HERE.parents[1])},
        check=False,
    )
    assert proc.returncode == 0 and proc.stdout.startswith("valid: run r-cli")


def test_a_bad_episode_override_is_refused_not_raised(job, capsys):
    code, found = as_json(
        capsys, "run", "--config", str(job), "--dry-run", "--episodes", "-1"
    )
    assert code == 2 and "episodes" in found["error"]


def test_a_single_policy_job_without_a_contract_is_refused_for_a_real_run(job, capsys):
    """Fix review I-b: refused at validation, with the fix; a dry run still runs."""
    job.write_text(
        job.read_text().replace("  initial_state_spec: initial-state.yaml\n", "")
    )
    code, found = as_json(capsys, "validate", "--config", str(job))
    assert code == 2 and "initial_state_spec" in found["error"]
    assert "no forward episode" in found["error"]
    code, found = as_json(capsys, "validate", "--config", str(job), "--dry-run")
    assert code == 0 and found["warnings"]
    assert "no forward episode" in found["warnings"][0]
    code, found = as_json(capsys, "doctor", "--config", str(job))
    checks = [c for c in found["checks"] if c["check"] == "launch"]
    assert checks and not checks[0]["ok"]
    code, found = as_json(
        capsys, "run", "--config", str(job), "--dry-run", "--episodes", "1"
    )
    assert code == 0 and found["state"] == "WAIT_HUMAN"
    # No reset runs that could never make the scene ready.
    assert found["metrics"]["reset"]["resets"] == 0 and found["robot"]["motions"] == 0


def test_launch_refuses_the_human_assisted_mode_without_a_scene_provider(job):
    """Reserved check (X2 G6): an eval-only run whose scene check cannot
    answer would cycle between WAIT_HUMAN and VERIFY_INITIAL."""
    job.write_text(job.read_text().replace("single_reset_policy", "human_assisted"))
    found = cli.load_job(job)
    assert cli.launch_problems(found, dry_run=True, scene_provider="fake") == []
    problems = cli.launch_problems(found, dry_run=False, scene_provider=None)
    assert problems and "scene" in problems[0]


@pytest.mark.parametrize("episodes", ["0"])
def test_dry_run_edge_cases_never_raise(job, capsys, tmp_path, episodes):
    code, found = as_json(
        capsys, "run", "--config", str(job), "--dry-run", "--episodes", episodes
    )
    assert code == 0 and found["state"] == "COMPLETED"
    plain = tmp_path / "a-file"
    plain.write_text("x")
    code, found = as_json(
        capsys, "run", "--config", str(job), "--dry-run", "--keep", str(plain)
    )
    assert code == 2 and "empty folder" in found["error"]


def test_levi_automatic_is_dispatched_by_the_levi_command(job, capsys, monkeypatch):
    from levi import cli as levi_cli

    monkeypatch.setattr(
        sys, "argv", ["levi", "automatic", "validate", "--config", str(job)]
    )
    with pytest.raises(SystemExit) as stop:
        levi_cli.main()
    assert stop.value.code == 0
    assert capsys.readouterr().out.startswith("valid: run r-cli")
