"""Event candidates, the evidence planner and the ablation scaffolding:
pure functions on synthetic signals (T-A-09)."""

import itertools
import json

import numpy as np
import pandas as pd
import pytest

from levi.agent.observations import ContextOverflow, Overflow
from levi.events import ablation, candidates, sampling

FPS = 10
NAMES = ["x", "y", "z", "gripper"]
INFO = {
    "fps": FPS,
    "features": {
        "observation.state": {"dtype": "float32", "shape": [4], "names": NAMES},
        "timestamp": {"dtype": "float32", "shape": [1], "names": None},
    },
}


def table(times, close_at=2.0, open_at=4.5):
    t = np.asarray(times, dtype=float)
    z = np.interp(t, [0, 1, 2, 3, 4, 6], [0.3, 0.3, 0.05, 0.05, 0.3, 0.3])
    grip = np.where((t >= close_at) & (t < open_at), 0.0, 1.0)
    state = np.stack([np.interp(t, [0, 6], [0.4, 0.6]), 0.1 + 0 * t, z, grip], axis=1)
    return pd.DataFrame(
        {"observation.state": [list(map(float, r)) for r in state], "timestamp": t}
    )


TIMES = np.arange(60) / FPS


# ------------------------------------------------------------ candidates


def test_gripper_events_lead_and_nearby_ones_merge_into_them():
    found = candidates.read(table(TIMES), INFO)
    kept = candidates.proposed(found)
    assert [c.event_type for c in kept[:2]] == ["gripper_close", "gripper_open"]
    assert [c.center_time_s for c in kept[:2]] == pytest.approx([2.0, 4.5])
    assert all(c.salience <= 1 and c.boundary_probability is None for c in found)
    # Everything found is kept in the record: merged ones say so.
    merged = [c for c in found if c.status == "merged"]
    assert merged and found == kept + merged
    for c in merged:
        assert any(
            abs(k.center_time_s - c.center_time_s) <= candidates.MERGE_SECONDS + 1e-9
            and k.salience >= c.salience
            for k in kept
        )
    # A kept candidate's window covers the ones merged into it.
    close = kept[0]
    assert close.time_window_s[0] <= 2.0 <= close.time_window_s[1]
    assert len({s.feature for s in close.sources}) >= 1


def test_kept_candidates_are_apart_by_more_than_the_merge_distance():
    for merge in (0.0, 0.3, 1.0, 2.5):
        kept = candidates.proposed(
            candidates.read(table(TIMES), INFO, merge_seconds=merge)
        )
        times = sorted(c.center_time_s for c in kept)
        assert all(b - a > merge for a, b in itertools.pairwise(times))


def test_sources_choose_the_readers():
    only = candidates.read(table(TIMES), INFO, sources=("gripper",))
    assert {c.event_type for c in only} == {"gripper_close", "gripper_open"}
    points = candidates.read(table(TIMES), INFO, sources=("change_point",))
    assert {c.event_type for c in points} == {"change_point"}
    assert all(c.salience <= candidates.PRIORITY["change_point"] for c in points)
    with pytest.raises(ValueError, match="Unknown event candidate sources"):
        candidates.read(table(TIMES), INFO, sources=("vision",))


def test_the_order_is_deterministic_and_independent_of_input_order():
    found = candidates.read(table(TIMES), INFO, merge_seconds=0)
    shuffled = list(found)
    np.random.default_rng(3).shuffle(shuffled)
    again = candidates.merge(shuffled, 0)
    assert [c.id for c in again] == [c.id for c in candidates.merge(found, 0)]
    assert json.dumps(
        [c.model_dump() for c in candidates.read(table(TIMES), INFO)]
    ) == (json.dumps([c.model_dump() for c in candidates.read(table(TIMES), INFO)]))


def test_a_dropped_frame_keeps_candidates_on_the_recorded_times():
    times = np.delete(TIMES, [19, 20, 21])  # 1.9-2.1 s missing
    kept = candidates.proposed(
        candidates.read(table(times), INFO, sources=("gripper",))
    )
    close = next(c for c in kept if c.event_type == "gripper_close")
    # The first recorded closed sample (2.2 s) in the window from 1.8 s.
    assert close.center_time_s == pytest.approx(2.2)
    assert close.time_window_s == pytest.approx((1.8, 2.2))
    assert times[close.source_frame_index] == pytest.approx(2.2)


