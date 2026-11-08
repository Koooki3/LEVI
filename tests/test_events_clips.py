"""The clip proposer (levi/events/clips.py).

Synthetic signals only: no real capture, no gold, no frozen or held-out set.
The 1-12 s range is the paper's (VPP2, arXiv:2610.10270); every other
parameter is ours and is judged here only by the invariants it must keep, not
by agreement with the paper.
"""

import ast
import csv
import json
from itertools import pairwise
from pathlib import Path

import numpy as np
import pytest

from levi.events import boundary_metrics, calibrate, clips
from levi.events.clips import ClipInputError, ClipParams, ClipSignals, propose_clips

ROOT = Path(__file__).resolve().parents[1]
TOL = 1e-9


def episode(
    duration=20.0,
    hz=20.0,
    stops=(),
    width=0.4,
    grip=(),
    noise=1e-4,
    seed=0,
    base=0.3,
    positions=True,
    gripper=True,
):
    """Constant speed ``base`` with a dip to 5% at every ``stops`` time (a
    Gaussian of ``width`` seconds), and a gripper that toggles open/closed at
    every ``grip`` time (it starts open)."""
    rng = np.random.default_rng(seed)
    t = np.arange(0, duration, 1 / hz)
    speed = np.full_like(t, base)
    for s in stops:
        speed = speed * (1 - 0.95 * np.exp(-(((t - s) / width) ** 2)))
    x = np.cumsum(speed / hz)
    pos = np.stack([x, 0.1 * np.sin(t), np.zeros_like(t)], axis=1)
    pos = pos + rng.normal(0, noise, pos.shape)
    g = np.full_like(t, 0.08)
    for k, when in enumerate(sorted(grip)):
        g[t >= when] = 0.0 if k % 2 == 0 else 0.08
    g = g + rng.normal(0, noise, g.shape)
    return ClipSignals(t, pos if positions else None, g if gripper else None)


def partition_problems(proposal, signals, params=None):
    """What is wrong with ``proposal`` as a partition of ``signals``."""
    params = params or proposal.params
    times = np.asarray(signals.times, dtype=float)
    out = []
    got = proposal.clips
    if not got:
        return ["no clips"]
    if got[0].start_s != times[0] or got[-1].end_s != times[-1]:
        out.append("does not span the episode")
    for a, b in pairwise(got):
        if a.end_s != b.start_s:
            out.append(f"gap or overlap between {a} and {b}")
    short = clips.TOO_SHORT_EPISODE in got[0].flags
    if short != (times[-1] - times[0] < params.min_s - TOL):
        out.append("too_short_episode flag is wrong")
    for c in got:
        if not c.start_s < c.end_s:
            out.append(f"empty or reversed clip {c}")
        if not short and not params.min_s - TOL <= c.duration_s <= params.max_s + TOL:
            out.append(f"clip {c} of {c.duration_s:.6f} s is out of range")
        known = {*clips.BOUNDARY_REASONS, clips.EPISODE_START, clips.EPISODE_END}
        if c.start_reason not in known or c.end_reason not in known:
            out.append(f"unknown reason in {c}")
        if set(c.flags) - set(clips.CLIP_FLAGS):
            out.append(f"unknown flag in {c}")
    for a, b in pairwise(got):
        if a.end_reason != b.start_reason:
            out.append(f"the boundary between {a} and {b} has two reasons")
    return out


def reasons_of(proposal):
    return [b.reason for b in proposal.boundaries]


def near(times, targets, tolerance):
    return all(np.min(np.abs(np.asarray(times) - s)) <= tolerance for s in targets)


# --- The shape of the output -----------------------------------------------


def test_the_default_parameters_are_the_papers_range_and_our_choices():
    p = ClipParams()
    assert (p.min_s, p.max_s) == (1.0, 12.0)
    assert set(clips.PARAM_SOURCES) == set(p.as_dict())
    assert {k for k, v in clips.PARAM_SOURCES.items() if v == "paper"} == {
        "min_s",
        "max_s",
    }
    assert set(clips.PARAM_SOURCES.values()) == {"paper", "ours"}


def test_a_known_episode_is_cut_where_the_arm_stops_and_the_gripper_moves():
    signals = episode(30.0, stops=(8.0, 16.0, 24.0), grip=(12.0, 20.0))
    proposal = propose_clips(signals)
    assert partition_problems(proposal, signals) == []
    assert reasons_of(proposal) == [
        "boundary_speed_min",
        "boundary_gripper",
        "boundary_speed_min",
        "boundary_gripper",
        "boundary_speed_min",
    ]
    cuts = [b.time_s for b in proposal.boundaries]
    assert near(cuts, [8.0, 12.0, 16.0, 20.0, 24.0], 0.3)
    assert proposal.clips[0].start_reason == "episode_start"
    assert proposal.clips[-1].end_reason == "episode_end"


def test_the_same_input_gives_the_same_output():
    signals = episode(40.0, stops=(7.0, 15.0, 22.0, 33.0), grip=(11.0, 27.0), seed=3)
    first = propose_clips(signals)
    again = propose_clips(
        ClipSignals(
            list(map(float, signals.times)),
            [list(map(float, row)) for row in signals.positions],
            list(map(float, signals.gripper)),
        )
    )
    assert first == again
    assert json.dumps(first.as_dict()) == json.dumps(again.as_dict())


# --- The length invariant, on random signals -------------------------------


