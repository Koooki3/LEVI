"""Task closure, local memory and the self-improvement loop (plan §6.6, §10.2)."""

# The shared fixtures are imported by name, which pytest needs and ruff reads
# as redefinition in every test that takes them.
# ruff: noqa: F811

import json
import re
from pathlib import Path

import numpy as np
import pytest
from test_agent_economy import bench, video_run, with_video  # noqa: F401

from levi.agent.capabilities import invoke
from levi.agent.security import Principal
from levi.harness import closure, improvements, layout, memory

GRASP = {
    "id": "grasp",
    "label": "grasp",
    "definition": "close on the plate",
    "starts_when": "fingers touch",
    "ends_when": "object moves",
    "success_when": "object is held",
}


def temporal_run(wb, context, agent, dataset, **workflow):
    return video_run(
        wb,
        context,
        agent,
        dataset,
        kind="temporal",
        coarse_step_seconds=0.5,
        definitions=[GRASP],
        **workflow,
    )[0]


def propose(wb, agent, run, uncertainty=""):
    items = invoke(wb, agent, "evidence.read", {"run_id": run["id"], "episode": 0})
    return invoke(
        wb,
        agent,
        "annotations.propose_segments",
        {
            "run_id": run["id"],
            "inspected_episodes": [0],
            "proposals": [
                {
                    "episode_index": 0,
                    "kind": "segment",
                    "subtask_id": "grasp",
                    "content": "Close on the plate",
                    "start": 0.0,
                    "end": 1.0,
                    "outcome": "unknown" if uncertainty else "success",
                    "uncertainty": uncertainty,
                    "evidence_note": "" if uncertainty else "held at 1.0 s",
                    "evidence_ids": [items["items"][0]["id"]],
                }
            ],
        },
    )


def commit(wb, receipt):
    human = Principal("operator", human=True)
    invoke(
        wb,
        human,
        "changes.approve",
        {"changeset_id": receipt["id"], "revision": receipt["revision"]},
    )
    return wb.commit(receipt["id"], f"commit-{receipt['id']}", receipt["revision"])


def test_a_committed_task_is_closed_once_with_ledger_and_memory(bench, dataset):
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run = temporal_run(wb, context, agent, dataset)
    receipt = propose(wb, agent, run)
    commit(wb, receipt)

    # Committing goes through wb.commit directly here; the next capability
    # call on the run notices it finished and closes it.
    invoke(wb, agent, "runs.get", {"run_id": run["id"]})
    record = wb.store.get("runs", run["id"])
    assert record["closure"]["state"] == "closed"
    assert record["closure"]["memory_updated"] is True

    key = record["dataset_key"]
    ledger_path = layout.task_dir(wb.store.state, key, run["id"]) / "ledger.json"
    ledger = json.loads(ledger_path.read_text())
    assert ledger["run_id"] == run["id"] and ledger["status"] == "succeeded"
    assert ledger["episodes"][0]["episode"] == "episode_000000"
    assert ledger["tokens"]["delivered"]["response_bytes"] > 0
    assert ledger["closure"]["state"] == "closed"

    remembered = json.loads(layout.memory_path(wb.store.state, key).read_text())
    assert remembered["sources"] == [run["id"]]
    assert remembered["episodes"]["episode_000000"]["segments"][0]["subtask"] == (
        "grasp"
    )
    assert remembered["subtasks"]["grasp"]["outcomes"] == {"success": 1}

    # Closing again is a no-op: same closure, memory not counted twice.
    assert closure.close(wb.store, run["id"], "again") == record["closure"]
    assert json.loads(layout.memory_path(wb.store.state, key).read_text())[
        "sources"
    ] == [run["id"]]


def test_paths_follow_the_dataset_and_run_names_without_hashes(bench, dataset):
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run = temporal_run(wb, context, agent, dataset)
    commit(wb, propose(wb, agent, run))
    invoke(wb, agent, "runs.get", {"run_id": run["id"]})
    key = wb.store.get("runs", run["id"])["dataset_key"]
    ledger = layout.task_dir(wb.store.state, key, run["id"]) / "ledger.json"
    remembered = layout.memory_path(wb.store.state, key)
    assert ledger.is_file() and remembered.is_file()
    assert ledger.parts[-5:] == ("datasets", key, "tasks", run["id"], "ledger.json")
    assert remembered.parts[-2:] == ("memory", f"{key}.json")
    for path in (ledger, remembered):
        assert not re.search(r"[0-9a-f]{8,}", path.name), path