def test_a_dataset_without_signals_has_no_candidates():
    bare = {"fps": FPS, "features": {"timestamp": {"dtype": "float32", "shape": [1]}}}
    assert candidates.read(pd.DataFrame({"timestamp": TIMES}), bare) == []


def test_the_change_point_penalty_is_passed_through():
    loose = candidates.read(
        table(TIMES), INFO, sources=("change_point",), change_point_penalty=0.1
    )
    strict = candidates.read(
        table(TIMES), INFO, sources=("change_point",), change_point_penalty=50
    )
    assert len(loose) >= len(strict)


# ------------------------------------------------------------ the planner


def plan(**kw):
    args = {
        "required": [],
        "tiers": [],
        "window": 1.0,
        "spacing": 0.1,
        "cameras": 1,
        "cap": 96,
    }
    return sampling.plan(TIMES, **{**args, **kw})


def tier(*ats, name=sampling.CANDIDATES):
    return (name, [(t, f"{name[0]}{i}", {}) for i, t in enumerate(ats)])


def test_required_windows_that_do_not_fit_raise_instead_of_thinning():
    with pytest.raises(Overflow, match="frame cap"):
        plan(required=[1.0, 3.0], cap=30)
    with pytest.raises(ContextOverflow, match="context holds"):
        plan(required=[1.0, 3.0], cap=96, limit=30)
    # Nothing asked: the first and last frame, as frame_scope reads.
    assert plan().positions == {0, 59}
    with pytest.raises(Overflow):
        plan(cap=1)


def test_a_window_that_does_not_fit_is_skipped_and_a_smaller_one_taken():
    # 21 frames per mid-episode window; 11 at the start.
    result = plan(required=[3.0], tiers=[tier(5.0, 0.0)], cap=21 + 15)
    assert result.windows() == [0.0]
    assert [(w.at, reason) for w, reason in result.skipped] == [(5.0, "budget")]
    assert result.images <= result.budget


def test_covered_windows_cost_nothing_and_do_not_count():
    result = plan(
        required=[3.0],
        tiers=[tier(3.0, 3.0, 5.5)],
        max_windows={sampling.CANDIDATES: 1},
    )
    assert [w.at for w in result.covered] == [3.0, 3.0]
    assert result.windows() == [5.5]


def test_tiers_go_in_order_and_max_windows_bounds_one_tier():
    result = plan(
        required=[0.0],
        tiers=[tier(2.0, 4.0, 5.9), tier(1.0, 5.5, name=sampling.UNCERTAIN)],
        max_windows={sampling.CANDIDATES: 2},
    )
    assert result.windows(sampling.CANDIDATES) == [2.0, 4.0]
    assert [(w.at, r) for w, r in result.skipped] == [(5.9, "max_windows")]
    # 1.0 s lies inside the draft's and the first candidate's windows.
    assert [w.at for w in result.covered] == [1.0]
    assert result.windows(sampling.UNCERTAIN) == [5.5]
    assert [w.tier for w, _ in result.accepted] == [
        sampling.CANDIDATES,
        sampling.CANDIDATES,
        sampling.UNCERTAIN,
    ]
    assert sum(result.added.values()) == len(result.positions)


def test_the_plan_never_passes_its_budget():
    rng = np.random.default_rng(11)
    for _ in range(300):
        cap = int(rng.integers(5, 200))
        limit = int(rng.integers(5, 200)) if rng.random() < 0.5 else None
        cameras = int(rng.integers(1, 4))
        required = list(rng.uniform(0, 6, size=rng.integers(0, 3)))
        try:
            result = plan(
                required=required,
                tiers=[
                    tier(*rng.uniform(-1, 7, size=rng.integers(0, 8))),
                    tier(*rng.uniform(0, 6, size=3), name=sampling.UNCERTAIN),
                ],
                cameras=cameras,
                cap=cap,
                limit=limit,
                spacing=float(rng.choice([0.05, 0.1, 0.2])),
                max_windows={sampling.CANDIDATES: int(rng.integers(1, 5))},
            )
        except Overflow:
            continue
        assert result.images == len(result.positions) * cameras
        assert result.images <= min(cap, limit or cap)
        summary = json.loads(json.dumps(result.summary()))
        assert summary["images"] == result.images