def random_case(rng):
    hz = float(rng.choice([5, 10, 20, 30, 50]))
    edge = [0.4, 0.99, 1.0, 1.01, 2.0, 11.9, 12.0, 12.1, 13.0, 24.0, 24.5, 36.2]
    duration = float(rng.choice(edge)) if rng.random() < 0.3 else rng.uniform(0.3, 70)
    n = max(2, int(duration * hz))
    t = np.cumsum((1 / hz) * rng.uniform(0.6, 1.4, n))
    speed = np.full(n, rng.uniform(0.05, 0.5))
    for _ in range(int(rng.integers(0, 12))):
        s = rng.uniform(t[0], t[-1])
        w = rng.uniform(0.1, 2.0)
        speed = speed * (1 - rng.uniform(0.3, 1.0) * np.exp(-(((t - s) / w) ** 2)))
    if rng.random() < 0.3 and n > 10:  # an exact stop
        a = int(rng.integers(0, n - 5))
        speed[a : a + int(rng.integers(2, 60))] = 0.0
    path = np.concatenate([[0], np.cumsum(speed[1:] * np.diff(t))])
    direction = rng.normal(size=3)
    direction /= np.linalg.norm(direction)
    pos = path[:, None] * direction + rng.normal(0, rng.choice([0, 1e-4, 1e-3]), (n, 3))
    g = np.full(n, 0.08)
    for k, when in enumerate(sorted(rng.uniform(t[0], t[-1], int(rng.integers(0, 9))))):
        g[t >= when] = 0.0 if k % 2 == 0 else 0.08
    g = g + rng.normal(0, 1e-3, n)
    keep_pos, keep_grip = rng.random() > 0.1, rng.random() > 0.1
    if not keep_pos and not keep_grip:
        keep_pos = True
    min_s = float(rng.choice([0.5, 1.0, 2.0, 3.0]))
    params = ClipParams(
        min_s=min_s,
        max_s=min_s * float(rng.choice([2, 3, 5, 12])),
        min_gap_s=float(rng.choice([0, 1, 2, 4])),
        split_window=float(rng.choice([0, 0.25, 0.5])),
    )
    signals = ClipSignals(t, pos if keep_pos else None, g if keep_grip else None)
    return signals, params


def test_every_clip_is_in_range_and_the_clips_tile_the_episode():
    rng = np.random.default_rng(20261106)
    flagged = merged = split = 0
    for case in range(400):
        signals, params = random_case(rng)
        proposal = propose_clips(signals, params)
        assert partition_problems(proposal, signals, params) == [], case
        # Serialising loses nothing.
        back = clips.ClipProposal.from_dict(json.loads(json.dumps(proposal.as_dict())))
        assert back == proposal, case
        flagged += clips.TOO_SHORT_EPISODE in proposal.clips[0].flags
        merged += any(clips.MERGE_SHORT in c.flags for c in proposal.clips)
        split += any(
            r.startswith("split_long") for c in proposal.clips for r in c.reasons
        )
    # The generator reaches the three repairs (otherwise the test says little).
    assert flagged > 5 and merged > 20 and split > 20


def test_a_gripper_crossing_keeps_its_boundary_when_nothing_is_close():
    rng = np.random.default_rng(11)
    for case in range(60):
        gap = 2.0
        times, when = [], 2.5
        while when < 55 and len(times) < 8:
            times.append(when)
            when += gap + rng.uniform(0, 5)
        stops = rng.uniform(1.5, 55, int(rng.integers(0, 8)))
        signals = episode(
            when + 2.5, hz=20, stops=tuple(stops), grip=tuple(times), seed=case
        )
        proposal = propose_clips(signals)
        assert partition_problems(proposal, signals) == [], case
        grip_cuts = [
            b.time_s for b in proposal.boundaries if b.reason == "boundary_gripper"
        ]
        assert len(grip_cuts) == len(times), case
        assert near(grip_cuts, times, 2 / 20), case


# --- Each cue ---------------------------------------------------------------


def test_the_gripper_alone_cuts_at_each_opening_and_closing():
    signals = episode(30.0, grip=(7.0, 14.5, 22.0), positions=False)
    proposal = propose_clips(signals)
    assert reasons_of(proposal) == ["boundary_gripper"] * 3
    assert near([b.time_s for b in proposal.boundaries], [7.0, 14.5, 22.0], 0.06)
    # With a constant-speed arm in view nothing else is added.
    assert propose_clips(episode(30.0, grip=(7.0, 14.5, 22.0))).boundaries == (
        proposal.boundaries
    )


def test_a_gripper_that_starts_closed_is_read_the_same():
    signals = episode(20.0, grip=(5.0, 12.0), positions=False)
    flipped = ClipSignals(signals.times, None, 0.08 - np.asarray(signals.gripper))
    assert propose_clips(flipped).boundaries == propose_clips(signals).boundaries


def test_a_speed_minimum_gets_a_boundary_and_steady_motion_gets_none():
    stops = (4.0, 9.5, 15.0)
    proposal = propose_clips(episode(20.0, stops=stops, gripper=False))
    assert reasons_of(proposal) == ["boundary_speed_min"] * 3
    assert near([b.time_s for b in proposal.boundaries], stops, 0.3)
    steady = propose_clips(episode(10.0, gripper=False, seed=5))
    assert len(steady.clips) == 1 and steady.boundaries == ()


def test_a_long_pause_is_cut_in_its_middle():
    # A pause from 5 to 8 s whose speed creeps up from 0.01 to 0.03 m/s: its
    # lowest point is at its start, but the cut belongs in the middle.
    t = np.arange(0, 16, 0.05)
    pause = (t > 5) & (t < 8)
    speed = np.where(pause, 0.01 + 0.02 * (t - 5) / 3, 0.3)
    x = np.concatenate([[0], np.cumsum(speed[1:] * 0.05)])
    pos = np.stack([x, np.zeros_like(t), np.zeros_like(t)], axis=1)
    proposal = propose_clips(ClipSignals(t, pos))
    assert reasons_of(proposal) == ["boundary_speed_min"]
    assert abs(proposal.boundaries[0].time_s - 6.5) < 0.4


def test_a_shallow_or_high_dip_is_not_a_cue():
    t = np.arange(0, 11, 0.05)

    def dipped(depth):
        speed = 0.3 * (1 - depth * np.exp(-(((t - 5.5) / 0.5) ** 2)))
        x = np.concatenate([[0], np.cumsum(speed[1:] * 0.05)])
        return ClipSignals(t, np.stack([x, 0 * x, 0 * x], axis=1))

    # Eases off to 80% (too shallow) and to 60% (above max_level, 0.5).
    assert propose_clips(dipped(0.2)).boundaries == ()
    assert propose_clips(dipped(0.4)).boundaries == ()
    # A dip to 5% is a cue, and the same 40% dip counts once the level limit
    # is raised.
    assert len(propose_clips(dipped(0.95)).boundaries) == 1
    assert len(propose_clips(dipped(0.4), ClipParams(max_level=0.7)).boundaries) == 1


