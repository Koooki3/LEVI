import itertools

import pytest

"""Token and time cost as part of the harness: measured per agent, learned per dataset."""

# ruff: noqa: F811

import json

from test_agent_economy import bench  # noqa: F401
from test_harness import commit, gap_ledger, propose, temporal_run

from levi.agent.capabilities import invoke
from levi.agent.security import Principal
from levi.harness import cost, improvements, layout


def closed(wb, agent, run):
    invoke(wb, agent, "runs.get", {"run_id": run["id"]})
    key = wb.store.get("runs", run["id"])["dataset_key"]
    ledger = json.loads(
        (layout.task_dir(wb.store.state, key, run["id"]) / "ledger.json").read_text()
    )
    return key, ledger


def test_an_external_run_gets_a_lower_bound_then_its_report(bench, dataset):
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run = temporal_run(wb, context, agent, dataset)
    commit(wb, propose(wb, agent, run))
    _, ledger = closed(wb, agent, run)
    record = ledger["cost"]
    assert record["provider_kind"] == "external"
    assert record["tokens"]["source"] == "delivered_lower_bound"
    assert record["tokens"]["value"] == record["tokens"]["delivered"] > 0
    assert record["seconds"]["tool"] >= 0 and record["seconds"]["wall"] > 0
    invoke(
        wb,
        agent,
        "runs.report_usage",
        {"run_id": run["id"], "input_tokens": 5000, "output_tokens": 500},
    )
    _, ledger = closed(wb, agent, run)
    assert ledger["cost"]["tokens"]["source"] == "reported"
    profile = invoke(wb, agent, "cost.profile", {"repo_id": context.repo_id})
    runs = profile["profiles"][record["agent_key"]]["runs"]
    assert [r["run_id"] for r in runs] == [run["id"]], "refolding must not duplicate"
    assert runs[0]["tokens"]["value"] == 5500


def test_a_model_run_is_costed_from_what_levi_metered():
    ledger = gap_ledger("temporal-m", "plates", episodes=2)
    ledger.update(
        workflow="temporal",
        tokens={"delivered": {"estimated_input_tokens": 0}, "reported": None},
    )
    ledger["episodes"] = [{"evidence_frames": 10}, {"evidence_frames": 10}]
    run = {
        "context": {"provider": "qwen-local", "workflow": {"kind": "temporal"}},
        "provider_config": {"kind": "ollama", "model": "qwen3.5:4b", "name": "qwen"},
        "tokens": 9000,
        "elapsed_seconds": 6.0,
    }
    record = cost.of(ledger, run, [])
    assert record["provider_kind"] == "ollama"
    assert record["agent_key"] == "model:qwen3.5:4b"
    assert record["tokens"]["source"] == "metered"
    assert record["tokens"]["per_episode"] == 4500
    assert record["tokens"]["per_frame"] == 450.0
    assert record["seconds"]["model"] == 6.0


def cost_ledger(run_id, tokens, seconds, key="plates", agent="external:mcp"):
    ledger = gap_ledger(run_id, key, episodes=2)
    ledger["cost"] = {
        "agent_key": agent,
        "tokens": {"per_episode": tokens, "source": "reported", "value": tokens * 2},
        "seconds": {"per_episode": seconds, "wall": seconds * 2},
        "breakdown": [{"part": "evidence images", "tokens": 1, "share": 0.5}],
        "harness": {},
        "model": None,
        "provider_kind": "external",
        "waste": {"duplicate_evidence_reads": 0},
    }
    return ledger