def test_a_late_usage_report_refreshes_the_ledger_without_triaging_again(
    bench, dataset
):
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run = temporal_run(wb, context, agent, dataset)
    commit(wb, propose(wb, agent, run))
    invoke(wb, agent, "runs.get", {"run_id": run["id"]})
    first = wb.store.get("runs", run["id"])["closure"]
    invoke(
        wb,
        agent,
        "runs.report_usage",
        {"run_id": run["id"], "input_tokens": 1000, "output_tokens": 50},
    )
    key = wb.store.get("runs", run["id"])["dataset_key"]
    ledger = json.loads(
        (layout.task_dir(wb.store.state, key, run["id"]) / "ledger.json").read_text()
    )
    assert ledger["tokens"]["reported"]["tokens"] == 1050
    assert wb.store.get("runs", run["id"])["closure"] == first
    # The dataset memory's cost follows the late report too.
    remembered = json.loads(layout.memory_path(wb.store.state, key).read_text())
    assert remembered["cost"]["reported_tokens_per_episode"] == 1050


def test_an_uncommitted_run_leaves_a_ledger_but_no_memory(bench, dataset):
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run = temporal_run(wb, context, agent, dataset)
    propose(wb, agent, run)
    invoke(wb, Principal("operator", human=True), "runs.cancel", {"run_id": run["id"]})
    record = wb.store.get("runs", run["id"])
    assert record["closure"]["state"] == "closed"
    assert record["closure"]["memory_updated"] is False
    # No reviewed facts from an uncommitted run -- but its cost was measured.
    remembered = json.loads(
        layout.memory_path(wb.store.state, record["dataset_key"]).read_text()
    )
    assert remembered["episodes"] == {} and remembered["sources"] == []
    assert remembered["cost_profiles"]


def test_propose_returns_a_receipt_not_the_whole_changeset(bench, dataset):
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run = temporal_run(wb, context, agent, dataset)
    receipt = propose(wb, agent, run)
    assert "proposals" not in receipt
    assert receipt["accepted"] == {"0": 1} and receipt["staged_proposals"] == 1
    assert wb.store.get("changes", receipt["id"])["proposals"]


def test_each_call_gets_its_own_id(bench, dataset):
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run = temporal_run(wb, context, agent, dataset)
    for _ in range(3):
        invoke(wb, agent, "evidence.read", {"run_id": run["id"], "episode": 0})
    ids = [
        e["call_id"]
        for e in wb.store.events(run["id"])
        if e["type"] == "action.completed" and e["tool"] == "evidence.read"
    ]
    assert len(ids) == 3 and len(set(ids)) == 3


def gap_ledger(run_id, key, episodes=4, parameters=None):
    note = (
        "Coarse samples are 2 s apart; a short event between them cannot be excluded."
    )
    return {
        "run_id": run_id,
        "dataset_key": key,
        "harness": {"parameters": parameters or {}, "sources": {}},
        "tools": {},
        "episodes_completed": episodes,
        "wall_seconds": 10.0,
        "episodes": [
            {"episode": f"episode_{i:06d}", "processed": True, "warnings": [note]}
            for i in range(episodes)
        ],
    }


def test_triage_proposes_once_then_files_recurrence_as_duplicate(tmp_path):
    state = tmp_path / "outputs/LEVI/workbench"
    first = improvements.triage(state, gap_ledger("temporal-a", "plates"))
    assert first == [{"slug": "refine-at-coarse-change", "outcome": "proposed"}]
    again = improvements.triage(state, gap_ledger("temporal-a", "plates"))
    assert again[0]["outcome"] == "no_op"
    later = improvements.triage(state, gap_ledger("temporal-b", "plates"))
    assert later[0]["outcome"] == "duplicate"
    files = sorted(p.name for p in (state / "improvements/plates").iterdir())
    assert files == ["refine-at-coarse-change.json"]
    candidate = improvements.load(state, "plates", "refine-at-coarse-change")
    assert [e["run_id"] for e in candidate["evidence"]] == ["temporal-a", "temporal-b"]
    # A single odd episode is not a pattern.
    quiet = improvements.triage(state, gap_ledger("temporal-c", "cups", episodes=2))
    assert quiet == [{"outcome": "no_op"}]


