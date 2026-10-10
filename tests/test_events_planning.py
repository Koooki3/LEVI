"""Event intelligence in a plan and a run (T-A-08, T-A-09).

``workflow.event_intelligence`` is frozen in the approved plan; a plan
without it freezes, caches and estimates exactly what it did before the
block existed (byte for byte, against a snapshot taken on main). With it, a
temporal run adds windows around event candidates of the recorded signals
to its boundary refinement, within the same frame cap and with the same
requests; the model's prompt does not change.
"""

import json

import cv2
import numpy as np
import pandas as pd
import pytest
from events_plan_snapshot import CONTEXTS, snapshot

from levi.agent.capabilities import invoke
from levi.agent.planning import EventIntelligence, Workflow, approve, material
from levi.agent.runtime import Workbench
from levi.agent.schema import ModelOutput, ProviderConfig, TaskContext
from levi.agent.security import Principal
from levi.agent.store import Conflict, digest
from levi.events import sampling

FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures" / "events"
ON = {"mode": "candidates"}


# ------------------------------------------------------------ the contract


def test_a_plan_without_event_intelligence_freezes_what_it_froze_before():
    """Context, clarify workflow, cache context and material digest of six
    plans, byte for byte as on main before the block existed."""
    before = json.loads((FIXTURES / "plan-material-before.json").read_text())
    assert snapshot() == before


@pytest.mark.parametrize(
    "off", [None, {"mode": "off"}, {"mode": "off", "max_windows": 9}]
)
def test_off_in_any_spelling_is_stored_as_no_key(off, monkeypatch):
    import events_plan_snapshot as module

    before = json.loads((FIXTURES / "plan-material-before.json").read_text())
    spelled = {
        name: {
            **raw,
            "workflow": {**(raw.get("workflow") or {}), "event_intelligence": off},
        }
        for name, raw in CONTEXTS.items()
        if (raw.get("workflow") or {}).get("kind") == "temporal"
    }
    monkeypatch.setattr(module, "CONTEXTS", spelled)
    after = module.snapshot()
    assert after == {name: before[name] for name in spelled}
    for name in spelled:
        assert "event_intelligence" not in after[name]["context"]


def context(**workflow):
    return TaskContext.model_validate(
        {
            **CONTEXTS["temporal-model"],
            "workflow": {**CONTEXTS["temporal-model"]["workflow"], **workflow},
        }
    )


def test_on_freezes_every_setting_in_the_context_and_the_digest():
    from events_plan_snapshot import run_of

    plain = context().model_dump()
    on = context(event_intelligence=ON).model_dump()
    block = on["workflow"]["event_intelligence"]
    # Defaults are written out: the plan approves values, not "whatever the
    # code defaults to later".
    assert block == {
        "mode": "candidates",
        "sources": ["gripper", "height", "still", "change_point"],
        "max_windows": 4,
        "merge_seconds": 0.5,
        "change_point_penalty": 0.75,
        "planner": "greedy",
        "active_evidence": False,
    }
    assert digest(material(run_of(on))) != digest(material(run_of(plain)))
    tuned = context(event_intelligence={**ON, "max_windows": 2}).model_dump()
    assert digest(material(run_of(tuned))) != digest(material(run_of(on)))
    # The model cache fingerprints the context: an on run never reuses an
    # off run's answer, and off is unchanged (see the snapshot test).
    assert TaskContext.model_validate(on).model_dump(exclude={"budget"}) != (
        TaskContext.model_validate(plain).model_dump(exclude={"budget"})
    )


@pytest.mark.parametrize(
    ("workflow", "message"),
    [
        ({"kind": "review", "event_intelligence": ON}, "temporal"),
        ({"kind": "objects", "event_intelligence": ON}, "temporal"),
        (
            {"kind": "temporal", "event_intelligence": {**ON, "active_evidence": True}},
            "",
        ),
        (
            {"kind": "temporal", "event_intelligence": {**ON, "sources": ["vision"]}},
            "sources",
        ),
        (
            {
                "kind": "temporal",
                "event_intelligence": {**ON, "sources": ["gripper"] * 2},
            },
            "distinct",
        ),
        (
            {"kind": "temporal", "event_intelligence": {**ON, "max_windows": 0}},
            "max_windows",
        ),
        (
            {"kind": "temporal", "event_intelligence": {**ON, "surprise": 1}},
            "surprise",
        ),
    ],
)
def test_a_block_outside_the_contract_is_refused(workflow, message):
    with pytest.raises(ValueError, match=message):
        Workflow.model_validate(workflow)


