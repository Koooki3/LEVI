"""Kaplan-Meier, log-rank and RMST.

Known answers are hand computations written out per test (product-limit
steps, Greenwood terms, the 2 x 2 hypergeometric log-rank sums, the area
under a step function). The log-rank statistic is also recomputed with a
separately written two-arm loop.
"""

import math

import numpy as np
import pytest

from levi.automatic.analysis import survival as sv
from levi.automatic.analysis._core import AnalysisInputError

TIMES = [1, 2, 2, 3, 4, 5]
EVENTS = [1, 1, 0, 1, 0, 1]


def test_kaplan_meier_hand_computed():
    out = sv.kaplan_meier(TIMES, EVENTS)
    steps = {s["time"]: s for s in out["steps"]}
    # t=1: 6 at risk, 1 event -> 5/6. t=2: 5 at risk (the censored 2 counts),
    # 1 event -> 2/3. t=3: 3 at risk -> 4/9. t=5: 1 at risk -> 0.
    assert [steps[t]["at_risk"] for t in (1, 2, 3, 5)] == [6, 5, 3, 1]
    assert steps[1]["survival"] == pytest.approx(5 / 6)
    assert steps[2]["survival"] == pytest.approx(2 / 3)
    assert steps[3]["survival"] == pytest.approx(4 / 9)
    assert steps[5]["survival"] == 0.0
    assert steps[3]["incidence"] == pytest.approx(5 / 9)
    # Greenwood: S^2 * (1/(6*5) + 1/(5*4)) at t = 2.
    assert steps[2]["se"] == pytest.approx(2 / 3 * math.sqrt(1 / 30 + 1 / 20))
    assert out["median_time"] == 3.0
    assert out["events"] == 4 and out["censored"] == 2
    lo, hi = steps[2]["low"], steps[2]["high"]
    assert 0 < lo < 2 / 3 < hi < 1


def test_kaplan_meier_edges():
    censored = sv.kaplan_meier([3, 4, 5], [0, 0, 0])
    assert censored["steps"] == [] and censored["median_time"] is None
    empty = sv.kaplan_meier([], [])
    assert empty["available"] is False
    nan = sv.kaplan_meier([1, None, 3], [1, 1, float("nan")])
    assert nan["dropped"] == 2 and nan["n"] == 1
    with pytest.raises(AnalysisInputError):
        sv.kaplan_meier([1, 2], [1])
    with pytest.raises(AnalysisInputError):
        sv.kaplan_meier([-1], [1])
    with pytest.raises(AnalysisInputError):
        sv.kaplan_meier([1], [2])
    with pytest.raises(AnalysisInputError):
        sv.kaplan_meier([float("inf")], [1])


def test_logrank_hand_computed():
    # A: events at 1 and 3. B: event at 2, censored at 4.
    # t=1: risk 2+2, O_A=1, E_A=1/2, V=1/4.  t=2: risk 1+2, E_A=1/3, V=2/9.
    # t=3: risk 1+1, O_A=1, E_A=1/2, V=1/4.  O-E = 2/3, V = 13/18 -> 8/13.
    out = sv.logrank({"A": ([1, 3], [1, 1]), "B": ([2, 4], [1, 0])})
    assert out["statistic"] == pytest.approx(8 / 13)
    assert out["observed"] == pytest.approx([2, 1])
    assert out["expected"] == pytest.approx([4 / 3, 5 / 3])
    assert out["p_value"] == pytest.approx(math.erfc(math.sqrt(8 / 13 / 2)))


def two_arm_logrank_loop(ta, ea, tb, eb) -> float:
    """Separately written two-arm log-rank: plain loops over event times."""
    times = sorted({t for t, e in zip(ta + tb, ea + eb, strict=True) if e})
    num = var = 0.0
    for t in times:
        ra = sum(1 for x in ta if x >= t)
        rb = sum(1 for x in tb if x >= t)
        da = sum(1 for x, e in zip(ta, ea, strict=True) if x == t and e)
        db = sum(1 for x, e in zip(tb, eb, strict=True) if x == t and e)
        r, d = ra + rb, da + db
        num += da - d * ra / r
        if r > 1:
            var += d * (ra / r) * (rb / r) * (r - d) / (r - 1)
    return num * num / var


def test_logrank_matches_a_separate_loop_on_random_data_with_ties():
    rng = np.random.Generator(np.random.PCG64(12))
    for _ in range(25):
        ta = rng.integers(1, 15, 20).tolist()
        tb = rng.integers(1, 15, 25).tolist()
        ea = (rng.random(20) < 0.7).astype(int).tolist()
        eb = (rng.random(25) < 0.7).astype(int).tolist()
        out = sv.logrank({"a": (ta, ea), "b": (tb, eb)})
        assert out["statistic"] == pytest.approx(
            two_arm_logrank_loop(ta, ea, tb, eb), rel=1e-10
        )


def test_logrank_is_label_symmetric_and_zero_for_identical_arms():
    data = ([1, 2, 3, 5, 8], [1, 0, 1, 1, 0])
    out = sv.logrank({"x": data, "y": data, "z": data})
    assert out["statistic"] == pytest.approx(0.0, abs=1e-12) and out["df"] == 2
    a, b = ([1, 3, 4], [1, 1, 0]), ([2, 6, 7], [1, 1, 1])
    assert sv.logrank({"a": a, "b": b})["statistic"] == pytest.approx(
        sv.logrank({"b": b, "a": a})["statistic"]
    )
    none = sv.logrank({"a": ([1, 2], [0, 0]), "b": ([1, 2], [0, 0])})
    assert none["available"] is False
    with pytest.raises(AnalysisInputError):
        sv.logrank({"a": data})


def test_rmst_hand_computed_and_difference_interval():
    # Area under 1, 5/6, 2/3, 4/9 on [0,1), [1,2), [2,3), [3,4) = 53/18.
    assert sv.rmst_value(
        np.array(TIMES, float), np.array(EVENTS), 4.0
    ) == pytest.approx(53 / 18)
    # Horizon inside a step: 1 + 5/6 * 0.5.
    assert sv.rmst_value(
        np.array(TIMES, float), np.array(EVENTS), 1.5
    ) == pytest.approx(1 + 5 / 12)
    out = sv.rmst(
        {"A": (TIMES, EVENTS), "B": ([1, 1, 1, 2, 2, 2], [1, 1, 1, 1, 1, 1])},
        tau=4.0,
        seed=3,
    )
    assert out["arms"]["B"]["rmst"] == pytest.approx(1.5)
    diff = out["differences"]["B"]
    assert diff["difference"] == pytest.approx(1.5 - 53 / 18)
    assert diff["low"] <= diff["difference"] <= diff["high"]
    assert out == sv.rmst(
        {"A": (TIMES, EVENTS), "B": ([1, 1, 1, 2, 2, 2], [1] * 6)}, tau=4.0, seed=3
    )
    with pytest.raises(AnalysisInputError):
        sv.rmst({"A": (TIMES, EVENTS)}, tau=0, seed=0)
    with pytest.raises(AnalysisInputError):
        sv.rmst({"A": ([], [])}, tau=3, seed=0)


def test_km_and_rmst_scale_to_large_arms():
    rng = np.random.Generator(np.random.PCG64(2))
    t = rng.integers(1, 300, 20_000)
    e = rng.integers(0, 2, 20_000)
    out = sv.rmst({"a": (t, e), "b": (t + 1, e)}, tau=300.0, seed=0, resamples=50)
    assert out["differences"]["b"]["difference"] > 0
