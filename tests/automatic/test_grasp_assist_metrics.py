"""GRASP_ASSIST is a planned human intervention (design STEP5-AERI-RESET-DESIGN §3): the run without any human ends at
it, the planned count includes it, and a journal that never entered it keeps the metrics it always had (the snapshot
was written by the metrics module of the base commit)."""

import json
from pathlib import Path

from test_subtask_grasp_assist import make

from levi.automatic import metrics as M

SNAPSHOT = Path(__file__).parent / "snapshots" / "metrics_subtask_reset_no_assist.json"
EVENT_FIELDS = ("record", "from_state", "to_state", "reason")


def report_of(r):
    return M.report(r.orch.journal.events, reset_mode="subtask_loop")


def test_a_journal_without_the_takeover_keeps_its_metrics(tmp_path):
    r = make(tmp_path, [], assist=False, rounds=2)
    assert r.orch.run() == "COMPLETED"
    found = report_of(r)
    assert json.dumps(found, sort_keys=True, indent=1) + "\n" == SNAPSHOT.read_text()
    assert "grasp_assist_started" not in found["automation"]["by_reason"]


def test_the_takeover_ends_the_run_without_any_human_and_counts_as_planned(tmp_path):
    base_run = make(tmp_path / "a", [], assist=False, rounds=2)
    assert base_run.orch.run() == "COMPLETED"
    plain = report_of(base_run)
    r = make(tmp_path / "b", ["c", "u", "g"] * 2, rounds=2)
    assert r.orch.run() == "COMPLETED"
    found = report_of(r)["automation"]
    base = plain["automation"]
    # Two takeovers (one per round) cut the four sealed forward episodes in two.
    assert base["longest_run_without_any_human"] == 4
    assert found["longest_run_without_any_human"] == 2
    assert found["interventions"] == base["interventions"] + 2
    assert found["planned"] == base["planned"] + 2
    assert found["planned_by_reason"]["grasp_assist_started"] == 2
    # It is planned: the run without an unplanned intervention is not cut by it.
    assert found["unplanned"] == base["unplanned"]
    assert (
        found["longest_run_without_unplanned"] == base["longest_run_without_unplanned"]
    )
    # Its time is a person's in the turnaround.
    assert report_of(r)["turnaround"]["with_person"] is not None