def test_the_estimate_says_what_it_adds_and_an_off_plan_says_nothing():
    from levi.agent.planning import attach

    def planned(ctx):
        return attach(
            {
                "context": ctx.model_dump(),
                "provider_config": {},
                "base_revision": "r",
                "base_content": "0" * 64,
                "planned_hashes": {},
                "files": [],
            }
        )["plan"]

    off = planned(context())
    on = planned(context(event_intelligence={**ON, "max_windows": 3}))
    assert (
        "event_intelligence" not in off and "event_intelligence" not in off["estimate"]
    )
    assert on["estimate"]["minimum_requests"] == off["estimate"]["minimum_requests"]
    assert on["estimate"]["basis"].startswith(off["estimate"]["basis"])
    assert "no extra request" in on["estimate"]["basis"]
    extra = on["estimate"]["event_intelligence"]
    # 1 s window either side at 0.1 s (half the 0.2 s tolerance): 21 frames.
    assert extra == {
        "extra_requests": 0,
        "max_windows_per_episode": 3,
        "frames_per_window": 21,
        "max_extra_frames_per_episode": 63,
        "frames_cap": 96,
    }
    assert on["event_intelligence"]["max_windows"] == 3


# ------------------------------------------------------------ a dataset with signals

FPS, FRAMES, CAMERA = 10, 60, "observation.images.front"
# Gripper (1 = open) closes at 2.0 s and opens at 4.5 s; the arm goes down
# from 1 s to 2 s and back up from 3 s to 4 s.
CLOSE_AT, OPEN_AT = 2.0, 4.5


def signals(t):
    z = np.interp(t, [0, 1, 2, 3, 4, 6], [0.3, 0.3, 0.05, 0.05, 0.3, 0.3])
    x = np.interp(t, [0, 6], [0.4, 0.6])
    grip = np.where((t >= CLOSE_AT) & (t < OPEN_AT), 0.0, 1.0)
    return np.stack([x, np.full_like(t, 0.1), z, grip], axis=1)


@pytest.fixture
def robot(tmp_path):
    root = tmp_path / "robot"
    (root / "meta").mkdir(parents=True)
    (root / "data/chunk-000").mkdir(parents=True)
    names = ["x", "y", "z", "gripper"]
    info = {
        "codebase_version": "v2.1",
        "robot_type": "fr3",
        "fps": FPS,
        "total_episodes": 1,
        "total_frames": FRAMES,
        "total_tasks": 1,
        "chunks_size": 1000,
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
        "features": {
            "action": {"dtype": "float32", "shape": [4], "names": names},
            "observation.state": {"dtype": "float32", "shape": [4], "names": names},
            CAMERA: {"dtype": "video", "shape": [48, 64, 3]},
            "timestamp": {"dtype": "float32", "shape": [1], "names": None},
            "episode_index": {"dtype": "int64", "shape": [1], "names": None},
            "frame_index": {"dtype": "int64", "shape": [1], "names": None},
            "index": {"dtype": "int64", "shape": [1], "names": None},
            "task_index": {"dtype": "int64", "shape": [1], "names": None},
        },
    }
    (root / "meta/info.json").write_text(json.dumps(info))
    (root / "meta/tasks.jsonl").write_text(
        json.dumps({"task_index": 0, "task": "Pick the plate"}) + "\n"
    )
    (root / "meta/episodes.jsonl").write_text(
        json.dumps({"episode_index": 0, "length": FRAMES, "tasks": ["Pick the plate"]})
        + "\n"
    )
    t = np.arange(FRAMES, dtype=float) / FPS
    state = signals(t)
    pd.DataFrame(
        {
            "action": [list(map(float, row)) for row in state],
            "observation.state": [list(map(float, row)) for row in state],
            "timestamp": t,
            "episode_index": [0] * FRAMES,
            "frame_index": range(FRAMES),
            "index": range(FRAMES),
            "task_index": [0] * FRAMES,
        }
    ).to_parquet(root / "data/chunk-000/episode_000000.parquet")
    folder = root / f"videos/chunk-000/{CAMERA}"
    folder.mkdir(parents=True)
    writer = cv2.VideoWriter(
        str(folder / "episode_000000.mp4"),
        cv2.VideoWriter_fourcc(*"mp4v"),
        FPS,
        (64, 48),
    )
    assert writer.isOpened()
    for i in range(FRAMES):
        writer.write(np.full((48, 64, 3), (i * 4) % 256, np.uint8))
    writer.release()
    return root


