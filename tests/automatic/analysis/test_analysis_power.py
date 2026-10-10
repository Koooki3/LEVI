"""Exact power and smallest detectable differences.

Every power number is recomputed two other ways: a brute-force enumeration
written separately (trinomial loop with exact-fraction McNemar p-values;
double loop with R-style float Fisher p-values) and a fixed-seed Monte
Carlo. The design table (levi2/design/aeri-campaign-and-reporting.md §2.7)
was computed by another author with their own script; it is matched cell
by cell.
"""

from itertools import pairwise

import analysis_reference as ref
import numpy as np
import pytest

from levi.automatic.analysis import power
from levi.automatic.analysis._core import AnalysisInputError

DESIGN_TABLE = {
    # n: {baseline: (unpaired Fisher, paired rho=0, paired rho=0.3)}
    20: {0.5: (0.42, 0.44, 0.39), 0.3: (0.48, 0.49, 0.42)},
    25: {0.5: (0.39, 0.40, 0.35), 0.3: (0.41, 0.44, 0.37)},
    30: {0.5: (0.36, 0.37, 0.32), 0.3: (0.39, 0.39, 0.33)},
    50: {0.5: (0.29, 0.29, 0.25), 0.3: (0.29, 0.30, 0.25)},
}
# The design prints 0.37 for n = 25; the width is 0.36496, which rounds to
# 0.36 (the design rounded half up from 0.365). Compared to within 0.006.
WILSON_WIDTH = {20: 0.40, 25: 0.37, 30: 0.34, 50: 0.27}


def test_power_table_reproduces_the_design_table_cell_by_cell():
    table = power.power_table(ns=sorted(DESIGN_TABLE))
    for row in table["rows"]:
        n = row["n"]
        assert row["wilson_width_at_half"] == pytest.approx(WILSON_WIDTH[n], abs=0.006)
        for entry in row["baselines"]:
            want = DESIGN_TABLE[n][entry["baseline"]]
            got = (
                entry["unpaired_fisher"],
                entry["paired_mcnemar"]["0.0"],
                entry["paired_mcnemar"]["0.3"],
            )
            assert got == pytest.approx(want), (n, entry["baseline"])


@pytest.mark.parametrize(
    ("n", "p0", "p1", "rho"),
    [
        (10, 0.5, 0.9, 0.0),
        (15, 0.3, 0.7, 0.3),
        (20, 0.5, 0.8, 0.0),
        (25, 0.2, 0.6, 0.1),
        (12, 0.6, 0.6, 0.0),
    ],
)
def test_paired_power_equals_a_brute_force_trinomial_enumeration(n, p0, p1, rho):
    got = power.power_paired(n, p0, p1, rho=rho)
    assert got == pytest.approx(
        ref.mcnemar_power_bruteforce(n, p0, p1, rho, 0.05), abs=1e-10
    )


@pytest.mark.parametrize(
    ("n", "p0", "p1"), [(8, 0.5, 0.9), (12, 0.3, 0.8), (20, 0.5, 0.85), (15, 0.4, 0.4)]
)
def test_unpaired_power_equals_a_brute_force_double_loop(n, p0, p1):
    got = power.power_unpaired(n, p0, p1)
    assert got == pytest.approx(ref.fisher_power_bruteforce(n, p0, p1, 0.05), abs=1e-10)


def test_power_agrees_with_monte_carlo_simulation():
    rng = np.random.Generator(np.random.PCG64(2026))
    n, p0, p1 = 30, 0.5, 0.86
    draws = 20_000
    x0 = rng.binomial(n, p0, draws)
    x1 = rng.binomial(n, p1, draws)
    cache = {}
    hits = 0
    for a, c in zip(x0.tolist(), x1.tolist(), strict=True):
        if (a, c) not in cache:
            cache[(a, c)] = ref.fisher_p_float(a, n, c, n) <= 0.05
        hits += cache[(a, c)]
    sim = hits / draws
    exact = power.power_unpaired(n, p0, p1)
    assert abs(sim - exact) < 4 * np.sqrt(exact * (1 - exact) / draws)

    # Paired: simulate correlated pairs directly from the cell probabilities.
    p0, p1, rho = 0.5, 0.85, 0.3
    p11 = p0 * p1 + rho * np.sqrt(p0 * (1 - p0) * p1 * (1 - p1))
    cells = [p11, p0 - p11, p1 - p11, 1 - p0 - p1 + p11]
    counts = rng.multinomial(n, cells, size=draws)
    hits = sum(
        float(ref.mcnemar_exact_p(int(f), int(g))) <= 0.05 for f, g in counts[:, 1:3]
    )
    sim = hits / draws
    exact = power.power_paired(n, p0, p1, rho=rho)
    assert abs(sim - exact) < 4 * np.sqrt(exact * (1 - exact) / draws)