def test_a_cost_rise_against_this_datasets_history_is_filed(tmp_path):
    state = tmp_path / "outputs/LEVI/workbench"
    profiles = {}
    for i, tokens in enumerate([10000, 11000]):
        cost.fold(
            profiles,
            cost_ledger(f"temporal-{i}", tokens, 50)["cost"]
            | {"run_id": f"temporal-{i}"},
        )
    _, before = cost.fold(
        profiles,
        cost_ledger("temporal-9", 20000, 50)["cost"] | {"run_id": "temporal-9"},
    )
    assert before == {
        "tokens_per_episode": 10500.0,
        "seconds_per_episode": 50.0,
        "runs": 2,
    }
    outcome = improvements.triage(
        state, cost_ledger("temporal-9", 20000, 50), before, None
    )
    assert {"slug": "cost-rise-external-mcp", "outcome": "proposed"} in outcome
    candidate = improvements.load(state, "plates", "cost-rise-external-mcp")
    assert candidate["kind"] == "cost"
    # One earlier run is not a baseline.
    quiet = improvements.triage(
        state,
        cost_ledger("temporal-8", 90000, 50, key="cups"),
        {"tokens_per_episode": 100, "runs": 1},
        None,
    )
    assert not any(r.get("slug", "").startswith("cost-rise") for r in quiet)


def test_narrower_sheets_are_proposed_only_down_to_a_proven_width(tmp_path):
    state = tmp_path / "outputs/LEVI/workbench"
    outcome = improvements.triage(state, cost_ledger("temporal-1", 1, 1), {}, 200)
    assert {"slug": "narrower-contact-sheets", "outcome": "proposed"} in outcome
    candidate = improvements.load(state, "plates", "narrower-contact-sheets")
    assert candidate["target"] == {
        "parameter": "evidence.mosaic_tile_width",
        "value": 200,
    }
    # Nothing proven narrower than the default: nothing proposed.
    none = improvements.triage(
        state, cost_ledger("temporal-2", 1, 1, key="cups"), {}, None
    )
    assert not any(r.get("slug") == "narrower-contact-sheets" for r in none)


def test_the_tile_width_candidate_is_evaluated_against_what_reviewers_accepted(
    bench, dataset
):
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run = temporal_run(wb, context, agent, dataset)
    invoke(
        wb,
        agent,
        "evidence.read",
        {"run_id": run["id"], "episode": 0, "layout": "mosaic", "tile_width": 200},
    )
    commit(wb, propose(wb, agent, run))
    closed(wb, agent, run)
    triage = wb.store.get("runs", run["id"])["closure"]["triage"]
    assert {"slug": "narrower-contact-sheets", "outcome": "proposed"} in triage
    invoke(
        wb,
        agent,
        "improvements.transition",
        {
            "repo_id": context.repo_id,
            "slug": "narrower-contact-sheets",
            "to": "evaluating",
        },
    )
    result = invoke(
        wb,
        agent,
        "improvements.evaluate",
        {"repo_id": context.repo_id, "slug": "narrower-contact-sheets"},
    )
    assert result["passed"] and result["legible_floor"] == 200
    assert result["image_token_savings"] > 0.5


def test_a_published_tile_width_is_the_default_read_width(bench, dataset):
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    first = temporal_run(wb, context, agent, dataset)
    key = first["dataset_key"]
    improvements.file_finding(
        wb.store.state,
        key,
        slug="narrower-contact-sheets",
        kind="harness",
        title="t",
        finding={"run_id": "r"},
        target={"parameter": "evidence.mosaic_tile_width", "value": 200},
    )
    for to, human in (
        ("evaluating", False),
        ("qualified", False),
        ("awaiting_authorization", False),
        ("published", True),
    ):
        improvements.transition(
            wb.store.state,
            key,
            "narrower-contact-sheets",
            to,
            by="t",
            human=human,
            evaluation={"passed": True} if to == "qualified" else None,
        )
    run = temporal_run(wb, context, agent, dataset)
    page = invoke(
        wb,
        agent,
        "evidence.read",
        {"run_id": run["id"], "episode": 0, "layout": "mosaic"},
    )
    assert page["mosaic"]["tile_width"] == 200
    assert "-w200" in page["mosaic"]["artifact"]
    prepared = invoke(wb, agent, "runs.prepare", {"run_id": run["id"]})
    assert prepared["read_with"]["tile_width"] == 200