def test_two_speed_cues_within_the_gap_keep_the_deeper_one():
    t = np.arange(0, 20, 0.05)
    speed = 0.3 * (
        1
        - 0.95 * np.exp(-(((t - 9.0) / 0.4) ** 2))
        - 0.8 * np.exp(-(((t - 10.2) / 0.4) ** 2))
    )
    x = np.concatenate([[0], np.cumsum(speed[1:] * 0.05)])
    signals = ClipSignals(t, np.stack([x, 0 * x, 0 * x], axis=1))
    proposal = propose_clips(signals)
    assert len(proposal.boundaries) == 1
    assert abs(proposal.boundaries[0].time_s - 9.0) < 0.3
    assert [(d.reason, d.why) for d in proposal.dropped] == [
        ("boundary_speed_min", "min_gap")
    ]
    assert abs(proposal.dropped[0].time_s - 10.2) < 0.3
    # Without a minimum gap both stay.
    both = propose_clips(signals, ClipParams(min_gap_s=0.5))
    assert len(both.boundaries) == 2


def test_a_cue_closer_to_an_end_than_min_s_is_dropped_not_cut():
    signals = episode(15.0, stops=(0.5, 7.0), grip=(14.3,))
    proposal = propose_clips(signals)
    assert partition_problems(proposal, signals) == []
    assert near([b.time_s for b in proposal.boundaries], [7.0], 0.3)
    assert len(proposal.boundaries) == 1
    assert sorted(d.why for d in proposal.dropped) == ["near_edge", "near_edge"]


def test_a_gripper_cue_wins_over_a_speed_cue_next_to_it():
    # The arm stops at 10.0 and the gripper closes at 10.6.
    signals = episode(20.0, stops=(10.0,), grip=(10.6,))
    proposal = propose_clips(signals)
    assert reasons_of(proposal) == ["boundary_gripper"]
    assert [(d.reason, d.why) for d in proposal.dropped] == [
        ("boundary_speed_min", "min_gap")
    ]
    # With the gap off both are cut, and 0.6 s is short: the speed cue goes.
    free = propose_clips(signals, ClipParams(min_gap_s=0.0))
    assert reasons_of(free) == ["boundary_gripper"]
    assert [(d.reason, d.why) for d in free.dropped] == [
        ("boundary_speed_min", "merge_short")
    ]
    assert clips.MERGE_SHORT in [f for c in free.clips for f in c.flags]


def test_a_given_gripper_range_makes_a_jittering_gripper_quiet():
    t = np.arange(0, 10, 0.05)
    # Jitter between 0.00 and 0.01 on a gripper whose range is 0 to 0.08.
    g = np.where(((t / 1.5).astype(int) % 2) == 0, 0.0, 0.01)
    own_range = propose_clips(ClipSignals(t, None, g))
    assert len(own_range.boundaries) > 4  # read on its own range it is a signal
    declared = propose_clips(ClipSignals(t, None, g, (0.0, 0.08)))
    assert declared.boundaries == ()  # moves 12% of its range: below 20%
    # Past the travel rule, the declared range still scales the hysteresis:
    # 0.01 of 0.08 never gets below the closing threshold.
    free = propose_clips(
        ClipSignals(t, None, g, (0.0, 0.08)), ClipParams(gripper_min_travel=0.1)
    )
    assert free.boundaries == ()


def test_a_still_gripper_with_jitter_has_no_boundary_without_a_range():
    """The review's probe: 20 s of a still gripper with sigma = 1e-4 jitter,
    read on its own range, used to cut 11 boundary_gripper."""
    for hz in (10, 30):
        t = np.arange(0, 20, 1 / hz)
        g = 0.08 + np.random.default_rng(0).normal(0, 1e-4, len(t))
        proposal = propose_clips(ClipSignals(t, None, g))
        assert clips.BOUNDARY_GRIPPER not in reasons_of(proposal), hz
    # Not a blanket refusal: a real close of 0.08 m is still a boundary, and
    # lowering the floor to zero brings the jitter back.
    t = np.arange(0, 20, 0.05)
    real = np.where(t < 10, 0.08, 0.0) + np.random.default_rng(1).normal(
        0, 1e-4, len(t)
    )
    assert clips.BOUNDARY_GRIPPER in reasons_of(
        propose_clips(ClipSignals(t, None, real))
    )
    g = 0.08 + np.random.default_rng(0).normal(0, 1e-4, len(t))
    loose = ClipParams(gripper_min_travel_abs=0.0)
    assert clips.BOUNDARY_GRIPPER in reasons_of(
        propose_clips(ClipSignals(t, None, g), loose)
    )


def test_a_given_range_is_the_scale_of_the_hysteresis():
    """The gripper closes from 0.08 to 0.04 m: half of a declared 0 to 0.08
    range, which is not below the closing threshold (0.35), though on its own
    range it is a full close."""
    t = np.arange(0, 20, 0.05)
    g = np.where(t < 10, 0.08, 0.04)
    own = propose_clips(ClipSignals(t, None, g))
    assert clips.BOUNDARY_GRIPPER in reasons_of(own)
    declared = propose_clips(ClipSignals(t, None, g, (0.0, 0.08)))
    assert clips.BOUNDARY_GRIPPER not in reasons_of(declared)


# --- Too long, too short ----------------------------------------------------


