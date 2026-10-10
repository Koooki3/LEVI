"""``levi automatic`` in both reset modes (T-CL-01, the ``cli:*`` rows of
levi/automatic/modes.py): the same job file with ``reset.strategy`` set to
each mode, through every subcommand. Dry runs drive the in-process fakes
only."""

import json

import pytest
from test_aeri_cli import CONTRACT, as_json, call, job_text

from levi.automatic import modes


@pytest.fixture
def job(tmp_path, reset_mode):
    root = tmp_path / "rollouts"
    root.mkdir()
    (tmp_path / "initial-state.yaml").write_text(CONTRACT)
    path = tmp_path / "automatic-eval.yaml"
    path.write_text(job_text(root, strategy=reset_mode))
    return path


@pytest.mark.mode_matrix("cli:validate")
def test_validate_in_each_mode(job, capsys, reset_mode):
    code, found = as_json(capsys, "validate", "--config", str(job), "--dry-run")
    assert code == 0 and found["plan"]["run"]["reset_strategy"] == reset_mode
    code, found = as_json(capsys, "validate", "--config", str(job))
    if reset_mode == modes.HUMAN:
        # No scene provider that can answer is configured in this version.
        assert code == 2 and "scene" in found["error"].lower()
    else:
        assert code == 0 and found["ok"]


@pytest.mark.mode_matrix("cli:doctor")
def test_doctor_in_each_mode(job, capsys, reset_mode):
    before = sorted(p.name for p in job.parent.rglob("*"))
    code, found = as_json(capsys, "doctor", "--config", str(job))
    checks = {c["check"]: c for c in found["checks"]}
    assert checks["job file"]["ok"] and checks["contract snapshots"]["ok"]
    assert not checks["real robot adapter"]["ok"]
    launch = checks.get("launch")
    if reset_mode == modes.HUMAN:
        assert launch is not None and not launch["ok"]
        assert "scene" in launch["detail"].lower()
    else:
        assert launch is None or launch["ok"]
    assert code == (0 if found["ok"] else 1)
    assert sorted(p.name for p in job.parent.rglob("*")) == before


@pytest.mark.mode_matrix("cli:run", "cli:status", "cli:report")
def test_a_dry_run_with_a_scene_that_is_not_ready(job, capsys, tmp_path, reset_mode):
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
    human = reset_mode == modes.HUMAN
    assert code == 0 and found["robot"]["refused"] == 0
    # The reset policy puts the scene back; without one a person must.
    assert found["state"] == ("WAIT_HUMAN" if human else "COMPLETED")
    assert found["metrics"]["reset"]["resets"] == (0 if human else 1)
    assert found["metrics"]["autonomous"]["forward_episodes"] == (0 if human else 2)
    path = [tuple(t) for t in found["transitions"]]
    assert any(b in modes.RESET_STATES for _, b, _ in path) == (not human)
    run_dir = keep / ".aeri" / "runs" / "r-cli"
    code, status = as_json(capsys, "status", "--run-dir", str(run_dir))
    assert code == 0 and status["state"] == found["state"]
    code, out = call(capsys, "report", "--run-dir", str(run_dir), "--config", str(job))
    assert code == 0 and f"# AERI run report ({found['state']})" in out
    code, out = call(capsys, "report", "--run-dir", str(run_dir), "--format", "json")
    report = json.loads(out)
    assert code == 0 and report["state"] == found["state"]
    assert "turnaround.turnaround_ms" in report["comparable"]
    if human:
        # A scene check sent the run to a person (planned when the mode is
        # known; the report infers it from the run, T-CL-06 writes it).
        assert report["automation"]["interventions"] == 1
        assert report["automation"]["by_reason"] == {"scene_reset_required": 1}
    else:
        assert report["reset_mode"] == modes.SINGLE
        assert report["automation"]["interventions"] == 0