class Segmenter:
    """One segment over the whole episode, citing every frame it was shown;
    records what each request carried."""

    def __init__(self):
        self.calls = []

    def generate(self, config, instruction, summary, evidence, artifacts, budget):
        self.calls.append({"summary": summary, "evidence": list(evidence)})
        return ModelOutput.model_validate(
            {
                "summary": "fixture",
                "proposals": [
                    {
                        "kind": "segment",
                        "episode_index": summary["episode_index"],
                        "content": "Pick",
                        "subtask_id": "grasp",
                        "start": summary["start"],
                        "end": summary["end"],
                        "outcome": "unknown",
                        "uncertainty": "fixture",
                        "evidence_ids": [evidence[0]["id"]],
                    }
                ],
            }
        ), {"requests": 1, "tokens": 20}


@pytest.fixture
def lab(client, robot, monkeypatch):
    monkeypatch.delenv("LEVI_MODEL_API_KEY", raising=False)
    from levi import catalog, service

    entry = catalog.register(str(robot))
    provider = Segmenter()
    wb = Workbench(service.STATE, provider=provider)
    wb.store.put(
        "providers",
        "fixture",
        ProviderConfig(
            name="fixture",
            base_url="https://example.invalid/v1",
            model="fixture",
            vision=True,
            tools=True,
        ).model_dump(),
    )
    return wb, entry["id"], provider


GRASP = CONTEXTS["temporal-model"]["workflow"]["definitions"][0]


def temporal(repo, provider="fixture", **workflow):
    return TaskContext.model_validate(
        {
            "repo_id": repo,
            "episodes": [0],
            "instruction": "Annotate the subtasks",
            "provider": provider,
            "cameras": [CAMERA],
            "allow_media_egress": True,
            "workflow": {
                "kind": "temporal",
                "definitions": [GRASP],
                "coarse_step_seconds": 2.0,
                **workflow,
            },
        }
    )


def run_once(wb, ctx):
    run = approve(wb, wb.plan(ctx)["id"], 1, "fixture-human")
    assert wb.store.claim(run["id"], "test-owner")
    wb.execute(run["id"], "test-owner", pilot=True)
    return wb.store.get("runs", run["id"])


def events_of(wb, run, kind):
    return [e for e in wb.store.events(run["id"]) if e["type"] == kind]


def near(rows, t, slack=0.06):
    return [r for r in rows if abs(r["timestamp"] - t) <= slack]


def test_an_on_run_refines_around_the_signal_events_with_the_same_requests(lab):
    wb, repo, provider = lab
    run = run_once(wb, temporal(repo, event_intelligence=ON))
    assert run["status"] == "waiting_for_review", run.get("reason")
    # Coarse plus one refinement, as without event intelligence.
    assert run["requests"] == 2 and len(provider.calls) == 2
    refined = provider.calls[1]["evidence"]
    # The gripper events are 2.0 and 4.5 s: the draft's boundaries are 0 and
    # 5.9 s, so these dense frames come from the candidates.
    for t in (CLOSE_AT, OPEN_AT):
        assert len(near(refined, t, 0.5)) >= 8, t
    (found,) = events_of(wb, run, "event_candidates")
    assert found["proposed"] >= 2 and found["error"] is None
    (plan,) = events_of(wb, run, "event_evidence_plan")
    kinds = {
        row["event_type"] for row in plan["accepted"] if row["tier"] == "candidates"
    }
    assert {"gripper_close", "gripper_open"} <= kinds
    assert plan["images"] <= plan["budget_images"]
    ledger = wb.store.get("evidence", f"{run['id']}:0")["items"]
    assert len(ledger) <= 96
    # The model is never told about the policy or shown the candidates.
    for call in provider.calls:
        assert "event_intelligence" not in call["summary"]["workflow"]
        assert "candidates" not in json.dumps(call["summary"]["workflow"])
    # The result's provenance names the settings and how frames were shared.
    change = wb.store.get("changes", run["changes"])
    provenance = change["provenance"]["event_intelligence"]
    assert provenance["settings"] == run["context"]["workflow"]["event_intelligence"]
    episode = provenance["episodes"]["0"]
    assert episode["candidates"]["proposed"] >= 2
    assert episode["candidates"]["error_code"] is None
    assert episode["batches"][0]["accepted"]
    assert provenance["summary"]["with_candidates"] == 1