def test_without_candidates_the_planner_matches_the_old_rule_when_all_fit():
    ranked = [1.5, 4.0]
    kept, positions = sampling.legacy(
        TIMES, required=[0.0], ranked=ranked, window=1.0, spacing=0.1, cameras=1, cap=96
    )
    result = plan(required=[0.0], tiers=[tier(*ranked, name=sampling.UNCERTAIN)])
    assert kept == ranked and positions == result.positions
    # Tight: the old rule drops from the end, the planner skips what does not fit.
    kept, _ = sampling.legacy(
        TIMES,
        required=[0.0],
        ranked=[3.0, 5.9],
        window=1.0,
        spacing=0.1,
        cameras=1,
        cap=40,
    )
    assert kept == [3.0]


# ------------------------------------------------------------ ablation


def spec():
    return {
        "settings": {"cap": 60, "window": 1.0, "spacing": 0.1, "tolerances": [0.2]},
        "episodes": {
            "a": {
                "times": TIMES.tolist(),
                "draft": [0.0, 5.9],
                "candidates": [{"at": 2.0, "id": "g0"}, {"at": 4.5, "id": "g1"}],
                "visual": [3.2, 1.0],
                "reference": [2.0, 4.5],
            },
            "b": {
                "times": (np.arange(100) / FPS).tolist(),
                "draft": [float(t) for t in range(10)],
                "candidates": [],
                "visual": [],
                "reference": [],
            },
        },
    }


def test_arms_are_compared_on_the_same_budget():
    result = ablation.plan(spec())
    arms = result["arms"]
    assert set(arms) == set(ablation.ARMS)
    assert all(a["images_most"] <= 60 for a in arms.values())
    a = result["episodes"]["a"]
    assert a["candidates"]["windows"][:2] == [2.0, 4.5]
    assert a["off"]["windows"] and 2.0 not in a["off"]["windows"]
    assert arms["candidates"]["reference_covered"]["0.2"]["share"] == 1.0
    assert arms["off"]["reference_covered"]["0.2"]["covered"] < 2 + 1
    # Episode b's draft alone exceeds the cap: no arm reads it, all say so.
    assert all(a["draft_did_not_fit"] == 1 for a in arms.values())
    with pytest.raises(ValueError, match="Unknown arm"):
        ablation.arm_plan("magic", spec()["episodes"]["a"], {})


def test_scores_compare_each_arm_with_off():
    reference = {
        "a": [
            {"start": 0, "end": 2, "subtask": "s"},
            {"start": 2, "end": 6, "subtask": "t"},
        ]
    }
    arms = {
        "off": {
            "a": [
                {"start": 0, "end": 3, "subtask": "s"},
                {"start": 3, "end": 6, "subtask": "t"},
            ]
        },
        "candidates": reference,
    }
    result = ablation.score(reference, arms, {"a": 6.0})
    assert result["candidates"]["boundaries"]["at"]["0.1"]["recall"] == 1.0
    assert result["candidates"]["minus_off"]["boundary_f1"]["0.1"] == 1.0
    assert result["candidates"]["minus_off"]["segment_f1_mean"]["0.7"] > 0
    assert "minus_off" not in result["off"]


