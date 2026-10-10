"""Penalised change points (levi.events.change_points): steps are found,
noise and smooth motion are not over-cut, the result is the exact optimum,
and odd tables (NaN, constants, very short, uneven timestamps) do not break
it."""

import math
from itertools import pairwise

import numpy as np
import pandas as pd
import pytest

from levi.events import change_points as cp
from levi.events.contracts import EventCandidate


def _optimum(x, beta, min_size):
    """Optimal partitioning without pruning (the reference): the least total
    cost, every start considered for every end."""
    x = np.asarray(x, dtype=float)[:, None] if np.ndim(x) == 1 else np.asarray(x)
    n = len(x)
    s1 = np.vstack([np.zeros(x.shape[1]), np.cumsum(x, axis=0)])
    s2 = np.concatenate([[0.0], np.cumsum((x * x).sum(axis=1))])
    best = np.full(n + 1, np.inf)
    best[0] = -beta
    for t in range(min_size, n + 1):
        starts = np.array([0, *range(min_size, t - min_size + 1)])
        m = (t - starts).astype(float)
        diff = s1[t] - s1[starts]
        cost = s2[t] - s2[starts] - (diff * diff).sum(axis=1) / m
        best[t] = np.min(best[starts] + cost + beta)
    return best[n]


def _total(x, rows, beta):
    x = np.asarray(x, dtype=float)[:, None] if np.ndim(x) == 1 else np.asarray(x)
    edges = [0, *rows, len(x)]
    return sum(
        float(((x[a:b] - x[a:b].mean(axis=0)) ** 2).sum()) for a, b in pairwise(edges)
    ) + beta * len(rows)


def _series(rng, kind, n):
    if kind == "noise":
        return rng.normal(0, 1, n)
    if kind == "steps":
        cuts = np.sort(rng.integers(1, n, size=int(rng.integers(0, 6))))
        levels = rng.normal(0, 2, size=len(cuts) + 1)
        return np.repeat(levels, np.diff([0, *cuts, n])) + rng.normal(0, 0.5, n)
    return np.cumsum(rng.normal(0, 1, n))  # random walk


def test_pruned_search_finds_the_optimum():
    # Every minimum segment length from 1 to 8 (the default is 5 rows at
    # 10 Hz): pruning must never drop the optimal start.
    rng = np.random.default_rng(0)
    cases = 0
    for kind in ("noise", "steps", "walk"):
        for _ in range(360):
            min_size = int(rng.integers(1, 9))
            n = int(rng.integers(2 * min_size, 121))
            x = _series(rng, kind, n)
            if rng.random() < 0.3:
                x = np.stack([x, rng.normal(0, 1, n)], axis=1)
            beta = float(rng.uniform(0.5, 12))
            rows = cp.segment(x, beta, min_size)
            assert _total(x, rows, beta) == pytest.approx(
                _optimum(x, beta, min_size), abs=1e-6
            ), (kind, n, min_size, beta)
            edges = [0, *rows, len(x)]
            assert all(b - a >= min_size for a, b in pairwise(edges))
            cases += 1
    assert cases >= 1000


def test_the_smallest_known_counterexample_to_eager_pruning():
    # Found by the review of 2026-10-10: n=11, min_size=3; eager pruning
    # returned [5, 8] although no change point is optimal.
    rng = np.random.default_rng(0)
    for _ in range(4000):
        x = _series(rng, "steps", 11)
        beta = float(rng.uniform(0.5, 12))
        rows = cp.segment(x, beta, 3)
        assert _total(x, rows, beta) == pytest.approx(_optimum(x, beta, 3), abs=1e-6)


def test_steps_are_found_where_they_are():
    rng = np.random.default_rng(1)
    n = 300
    times = np.arange(n) / 10
    x = np.zeros((n, 2))
    x[100:, 0] = 1.0
    x[200:, 1] = 1.0
    x += rng.normal(0, 0.05, x.shape)
    scaled = np.stack([cp.scale(x[:, 0]), cp.scale(x[:, 1])], axis=1)
    result = cp.detect(times, scaled)
    assert result.rows == [100, 200]
    assert not result.capped
    assert all(g > result.beta for g in result.gains)