def test_a_long_steady_stretch_is_cut_into_equal_pieces():
    for duration, pieces in [(24.0, 2), (30.0, 3), (50.0, 5), (12.5, 2)]:
        signals = episode(duration, gripper=False)
        proposal = propose_clips(signals)
        assert partition_problems(proposal, signals) == []
        assert len(proposal.clips) == pieces
        assert set(reasons_of(proposal)) == {"split_long_even"}
        lengths = [c.duration_s for c in proposal.clips]
        assert max(lengths) - min(lengths) < 0.1  # equal division (to a sample)
        assert all("split_long_even" in c.reasons for c in proposal.clips[:-1])


def test_a_long_clip_is_cut_at_a_held_back_speed_cue_near_the_even_point():
    # Dips at 12 s (deep) and 20 s (80%); with a gap of 10 s the second is
    # not a cue (it is within 10 s of the first), so the 28 s left after 12 s
    # must be cut: its first even cut is at 21.3 s, and the dip at 20 s is
    # inside the window of a quarter of a piece around it.
    signals = episode(40.0, stops=(12.0,), gripper=False)
    t = np.asarray(signals.times)
    speed = 0.3 * (1 - 0.95 * np.exp(-(((t - 12.0) / 0.4) ** 2)))
    speed = speed * (1 - 0.8 * np.exp(-(((t - 20.0) / 0.4) ** 2)))
    x = np.concatenate([[0], np.cumsum(speed[1:] * np.diff(t))])
    signals = ClipSignals(t, np.stack([x, 0 * x, 0 * x], axis=1))
    proposal = propose_clips(signals, ClipParams(min_gap_s=10.0))
    assert partition_problems(proposal, signals) == []
    assert reasons_of(proposal) == [
        "boundary_speed_min",
        "split_long",
        "split_long_even",
    ]
    cuts = [b.time_s for b in proposal.boundaries]
    assert abs(cuts[0] - 12.0) < 0.3 and abs(cuts[1] - 20.0) < 0.3
    assert abs(cuts[2] - (cuts[1] + 40.0) / 2) < 0.1  # the rest is cut evenly
    assert [(d.reason, d.why) for d in proposal.dropped] == [
        ("boundary_speed_min", "min_gap")
    ]
    # With a window of zero the held-back cue is ignored.
    even = propose_clips(signals, ClipParams(min_gap_s=10.0, split_window=0))
    assert reasons_of(even)[1:] == ["split_long_even"] * 2


def test_a_ripple_that_is_not_a_cue_does_not_move_a_cut():
    # 30 s steady with two 25% ripples (too shallow for a cue) near the even
    # cut points: the cuts stay even.
    t = np.arange(0, 30.0, 0.05)
    speed = 0.3 * (
        1
        - 0.25 * np.exp(-(((t - 9.4) / 0.2) ** 2))
        - 0.25 * np.exp(-(((t - 21.0) / 0.2) ** 2))
    )
    x = np.concatenate([[0], np.cumsum(speed[1:] * 0.05)])
    proposal = propose_clips(ClipSignals(t, np.stack([x, 0 * x, 0 * x], axis=1)))
    assert reasons_of(proposal) == ["split_long_even"] * 2
    assert near([b.time_s for b in proposal.boundaries], [10.0, 20.0], 0.1)


def test_a_cut_leaves_a_rest_that_the_other_pieces_can_fill():
    # 41 s: a deep dip at 36 s is the only cue (a gap of 30 s holds the dip at
    # 10 s back), so the 36 s before it need 3 pieces, which leaves exactly
    # one cut point, 12 s. A cut at the held-back dip (10 s, inside the
    # window around 12 s) would leave 26 s, which 2 pieces cannot fill.
    t = np.arange(0, 41.0, 0.05)
    speed = 0.3 * (1 - 0.95 * np.exp(-(((t - 36.0) / 0.4) ** 2)))
    speed = speed * (1 - 0.8 * np.exp(-(((t - 10.0) / 0.4) ** 2)))
    x = np.concatenate([[0], np.cumsum(speed[1:] * 0.05)])
    signals = ClipSignals(t, np.stack([x, 0 * x, 0 * x], axis=1))
    proposal = propose_clips(signals, ClipParams(min_gap_s=30.0))
    assert partition_problems(proposal, signals) == []
    assert reasons_of(proposal) == [
        "split_long_even",
        "split_long_even",
        "boundary_speed_min",
    ]
    assert near([b.time_s for b in proposal.boundaries], [12.0, 24.0, 36.0], 0.3)
    assert [d.why for d in proposal.dropped] == ["min_gap"]


def test_short_clips_are_merged_and_the_dropped_cut_is_recorded():
    # Two gripper cuts 0.5 s apart in a 13 s episode: one goes.
    signals = episode(13.0, grip=(6.0, 6.5), positions=False)
    proposal = propose_clips(signals)
    assert partition_problems(proposal, signals) == []
    assert len(proposal.boundaries) == 1
    assert [d.why for d in proposal.dropped] == ["merge_short"]
    merged = [c for c in proposal.clips if clips.MERGE_SHORT in c.flags]
    assert len(merged) == 1 and merged[0].reasons.count("merge_short") == 1
    # The earlier cut goes (equal strength, equal merged length rules: the
    # merge into the shorter neighbour, then the earlier boundary).
    assert abs(proposal.boundaries[0].time_s - 6.5) < 0.06


def test_a_weaker_cut_goes_first_even_when_the_other_would_fit_better():
    # Short clip between a gripper cut (10.0) and a speed cut (10.5,
    # min_gap_s = 0 keeps both): the speed cut is dropped.
    signals = episode(24.0, stops=(10.5,), grip=(10.0,))
    proposal = propose_clips(signals, ClipParams(min_gap_s=0.0))
    assert partition_problems(proposal, signals) == []
    assert "boundary_gripper" in reasons_of(proposal)
    assert [d.why for d in proposal.dropped] == ["merge_short"]
    assert proposal.dropped[0].reason == "boundary_speed_min"