def test_exact_tests_hold_their_size_under_the_null():
    for n in (10, 20, 30):
        for p in (0.2, 0.5, 0.8):
            assert power.power_unpaired(n, p, p) <= 0.05 + 1e-12
            assert power.power_paired(n, p, p) <= 0.05 + 1e-12


def test_power_grows_with_the_difference():
    for design in ("paired", "unpaired"):
        values = [
            (
                power.power_paired(30, 0.3, 0.3 + d)
                if design == "paired"
                else power.power_unpaired(30, 0.3, 0.3 + d)
            )
            for d in np.arange(0.0, 0.7, 0.05)
        ]
        assert all(a <= b + 1e-12 for a, b in pairwise(values))


def test_detectable_difference_falls_with_n_across_the_planning_range():
    table = power.power_table()
    ns = [row["n"] for row in table["rows"]]
    assert ns == [10, 15, 20, 25, 30, 40, 50, 60, 70, 80, 90, 100]
    for b in range(2):
        unpaired = [row["baselines"][b]["unpaired_fisher"] for row in table["rows"]]
        paired = [row["baselines"][b]["paired_mcnemar"]["0.0"] for row in table["rows"]]
        # n = 10 pairs cannot reach 80% power even against p1 = 1 (None):
        # at least 6 discordant pairs are needed, P(Bin(10, 0.5) >= 6) = 0.377.
        for series in (unpaired, paired):
            series = [2.0 if x is None else x for x in series]
            assert all(x <= 1 for x in series[1:])
            assert series[0] > series[-1]
            assert all(a >= b - 0.011 for a, b in pairwise(series))
    assert table["exploratory"] is False and table["kind"] == "power_table"


def test_correlation_within_pairs_helps_the_paired_design():
    plain = power.min_detectable_difference(30, 0.5, design="paired", rho=0.0)
    correlated = power.min_detectable_difference(30, 0.5, design="paired", rho=0.3)
    assert correlated < plain


def test_large_n_uses_the_normal_approximation_and_stays_close_to_exact():
    exact = power.min_detectable_difference(200, 0.5, design="paired")
    approx = power.min_detectable_difference(201, 0.5, design="paired")
    assert abs(exact - approx) <= 0.02
    exact_u = power.min_detectable_difference(200, 0.5, design="unpaired")
    approx_u = power.min_detectable_difference(201, 0.5, design="unpaired")
    assert abs(exact_u - approx_u) <= 0.02
    _, caveats, mdd, _ = power.assess(
        100_000, design="paired", design_difference=0.05, design_baseline=0.5
    )
    assert mdd is not None and mdd <= 0.02
    assert any(c["code"] == "power_approximate" for c in caveats)


def test_ten_pairs_cannot_reach_80_percent_power_at_baseline_half():
    # Hand computation: the best case p1 = 1 gives power P(Bin(10, .5) >= 6).
    best = power.power_paired(10, 0.5, 1.0)
    assert best == pytest.approx(sum(ref.binom_pmf(j, 10, 0.5) for j in range(6, 11)))
    assert power.min_detectable_difference(10, 0.5, design="paired") is None


def test_edges_and_bad_input():
    assert power.min_detectable_difference(0, 0.5) is None
    assert power.min_detectable_difference(1, 0.5) is None  # nothing is detectable
    with pytest.raises(AnalysisInputError):
        power.min_detectable_difference(10, 1.0)
    with pytest.raises(AnalysisInputError):
        power.min_detectable_difference(10, 0.5, design="crossover")
    with pytest.raises(AnalysisInputError):
        power.power_paired(10, 0.1, 0.9, rho=0.9)  # unattainable correlation
    with pytest.raises(AnalysisInputError):
        power.power_table(ns=[0])
    exploratory, caveats, mdd, basis = power.assess(
        0, design="paired", design_difference=None
    )
    assert basis["basis"] == "planned"
    assert exploratory and mdd is None and caveats[0]["code"] == "empty"
