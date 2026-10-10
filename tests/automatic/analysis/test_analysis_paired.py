"""Paired two-arm comparisons.

Known answers: Fagerland, Lydersen and Laake (2013) Table 6 (two worked
examples, exact conditional, mid-p and asymptotic McNemar p-values; read
from the open-access full text, Europe PMC PMC3716987, 2026-10-10);
Newcombe (1998, paired data) Table III, method 10 (read from the paper's
scanned pages, 2026-10-10).
"""

import time
from fractions import Fraction

import analysis_reference as ref
import numpy as np
import pytest

from levi.automatic.analysis import paired, resampling
from levi.automatic.analysis._core import AnalysisInputError, z_of


def pairs_of(both, only_a, only_b, neither):
    return [(1, 1)] * both + [(1, 0)] * only_a + [(0, 1)] * only_b + [(0, 0)] * neither


@pytest.mark.parametrize(
    ("only_a", "only_b", "exact", "mid", "asym"),
    [
        (1, 7, 0.0703, 0.0391, 0.0339),  # airway hyper-responsiveness example
        (6, 16, 0.0525, 0.0347, 0.0330),  # complete-response example
    ],
)
def test_mcnemar_matches_fagerland_table_6(only_a, only_b, exact, mid, asym):
    out = paired.mcnemar(pairs_of(5, only_a, only_b, 5))
    assert out["p_exact"] == pytest.approx(exact, abs=5e-5)
    assert out["p_mid"] == pytest.approx(mid, abs=5e-5)
    assert out["p_asymptotic"] == pytest.approx(asym, abs=5e-5)


def test_mcnemar_exact_equals_an_exact_fraction_computation():
    for f in range(15):
        for g in range(15):
            got = paired.mcnemar_counts(f, g)["exact"]
            assert got == pytest.approx(float(ref.mcnemar_exact_p(f, g)), rel=1e-12)


def test_mcnemar_hand_computed():
    # 12 vs 2 discordant: 2 (1 + 14 + 91) / 2^14 and mid-p subtracts 91 / 2^14.
    out = paired.mcnemar_counts(12, 2)
    assert out["exact"] == pytest.approx(212 / 16384)
    assert out["mid_p"] == pytest.approx(121 / 16384)
    assert out["statistic"] == pytest.approx(100 / 14)


def test_mcnemar_is_symmetric_and_ignores_concordant_pairs():
    a = paired.mcnemar(pairs_of(3, 2, 9, 4))
    b = paired.mcnemar(pairs_of(3, 9, 2, 4))
    c = paired.mcnemar(pairs_of(30, 2, 9, 40))
    assert a["p_exact"] == b["p_exact"] == c["p_exact"]
    assert a["p_mid"] <= a["p_exact"]


NEWCOMBE_TABLE_III = [
    # (e, f, g, h) -> method 10 limits for (f - g) / n
    ((36, 12, 2, 0), (0.0569, 0.3404)),
    ((20, 12, 2, 16), (0.0562, 0.3292)),
    ((18, 12, 2, 18), (0.0562, 0.3290)),
    ((36, 14, 0, 0), (0.1528, 0.4167)),
    ((35, 14, 0, 1), (0.1461, 0.4175)),
    ((18, 14, 0, 18), (0.1441, 0.3963)),
    ((2, 97, 1, 0), (0.8721, 0.9854)),
    ((1, 97, 1, 1), (0.8736, 0.9850)),
    ((0, 29, 1, 0), (0.6666, 0.9882)),
    ((2, 98, 0, 0), (0.9178, 0.9945)),
    ((1, 98, 0, 1), (0.9171, 0.9916)),
    ((0, 30, 0, 0), (0.8395, 1.0)),
    ((54, 0, 0, 0), (-0.0664, 0.0664)),
    ((53, 0, 0, 1), (-0.0729, 0.0729)),
    ((30, 0, 0, 24), (-0.0358, 0.0358)),
    ((29, 0, 0, 25), (-0.0354, 0.0354)),
    ((28, 0, 0, 26), (-0.0352, 0.0352)),
    ((27, 0, 0, 27), (-0.0351, 0.0351)),
]


@pytest.mark.parametrize(("cells", "expected"), NEWCOMBE_TABLE_III)
def test_newcombe_paired_matches_table_iii_method_10(cells, expected):
    low, high = paired.newcombe_paired_bounds(*cells, z_of(0.95))
    assert low == pytest.approx(expected[0], abs=1e-4)
    assert high == pytest.approx(expected[1], abs=1e-4)


def test_newcombe_paired_public_function_orients_b_minus_a():
    # B succeeds alone 12 times, A alone 2 times: p_B - p_A = 10 / 50.
    out = paired.newcombe_paired(pairs_of(36, 2, 12, 0))
    assert out["difference"] == pytest.approx(0.2)
    assert out["low"] == pytest.approx(0.0569, abs=1e-4)
    assert out["high"] == pytest.approx(0.3404, abs=1e-4)
    swapped = paired.newcombe_paired(pairs_of(36, 12, 2, 0))
    assert swapped["low"] == pytest.approx(-out["high"])
    assert swapped["high"] == pytest.approx(-out["low"])