def test_a_merge_that_makes_a_clip_too_long_is_cut_again():
    # 12.0 and 12.5 s cuts in a 24.5 s episode: dropping one leaves 12.5 s.
    signals = episode(24.5, grip=(12.0, 12.5), positions=False)
    proposal = propose_clips(signals)
    assert partition_problems(proposal, signals) == []
    assert len(proposal.clips) == 3
    assert sorted(reasons_of(proposal)) == ["boundary_gripper", "split_long_even"]
    assert [d.why for d in proposal.dropped] == ["merge_short"]


@pytest.mark.parametrize("duration", [0.2, 0.5, 0.99])
def test_an_episode_shorter_than_min_s_is_one_flagged_clip(duration):
    signals = episode(duration, hz=100, stops=(duration / 2,), grip=(duration / 2,))
    proposal = propose_clips(signals)
    assert len(proposal.clips) == 1
    only = proposal.clips[0]
    assert only.flags == ("too_short_episode",)
    assert (only.start_s, only.end_s) == (signals.times[0], signals.times[-1])
    assert (only.start_reason, only.end_reason) == ("episode_start", "episode_end")
    assert proposal.dropped == ()


def test_an_episode_exactly_min_s_long_is_not_flagged():
    t = np.linspace(0, 1.0, 21)
    proposal = propose_clips(ClipSignals(t, np.stack([t, t, t], axis=1)))
    assert proposal.clips[0].flags == ()
    assert proposal.clips[0].duration_s == 1.0


# --- Input that must be refused, loudly -------------------------------------


def refused(code, signals_or_params, match):
    with pytest.raises(ClipInputError, match=match) as info:
        if isinstance(signals_or_params, ClipSignals):
            propose_clips(signals_or_params)
        else:
            signals_or_params()
    assert info.value.code == code
    assert str(info.value).startswith(code + ":")


def test_bad_signals_are_refused_with_the_cause():
    t = np.arange(0, 5, 0.1)
    xyz = np.zeros((len(t), 3))
    g = np.zeros(len(t))
    nan_t = t.copy()
    nan_t[7] = np.nan
    refused("non_finite", ClipSignals(nan_t, xyz, g), r"times has 1 non-finite.*row 7")
    nan_x = xyz.copy()
    nan_x[3, 1] = np.nan
    refused("non_finite", ClipSignals(t, nan_x, g), r"positions.*first is row 3")
    inf_g = g.copy()
    inf_g[4] = np.inf
    refused("non_finite", ClipSignals(t, xyz, inf_g), r"gripper.*row 4")
    swapped = t.copy()
    swapped[[10, 11]] = swapped[[11, 10]]
    refused("non_monotonic", ClipSignals(swapped, xyz, g), r"times\[11\].*not after")
    twice = t.copy()
    twice[20] = twice[19]
    refused("non_monotonic", ClipSignals(twice, xyz, g), r"strictly increase")
    refused("non_monotonic", ClipSignals(t[::-1], xyz, g), r"strictly increase")
    refused("empty", ClipSignals([], None, []), r"no episode")
    refused("too_few_samples", ClipSignals([1.0], [[0, 0, 0]], [0.0]), r"at least 2")
    refused("shape_mismatch", ClipSignals(t, xyz[:-1], g), r"positions has shape")
    refused("shape_mismatch", ClipSignals(t, xyz, g[:-2]), r"gripper has")
    refused("shape_mismatch", ClipSignals(t, xyz[:, 0], g), r"positions must have 2")
    refused("shape_mismatch", ClipSignals(t.reshape(-1, 1), xyz, g), r"times must have")
    refused("no_signal", ClipSignals(t), r"positions or a gripper")
    refused("not_numeric", ClipSignals(["a", "b"], None, [0, 1]), r"times is not")
    refused("bad_bounds", ClipSignals(t, None, g, (1.0, 1.0)), r"min < max")
    refused("bad_bounds", ClipSignals(t, None, g, (0.0, np.nan)), r"min < max")


def test_bad_parameters_are_refused_with_the_cause():
    for kwargs, match in [
        ({"min_s": 0}, "min_s must be positive"),
        ({"min_s": 5, "max_s": 9}, r"max_s must be at least 2 \* min_s"),
        ({"smooth_s": -1}, "smooth_s must be positive"),
        ({"min_prominence": 0}, "min_prominence"),
        ({"max_level": -0.1}, "max_level"),
        ({"min_gap_s": -1}, "min_gap_s"),
        ({"split_window": 1.5}, "split_window"),
        ({"gripper_min_travel": 2}, "gripper_min_travel"),
        ({"min_s": float("nan")}, "finite"),
        ({"min_s": "1"}, "must be a number"),
        ({"max_s": True}, "must be a number"),
    ]:
        refused("bad_params", lambda kw=kwargs: ClipParams(**kw), match)


def test_nothing_is_repaired_silently_a_refused_episode_makes_no_clips():
    t = np.arange(0, 20, 0.1)
    xyz = np.zeros((len(t), 3))
    xyz[50] = np.nan
    with pytest.raises(ClipInputError):
        propose_clips(ClipSignals(t, xyz))


# --- Parameters, hash, record ----------------------------------------------


def test_the_parameter_hash_changes_with_every_parameter_and_only_then():
    base = ClipParams()
    assert ClipParams().sha256() == base.sha256()
    assert ClipParams(min_s=1, max_s=12).sha256() == base.sha256()  # ints = floats
    hashes = {base.sha256()}
    for name, value in [
        ("min_s", 1.5),
        ("max_s", 11.0),
        ("smooth_s", 0.6),
        ("min_prominence", 0.25),
        ("max_level", 0.4),
        ("min_gap_s", 1.0),
        ("split_window", 0.2),
        ("gripper_min_travel", 0.3),
        ("gripper_min_travel_abs", 0.002),
    ]:
        hashes.add(ClipParams(**{name: value}).sha256())
    assert len(hashes) == 10
    assert len(base.sha256()) == 64