def test_the_in_process_runtime_refines_the_published_windows_within_the_cap(
    bench, dataset
):
    from levi.agent.observations import harness_windows

    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run = temporal_run(wb, context, agent, dataset)
    wb.store.mutate(
        "runs",
        run["id"],
        lambda r: r["harness"]["parameters"].update({"evidence.refine_top_k": 3}),
    )
    run = wb.store.get("runs", run["id"])
    boundaries, windows = harness_windows(wb, run, 0, [])
    assert 1 <= len(windows) <= 3 and len(boundaries) == len(windows)
    # A cap too small for every window drops windows, never fails the run.
    wb.store.mutate(
        "runs",
        run["id"],
        lambda r: r["context"]["workflow"].update({"max_evidence_frames": 8}),
    )
    boundaries, windows = harness_windows(wb, wb.store.get("runs", run["id"]), 0, [])
    assert len(windows) < 3


def test_refinement_fits_the_models_context_window(bench, dataset):
    """A local model's context holds so many images: windows go first, then
    boundary sampling coarsens, and past that the run says why it stopped."""
    from levi.agent.observations import (
        Candidate,
        ContextOverflow,
        fit_refinement,
        image_limit,
        refine_spacings,
    )
    from levi.agent.schema import ProviderConfig, TaskContext

    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run = temporal_run(wb, context, agent, dataset)
    wb.store.mutate(
        "runs",
        run["id"],
        lambda r: r["harness"]["parameters"].update({"evidence.refine_top_k": 3}),
    )
    run = wb.store.get("runs", run["id"])
    config = ProviderConfig(
        name="local",
        kind="ollama",
        base_url="http://127.0.0.1:1",
        model="m",
        context_tokens=32768,
    )
    evidence = [{"artifact": f"f{i}.png"} for i in range(20)]
    # The real coarse call: 6842 tokens for 7 images of 640x480 and ~16k
    # characters of prompt -- most of it text, so images cost far less.
    evidence = evidence[:7]
    blind = image_limit(config, {"reported_tokens": 6842}, evidence, {})
    split = image_limit(
        config, {"reported_tokens": 6842, "prompt_chars": 16000}, evidence, {}
    )
    assert 15 < blind < split
    assert image_limit(config, {"reported_tokens": None}, evidence, {}) is None
    finest = refine_spacings(TaskContext.model_validate(run["context"]))[0]
    _, windows, spacing = fit_refinement(wb, run, 0, [Candidate(1.0)], None)
    assert spacing == finest and windows
    # Room for 10 images: windows are trimmed and the spacing coarsened
    # until the model's own boundary fits; at 4 nothing does.
    _, windows, spacing = fit_refinement(wb, run, 0, [Candidate(1.0)], 10)
    assert len(windows) <= 1 and spacing > finest
    with pytest.raises(ContextOverflow, match="does not fit the frame cap"):
        fit_refinement(wb, run, 0, [Candidate(1.0)], 4)
    # The plan's frame cap coarsens the same way, whatever the context.
    wb.store.mutate(
        "runs",
        run["id"],
        lambda r: r["context"]["workflow"].update({"max_evidence_frames": 10}),
    )
    run = wb.store.get("runs", run["id"])
    _, _, spacing = fit_refinement(wb, run, 0, [Candidate(1.0)], None)
    assert spacing > finest