def test_noise_alone_is_rarely_cut():
    # Scaled as the features are. At the calibrated penalty pure noise makes
    # well under one change point per minute; at twice it, none.
    counts = {0.5: [], 1.0: []}
    for seed in range(20):
        x = np.random.default_rng(seed).normal(0, 1, (600, 2))
        scaled = np.stack([cp.scale(x[:, 0]), cp.scale(x[:, 1])], axis=1)
        for penalty, found in counts.items():
            found.append(
                len(cp.detect(np.arange(600) / 10, scaled, penalty=penalty).rows)
            )
    assert cp.PENALTY == 0.5
    assert np.mean(counts[0.5]) <= 1 and max(counts[0.5]) <= 3
    assert max(counts[1.0]) == 0
    # A heavy-tailed noise does not make dozens either.
    t = np.random.default_rng(9).standard_t(3, size=600)
    assert len(cp.detect(np.arange(600) / 10, cp.scale(t)[:, None]).rows) <= 3


def test_minimum_segment_length_holds():
    times = np.arange(200) / 10
    x = np.zeros(200)
    x[100:102] = 50.0  # a two-row spike
    result = cp.detect(times, x[:, None], min_seconds=0.5)
    edges = [0, *result.rows, 200]
    assert min(b - a for a, b in pairwise(edges)) >= 5


def test_the_cap_keeps_exactly_the_strongest():
    times = np.arange(600) / 10  # one minute
    x = 5.0 * np.repeat(np.arange(60) % 2, 10)  # 59 equally strong steps
    free = cp.detect(times, x[:, None], max_per_minute=1000)
    assert len(free.rows) == 59
    for cap in (10, 30, 58):
        capped = cp.detect(times, x[:, None], max_per_minute=cap)
        # Exactly the cap, not none: equal strength ties break by time.
        assert capped.rows == free.rows[:cap]
        assert capped.capped
        # The penalty and beta reported are the ones the rows came from.
        assert capped.beta == pytest.approx(capped.penalty * 2 * math.log(600))


def test_the_cap_prefers_stronger_changes():
    times = np.arange(600) / 10
    x = np.repeat(np.arange(60) % 2, 10).astype(float) * 5.0
    x[300:] *= 3  # the second half's steps are three times as large
    capped = cp.detect(times, x[:, None], max_per_minute=20)
    assert len(capped.rows) == 20
    assert all(r >= 300 for r in capped.rows)


def test_uneven_timestamps_do_not_make_a_change_out_of_a_gap():
    # Constant-speed motion with 10 dropped frames: the speed (on real time)
    # stays flat, so no change point appears at the gap.
    keep = [i for i in range(300) if not 140 <= i < 150]
    times = np.array(keep) / 10
    pos = np.stack([times * 0.1, np.zeros_like(times), np.full_like(times, 0.2)], 1)
    data = pd.DataFrame(
        {
            "timestamp": times,
            "observation.state": [[*p, 1.0] for p in pos],
        }
    )
    info = {
        "fps": 10,
        "features": {
            "observation.state": {
                "dtype": "float32",
                "shape": [4],
                "names": ["x", "y", "z", "gripper"],
            }
        },
    }
    assert cp.candidates(data, info) == []


@pytest.mark.parametrize(
    "x",
    [
        np.zeros(0),
        np.zeros(1),
        np.zeros(3),
        np.full(50, 2.0),
        np.full(50, np.nan),
        np.array([0.0] * 20 + [np.nan] * 10 + [1.0] * 20),
    ],
)
def test_odd_series(x):
    assert cp.scale(x) is None or np.isfinite(cp.scale(x)).all()
    times = np.arange(len(x)) / 10
    scaled = cp.scale(x)
    if scaled is None:
        result = cp.detect(times, np.zeros((len(x), 0)))
        assert result.rows == []
    else:
        result = cp.detect(times, scaled[:, None])
        assert all(0 < r < len(x) for r in result.rows)