def test_oversized_responses_become_a_software_report_the_harness_cannot_publish(
    tmp_path,
):
    state = tmp_path / "outputs/LEVI/workbench"
    ledger = gap_ledger("temporal-a", "plates", episodes=1)
    ledger["tools"] = {
        "annotations.propose_segments": {
            "calls": 10,
            "response_bytes": 184_676,
        }
    }
    outcome = improvements.triage(state, ledger)
    slug = "oversized-annotations-propose_segments-responses"
    assert {"slug": slug, "outcome": "proposed"} in outcome
    improvements.transition(state, "plates", slug, "evaluating", by="a", human=False)
    with pytest.raises(ValueError, match="developer"):
        improvements.transition(state, "plates", slug, "qualified", by="a", human=False)


def test_publication_needs_a_passed_evaluation_and_a_person(tmp_path):
    state = tmp_path / "outputs/LEVI/workbench"
    improvements.triage(state, gap_ledger("temporal-a", "plates"))
    slug = "refine-at-coarse-change"
    improvements.transition(state, "plates", slug, "evaluating", by="a", human=False)
    with pytest.raises(ValueError, match="passed"):
        improvements.transition(state, "plates", slug, "qualified", by="a", human=False)
    improvements.transition(
        state,
        "plates",
        slug,
        "qualified",
        by="a",
        human=False,
        evaluation={"passed": True},
    )
    improvements.transition(
        state, "plates", slug, "awaiting_authorization", by="a", human=False
    )
    with pytest.raises(PermissionError):
        improvements.transition(state, "plates", slug, "published", by="a", human=False)
    assert (
        improvements.snapshot(state, "plates")["parameters"]["evidence.refine_top_k"]
        == 0
    )
    published = improvements.transition(
        state, "plates", slug, "published", by="operator", human=True
    )
    assert published["state"] == "observing"
    snapshot = improvements.snapshot(state, "plates")
    assert snapshot["parameters"]["evidence.refine_top_k"] == 2
    assert snapshot["sources"] == {"evidence.refine_top_k": slug}
    lesson = memory.load(state, "plates")["lessons"][0]
    assert lesson["slug"] == slug and lesson["status"] == "observing"
    improvements.transition(state, "plates", slug, "rolled_back", by="op", human=True)
    assert improvements.snapshot(state, "plates")["sources"] == {}


def test_only_listed_parameters_within_bounds_can_be_targeted(tmp_path):
    state = tmp_path / "outputs/LEVI/workbench"
    finding = {"run_id": "r", "observation": "x"}
    for target in (
        {"parameter": "validators.enabled", "value": 0},
        {"parameter": "evidence.refine_top_k", "value": 9},
    ):
        with pytest.raises(ValueError):
            improvements.file_finding(
                state,
                "plates",
                slug="bad",
                kind="harness",
                title="t",
                finding=finding,
                target=target,
            )


def test_a_run_keeps_the_harness_it_was_planned_with(bench, dataset):
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    before = temporal_run(wb, context, agent, dataset)
    assert before["harness"]["parameters"]["evidence.refine_top_k"] == 0
    state, key = wb.store.state, before["dataset_key"]
    improvements.triage(state, gap_ledger("temporal-x", key))
    slug = "refine-at-coarse-change"
    for to, human in (
        ("evaluating", False),
        ("qualified", False),
        ("awaiting_authorization", False),
        ("published", True),
    ):
        improvements.transition(
            state,
            key,
            slug,
            to,
            by="t",
            human=human,
            evaluation={"passed": True} if to == "qualified" else None,
        )
    assert (
        wb.store.get("runs", before["id"])["harness"]["parameters"][
            "evidence.refine_top_k"
        ]
        == 0
    )
    after = temporal_run(wb, context, agent, dataset)
    assert after["harness"]["parameters"]["evidence.refine_top_k"] == 2
    assert after["harness"]["sources"] == {"evidence.refine_top_k": slug}


def jump_video(monkeypatch, jump=12):
    """Constant picture except a bright block from frame ``jump`` on.

    video_run writes the fixture's videos itself, so the jump is applied
    right after it does.
    """
    import cv2
    import test_agent_economy

    original = test_agent_economy.with_video

    def patched(dataset, camera="observation.images.front", frames=20):
        original(dataset, camera, frames)
        path = dataset / f"videos/chunk-000/{camera}/episode_000000.mp4"
        writer = cv2.VideoWriter(
            str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 48)
        )
        for index in range(frames):
            frame = np.full((48, 64, 3), 40, np.uint8)
            if index >= jump:
                frame[:, :32] = 220
            writer.write(frame)
        writer.release()
        return camera

    monkeypatch.setattr(test_agent_economy, "with_video", patched)