def test_the_command_line_reads_only_what_it_is_given_and_refuses_test_sets(
    tmp_path, capsys
):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec()))
    assert ablation.main(["plan", "--spec", str(path), "--arm", "off"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert set(printed["arms"]) == {"off"}
    out = tmp_path / "result.json"
    assert ablation.main(["plan", "--spec", str(path), "--out", str(out)]) == 0
    assert json.loads(out.read_text())["arms"]["candidates"]
    for word in ("frozen", "heldout", "held-out"):
        blind = tmp_path / f"generic-{word}" / "spec.json"
        blind.parent.mkdir()
        blind.write_text(json.dumps(spec()))
        with pytest.raises(SystemExit, match="never reads a test set"):
            ablation.main(["plan", "--spec", str(blind)])


# ------------------------------------------------------------ resumed refinements


def _resume_case(rng):
    """One synthetic episode and two refinement plans of it, the second as a
    resumed run makes it: the same draft and candidates, a different image
    limit (the fitted one drifts between requests), the frames the first
    left in the ledger already held."""
    n = int(rng.integers(30, 160))
    steps = rng.choice([0.1, 0.1, 0.1, 0.2, 0.3], size=n - 1)
    times = np.concatenate([[0.0], np.cumsum(steps)])
    cameras = int(rng.integers(1, 4))
    grid = sorted(
        {int(np.abs(times - t).argmin()) for t in np.arange(0, times[-1], 2.0)}
        | {n - 1}
    )
    most = int(rng.integers(len(grid) * cameras + 5, len(grid) * cameras + 120))
    draft = list(rng.uniform(0, times[-1], size=rng.integers(0, 3)))
    found = [(t, f"c{i}", {}) for i, t in enumerate(rng.uniform(0, times[-1], size=8))]
    settings = {
        "required": draft,
        "tiers": [(sampling.CANDIDATES, found)],
        "window": float(rng.choice([0.3, 0.6, 1.0])),
        "spacing": float(rng.choice([0.1, 0.2])),
        "cameras": cameras,
        "cap": most - len(grid) * cameras,
        "max_windows": {sampling.CANDIDATES: int(rng.integers(1, 6))},
    }
    first_limit = int(rng.integers(5, 80))
    return times, grid, cameras, most, settings, first_limit


def test_a_resumed_refinement_never_takes_the_ledger_past_the_frame_cap():
    """A refinement whose frames were persisted but whose request did not
    finish is planned again on resume, under the image limit of that moment.
    Greedy plans under two limits are not nested, so without the ledger the
    two together passed the cap (review A3, I-1: 96 of 5000; here the
    naive count is asserted to be non-zero). Planned against the ledger:
    0 over."""
    rng = np.random.default_rng(20261010)
    over_naive = over = planned = 0
    for _ in range(5000):
        times, grid, cameras, most, settings, first_limit = _resume_case(rng)
        try:
            first = sampling.plan(times, limit=first_limit, **settings)
            naive = sampling.plan(times, limit=None, **settings)
        except Overflow:
            continue
        ledger = set(grid) | first.positions
        over_naive += len(ledger | naive.positions) * cameras > most
        coarse = set(grid)
        try:
            first = sampling.plan(
                times,
                limit=first_limit,
                held=frozenset(coarse),
                held_images=len(coarse) * cameras,
                ledger_cap=most,
                **settings,
            )
            ledger = coarse | first.positions
            second = sampling.plan(
                times,
                limit=None,
                held=frozenset(ledger),
                held_images=len(ledger) * cameras,
                ledger_cap=most,
                **settings,
            )
        except Overflow:
            continue
        planned += 1
        over += len(ledger | second.positions) * cameras > most
        assert second.images <= second.budget
    assert over_naive > 0, "the scenario no longer reproduces the review's case"
    assert planned > 3500, planned
    assert over == 0, (over, over_naive, planned)


def test_the_ledger_changes_nothing_on_a_first_refinement():
    """On a fresh run the ledger holds only the coarse frames, which the cap
    already left out: the plan is the one made without it."""
    rng = np.random.default_rng(5)
    for _ in range(2000):
        times, grid, cameras, most, settings, first_limit = _resume_case(rng)
        try:
            without = sampling.plan(times, limit=first_limit, **settings)
        except Overflow:
            with pytest.raises(Overflow):
                sampling.plan(
                    times,
                    limit=first_limit,
                    held=frozenset(grid),
                    held_images=len(grid) * cameras,
                    ledger_cap=most,
                    **settings,
                )
            continue
        within = sampling.plan(
            times,
            limit=first_limit,
            held=frozenset(grid),
            held_images=len(grid) * cameras,
            ledger_cap=most,
            **settings,
        )
        assert within.positions == without.positions
        assert within.windows() == without.windows()