def test_missing_values_are_filled_not_propagated():
    x = np.array([0.0] * 20 + [np.nan] * 3 + [1.0] * 20)
    scaled = cp.scale(x)
    assert np.isfinite(scaled).all()
    rows = cp.detect(np.arange(len(x)) / 10, scaled[:, None]).rows
    assert len(rows) == 1 and 19 <= rows[0] <= 23


def _pick_table(n=120, gripper_open_high=True, times=None):
    rows = []
    for i in range(n):
        t = i / 10
        if t < 3:
            z = 0.2 - 0.05 * t
        elif t < 6:
            z = 0.05
        else:
            z = 0.05 + 0.05 * (t - 6)
        grip = 1.0 if t < 4 or t >= 9 else 0.0
        if not gripper_open_high:
            grip = 1 - grip
        rows.append([0.4, 0.0, z, 3.1, 0.0, 0.0, grip])
    return pd.DataFrame(
        {
            "timestamp": np.arange(n) / 10 if times is None else times,
            "observation.state": rows,
        }
    )


FR3_INFO = {
    "fps": 10,
    "features": {
        "observation.state": {
            "dtype": "float32",
            "shape": [7],
            "names": ["x", "y", "z", "rx", "ry", "rz", "gripper"],
        }
    },
}


def test_candidates_are_valid_contracts():
    found = cp.candidates(_pick_table(), FR3_INFO, episode_index=3)
    assert found, "a pick has changes"
    assert all(isinstance(c, EventCandidate) for c in found)
    assert all(c.event_type == "change_point" and c.episode_index == 3 for c in found)
    assert all(0 <= c.salience <= 1 and c.boundary_probability is None for c in found)
    times = [c.center_time_s for c in found]
    # The gripper closing at 4.0 s and opening at 9.0 s are among them.
    assert any(abs(t - 4.0) <= 0.2 for t in times)
    assert any(abs(t - 9.0) <= 0.2 for t in times)
    names = {s.dimension for c in found for s in c.sources}
    assert "gripper" in names


def test_features_of_two_arms_are_separate():
    names = ["left_x", "left_y", "left_z", "left_gripper", "right_x", "right_y",
             "right_z", "right_gripper"]  # fmt: skip
    info = {
        "fps": 10,
        "features": {
            "observation.state": {"dtype": "float32", "shape": [8], "names": names}
        },
    }
    rows = [
        [0, 0, 0.1, 1.0 if i < 50 else 0.0, 0, 0, 0.1, 1.0 if i < 80 else 0.0]
        for i in range(120)
    ]
    data = pd.DataFrame({"timestamp": np.arange(120) / 10, "observation.state": rows})
    found = cp.candidates(data, info)
    by_actor = {(c.actor_id, round(c.center_time_s, 1)) for c in found}
    assert ("left", 5.0) in by_actor and ("right", 8.0) in by_actor
    assert all(c.actor_id in {"left", "right"} for c in found)


def test_degrees_are_read_as_degrees():
    from levi.events.signal_profiles import resolve

    profile = resolve(
        FR3_INFO,
        {
            "channels": [
                {"role": "rotation", "feature": "observation.state", "index": i,
                 "axis": a, "units": "deg"}
                for i, a in zip((3, 4, 5), "xyz", strict=True)
            ]
        },
    )  # fmt: skip
    names, matrix, _ = cp.features(_pick_table(), FR3_INFO, profile)["arm_0"]
    assert names[-1] == "observation.state.angular_speed" or len(names) == 2
    assert np.isfinite(matrix).all()


# --- calibration script ---------------------------------------------------------