def test_paired_functions_count_missing_and_reject_bad_values():
    data = pairs_of(3, 1, 4, 2) + [(None, 1), (1, float("nan"))]
    out = paired.mcnemar(data)
    assert out["dropped"] == 2 and out["n_pairs"] == 10
    assert any(c["code"] == "dropped_missing" for c in out["caveats"])
    with pytest.raises(AnalysisInputError):
        paired.mcnemar([(1, 2)])
    with pytest.raises(AnalysisInputError):
        paired.mcnemar([(1, float("inf"))])
    empty = paired.newcombe_paired([])
    assert empty["available"] is False
    none = paired.mcnemar(pairs_of(5, 0, 0, 5))
    assert none["p_exact"] == 1.0
    assert any(c["code"] == "no_discordant" for c in none["caveats"])


def test_exploratory_flag_follows_power_and_preregistration():
    small = paired.mcnemar(pairs_of(5, 2, 8, 5))
    assert small["exploratory"] is True
    codes = {c["code"] for c in small["caveats"]}
    assert {"detectable_difference", "not_preregistered"} <= codes
    under = paired.mcnemar(pairs_of(5, 2, 8, 5), design_difference=0.2)
    assert under["exploratory"] is True
    assert any(c["code"] == "underpowered" for c in under["caveats"])
    big = paired.mcnemar(pairs_of(100, 20, 60, 120), design_difference=0.3)
    assert big["exploratory"] is False


def test_paired_bootstrap_is_deterministic_and_seed_dependent():
    data = pairs_of(10, 4, 9, 7)
    a = paired.paired_bootstrap(data, seed=3)
    b = paired.paired_bootstrap(data, seed=3)
    c = paired.paired_bootstrap(data, seed=4)
    assert a == b and a["seed"] == 3 and c["seed"] == 4
    assert a["estimate"] == pytest.approx(5 / 30)
    diffs = np.array([0.0] * 17 + [-1.0] * 4 + [1.0] * 9)
    r3 = resampling.bootstrap_distribution(diffs, "mean", 500, 3)
    assert np.array_equal(r3, resampling.bootstrap_distribution(diffs, "mean", 500, 3))
    assert not np.array_equal(
        r3, resampling.bootstrap_distribution(diffs, "mean", 500, 4)
    )


def test_paired_bootstrap_coverage_by_monte_carlo():
    rng = np.random.Generator(np.random.PCG64(99))
    n, pa, pb = 40, 0.5, 0.65
    covered = 0
    trials = 300
    for i in range(trials):
        u = rng.random(n)
        a = (u < pa).astype(int)
        b = ((u < pb) if i % 2 else (rng.random(n) < pb)).astype(int)
        out = paired.paired_bootstrap(
            list(zip(a, b, strict=True)), seed=i, resamples=1000
        )
        covered += out["low"] <= pb - pa <= out["high"]
    # Percentile intervals are a little liberal at n = 40.
    assert 0.88 <= covered / trials <= 0.99


def test_paired_bootstrap_bca_and_percentile_agree_roughly_and_bca_is_named():
    rng = np.random.Generator(np.random.PCG64(5))
    data = list(zip(rng.integers(0, 2, 80), rng.integers(0, 2, 80), strict=True))
    pct = paired.paired_bootstrap(data, seed=1, method="percentile")
    bca = paired.paired_bootstrap(data, seed=1, method="bca")
    assert (
        bca["method"] == "paired_bootstrap_bca" and "efron1987bca" in bca["references"]
    )
    assert abs(pct["low"] - bca["low"]) < 0.05 and abs(pct["high"] - bca["high"]) < 0.05


def test_bootstrap_of_100k_pairs_is_bounded_and_fast():
    rng = np.random.Generator(np.random.PCG64(11))
    a = rng.integers(0, 2, 100_000)
    b = rng.integers(0, 2, 100_000)
    started = time.perf_counter()
    out = paired.paired_bootstrap(list(zip(a, b, strict=True)), seed=0, resamples=10**9)
    assert out["resamples"] == 100_000
    assert any(c["code"] == "resamples_capped" for c in out["caveats"])
    cont = paired.paired_bootstrap(
        list(zip(rng.random(100_000), rng.random(100_000), strict=True)),
        seed=0,
        resamples=200,
        binary=False,
        statistic="median",
    )
    assert cont["low"] < cont["estimate"] < cont["high"]
    assert time.perf_counter() - started < 60


def test_unpaired_bootstrap_hand_case_and_determinism():
    out = paired.unpaired_bootstrap([1, 2, 3, 4], [11, 12, 13, 14], seed=2)
    assert out["estimate"] == pytest.approx(10.0)
    assert out["low"] <= 10 <= out["high"]
    assert out == paired.unpaired_bootstrap([1, 2, 3, 4], [11, 12, 13, 14], seed=2)
    nan = paired.unpaired_bootstrap([1, float("nan")], [2, 3], seed=0)
    assert nan["dropped"] == 1
    assert paired.unpaired_bootstrap([], [1], seed=0)["available"] is False


def test_seed_must_be_explicit_and_valid():
    with pytest.raises(TypeError):
        paired.paired_bootstrap(pairs_of(1, 1, 1, 1))  # no seed
    with pytest.raises(AnalysisInputError):
        paired.paired_bootstrap(pairs_of(1, 1, 1, 1), seed=-1)
    with pytest.raises(AnalysisInputError):
        paired.paired_bootstrap(pairs_of(1, 1, 1, 1), seed=1.5)


def test_fraction_reference_is_really_independent():
    # Sanity of the reference itself: the two-sided exact p of (0, 5) is 2/32.
    assert ref.mcnemar_exact_p(0, 5) == Fraction(2, 32)
