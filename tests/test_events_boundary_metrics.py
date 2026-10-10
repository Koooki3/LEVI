"""Boundary and segment metrics of event readers (levi.events.boundary_metrics)."""

import itertools
import random

import pytest

from levi.events.boundary_metrics import (
    boundaries,
    boundary_scores,
    match,
    segment_f1,
)
from levi.harness import grading


def test_inner_boundaries_of_contiguous_segments():
    segs = [
        {"start": 0.0, "end": 1.2},
        {"start": 1.2, "end": 3.0},
        {"start": 3.0, "end": 5.5},
    ]
    assert boundaries(segs) == [1.2, 3.0]
    assert boundaries(segs[:1]) == []
    assert boundaries([]) == []


def _brute_force(reference, candidates, tol):
    best = 0
    for k in range(min(len(reference), len(candidates)), 0, -1):
        for refs in itertools.combinations(reference, k):
            for cands in itertools.permutations(candidates, k):
                if all(
                    abs(r - c) <= tol + 1e-9 for r, c in zip(refs, cands, strict=True)
                ):
                    return k
    return best


def test_sweep_is_a_maximum_matching():
    rng = random.Random(7)
    for _ in range(300):
        ref = sorted(round(rng.uniform(0, 3), 2) for _ in range(rng.randint(0, 4)))
        cand = [round(rng.uniform(0, 3), 2) for _ in range(rng.randint(0, 4))]
        tol = rng.choice([0.1, 0.2, 0.5])
        pairs = match(ref, cand, tol)
        assert len(pairs) == _brute_force(ref, cand, tol)
        assert all(abs(r - c) <= tol + 1e-9 for r, c in pairs)
        # One-to-one.
        assert len({c for _, c in pairs}) == len(pairs) or len(set(cand)) < len(cand)


def test_one_candidate_counts_for_one_boundary():
    assert match([1.0, 1.05], [1.02], 0.1) == [(1.0, 1.02)]


def test_boundary_scores_by_hand():
    reference = {0: [1.0, 2.0, 4.0], 1: [3.0]}
    candidates = {0: [1.05, 2.15, 6.0, 7.0], 1: []}
    durations = {0: 30.0, 1: 30.0}  # one minute in all
    out = boundary_scores(reference, candidates, durations)
    assert out["reference_boundaries"] == 4
    assert out["candidates"] == 4
    assert out["candidates_per_minute"] == 4.0
    at1, at2 = out["at"]["0.1"], out["at"]["0.2"]
    assert (at1["matched"], at1["recall"], at1["precision"]) == (1, 0.25, 0.25)
    assert at1["false_per_minute"] == 3.0
    assert (at2["matched"], at2["recall"]) == (2, 0.5)
    assert at2["matched_mae"] == pytest.approx(0.1)
    # Nearest candidate of 1.0, 2.0, 4.0 in episode 0; episode 1 has none.
    assert out["nearest_mae"] == pytest.approx((0.05 + 0.15 + 1.85) / 3, abs=1e-4)
    assert out["no_candidate"] == 1


def test_boundary_scores_ignore_non_finite_and_unjudged_episodes():
    out = boundary_scores(
        {0: [1.0, float("nan")]},
        {0: [1.0, float("inf")], 9: [1.0, 2.0]},
        {0: 60.0},
    )
    assert out["reference_boundaries"] == 1
    assert out["candidates"] == 1
    assert out["at"]["0.1"]["recall"] == 1.0


def test_boundary_scores_with_nothing():
    out = boundary_scores({}, {}, {})
    assert out["candidates_per_minute"] is None
    assert out["at"]["0.2"]["recall"] is None
    out = boundary_scores({0: []}, {0: [1.0]}, {0: 60})
    assert out["at"]["0.2"]["recall"] is None
    assert out["at"]["0.2"]["precision"] == 0.0
    assert out["at"]["0.2"]["false_per_minute"] == 1.0


def _random_segments(rng):
    t, out = 0.0, []
    for _ in range(rng.randint(0, 6)):
        length = rng.uniform(0.3, 3)
        out.append(
            {
                "start": round(t, 2),
                "end": round(t + length, 2),
                "subtask": rng.choice(["grasp", "place"]),
            }
        )
        t += length + rng.choice([0, 0, 0.4])
    return out


def test_segment_f1_at_iou_0_3_is_gradings():
    rng = random.Random(3)
    reference, candidates = {}, {}
    for e in range(40):
        reference[e] = _random_segments(rng)
        candidates[e] = [
            {**s, "start": s["start"] + rng.uniform(-0.5, 0.5)}
            for s in _random_segments(rng) + reference[e]
            if rng.random() < 0.7
        ]
    ours = segment_f1(reference, candidates)["0.3"]
    graded = grading.grade(
        {"task": "t", "episodes": {e: reference[e] for e in reference}},
        candidates,
    )
    per = [graded["episodes"][e]["segment_f1"] for e in reference]
    assert ours["mean"] == pytest.approx(sum(per) / len(per), abs=2e-3)


def test_segment_f1_falls_with_a_stricter_iou():
    ref = {0: [{"start": 0, "end": 2, "subtask": "a"}]}
    cand = {0: [{"start": 0.5, "end": 2, "subtask": "a"}]}  # IoU 0.75
    out = segment_f1(ref, cand, ious=(0.3, 0.7, 0.8))
    assert [out[k]["pooled"] for k in ("0.3", "0.7", "0.8")] == [1.0, 1.0, 0.0]
    assert segment_f1({0: []}, {0: []})["0.5"]["pooled"] is None