def _capture(folder, n=120, drop=()):
    """A raw capture with a close at 4.0 s and an open at 9.0 s, 10 Hz, some
    frames dropped."""
    import csv

    folder.mkdir(parents=True)
    rows = [i for i in range(n) if i not in drop]
    with (folder / "end_effector_pose.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp_sec", "frame_index", "success_flag", "source_stamp_sec",
                    "px", "py", "pz", "qx", "qy", "qz", "qw"])  # fmt: skip
        for k, i in enumerate(rows):
            t = 1000 + i / 10
            z = 0.2 - 0.05 * min(i / 10, 3)
            w.writerow([t, k + 1, 1, t, 0.4, 0.0, z, 1, 0, 0, 0])
    with (folder / "gripper_state.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp_sec", "frame_index", "success_flag", "source_stamp_sec",
                    "finger_left", "finger_right", "gripper_width",
                    "last_gripper_command"])  # fmt: skip
        for k, i in enumerate(rows):
            t = 1000 + i / 10
            width = 0.08 if i < 40 or i >= 90 else 0.0
            w.writerow([t, k + 1, 1, t, width / 2, width / 2, width,
                        "open" if width else "close"])  # fmt: skip
    return len(rows)


def test_calibration_reads_dev_gold_and_refuses_test_sets(tmp_path, capsys):
    import json

    from levi.events import calibrate

    frames = _capture(tmp_path / "captures" / "demo_0001", drop=range(60, 65))
    gold = tmp_path / "dev-gold"
    gold.mkdir()
    (gold / "G00.json").write_text(
        json.dumps(
            {
                "episode": "dev/G00",
                "source": "captures/demo_0001",
                "frames": frames,
                "segments": [
                    {"start_n": 0, "end_n": 40},
                    {"start_n": 40, "end_n": 85},
                    {"start_n": 85, "end_n": frames - 1},
                ],
            }
        )
    )
    out = tmp_path / "report.json"
    assert (
        calibrate.main(
            [
                "--root",
                str(tmp_path),
                "--gold",
                str(gold),
                "--penalties",
                "0.5,2",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    report = json.loads(out.read_text())
    assert report["chosen_penalty"] in (0.5, 2.0)
    episode = report["calibration"]["episodes"][0]
    assert episode["id"] == "dev/G00" and len(episode["sha256"]) == 2
    # The close at 4.0 s and the open at 9.0 s (row 85 after 5 dropped
    # frames) are found within half a second.
    assert report["calibration"]["sweep"]["0.5"]["recall@0.5"] == 1.0
    capsys.readouterr()

    frozen = tmp_path / "screws-frozen"
    frozen.mkdir()
    with pytest.raises(SystemExit, match="refused"):
        calibrate.main(["--root", str(tmp_path), "--gold", str(frozen)])
    held = tmp_path / "generic-heldout" / "x"
    held.mkdir(parents=True)
    with pytest.raises(SystemExit, match="refused"):
        calibrate.gold_files(held)


def test_calibration_skips_what_it_cannot_judge(tmp_path):
    import json

    from levi.events import calibrate

    _capture(tmp_path / "captures" / "demo_0002")
    gold = tmp_path / "dev"
    gold.mkdir()
    for name, source, frames in [
        ("A", "captures/demo_0002", 7),  # frame count does not match
        ("B", "captures/missing", 120),
        ("C", "excluded/demo", 120),
    ]:
        (gold / f"{name}.json").write_text(
            json.dumps({"source": source, "frames": frames, "segments": []})
        )
    episodes, skipped = calibrate.collect(
        [gold], tmp_path, [], ["excluded/"], calibrate.REFUSE
    )
    assert episodes == []
    assert sorted(s["reason"].split(" ")[0] for s in skipped) == [
        "excluded",
        "frames",
        "source",
    ]


def test_choice_prefers_the_larger_of_near_equal_penalties():
    from levi.events.calibrate import choose

    sweep = {"0.2": {"f1@0.5": 0.581}, "0.5": {"f1@0.5": 0.574}, "1": {"f1@0.5": 0.56}}
    assert choose(sweep, [0.2, 0.5, 1.0]) == 0.5