def test_the_last_interval_of_an_episode_is_ranked_too(bench, dataset):
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run = temporal_run(wb, context, agent, dataset)
    ranked = invoke(wb, agent, "evidence.changes", {"run_id": run["id"], "episode": 0})
    assert max(row["to"] for row in ranked["intervals"]) == pytest.approx(1.9)


def test_evidence_changes_ranks_the_interval_where_the_picture_changed(
    bench, dataset, monkeypatch
):
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    jump_video(monkeypatch)
    run = temporal_run(wb, context, agent, dataset)
    ranked = invoke(
        wb, agent, "evidence.changes", {"run_id": run["id"], "episode": 0, "top_k": 1}
    )
    top = ranked["intervals"][0]
    assert top["from"] < 1.2 < top["to"] + 1e-6
    assert ranked["suggested_around_seconds"] == [top["around_seconds"]]


def test_evaluation_is_computed_by_levi_on_stored_evidence(bench, dataset, monkeypatch):
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    jump_video(monkeypatch)
    run = temporal_run(wb, context, agent, dataset)
    state, key = wb.store.state, run["dataset_key"]
    improvements.triage(state, gap_ledger("temporal-x", key))
    slug = "refine-at-coarse-change"
    invoke(
        wb,
        agent,
        "improvements.transition",
        {"repo_id": context.repo_id, "slug": slug, "to": "evaluating"},
    )
    result = invoke(
        wb,
        agent,
        "improvements.evaluate",
        {
            "repo_id": context.repo_id,
            "slug": slug,
            "probes": [{"run_id": run["id"], "episode": 0, "start": 1.1, "end": 1.3}],
        },
    )
    assert result["passed"] and result["probes"][0]["rank"] == 0
    for to in ("qualified", "awaiting_authorization"):
        invoke(
            wb,
            agent,
            "improvements.transition",
            {"repo_id": context.repo_id, "slug": slug, "to": to},
        )
    with pytest.raises(PermissionError):
        invoke(
            wb,
            agent,
            "improvements.transition",
            {"repo_id": context.repo_id, "slug": slug, "to": "published"},
        )


def test_quality_inspect_keeps_an_hourly_report_with_the_dataset(bench):
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    digest = invoke(
        wb, agent, "quality.inspect", {"repo_id": context.repo_id, "checks": []}
    )
    assert digest["status"] in {"pass", "warn", "fail"}
    assert all(row["status"] != "pass" for row in digest["findings"])
    name = digest["path"].rsplit("/", 1)[-1]
    assert re.fullmatch(r"quality-\d{8}T\d{2}\.json", name), name
    assert "/datasets/fixture/reports/" in digest["path"]
    # A second check in the same hour replaces that hour's report.
    invoke(wb, agent, "quality.inspect", {"repo_id": context.repo_id, "checks": []})
    reports = layout.reports_dir(wb.store.state, "fixture")
    assert [p.name for p in reports.iterdir()] == [name]


def test_memory_reaches_the_next_task_at_plan_time(bench, dataset):
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run = temporal_run(wb, context, agent, dataset)
    commit(wb, propose(wb, agent, run))
    invoke(wb, agent, "runs.get", {"run_id": run["id"]})
    recalled = invoke(wb, agent, "memory.get", {"repo_id": context.repo_id})
    assert recalled["episodes_annotated"] == 1
    assert "grasp" in recalled["subtasks"]
    hits = invoke(
        wb, agent, "memory.search", {"repo_id": context.repo_id, "query": "plate"}
    )
    assert hits["total"] == 1 and hits["hits"][0]["episode"] == "episode_000000"
    following = temporal_run(wb, context, agent, dataset)
    assert following["harness"]["memory"]["episodes_annotated"] == 1


