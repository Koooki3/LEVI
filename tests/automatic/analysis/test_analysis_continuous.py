"""Rank tests, effect sizes and smoothness.

Known answers are hand computations (written out per test); the exact null
distributions are checked against brute-force listings of every sign
pattern or relabelling (``analysis_reference``), never against the
library's own dynamic programme. The minimum-jerk LDLJ is the analytic
value -ln(720 / 1.875^2): the jerk cost of a minimum-jerk movement is
720 D^2 / T^5 and its peak speed 1.875 D / T.
"""

import math

import analysis_reference as ref
import numpy as np
import pytest

from levi.automatic.analysis import continuous as ct
from levi.automatic.analysis import movement as sm
from levi.automatic.analysis._core import AnalysisInputError


def test_signed_rank_hand_computed():
    # Five positive differences: W+ = 15 is the maximum; p = 2 / 2^5.
    out = ct.wilcoxon_signed_rank([(0, d) for d in (1, 2, 3, 4, 5)])
    assert out["statistic"] == 15 and out["mode"] == "exact"
    assert out["p_value"] == pytest.approx(2 / 32)


def test_signed_rank_exact_equals_brute_force_with_ties_and_zeros():
    rng = np.random.Generator(np.random.PCG64(3))
    for _ in range(40):
        n = int(rng.integers(1, 13))
        d = rng.integers(-4, 5, n).astype(float)
        out = ct.wilcoxon_signed_rank([(0.0, x) for x in d])
        if (d != 0).sum() == 0:
            assert out["available"] is False
            continue
        assert out["p_value"] == pytest.approx(
            ref.signed_rank_p_bruteforce(list(d)), abs=1e-12
        )
        assert out["zeros"] == int((d == 0).sum())


def test_signed_rank_normal_approximation_is_close_beyond_the_exact_range():
    rng = np.random.Generator(np.random.PCG64(4))
    d = rng.normal(0.3, 1.0, 60)
    out = ct.wilcoxon_signed_rank([(0.0, x) for x in d])
    assert out["mode"] == "normal"
    ranks = ct._core.midranks(np.abs(d))
    counts = ct.signed_rank_null([round(2 * r) for r in ranks])
    support = np.arange(len(counts))
    exact = ct._two_sided_from_counts(
        counts.astype(float), support, ranks.sum(), round(2 * out["statistic"])
    )
    assert out["p_value"] == pytest.approx(exact, abs=0.01)


def test_rank_sum_hand_computed_and_brute_force():
    out = ct.mann_whitney([1, 2, 3], [4, 5, 6])
    # B holds the top three ranks: 2 of C(6, 3) = 20 splits are as extreme.
    assert out["p_value"] == pytest.approx(2 / 20) and out["u_b"] == 9
    rng = np.random.Generator(np.random.PCG64(5))
    for _ in range(30):
        a = list(rng.integers(0, 6, int(rng.integers(1, 7))).astype(float))
        b = list(rng.integers(0, 6, int(rng.integers(1, 7))).astype(float))
        assert ct.mann_whitney(a, b)["p_value"] == pytest.approx(
            ref.rank_sum_p_bruteforce(a, b), abs=1e-12
        )


def test_rank_sum_symmetry_and_normal_mode():
    rng = np.random.Generator(np.random.PCG64(6))
    a, b = rng.normal(0, 1, 50), rng.normal(0.5, 1, 40)
    ab, ba = ct.mann_whitney(a, b), ct.mann_whitney(b, a)
    assert ab["mode"] == "normal"
    assert ab["p_value"] == pytest.approx(ba["p_value"])
    assert ab["u_b"] + ba["u_b"] == pytest.approx(50 * 40)


def test_cliffs_delta_matches_brute_force_and_the_u_identity():
    rng = np.random.Generator(np.random.PCG64(7))
    for _ in range(30):
        a = rng.integers(0, 8, int(rng.integers(1, 15))).astype(float)
        b = rng.integers(0, 8, int(rng.integers(1, 15))).astype(float)
        delta = ct.cliffs_delta(a, b)["delta"]
        assert delta == pytest.approx(ref.cliffs_delta_bruteforce(a, b))
        u = ct.mann_whitney(a, b)["u_b"]
        assert delta == pytest.approx(2 * u / (len(a) * len(b)) - 1)
    assert ct.cliffs_delta([1, 2], [3, 4])["delta"] == 1.0
    assert ct.cliffs_delta([3, 4], [1, 2])["delta"] == -1.0
    assert ct.cliffs_delta([1, 1], [1, 1])["delta"] == 0.0


def test_hodges_lehmann_hand_computed():
    # Walsh averages of 1, 2, 3: 1, 1.5, 2, 2, 2.5, 3; median 2.
    paired = ct.hodges_lehmann([(0, 1), (0, 2), (0, 3)], paired=True, seed=0)
    assert paired["estimate"] == pytest.approx(2.0)
    # Cross differences of b = (3, 5) over a = (1, 2): 2, 1, 4, 3; median 2.5.
    two = ct.hodges_lehmann([1, 2], [3, 5], paired=False, seed=0)
    assert two["estimate"] == pytest.approx(2.5)
    assert two["low"] <= two["estimate"] <= two["high"]