def test_the_record_carries_schema_versions_hash_and_sources():
    signals = episode(30.0, stops=(8.0, 16.0), grip=(12.0,))
    params = ClipParams(min_gap_s=3.0)
    record = json.loads(json.dumps(propose_clips(signals, params).as_dict()))
    assert record["schema_version"] == "levi.clips.v1"
    assert record["algorithm_version"] == clips.VERSION
    assert record["params"] == params.as_dict()
    assert record["params_sha256"] == params.sha256()
    assert record["param_sources"]["min_s"] == "paper"
    assert record["param_sources"]["smooth_s"] == "ours"
    first = record["clips"][0]
    assert set(first) == {
        "start_s",
        "end_s",
        "duration_s",
        "start_reason",
        "end_reason",
        "flags",
        "reasons",
    }
    assert first["start_reason"] == "episode_start"
    # Different parameters, different hash, in the output too.
    other = propose_clips(signals, ClipParams(min_gap_s=1.0)).as_dict()
    assert other["params_sha256"] != record["params_sha256"]


def test_a_record_is_read_back_and_a_forged_one_is_refused():
    proposal = propose_clips(episode(30.0, stops=(8.0,), grip=(12.0,)))
    record = proposal.as_dict()
    assert clips.ClipProposal.from_dict(record) == proposal

    def forged(edit):
        data = json.loads(json.dumps(record))
        edit(data)
        return lambda: clips.ClipProposal.from_dict(data)

    refused("bad_record", forged(lambda d: d.update(schema_version="x")), "schema")
    refused(
        "bad_record",
        forged(lambda d: d["params"].update(min_s=2.0)),
        "params_sha256",
    )
    refused(
        "bad_record",
        forged(lambda d: d["clips"][1].update(start_s=d["clips"][1]["start_s"] + 1)),
        "follow one another",
    )
    refused("bad_record", forged(lambda d: d.pop("clips")), "not a levi.clips.v1")
    refused("bad_record", forged(lambda d: d["params"].update(zzz=1)), "not a levi")


def test_the_core_imports_only_numpy_and_reads_no_file():
    tree = ast.parse((ROOT / "levi/events/clips.py").read_text())
    top = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            top.add("." * node.level + (node.module or ""))
    assert top <= {
        "hashlib",
        "json",
        "math",
        "dataclasses",
        "itertools",
        "typing",
        "numpy",
        ".",
    }, top
    # The relative imports are the two kernels in the package.
    local = {
        a.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.level == 1
        for a in node.names
    }
    assert local == {"boundary_metrics", "gripper"}
    # Only evaluate_gold may touch files (through calibrate), and baseline
    # readers import what they need inside the function.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "open"
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"read_text", "read_bytes", "open", "write_text"}


# --- The helpers, against brute force ---------------------------------------


def test_minima_and_prominence_by_hand():
    s = np.array([1.0, 0.5, 0.8, 0.2, 0.2, 0.9, 0.4, 0.4, 1.0, 0.1])
    # Runs: 0.5 (index 1), the 0.2 run (3-4, middle 3), the 0.4 run (6-7,
    # middle 6); the 0.1 at the end touches the end and is no minimum.
    assert clips.interior_minima(s) == [1, 3, 6]
    # 0.5: left max 1.0 (the start) , right max up to the lower 0.2 is 0.8:
    #     min(1.0, 0.8) - 0.5.  0.2: both sides end at the series ends or a
    # lower point only at the last sample: left 1.0, right 1.0 -> 0.8.
    # 0.4: left up to the lower 0.2: 0.9; right up to 0.1 (the last): 1.0.
    got = clips.prominences(s, [1, 3, 6])
    assert got == pytest.approx([0.3, 0.8, 0.5])


def test_prominence_matches_a_brute_force_scan():
    def brute(s, i):
        # Walk out while the series is not strictly lower; the highest point
        # reached on each side, the lower of the two, minus the minimum.
        a = i
        while a > 0 and s[a - 1] >= s[i]:
            a -= 1
        b = i
        while b < len(s) - 1 and s[b + 1] >= s[i]:
            b += 1
        return min(s[a : i + 1].max(), s[i : b + 1].max()) - s[i]

    rng = np.random.default_rng(4)
    checked = 0
    for _ in range(300):
        s = rng.integers(0, 6, int(rng.integers(3, 40))).astype(float)
        idx = clips.interior_minima(s)
        got = clips.prominences(s, idx)
        for i, value in zip(idx, got, strict=True):
            assert value == pytest.approx(brute(s, i))
            checked += 1
    assert checked > 200


def test_smoothed_speed_of_steady_motion_is_that_speed_even_at_the_ends():
    for t in [
        np.arange(0, 5, 0.1),
        np.cumsum(np.random.default_rng(1).uniform(0.02, 0.2, 80)),
    ]:
        pos = np.stack([2.0 * t, 0 * t, 0 * t], axis=1)
        speed = clips.smoothed_speed(t, pos, 0.5)
        assert speed == pytest.approx(np.full(len(t), 2.0))


# --- Judging it ---------------------------------------------------------------


def synthetic_gold(count=3):
    out = []
    for k in range(count):
        stops, grip = (6.0 + k, 16.0), (11.0, 21.0 + k)
        signals = episode(30.0, stops=stops, grip=grip, seed=k)
        out.append(
            {
                "id": f"syn-{k}",
                "signals": signals,
                "boundaries": sorted([*stops, *grip]),
                "duration": 30.0 - 0.05,
            }
        )
    return out


def test_evaluate_scores_against_a_synthetic_gold():
    result = clips.evaluate(synthetic_gold())
    at = result["boundary"]["at"]
    assert result["schema_version"] == "levi.clip_proposal_eval.v1"
    assert result["params_sha256"] == ClipParams().sha256()
    assert result["episodes"] == 3 and result["skipped"] == []
    assert at["0.5"]["recall"] == 1.0 and at["0.5"]["precision"] == 1.0
    assert at["0.5"]["false_per_minute"] == 0.0
    assert result["segments"]["0.5"]["mean"] == 1.0
    assert result["clips"]["total"] == 15 and result["baseline"] is None
    assert result["clips"]["boundaries_by_reason"]["boundary_gripper"] == 6
    # The same numbers, by hand, from boundary_metrics.
    expect = boundary_metrics.boundary_scores(
        {k: g["boundaries"] for k, g in enumerate(synthetic_gold())},
        {
            k: [
                b.time_s
                for b in propose_clips(synthetic_gold()[k]["signals"]).boundaries
            ]
            for k in range(3)
        },
        {k: 30.0 - 0.05 for k in range(3)},
        clips.TOLERANCES,
    )
    assert result["boundary"]["at"] == expect["at"]