def test_a_candidate_is_revised_at_most_once_and_retested(tmp_path):
    state = tmp_path / "outputs/LEVI/workbench"
    improvements.triage(state, gap_ledger("temporal-a", "plates"))
    slug = "refine-at-coarse-change"
    improvements.transition(state, "plates", slug, "evaluating", by="a", human=False)
    first = improvements.load(state, "plates", slug)
    first["evaluation"] = {"passed": False, "hit_rate": 0.667}
    improvements.write_json(improvements.path(state, "plates", slug), first)
    revised = improvements.revise(state, "plates", slug, 3, by="a", note="k=2 missed")
    assert revised["target"]["value"] == 3 and revised["evaluation"] is None
    assert revised["revisions"][0]["previous_evaluation"]["hit_rate"] == 0.667
    with pytest.raises(ValueError, match="already revised"):
        improvements.revise(state, "plates", slug, 4, by="a")
    with pytest.raises(ValueError, match="passed"):
        improvements.transition(state, "plates", slug, "qualified", by="a", human=False)


def test_refinement_frames_do_not_split_the_ranked_intervals(bench, dataset):
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run = temporal_run(wb, context, agent, dataset)
    before = invoke(wb, agent, "evidence.changes", {"run_id": run["id"], "episode": 0})
    invoke(
        wb,
        agent,
        "evidence.refine",
        {"run_id": run["id"], "episode": 0, "around_seconds": [1.0]},
    )
    after = invoke(wb, agent, "evidence.changes", {"run_id": run["id"], "episode": 0})
    spans = sorted((row["from"], row["to"]) for row in after["intervals"])
    assert spans == sorted((row["from"], row["to"]) for row in before["intervals"])


def test_what_the_web_ui_fetches_is_not_counted_as_agent_tokens(bench, dataset):
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    human = Principal("local-human", human=True)
    run = temporal_run(wb, context, agent, dataset)
    receipt = propose(wb, agent, run)
    invoke(wb, human, "changes.diff", {"changeset_id": receipt["id"]})
    for _ in range(5):
        invoke(wb, human, "media.sample", {"run_id": run["id"]})
    commit(wb, receipt)
    invoke(wb, agent, "runs.get", {"run_id": run["id"]})
    key = wb.store.get("runs", run["id"])["dataset_key"]
    ledger = json.loads(
        (layout.task_dir(wb.store.state, key, run["id"]) / "ledger.json").read_text()
    )
    assert "media.sample" not in ledger["tools"]
    assert ledger["human_calls"]["media.sample"]["calls"] == 5
    assert "annotations.propose_segments" in ledger["tools"]
    triage = wb.store.get("runs", run["id"])["closure"]["triage"]
    assert not any("media-sample" in row.get("slug", "") for row in triage)


def test_refine_answers_with_compact_rows(bench, dataset):
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run = temporal_run(wb, context, agent, dataset)
    refined = invoke(
        wb,
        agent,
        "evidence.refine",
        {"run_id": run["id"], "episode": 0, "around_seconds": [1.0]},
    )
    # One summary line per instant, not one row per frame.
    assert set(refined["added"][0]) == {"around", "frames", "from", "to"}
    assert refined["added"][0]["frames"] > 0


def test_a_software_candidate_is_resolved_by_a_person_not_published(tmp_path):
    state = tmp_path / "outputs/LEVI/workbench"
    ledger = gap_ledger("temporal-a", "plates", episodes=1)
    ledger["tools"] = {"evidence.refine": {"calls": 10, "response_bytes": 340_400}}
    improvements.triage(state, ledger)
    slug = "oversized-evidence-refine-responses"
    with pytest.raises(PermissionError):
        improvements.transition(state, "plates", slug, "resolved", by="a", human=False)
    done = improvements.transition(
        state, "plates", slug, "resolved", by="dev", human=True, note="compact rows"
    )
    assert done["state"] == "resolved"
    # Recurrence after a fix is recorded, not reopened as a duplicate.
    ledger["run_id"] = "temporal-b"
    assert improvements.triage(state, ledger)[0]["outcome"] == "no_op"


def test_a_mosaic_page_names_each_frame_once(bench, dataset):
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run = temporal_run(wb, context, agent, dataset)
    page = invoke(
        wb,
        agent,
        "evidence.read",
        {"run_id": run["id"], "episode": 0, "layout": "mosaic", "ids": True},
    )
    # The sheet follows the items list; no per-tile index repeats it.
    assert "tiles" not in page["mosaic"] and page["mosaic"]["columns"] == 6
    assert len({i["id"] for i in page["items"]}) == len(page["items"])