def test_hodges_lehmann_shift_equivariance_and_brute_force():
    rng = np.random.Generator(np.random.PCG64(8))
    a, b = rng.normal(0, 1, 25), rng.normal(1, 1, 30)
    base = ct.hodges_lehmann(a, b, paired=False, seed=1)["estimate"]
    assert ct.hodges_lehmann(a, b + 2.5, paired=False, seed=1)[
        "estimate"
    ] == pytest.approx(base + 2.5)
    assert base == pytest.approx(ref.median([y - x for x in a for y in b]))
    d = rng.normal(0, 1, 15)
    walsh = [(d[i] + d[j]) / 2 for i in range(15) for j in range(i, 15)]
    est = ct.hodges_lehmann([(0.0, x) for x in d], paired=True, seed=1)["estimate"]
    assert est == pytest.approx(ref.median(walsh))


def test_hodges_lehmann_caps_and_edges():
    big = ct.hodges_lehmann(
        [(0.0, float(i)) for i in range(ct.HL_PAIRS_MAX + 1)], paired=True, seed=0
    )
    assert big["available"] is False and big["caveats"][-1]["code"] == "too_large"
    mid = ct.hodges_lehmann([(0.0, float(i)) for i in range(500)], paired=True, seed=0)
    assert mid["low"] is None and any(
        c["code"] == "interval_skipped" for c in mid["caveats"]
    )
    assert ct.hodges_lehmann([], paired=True, seed=0)["available"] is False
    with pytest.raises(AnalysisInputError):
        ct.hodges_lehmann([(0, 1)], [1], paired=True, seed=0)


def test_improvement_share_hand_computed():
    pairs = [(10, 8), (10, 12), (5, 5), (7, 3)]
    # Lower is better (e.g. completion time): B better in 2, tie in 1.
    assert ct.improvement_share(pairs, higher_is_better=False)[
        "share"
    ] == pytest.approx(2.5 / 4)
    assert ct.improvement_share(pairs, higher_is_better=True)["share"] == pytest.approx(
        1.5 / 4
    )


def test_rank_tests_report_missing_and_reject_infinite():
    out = ct.mann_whitney([1, float("nan"), 3], [2, 4])
    assert out["n_a"] == 2 and any(c["code"] == "dropped_nan" for c in out["caveats"])
    with pytest.raises(AnalysisInputError):
        ct.mann_whitney([1, float("inf")], [2])
    assert ct.mann_whitney([], [1])["available"] is False
    assert ct.wilcoxon_signed_rank([])["available"] is False


def min_jerk(
    amplitude: float, duration: float, rate: float, dims=(1.0, 0.5)
) -> np.ndarray:
    t = np.arange(0, duration + 1e-12, 1 / rate) / duration
    s = 10 * t**3 - 15 * t**4 + 6 * t**5
    return amplitude * np.outer(s, np.array(dims))


def test_ldlj_of_minimum_jerk_matches_the_analytic_value():
    analytic = -math.log(720 / 1.875**2)
    for amplitude, duration in ((0.1, 1.0), (2.0, 3.0), (0.5, 0.7)):
        out = sm.smoothness(min_jerk(amplitude, duration, 1000.0), rate_hz=1000.0)
        assert out["ldlj"] == pytest.approx(analytic, abs=0.02)


def test_sparc_is_amplitude_invariant_and_penalises_two_submovements():
    one = min_jerk(1.0, 2.0, 100.0)
    out1 = sm.smoothness(one, rate_hz=100.0)
    out2 = sm.smoothness(3.0 * one, rate_hz=100.0)
    assert out1["sparc"] == pytest.approx(out2["sparc"], abs=1e-12)
    two = np.vstack([one, one[1:] + one[-1]])
    assert sm.smoothness(two, rate_hz=100.0)["sparc"] < out1["sparc"]
    rng = np.random.Generator(np.random.PCG64(1))
    noisy = one + rng.normal(0, 0.002, one.shape)
    assert sm.smoothness(noisy, rate_hz=100.0)["ldlj"] < out1["ldlj"]


def test_smoothness_edges():
    assert sm.smoothness([0.0, 1.0, 2.0], rate_hz=10.0)["available"] is False
    with pytest.raises(AnalysisInputError):
        sm.smoothness([0.0, float("nan"), 1.0, 2.0, 3.0], rate_hz=10.0)
    with pytest.raises(AnalysisInputError):
        sm.smoothness(np.zeros((10, 2)), rate_hz=10.0)
    with pytest.raises(AnalysisInputError):
        sm.smoothness(np.zeros((10, 2)), rate_hz=0.0)
