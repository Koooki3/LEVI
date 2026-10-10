"""Single-arm and independent two-arm success rates.

Known answers: Newcombe (1998, independent proportions) Table II, method 10,
as reproduced to four decimals by the McGill implementation page
(https://joseph.research.mcgill.ca/PBelisle/ProportionsDiffCIsNewcombe.html,
read 2026-10-10); closed forms of the Clopper-Pearson limits at k = 0 and
k = n; hand-computed hypergeometric sums for Fisher's test.
"""

import math

import analysis_reference as ref
import numpy as np
import pytest

from levi.automatic.analysis import proportions as pr
from levi.automatic.analysis._core import AnalysisInputError, z_of
from levi.live import stats

NEWCOMBE_TABLE_II = [
    # (x1, n1, x2, n2) -> (lower, upper) of p1 - p2, method 10
    ((56, 70, 48, 80), (0.0524, 0.3339)),
    ((9, 10, 3, 10), (0.1705, 0.8090)),
    ((6, 7, 2, 7), (0.0582, 0.8062)),
    ((5, 56, 0, 29), (-0.0381, 0.1926)),
    ((0, 10, 0, 20), (-0.1611, 0.2775)),
    ((0, 10, 0, 10), (-0.2775, 0.2775)),
    ((10, 10, 0, 20), (0.6791, 1.0)),
    ((10, 10, 0, 10), (0.6075, 1.0)),
]


@pytest.mark.parametrize(("counts", "expected"), NEWCOMBE_TABLE_II)
def test_newcombe_independent_matches_the_published_table(counts, expected):
    out = pr.newcombe_independent(*counts)
    assert out["low"] == pytest.approx(expected[0], abs=1e-4)
    assert out["high"] == pytest.approx(expected[1], abs=1e-4)
    assert out["low"] <= out["difference"] <= out["high"]


def test_wilson_limit_of_zero_in_ten_is_the_published_0_2775():
    # Newcombe's Table II rows 5-6: the Wilson upper limit of 0/10.
    low, high = pr.wilson_bounds(0, 10, z_of(0.95))
    assert low == 0.0
    assert high == pytest.approx(0.2775, abs=1e-4)


def test_unrounded_wilson_agrees_with_the_live_page_after_rounding():
    z = z_of(0.95)
    for n in range(1, 61):
        for k in range(n + 1):
            low, high = pr.wilson_bounds(k, n, z)
            assert stats.wilson(k, n, z) == [round(low, 3), round(high, 3)]


def test_proportion_reports_the_live_wilson_interval():
    out = pr.proportion(7, 20)
    assert [out["wilson"]["low"], out["wilson"]["high"]] == stats.wilson(7, 20, z_of(0.95))
    assert out["schema_version"] == "levi.aeri.analysis.v1"


@pytest.mark.parametrize("n", [1, 5, 10, 30, 100])
def test_clopper_pearson_closed_forms_at_the_edges(n):
    # k = 0: upper = 1 - (alpha/2)^(1/n); k = n: lower = (alpha/2)^(1/n).
    low0, high0 = pr.clopper_pearson_bounds(0, n, 0.95)
    lown, highn = pr.clopper_pearson_bounds(n, n, 0.95)
    assert low0 == 0.0 and highn == 1.0
    assert high0 == pytest.approx(1 - 0.025 ** (1 / n), abs=1e-9)
    assert lown == pytest.approx(0.025 ** (1 / n), abs=1e-9)


@pytest.mark.parametrize(("k", "n"), [(1, 10), (5, 10), (3, 20), (17, 30), (50, 100)])
def test_clopper_pearson_limits_solve_their_defining_tail_equations(k, n):
    low, high = pr.clopper_pearson_bounds(k, n, 0.95)
    # Recomputed with term-by-term tails, not the library's binomial code.
    assert ref.binom_upper_tail(k, n, low) == pytest.approx(0.025, abs=1e-8)
    assert ref.binom_lower_tail(k, n, high) == pytest.approx(0.025, abs=1e-8)


def test_clopper_pearson_coverage_is_never_below_nominal():
    n = 25
    bounds = [pr.clopper_pearson_bounds(k, n, 0.95) for k in range(n + 1)]
    for p in np.linspace(0.01, 0.99, 99):
        coverage = sum(
            ref.binom_pmf(k, n, p) for k, (lo, hi) in enumerate(bounds) if lo <= p <= hi
        )
        assert coverage >= 0.95 - 1e-9


def test_wilson_coverage_is_near_nominal_by_monte_carlo():
    rng = np.random.Generator(np.random.PCG64(20261010))
    n, p = 30, 0.3
    z = z_of(0.95)
    ks = rng.binomial(n, p, size=20_000)
    covered = np.mean([lo <= p <= hi for lo, hi in (pr.wilson_bounds(int(k), n, z) for k in ks)])
    # Exact coverage at (30, 0.3) is about 0.95; 20k draws give SE ~ 0.0015.
    assert 0.93 <= covered <= 0.97