def test_a_failure_reason_is_not_filed_as_doubt():
    assert memory.classify("It comes to rest on the pink plate.", "failure") == [
        "failure_reason"
    ]
    assert memory.classify("It comes to rest on the pink plate.", "unknown") == [
        "other"
    ]
    assert memory.classify("A human hand enters.", "failure") == ["human_intervention"]


def test_memory_rebuild_reproduces_what_closing_produced(bench, dataset):
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run = temporal_run(wb, context, agent, dataset)
    commit(wb, propose(wb, agent, run))
    invoke(wb, agent, "runs.get", {"run_id": run["id"]})
    key = wb.store.get("runs", run["id"])["dataset_key"]
    before = json.loads(layout.memory_path(wb.store.state, key).read_text())
    with pytest.raises(PermissionError):
        invoke(wb, agent, "memory.rebuild", {"repo_id": context.repo_id})
    human = Principal("operator", human=True)
    result = invoke(wb, human, "memory.rebuild", {"repo_id": context.repo_id})
    assert result["folded"] == [run["id"]]
    after = json.loads(layout.memory_path(wb.store.state, key).read_text())
    for field in ("episodes", "subtasks", "uncertainty", "sources"):
        assert after[field] == before[field]


def test_pending_teaching_on_an_unsupervised_task_is_empty_not_an_error(bench, dataset):
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run = temporal_run(wb, context, agent, dataset)
    value = invoke(wb, agent, "supervision.pending", {"run_id": run["id"]})
    assert value["items"] == [] and value["supervision"] == "none"


def test_disconnect_takes_back_only_its_own_mcp_entry(tmp_path, monkeypatch):
    from levi import paths
    from levi.agent import control

    state = tmp_path / "state"
    monkeypatch.setattr(paths, "STATE", state)
    folder = state / "agent" / "connections" / "claude-20260922T0843"
    folder.mkdir(parents=True)
    config = tmp_path / "project" / ".mcp.json"
    config.parent.mkdir()
    mine = {"env": {"LEVI_AGENT_GRANT_FILE": str(folder / "credential")}}
    config.write_text(json.dumps({"mcpServers": {"levi": mine, "other": {}}}))
    (folder / "connection.json").write_text(
        json.dumps(
            {
                "client": "claude",
                "grant_id": "20260922T0843",
                "project": str(config.parent),
                "config": str(config),
            }
        )
    )
    assert "removed" in control.forget_configuration("20260922T0843")["configuration"]
    assert json.loads(config.read_text()) == {"mcpServers": {"other": {}}}
    # An entry pointing at another credential is left alone.
    config.write_text(json.dumps({"mcpServers": {"levi": {"env": {}}}}))
    assert (
        "no levi entry"
        in control.forget_configuration("20260922T0843")["configuration"]
    )
    assert "levi" in json.loads(config.read_text())["mcpServers"]


def test_two_connections_in_one_minute_get_their_own_folders(tmp_path, monkeypatch):
    """A second connect in the same minute used to crash after its grant was
    issued, leaving a grant with no credential file."""
    import argparse

    from levi import paths
    from levi.agent import control

    state = tmp_path / "state"
    monkeypatch.setattr(paths, "STATE", state)
    issued = iter(["g1", "g2"])
    monkeypatch.setattr(
        control, "api", lambda *a, **k: {"id": next(issued), "token": "t"}
    )
    folders = []
    for name in ("one", "two"):
        project = tmp_path / name
        project.mkdir()
        control.connect(
            argparse.Namespace(
                client="claude",
                dataset=["local/x"],
                project=str(project),
                apply=True,
                hours=None,
                max_calls=None,
            )
        )
        folders = sorted(p.name for p in (state / "agent/connections").iterdir())
    assert len(folders) == 2 and folders[1].endswith("-2"), folders


def test_provenance_names_the_skills_and_harness_the_run_used(bench, dataset):
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run = temporal_run(wb, context, agent, dataset)
    receipt = propose(wb, agent, run)
    provenance = wb.store.get("changes", receipt["id"])["provenance"]
    assert "levi-overview@11" in provenance["skills_version"]
    assert provenance["harness"]["parameters"]["evidence.refine_top_k"] == 0