def test_evaluate_lists_an_episode_it_refuses_and_scores_the_rest():
    gold = synthetic_gold()
    bad = gold[1]["signals"]
    pos = np.array(bad.positions)
    pos[5, 0] = np.nan
    gold[1] = {**gold[1], "signals": ClipSignals(bad.times, pos, bad.gripper)}
    result = clips.evaluate(gold)
    assert result["episodes"] == 2
    assert [s["id"] for s in result["skipped"]] == ["syn-1"]
    assert result["skipped"][0]["code"] == "non_finite"
    assert "row 5" in result["skipped"][0]["message"]


def test_evaluate_scores_a_baseline_on_the_same_gold_and_episodes():
    gold = synthetic_gold()

    def baseline(record):
        # Every second, in a made-up priority order: the gold boundaries first.
        return [*record["boundaries"], *np.arange(1.0, 29.0, 1.0)]

    result = clips.evaluate(gold, baseline=baseline)
    base = result["baseline"]
    assert (
        base["all"]["reference_boundaries"]
        == result["boundary"]["reference_boundaries"]
    )
    assert base["all"]["episodes"] == result["episodes"] == 3
    assert base["all"]["candidates"] == 3 * (4 + 28)
    assert base["all"]["at"]["0.5"]["recall"] == 1.0
    # The first n where the proposer made n boundaries (4 each).
    assert base["same_count"]["candidates"] == result["boundary"]["candidates"] == 12
    assert base["same_count"]["at"]["0.5"]["recall"] == pytest.approx(
        3 * 4 / (3 * 4)
    )  # its first four are the gold boundaries


def test_a_baseline_failure_drops_the_episode_from_both_arms():
    gold = synthetic_gold()

    def baseline(record):
        if record["id"] == "syn-2":
            raise RuntimeError("boom")
        return record["boundaries"]

    result = clips.evaluate(gold, baseline=baseline)
    assert result["episodes"] == 2 and result["baseline"]["all"]["episodes"] == 2
    assert result["skipped"] == [
        {"id": "syn-2", "code": "baseline_error", "message": "RuntimeError('boom')"}
    ]


def fr3_table(duration=30.0, hz=10, close=11.0, reopen=21.0):
    """A pandas table in the layout ``calibrate`` reads: x, y, z, rx, ry, rz,
    gripper width."""
    import pandas as pd

    t = np.arange(0, duration, 1 / hz)
    speed = 0.3 * (1 - 0.95 * np.exp(-(((t - 6.0) / 0.4) ** 2)))
    x = np.cumsum(speed / hz)
    width = np.where((t >= close) & (t < reopen), 0.0, 0.08)
    state = np.stack([x, 0 * x, 0 * x + 0.2, 0 * x, 0 * x, 0 * x, width], axis=1)
    return pd.DataFrame({"timestamp": t, "observation.state": list(state)})


def test_the_baseline_can_be_the_existing_candidate_pipeline():
    table = fr3_table()
    gold = [
        {
            "id": "fr3-0",
            "table": table,
            "boundaries": [6.0, 11.0, 21.0],
            "duration": 29.9,
        }
    ]
    layout = {
        "signals_of": lambda e: clips.signals_from_table(
            e["table"], "observation.state", (0, 1, 2), 6
        )
    }
    result = clips.evaluate(gold, baseline=clips.candidates_baseline(), **layout)
    assert result["boundary"]["at"]["0.5"]["recall"] == 1.0
    assert result["boundary"]["at"]["0.5"]["false_per_minute"] == 0.0
    base = result["baseline"]["all"]
    assert base["candidates"] > 0 and base["reference_boundaries"] == 3
    # The baseline's candidates really are candidates.read's kept ones.
    from levi.events import candidates
    from levi.events.signal_profiles import resolve

    kept = candidates.proposed(
        candidates.read(
            table, calibrate.INFO, resolve(calibrate.INFO, calibrate.PROFILE)
        )
    )
    assert base["candidates"] == len(kept)


def test_the_table_adapter_declares_its_columns_and_refuses_the_rest():
    table = fr3_table()
    signals = clips.signals_from_table(table, "observation.state", (0, 1, 2), 6)
    assert signals.positions.shape == (len(table), 3)
    assert signals.gripper[0] == pytest.approx(0.08)
    with pytest.raises(ClipInputError, match="bad_table"):
        clips.signals_from_table(table, "missing", (0, 1, 2), 6)
    with pytest.raises(ClipInputError, match="bad_table"):
        clips.signals_from_table(table, "observation.state", (0, 1, 2), 9)


# --- Gold only through calibrate.Guard -------------------------------------