def test_the_model_cache_key_carries_the_block_only_when_it_is_on(lab, monkeypatch):
    """Measured on real runs: the fingerprint a model answer is cached under
    includes the plan's context (so on and off never share an answer) and
    the request summary, whose workflow never names the block."""
    from levi.agent import runtime

    wb, repo, _ = lab
    seen = []
    original = runtime.digest

    def track(value):
        if isinstance(value, dict) and {"evidence", "config", "context"} <= set(value):
            seen.append(value)
        return original(value)

    monkeypatch.setattr(runtime, "digest", track)
    run_once(wb, temporal(repo))
    off = list(seen)
    seen.clear()
    run_once(wb, temporal(repo, event_intelligence=ON))
    on = list(seen)
    assert len(off) == len(on) == 2
    for value in off:
        assert "event_intelligence" not in value["context"]["workflow"]
    for value in on:
        assert (
            value["context"]["workflow"]["event_intelligence"]["mode"] == "candidates"
        )
    for value in off + on:
        assert "event_intelligence" not in value["summary"]["workflow"]
    # Same snapshot, same draft: the coarse requests differ only by the plan.
    assert off[0]["summary"] == on[0]["summary"]
    assert off[0]["evidence"] == on[0]["evidence"]
    assert original(off[0]) != original(on[0])


def test_the_same_episode_gets_the_same_plan_again(lab):
    """Candidates and the plan are rebuilt from the snapshot, so a repeated
    run reads the same frames -- under the same image limit (here none). The
    plan depends on the limit, which a local model's fitted request cost
    moves between requests: see the resumed-refinement test above."""

    def plans(run):
        return [
            {k: v for k, v in e.items() if k not in {"seq", "time"}}
            for e in events_of(wb, run, "event_evidence_plan")
        ]

    wb, repo, _ = lab
    first = run_once(wb, temporal(repo, event_intelligence=ON))
    second = run_once(wb, temporal(repo, event_intelligence=ON))
    assert plans(first) and plans(first) == plans(second)


class FailsFirstRefinement(Segmenter):
    """The first refinement request fails (a provider error after its frames
    were persisted); everything else answers."""

    def __init__(self):
        super().__init__()
        self.failed = False

    def generate(self, config, instruction, summary, evidence, artifacts, budget):
        if summary.get("phase") == "boundary_refinement" and not self.failed:
            self.failed = True
            raise RuntimeError("provider went away")
        return super().generate(
            config, instruction, summary, evidence, artifacts, budget
        )


def test_a_resumed_refinement_under_another_image_limit_stays_within_the_cap(
    lab, monkeypatch
):
    """Review A3, I-1. Draft windows take 22 frames; the gripper windows add
    20 (2.0 s) and 14 (4.5 s); cap 50, 4 coarse frames. Under an image limit
    of 40 only the 4.5 s window fits; resumed without a limit, the planner
    alone would take the 2.0 s one instead: 22 + 20 + 14 = 56 frames in the
    ledger, past the cap, and the episode would fail on every resume. Planned
    against the ledger it keeps what it holds and the run completes."""
    from levi.agent import observations

    wb, repo, _ = lab
    provider = FailsFirstRefinement()
    wb.provider = provider
    limits = iter([40])
    monkeypatch.setattr(observations, "image_limit", lambda *a, **k: next(limits, None))
    ctx = temporal(repo, event_intelligence=ON, max_evidence_frames=50)
    run = run_once(wb, ctx)
    assert run["status"] == "blocked" and provider.failed
    first = {r["id"] for r in wb.store.get("evidence", f"{run['id']}:0")["items"]}
    # 36 refinement frames; three of the four coarse ones are among them.
    assert len(first) == 37
    assert wb.store.claim(run["id"], "test-owner")
    wb.execute(run["id"], "test-owner", pilot=True)
    run = wb.store.get("runs", run["id"])
    assert run["status"] == "waiting_for_review", run.get("reason")
    ledger = wb.store.get("evidence", f"{run['id']}:0")["items"]
    assert len(ledger) <= 50
    plans = events_of(wb, run, "event_evidence_plan")
    assert [p["images"] for p in plans] == [36, 36]
    # The frames depend on the image limit: a fresh run without one reads
    # the 2.0 s window, not the 4.5 s one (the same frames only under the
    # same limit).
    wb.provider = Segmenter()
    fresh = run_once(wb, ctx)
    (plan,) = events_of(wb, fresh, "event_evidence_plan")
    taken = {row["at"] for row in plan["accepted"]}
    assert 2.0 in taken and 4.5 not in taken