def test_proportion_edges():
    empty = pr.proportion(0, 0)
    assert empty["available"] is False and empty["rate"] is None
    full = pr.proportion(10, 10)
    assert full["wilson"]["high"] == 1.0 and full["clopper_pearson"]["high"] == 1.0
    none = pr.proportion(0, 10)
    assert none["wilson"]["low"] == 0.0 and none["clopper_pearson"]["low"] == 0.0
    assert pr.proportion(3.0, 10.0)["k"] == 3
    for bad in ((11, 10), (-1, 5), (True, 3), (1.5, 4)):
        with pytest.raises(AnalysisInputError):
            pr.proportion(*bad)
    with pytest.raises(AnalysisInputError):
        pr.proportion(3, 10, level=1.0)


def test_fisher_hand_computed_tables():
    # 3/3 vs 0/3: weights C(3,x)C(3,3-x) = 1, 9, 9, 1 over C(6,3) = 20.
    assert pr.fisher_exact(3, 3, 0, 3)["p_value"] == pytest.approx(2 / 20)
    # 4/4 vs 0/4: 2 / C(8,4) = 2/70.
    assert pr.fisher_exact(4, 4, 0, 4)["p_value"] == pytest.approx(2 / 70)
    # Identical arms: p = 1.
    assert pr.fisher_exact(5, 10, 5, 10)["p_value"] == pytest.approx(1.0)


def test_fisher_matches_a_float_tolerance_implementation_on_every_small_table():
    for n1, n2 in ((5, 5), (7, 4), (12, 9)):
        pvals = pr.fisher_pvalues(n1, n2)
        for a in range(n1 + 1):
            for c in range(n2 + 1):
                assert pvals[a, c] == pytest.approx(ref.fisher_p_float(a, n1, c, n2), rel=1e-9)


def test_fisher_is_symmetric_in_the_arms_and_in_success_failure():
    for k1, n1, k2, n2 in ((3, 9, 7, 11), (0, 5, 4, 6), (12, 20, 4, 15)):
        p = pr.fisher_exact(k1, n1, k2, n2)["p_value"]
        assert pr.fisher_exact(k2, n2, k1, n1)["p_value"] == pytest.approx(p)
        assert pr.fisher_exact(n1 - k1, n1, n2 - k2, n2)["p_value"] == pytest.approx(p)


def test_boschloo_is_never_larger_than_fisher_and_matches_a_brute_force_maximum():
    n1 = n2 = 5
    pvals = [[ref.fisher_p_float(a, n1, c, n2) for c in range(n2 + 1)] for a in range(n1 + 1)]
    for k1, k2 in ((5, 1), (4, 0), (3, 1), (2, 2)):
        out = pr.boschloo_exact(k1, n1, k2, n2)
        assert out["p_value"] <= out["fisher_p_value"] + 1e-12
        obs = pvals[k1][k2]
        best = 0.0
        for i in range(1, 4000):
            pi = i / 4000
            size = sum(
                ref.binom_pmf(a, n1, pi) * ref.binom_pmf(c, n2, pi)
                for a in range(n1 + 1)
                for c in range(n2 + 1)
                if pvals[a][c] <= obs * (1 + 1e-7)
            )
            best = max(best, size)
        assert out["p_value"] == pytest.approx(min(1.0, best), abs=1e-5)


def test_agresti_caffo_hand_computed():
    # 56/70 vs 48/80: (57/72 - 49/82) +- 1.959964 * sqrt(57*15/72^3 + 49*33/82^3)
    a, b = 57 / 72, 49 / 82
    se = math.sqrt(a * (1 - a) / 72 + b * (1 - b) / 82)
    out = pr.agresti_caffo(56, 70, 48, 80)
    assert out["low"] == pytest.approx(0.05246, abs=2e-5)
    assert out["high"] == pytest.approx(0.33575, abs=2e-5)
    assert out["high"] - out["low"] == pytest.approx(2 * 1.959964 * se, abs=1e-5)


def test_posterior_probability_against_numeric_integration_and_sampling():
    for k_a, n_a, k_b, n_b in ((10, 20, 14, 20), (3, 10, 3, 10), (0, 5, 5, 5), (25, 30, 20, 30)):
        exact = pr.posterior_prob_greater(k_a, n_a, k_b, n_b)["prob_b_greater"]
        numeric = ref.beta_prob_greater_numeric(1 + k_a, 1 + n_a - k_a, 1 + k_b, 1 + n_b - k_b)
        assert exact == pytest.approx(numeric, abs=1e-4)
        rng = np.random.Generator(np.random.PCG64(7))
        sampled = np.mean(rng.beta(1 + k_b, 1 + n_b - k_b, 200_000) > rng.beta(1 + k_a, 1 + n_a - k_a, 200_000))
        assert exact == pytest.approx(sampled, abs=0.005)
    assert pr.posterior_prob_greater(4, 9, 4, 9)["prob_b_greater"] == pytest.approx(0.5)


def test_posterior_is_monotone_in_the_successes_of_b():
    probs = [pr.posterior_prob_greater(10, 20, k, 20)["prob_b_greater"] for k in range(21)]
    assert all(x < y for x, y in zip(probs, probs[1:], strict=False))


def test_two_arm_functions_report_an_empty_arm_as_unavailable():
    assert pr.fisher_exact(0, 0, 3, 5)["available"] is False
    assert pr.boschloo_exact(1, 4, 0, 0)["available"] is False
    assert pr.newcombe_independent(0, 0, 0, 0)["available"] is False