def write_capture(folder, n=120):
    folder.mkdir(parents=True)
    pose = ["timestamp_sec", "frame_index", "success_flag", "source_stamp_sec"]
    pose += ["px", "py", "pz", "qx", "qy", "qz", "qw"]
    grip = ["timestamp_sec", "frame_index", "success_flag", "source_stamp_sec"]
    grip += ["finger_left", "finger_right", "gripper_width", "last_gripper_command"]
    with (folder / "end_effector_pose.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(pose)
        for i in range(n):
            t = 1000 + i / 10
            speed_dip = 1 - 0.95 * np.exp(-(((i / 10 - 3.0) / 0.4) ** 2))
            w.writerow([t, i + 1, 1, t, 0.4 + 0.05 * speed_dip * i / 10, 0, 0.2,
                        1, 0, 0, 0])  # fmt: skip
    with (folder / "gripper_state.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(grip)
        for i in range(n):
            t = 1000 + i / 10
            width = 0.08 if i < 40 or i >= 90 else 0.0
            w.writerow([t, i + 1, 1, t, width / 2, width / 2, width,
                        "open" if width else "close"])  # fmt: skip


def write_gold(folder, source, frames, **extra):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "G00.json").write_text(
        json.dumps(
            {
                "episode": "dev/G00",
                "source": source,
                "frames": frames,
                "segments": [
                    {"start_n": 0, "end_n": 40},
                    {"start_n": 40, "end_n": 90},
                    {"start_n": 90, "end_n": frames - 1},
                ],
                **extra,
            }
        )
    )
    return folder


def test_evaluate_gold_reads_a_development_gold_through_the_guard(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("LEVI_POOL_HELDOUT", raising=False)
    write_capture(tmp_path / "captures" / "demo_0001")
    gold = write_gold(tmp_path / "dev-gold", "captures/demo_0001", 120)
    result = clips.evaluate_gold([gold], tmp_path)
    assert result["episodes"] == 1 and result["skipped"] == []
    # Gripper closes at 4.0 s and opens at 9.0 s: both found.
    assert result["boundary"]["at"]["0.5"]["recall"] == 1.0
    assert result["baseline"]["all"]["candidates"] > 0
    assert result["gold"]["episodes"][0]["id"] == "dev/G00"
    assert len(result["gold"]["episodes"][0]["sha256"]) == 2
    assert result["gold"]["guards"]["refuse"] == list(calibrate.REFUSE)
    assert result["baseline"] is not None
    assert clips.evaluate_gold([gold], tmp_path, baseline=False)["baseline"] is None


def test_evaluate_gold_passes_the_gripper_bounds_on(tmp_path, monkeypatch):
    monkeypatch.delenv("LEVI_POOL_HELDOUT", raising=False)
    write_capture(tmp_path / "captures" / "demo_0001")
    gold = write_gold(tmp_path / "dev-gold", "captures/demo_0001", 120)
    seen = []
    real = clips.signals_from_table

    def spy(*args, **kwargs):
        signals = real(*args, **kwargs)
        seen.append(signals.gripper_bounds)
        return signals

    monkeypatch.setattr(clips, "signals_from_table", spy)
    own = clips.evaluate_gold([gold], tmp_path, baseline=False)
    wide = clips.evaluate_gold(
        [gold], tmp_path, baseline=False, gripper_bounds=(0.0, 10.0)
    )
    assert seen[0] is None and seen[-1] == (0.0, 10.0)
    # The capture's gripper moves 0.08 of a declared range of 10: no events.
    assert own["boundary"]["at"]["0.5"]["recall"] == 1.0
    assert wide["boundary"]["at"]["0.5"]["recall"] < 1.0


def test_evaluate_gold_refuses_test_sets_and_excluded_sources(tmp_path, monkeypatch):
    monkeypatch.delenv("LEVI_POOL_HELDOUT", raising=False)
    write_capture(tmp_path / "captures" / "demo_0001")
    # A frozen or held-out gold folder ends the run, not the episode.
    for name in ("screws-frozen", "generic-heldout", "x/held-out"):
        folder = write_gold(tmp_path / name, "captures/demo_0001", 120)
        with pytest.raises(SystemExit, match="refused"):
            clips.evaluate_gold([folder], tmp_path)
    # A source mapped into a test set.
    write_capture(tmp_path / "eval" / "screws_frozen60" / "demo_0001")
    dev = write_gold(tmp_path / "dev", "captures/demo_0001", 120)
    with pytest.raises(SystemExit, match="refused"):
        clips.evaluate_gold(
            [dev], tmp_path, mapping=[("captures/", "eval/screws_frozen60/")]
        )
    # A source on an exclusion list, by path and through LEVI_POOL_HELDOUT.
    listed = tmp_path / "lists" / "set-v1.json"
    listed.parent.mkdir()
    listed.write_text(json.dumps({"episodes": [{"path": "captures/demo_0001"}]}))
    with pytest.raises(SystemExit, match="set-v1"):
        clips.evaluate_gold([dev], tmp_path, exclusion_lists=[listed])
    monkeypatch.setenv("LEVI_POOL_HELDOUT", str(listed))
    with pytest.raises(SystemExit, match="set-v1"):
        clips.evaluate_gold([dev], tmp_path)
    # A guard that is given replaces the lists but still refuses by path.
    monkeypatch.delenv("LEVI_POOL_HELDOUT")
    with pytest.raises(SystemExit, match="refused"):
        clips.evaluate_gold(
            [tmp_path / "screws-frozen"], tmp_path, guard=calibrate.Guard(tmp_path)
        )


def test_evaluate_gold_lists_what_it_skips(tmp_path, monkeypatch):
    monkeypatch.delenv("LEVI_POOL_HELDOUT", raising=False)
    write_capture(tmp_path / "captures" / "demo_0001")
    dev = write_gold(tmp_path / "dev", "captures/demo_0001", 7)  # wrong frames
    (dev / "V.json").write_text(
        json.dumps(
            {"source": "captures/demo_0001", "frames": 120, "episode_success": "void"}
        )
    )
    result = clips.evaluate_gold([dev], tmp_path)
    assert result["episodes"] == 0
    assert sorted(s["reason"].split(" ")[0] for s in result["gold"]["skipped"]) == [
        "frames",
        "void",
    ]


def test_the_registry_names_vpp2_and_the_third_party_rules_still_hold():
    from levi.events import third_party

    registry = third_party.load()
    assert third_party.problems(registry, ROOT) == []
    vpp2 = next(c for c in registry["components"] if c["key"] == "vpp2")
    assert vpp2["use"] == "adopt-idea"
    assert not vpp2["shipped"] and not vpp2["code_copied"]
    assert vpp2["citation"]["arxiv"] == "2610.10270"
    assert vpp2["reference_status"] == "VERIFIED"
    assert "levi/events/clips.py" in vpp2["where"]
    assert "arXiv:2610.10270" in (ROOT / "levi/events/clips.py").read_text()