def test_a_long_draft_is_refined_in_batches_that_each_fit(bench, dataset):
    from levi.agent.observations import Batch, Candidate, merge, plan_refinement
    from levi.agent.schema import ModelOutput

    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run = temporal_run(wb, context, agent, dataset)
    draft = [Candidate(t) for t in (0.0, 0.4, 0.8, 1.2)]
    whole = plan_refinement(wb, run, 0, draft, None, 0)
    assert len(whole) == 1 and whole[0].start is None
    batches = plan_refinement(wb, run, 0, draft, 6, 0)
    assert len(batches) > 1
    # Consecutive windows that partition the draft, every attempt in one.
    assert [b.start for b in batches] == sorted(b.start for b in batches)
    assert sum(len(b.boundaries) for b in batches) == len(draft)
    assert all(b.end == nxt.start for b, nxt in itertools.pairwise(batches))

    def seg(start, end, content):
        return {
            "episode_index": 0,
            "kind": "segment",
            "subtask_id": "other",
            "content": content,
            "start": start,
            "end": end,
            "outcome": "unknown",
            "evidence_ids": ["e"],
        }

    old = ModelOutput(summary="d", proposals=[seg(0, 1, "a"), seg(1, 2, "b")])
    part = ModelOutput(summary="p", proposals=[seg(1.1, 2, "B"), seg(0, 0.5, "x")])
    merged = merge(old, part, Batch([], [], 0.1, 1.0, float("inf")))
    assert [p.content for p in merged.proposals] == ["a", "B"]
    # A refined batch starting earlier than the previous interval ended:
    # the seam moves to where the later batch put it.
    early = ModelOutput(summary="p", proposals=[seg(0.8, 2, "C")])
    merged = merge(old, early, Batch([], [], 0.1, 0.5, float("inf")))
    assert [(p.start, p.end) for p in merged.proposals] == [(0, 0.8), (0.8, 2)]


def test_the_estimate_uses_this_datasets_own_cost(bench, dataset):
    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run = temporal_run(wb, context, agent, dataset)
    commit(wb, propose(wb, agent, run))
    invoke(
        wb,
        agent,
        "runs.report_usage",
        {"run_id": run["id"], "input_tokens": 4000, "output_tokens": 0},
    )
    closed(wb, agent, run)
    following = temporal_run(wb, context, agent, dataset)
    estimate = invoke(wb, agent, "plans.estimate", {"run_id": following["id"]})
    assert estimate["local"]["tokens_per_episode"] == 4000
    assert "cost_profiles" in following["harness"]["memory"]


def test_sweep_closes_what_ended_unnoticed_and_retries_a_failure_once(bench, dataset):
    from levi.harness import closure

    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run = temporal_run(wb, context, agent, dataset)
    commit(wb, propose(wb, agent, run))  # committed outside invoke: not closed yet
    assert not wb.store.get("runs", run["id"]).get("closure")
    assert closure.sweep(wb.store) == [run["id"]]
    assert wb.store.get("runs", run["id"])["closure"]["state"] == "closed"
    assert closure.sweep(wb.store) == []
    # A failed closing is retried once, then left for a person.
    wb.store.mutate(
        "runs",
        run["id"],
        lambda r: r.update(
            closure={"state": "failed", "reason": "x", "error": "boom", "attempts": 1}
        ),
    )
    assert closure.sweep(wb.store) == [run["id"]]
    wb.store.mutate(
        "runs",
        run["id"],
        lambda r: r.update(
            closure={"state": "failed", "reason": "x", "error": "boom", "attempts": 2}
        ),
    )
    assert closure.sweep(wb.store) == []


def test_rebuild_keeps_lessons_and_never_writes_into_a_ledger(bench, dataset):
    from levi.harness import memory

    wb, context = bench
    agent = Principal("conn", datasets=(context.repo_id,))
    run = temporal_run(wb, context, agent, dataset)
    commit(wb, propose(wb, agent, run))
    key, before = closed(wb, agent, run)
    memory.record_lesson(
        wb.store.state, key, {"slug": "s", "text": "t", "status": "retained"}
    )
    result = memory.rebuild(wb.store, key)
    assert result["path"] == str(layout.memory_path(wb.store.state, key))
    assert [x["slug"] for x in memory.load(wb.store.state, key)["lessons"]] == ["s"]
    _, after = closed(wb, agent, run)
    assert after["schema"] == "levi.harness.ledger.v1"
    assert after["run_id"] == before["run_id"]