def test_an_off_run_reads_no_candidates_and_records_nothing_new(lab):
    wb, repo, provider = lab
    run = run_once(wb, temporal(repo))
    assert run["status"] == "waiting_for_review"
    assert run["requests"] == 2
    assert not events_of(wb, run, "event_candidates")
    assert not events_of(wb, run, "event_evidence_plan")
    shard = wb.store.get("shards", f"{run['id']}:0")
    assert set(shard) == {"output", "usage"}
    change = wb.store.get("changes", run["changes"])
    assert "event_intelligence" not in change["provenance"]
    refined = provider.calls[1]["evidence"]
    assert not near(refined, CLOSE_AT, 0.3) or len(near(refined, CLOSE_AT, 0.3)) < 4


def test_a_small_cap_skips_candidate_windows_but_never_the_draft(lab):
    wb, repo, _ = lab
    # 4 coarse frames, then 30 left: the draft's two boundary windows take
    # 10 + 11, so at most one 21-frame candidate window would not fit.
    run = run_once(wb, temporal(repo, event_intelligence=ON, max_evidence_frames=34))
    assert run["status"] == "waiting_for_review", run.get("reason")
    (plan,) = events_of(wb, run, "event_evidence_plan")
    assert plan["counts"]["skipped"] >= 1
    assert {row["reason"] for row in plan["skipped"]} <= {"budget", "max_windows"}
    assert plan["images"] <= plan["budget_images"] <= 34
    assert len(wb.store.get("evidence", f"{run['id']}:0")["items"]) <= 34


def test_max_windows_bounds_the_candidate_windows(lab):
    wb, repo, _ = lab
    run = run_once(wb, temporal(repo, event_intelligence={**ON, "max_windows": 1}))
    (plan,) = events_of(wb, run, "event_evidence_plan")
    taken = [row for row in plan["accepted"] if row["tier"] == "candidates"]
    assert len(taken) == 1 and taken[0]["event_type"].startswith("gripper_")
    assert any(row["reason"] == "max_windows" for row in plan["skipped"])


def test_the_call_budget_still_stops_the_run(lab):
    wb, repo, provider = lab
    ctx = temporal(repo, event_intelligence=ON)
    ctx = ctx.model_copy(
        update={"budget": ctx.budget.model_copy(update={"max_calls": 1})}
    )
    run = run_once(wb, ctx)
    assert run["status"] == "blocked" and "budget" in run["reason"]
    assert run["requests"] <= 1 and len(provider.calls) == 1


def test_changing_the_block_after_approval_needs_a_new_approval(lab):
    from levi.agent.planning import require

    wb, repo, _ = lab
    run = approve(wb, wb.plan(temporal(repo, event_intelligence=ON))["id"], 1, "h")
    require(wb, run)
    wb.store.mutate(
        "runs",
        run["id"],
        lambda r: r["context"]["workflow"]["event_intelligence"].update(max_windows=9),
    )
    with pytest.raises(Conflict, match="approved again"):
        require(wb, wb.store.get("runs", run["id"]))
    # Turning it on in an approved off plan is the same change.
    plain = approve(wb, wb.plan(temporal(repo))["id"], 1, "h")
    wb.store.mutate(
        "runs",
        plain["id"],
        lambda r: r["context"]["workflow"].update(event_intelligence=dict(ON)),
    )
    with pytest.raises(Conflict, match="approved again"):
        require(wb, wb.store.get("runs", plain["id"]))