def test_concurrent_closures_of_one_dataset_keep_every_finding(tmp_path):
    """Two runs of the same dataset triaged at once: neither finding is lost."""
    import threading

    state = tmp_path / "outputs/LEVI/workbench"
    (state / "agent").mkdir(parents=True)
    improvements.triage(state, gap_ledger("temporal-0", "plates"))

    def close(run_id):
        with layout.harness_lock(state, "plates"):
            improvements.triage(state, gap_ledger(run_id, "plates"))

    threads = [
        threading.Thread(target=close, args=(f"temporal-{i}",)) for i in range(1, 9)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    candidate = improvements.load(state, "plates", "refine-at-coarse-change")
    assert sorted(e["run_id"] for e in candidate["evidence"]) == [
        f"temporal-{i}" for i in range(9)
    ]


def published_atoms(wb, run):
    folder = wb.store.bundle(run["dataset_key"])
    return json.loads((folder / "annotations/episode_000000.json").read_text())["atoms"]


def test_reannotating_an_episode_replaces_the_earlier_agent_segments(bench, dataset):
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    first = temporal_run(wb, context, agent, dataset)
    commit(wb, propose(wb, agent, first))
    atoms = published_atoms(wb, first)
    assert len(atoms) == 1
    # What the reviewer approved survives publication, not just the text.
    assert atoms[0]["levi"]["subtask_id"] == "grasp"
    assert atoms[0]["levi"]["outcome"] == "success"
    assert atoms[0]["levi"]["origin"]["run_id"] == first["id"]

    second = temporal_run(wb, context, agent, dataset)
    receipt = commit(wb, propose(wb, agent, second))
    atoms = published_atoms(wb, second)
    assert len(atoms) == 1, "the second run must not stack on the first"
    assert atoms[0]["levi"]["origin"]["run_id"] == second["id"]
    assert receipt["replaced"] == {"0": 1}


def test_a_segment_a_person_edited_is_never_replaced(bench, dataset):
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    first = temporal_run(wb, context, agent, dataset)
    commit(wb, propose(wb, agent, first))
    folder = wb.store.bundle(first["dataset_key"])
    path = folder / "annotations/episode_000000.json"
    data = json.loads(path.read_text())
    data["atoms"][0]["content"] = "Close on the plate (checked by hand)"
    data["atoms"].append(
        {
            "role": "assistant",
            "content": "Human note",
            "style": "subtask",
            "timestamp": 0.5,
            "to": 1.5,
            "camera": None,
        }
    )
    path.write_text(json.dumps(data))
    from levi.agent.supersede import split

    kept, replaced = split(
        data["atoms"], [{"kind": "segment", "style": "subtask"}], [], 0
    )
    assert replaced == [] and len(kept) == 2


def test_segments_published_before_origin_existed_are_matched_by_history():
    from levi.agent.supersede import split

    old = {
        "role": "assistant",
        "content": "Approach",
        "style": "subtask",
        "timestamp": 0.0,
        "to": 2.0,
        "camera": None,
    }
    human = {**old, "content": "Approach (human)"}
    history = [
        {
            "status": "committed",
            "decisions": {},
            "proposals": [
                {
                    "episode_index": 0,
                    "kind": "segment",
                    "content": "Approach",
                    "start": 0.0,
                    "end": 2.0,
                }
            ],
        }
    ]
    kept, replaced = split(
        [old, human], [{"kind": "segment", "style": "subtask"}], history, 0
    )
    assert replaced == [old] and kept == [human]


def test_a_committed_full_dataset_annotation_leaves_an_evaluation_record(
    bench, dataset, monkeypatch, tmp_path
):
    from levi.agent.store import resolve
    from levi.eval import record

    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run = temporal_run(wb, context, agent, dataset)
    # The fixture run covers episode 0 of a two-episode dataset; treat it as
    # the whole dataset so closing writes the record.
    monkeypatch.setattr(record, "eligible", lambda run: True)
    monkeypatch.setattr(record, "dataset_root", lambda name: dataset)
    monkeypatch.setattr(
        record,
        "annotations_dir",
        lambda name: resolve(wb.store.state, name, "annotations"),
    )
    commit(wb, propose(wb, agent, run))
    invoke(wb, agent, "runs.get", {"run_id": run["id"]})
    closed = wb.store.get("runs", run["id"])["closure"]
    path = closed["evaluation"]
    assert isinstance(path, str) and path.endswith("_external-mcp.md"), closed
    text = Path(path).read_text()
    assert "### 7. Agent 效率与成本" in text
    assert "子任务标注总数 | 1" in text
