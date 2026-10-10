"""T-JNL-1 end to end: a run started by the orchestrator or by the dry run
names its reset mode and scene check in the header, keeps its plan as
``plan.json``, and a report takes the mode from the run header; a restart
with another mode is refused."""

import json

import pytest
from aeri_harness import build, config, restart
from test_aeri_cli import CONTRACT, as_json, call, job_text

from levi.automatic import journal as J
from levi.automatic import metrics as M
from levi.domain import aeri


def test_a_new_run_names_its_modes_in_the_header(tmp_path, reset_mode):
    r = build(tmp_path, cfg=config(reset_strategy=reset_mode))
    r.orch.close()
    header = J.Journal.read(r.directory).events[0].header
    assert (header.reset_mode, header.scene_check) == (reset_mode, "provider")
    found = M.mode_of(J.Journal.read(r.directory).events)
    assert found["reset_mode"] == reset_mode
    assert found["source"] == {"reset_mode": "run_header", "scene_check": "run_header"}


def test_a_restart_with_another_reset_mode_is_refused(tmp_path):
    r = build(tmp_path, cfg=config(reset_strategy="single_reset_policy"))
    with pytest.raises(J.JournalRefused) as caught:
        restart(r, cfg=config(reset_strategy="human_assisted"))
    assert caught.value.code == "E_PLAN"
    events = J.Journal.read(r.directory).events
    assert events[-1].record == "note"
    assert events[-1].note.code == "run_header_mismatch"
    assert events[-1].authority.principal_kind == "recovery"
    assert events[0].header.reset_mode == "single_reset_policy"
    # The same configuration still takes the run over.
    again = restart(r)
    assert again.orch.state == "FAULT_LOCKED"
    again.orch.close()


@pytest.fixture
def job(tmp_path, reset_mode):
    root = tmp_path / "rollouts"
    root.mkdir()
    (tmp_path / "initial-state.yaml").write_text(CONTRACT)
    path = tmp_path / "automatic-eval.yaml"
    path.write_text(job_text(root, strategy=reset_mode))
    return path


def test_a_dry_run_keeps_its_plan_and_its_report_reads_the_header(
    job, capsys, tmp_path, reset_mode
):
    keep = tmp_path / "kept"
    code, valid = as_json(capsys, "validate", "--config", str(job), "--dry-run")
    assert code == 0
    code, found = as_json(
        capsys, "run", "--config", str(job), "--dry-run", "--keep", str(keep)
    )
    assert code == 0, found
    run_dir = keep / ".aeri" / "runs" / "r-cli"
    scan = J.Journal.read(run_dir)
    assert scan.corrupt is None
    assert {e.minor for e in scan.events} == {aeri.MINORS["run_event"]}
    header = scan.events[0].header
    assert (header.reset_mode, header.scene_check) == (reset_mode, "provider")
    kept = J.read_plan(run_dir)
    assert kept["plan_sha256"] == header.plan_sha256 == valid["plan"]["plan_sha256"]
    assert kept["plan"]["reset_mode"] == reset_mode
    assert aeri.header_modes(header, kept["plan"]) == {
        "reset_mode": reset_mode,
        "scene_check": "provider",
    }
    code, out = call(capsys, "report", "--run-dir", str(run_dir), "--format", "json")
    report = json.loads(out)
    assert code == 0 and report["reset_mode"] == reset_mode
    assert report["mode_source"] == {
        "reset_mode": "run_header",
        "scene_check": "run_header",
    }