def test_a_stored_plan_from_before_the_block_still_runs(lab):
    """An approved, not yet executed plan written by the old code: its
    stored context has no key; require() accepts it and the run does not
    read candidates."""
    from levi.agent.planning import require

    wb, repo, _ = lab
    run = approve(wb, wb.plan(temporal(repo))["id"], 1, "h")
    stored = wb.store.get("runs", run["id"])
    assert "event_intelligence" not in stored["context"]["workflow"]
    require(wb, stored)
    assert wb.store.claim(run["id"], "test-owner")
    wb.execute(run["id"], "test-owner", pilot=True)
    assert wb.store.get("runs", run["id"])["status"] == "waiting_for_review"
    assert not events_of(wb, run, "event_candidates")


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (FileNotFoundError("/home/someone/runs/x/input/meta/info.json"), "unreadable"),
        (ValueError("no such column"), "invalid_signals"),
        (RuntimeError("a bug"), "internal_error"),
    ],
)
def test_signals_that_cannot_be_read_are_said_so_in_shard_and_provenance(
    lab, monkeypatch, error, code
):
    """The run goes on from its pictures, but an 'on' result whose
    candidates were never read is named as such (review A3, I-2): in the
    run event, the shard and the change set's provenance, with a reason
    code; an external agent gets the code and the error's type, no path."""
    from levi.events import candidates

    def broken(*args, **kwargs):
        raise error

    monkeypatch.setattr(candidates, "read", broken)
    wb, repo, _ = lab
    run = run_once(wb, temporal(repo, event_intelligence=ON))
    assert run["status"] == "waiting_for_review"
    (found,) = events_of(wb, run, "event_candidates")
    assert found["proposed"] == 0 and found["error_code"] == code
    shard = wb.store.get("shards", f"{run['id']}:0")
    assert shard["event_plan"]["candidates"]["error_code"] == code
    assert shard["event_plan"]["candidates"]["proposed"] == 0
    provenance = wb.store.get("changes", run["changes"])["provenance"]
    summary = provenance["event_intelligence"]["summary"]
    assert summary == {
        "episodes": 1,
        "with_candidates": 0,
        "candidates_unavailable": 1,
        "reasons": {code: 1},
    }
    assert (
        provenance["event_intelligence"]["episodes"]["0"]["candidates"]["error_code"]
        == code
    )
    assert "/home/" not in json.dumps(provenance)
    agent, external = external_run(wb, repo, event_intelligence=ON)
    invoke(wb, agent, "runs.prepare", {"run_id": external["id"]})
    value = invoke(
        wb, agent, "events.candidates", {"run_id": external["id"], "episode": 0}
    )
    assert value["error_code"] == code
    assert value["error"] == type(error).__name__


def test_an_episode_whose_signals_say_nothing_is_counted_too(lab, monkeypatch):
    from levi.events import candidates

    monkeypatch.setattr(candidates, "read", lambda *a, **k: [])
    wb, repo, _ = lab
    run = run_once(wb, temporal(repo, event_intelligence=ON))
    provenance = wb.store.get("changes", run["changes"])["provenance"]
    assert provenance["event_intelligence"]["summary"]["reasons"] == {
        "no_candidates": 1
    }
    assert provenance["event_intelligence"]["summary"]["with_candidates"] == 0


def test_planned_windows_are_the_frames_frame_scope_reads(lab):
    """``sampling.window_positions`` is ``frame_scope``'s arithmetic: the
    planner's count is the count the run is then held to."""
    from levi.agent.observations import Candidate, frame_scope

    wb, repo, _ = lab
    run = run_once(wb, temporal(repo, event_intelligence=ON))
    ctx = TaskContext.model_validate(run["context"])
    root = wb.store.run_dir(run["id"]) / "input"
    times = np.arange(FRAMES, dtype=float) / FPS
    rng = np.random.default_rng(7)
    for _ in range(40):
        instants = list(rng.uniform(-0.5, 6.5, size=rng.integers(1, 5)))
        spacing = float(rng.choice([0.05, 0.1, 0.2, 0.4]))
        expected = set()
        for t in instants:
            expected |= sampling.window_positions(times, t, 1.0, spacing)
        frames = frame_scope(
            ctx, root, 0, [Candidate(t) for t in instants], spacing, None, 10_000
        )
        assert set(frames) == expected


# ------------------------------------------------------------ external agents


def external_run(wb, repo, **workflow):
    ctx = temporal(repo, provider="external", **workflow)
    agent = Principal("conn", datasets=(repo,))
    run = approve(wb, wb.plan(ctx, agent)["id"], 1, "fixture-human")
    return agent, run


def test_an_external_agent_reads_candidates_as_text_and_where_to_refine(lab):
    wb, repo, _ = lab
    agent, run = external_run(wb, repo, event_intelligence={**ON, "max_windows": 2})
    with pytest.raises(ValueError, match="Prepare this episode"):
        invoke(wb, agent, "events.candidates", {"run_id": run["id"], "episode": 0})
    prepared = invoke(wb, agent, "runs.prepare", {"run_id": run["id"]})
    first = prepared["events_first"]["0"]
    assert 1 <= len(first) <= 2
    assert "never a boundary" in prepared["events_policy"]
    value = invoke(wb, agent, "events.candidates", {"run_id": run["id"], "episode": 0})
    assert value["suggested_around_seconds"] == first
    types = [c["event_type"] for c in value["candidates"]]
    assert types[:2] == sorted(types[:2]) and {"gripper_close", "gripper_open"} <= set(
        types
    )
    text = json.dumps(value)
    assert not {"mosaic", "mosaics", "artifact", "sha256"} & set(value)
    assert ".jpg" not in text and ".png" not in text
    assert "not a probability" in value["reading"]
    # Refining around them is the ordinary capability, inside the frame cap.
    refined = invoke(
        wb,
        agent,
        "evidence.refine",
        {"run_id": run["id"], "episode": 0, "around_seconds": first},
    )
    assert refined["total"] <= refined["cap"]


def test_without_media_egress_the_candidates_are_readable_but_frames_are_not(lab):
    """Candidates come from the recorded signals, not the cameras, so they
    are text an agent may read; every frame stays behind the plan's media
    authorisation. (A plan for an external agent cannot be made without it,
    so the stored grant is withdrawn here, as a revoked plan would.)"""
    wb, repo, _ = lab
    agent, run = external_run(wb, repo, event_intelligence=ON)
    invoke(wb, agent, "runs.prepare", {"run_id": run["id"]})
    wb.store.mutate(
        "runs", run["id"], lambda r: r["context"].update(allow_media_egress=False)
    )
    value = invoke(wb, agent, "events.candidates", {"run_id": run["id"], "episode": 0})
    assert value["suggested_around_seconds"]
    for name, args in (
        ("evidence.refine", {"around_seconds": value["suggested_around_seconds"]}),
        ("evidence.read", {}),
    ):
        with pytest.raises(PermissionError, match="not authorized"):
            invoke(wb, agent, name, {"run_id": run["id"], "episode": 0, **args})


def test_a_plan_without_event_intelligence_has_no_candidates_to_read(lab):
    wb, repo, _ = lab
    agent, run = external_run(wb, repo)
    prepared = invoke(wb, agent, "runs.prepare", {"run_id": run["id"]})
    assert "events_first" not in prepared and "events_policy" not in prepared
    with pytest.raises(ValueError, match="did not approve event intelligence"):
        invoke(wb, agent, "events.candidates", {"run_id": run["id"], "episode": 0})


def test_candidates_stay_inside_the_approved_scope(lab):
    wb, repo, _ = lab
    agent, run = external_run(wb, repo, event_intelligence=ON)
    invoke(wb, agent, "runs.prepare", {"run_id": run["id"]})
    with pytest.raises(ValueError, match="outside approved scope"):
        invoke(wb, agent, "events.candidates", {"run_id": run["id"], "episode": 3})
    other = Principal("other", datasets=("local/elsewhere",))
    with pytest.raises(PermissionError):
        invoke(wb, other, "events.candidates", {"run_id": run["id"], "episode": 0})


def test_the_contract_object_round_trips():
    block = EventIntelligence(mode="candidates", sources=["gripper"], max_windows=2)
    assert EventIntelligence.model_validate(block.model_dump()) == block
