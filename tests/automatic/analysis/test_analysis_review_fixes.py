"""Regression tests for the independent review of the analysis library
(levi2/recon/review-CP1.md): planned-effect power rule, early-stop pairing
by (slot, round), BH step-up and BCa acceleration, runtime versions in the
envelope, degenerate bootstrap samples, and the suggested edge fixes."""

import math

import numpy as np
import pytest

import levi.automatic.analysis as an
from levi.automatic.analysis import (
    multiple,
    paired,
    power,
    proportions,
    resampling,
    survival,
)
from levi.automatic.analysis._core import AnalysisInputError


def pairs_of(both, only_a, only_b, neither):
    return [(1, 1)] * both + [(1, 0)] * only_a + [(0, 1)] * only_b + [(0, 0)] * neither


# ---------------------------------------------------------------- 1 planned power


def test_exploratory_does_not_depend_on_the_observed_outcomes():
    # n = 30 pairs, planned difference 0.35. The review found that extreme
    # outcomes (pooled rate 0.08 or 0.92) turned the result confirmatory.
    tables = [
        (15, 3, 6, 6),
        (0, 0, 5, 25),
        (27, 1, 0, 2),
        (0, 15, 15, 0),
        (30, 0, 0, 0),
    ]
    flags = {
        paired.mcnemar(pairs_of(*t), design_difference=0.35)["exploratory"]
        for t in tables
    }
    assert flags == {True}
    flags = {
        paired.newcombe_paired(pairs_of(*t), design_difference=0.35)["exploratory"]
        for t in tables
    }
    assert flags == {True}
    mdds = {paired.mcnemar(pairs_of(*t))["min_detectable_difference"] for t in tables}
    assert len(mdds) == 1  # reported at the planned baseline, not the observed one
    unpaired = {
        proportions.fisher_exact(k1, 30, k2, 30, design_difference=0.35)["exploratory"]
        for k1, k2 in ((15, 16), (0, 3), (30, 27), (2, 29))
    }
    assert unpaired == {True}


def test_power_basis_is_planned_and_explicit():
    out = paired.mcnemar(
        pairs_of(10, 3, 9, 8), design_difference=0.3, design_baseline=0.5
    )
    assert out["power_basis"]["basis"] == "planned"
    assert out["power_basis"]["baseline"] == 0.5
    assert out["power_basis"]["difference"] == 0.3
    default = paired.mcnemar(pairs_of(10, 3, 9, 8))
    assert default["power_basis"]["basis"] == "planned"
    # Without a planned difference the detectable difference is the design
    # table's value for this n at baseline 0.5.
    assert default["min_detectable_difference"] == power.min_detectable_difference(
        30, 0.5, design="paired"
    )
    assert default["exploratory"] is True


def test_a_planned_design_with_enough_power_is_confirmatory_whatever_the_data():
    for t in ((100, 20, 60, 120), (300, 0, 0, 0), (0, 150, 150, 0)):
        out = paired.mcnemar(pairs_of(*t), design_difference=0.3, design_baseline=0.5)
        assert out["exploratory"] is False


def test_without_a_planned_baseline_the_least_favourable_baseline_decides():
    # n = 30 unpaired: delta 0.37 is detectable at baseline 0.5 (0.36) but
    # not at baseline 0.3 (0.39); with no planned baseline it stays exploratory.
    at_half = proportions.fisher_exact(
        10, 30, 20, 30, design_difference=0.37, design_baseline=0.5
    )
    unknown = proportions.fisher_exact(10, 30, 20, 30, design_difference=0.37)
    assert at_half["exploratory"] is False
    assert unknown["exploratory"] is True


def test_adjusted_families_inherit_the_tests_exploratory_status():
    assert multiple.holm([0.01, 0.02])["exploratory"] is True  # unknown inputs
    assert (
        multiple.holm([0.01, 0.02], exploratory=[False, False])["exploratory"] is False
    )
    assert multiple.holm([0.01, 0.02], exploratory=[False, True])["exploratory"] is True
    assert multiple.bonferroni([0.01], exploratory=[False])["exploratory"] is False
    assert (
        multiple.benjamini_hochberg([0.01], exploratory=[False])["exploratory"] is True
    )
    with pytest.raises(AnalysisInputError):
        multiple.holm([0.01, 0.02], exploratory=[False])


def test_large_n_power_approximation_is_flagged_as_optimistic():
    out = paired.mcnemar(pairs_of(100, 40, 50, 60))
    codes = {c["code"] for c in out["caveats"]}
    assert "power_approximate" in codes
    text = next(c for c in out["caveats"] if c["code"] == "power_approximate")[
        "message"
    ]
    assert "0.01" in text


# ---------------------------------------------------------------- 2 early stop


def control(arm, slot, round_, would_stop):
    return {
        "arm": arm,
        "control": True,
        "truth": "failure",
        "would_stop": would_stop,
        "complete": True,
        "slot": slot,
        "round": round_,
    }


def test_early_stop_pairs_by_slot_and_round_over_many_rounds():
    episodes = []
    for round_ in range(5):
        for slot in range(4):
            episodes.append(control("a", slot, round_, True))
            episodes.append(control("b", slot, round_, round_ == 0))
    out = an.early_stop(episodes)
    pair = out["paired_false_early_stop"]["a|b"]
    assert pair["n_pairs"] == 20
    assert pair["cells"]["only_a"] == 16
    assert out["unpaired"]["a|b"] == 0


def test_early_stop_counts_unpaired_and_refuses_duplicates():
    episodes = [
        control("a", 1, 0, True),
        control("a", 2, 0, False),
        control("b", 1, 0, False),
        control("b", 3, 0, False),
        {**control("a", None, 0, True)},
    ]
    out = an.early_stop(episodes)
    assert out["paired_false_early_stop"]["a|b"]["n_pairs"] == 1
    # a: slot 2 and the slot-less episode; b: slot 3.
    assert out["unpaired"]["a|b"] == 3
    with pytest.raises(AnalysisInputError):
        an.early_stop([control("a", 1, 0, True), control("a", 1, 0, False)])


# ---------------------------------------------------------------- 3 BH and BCa


def test_benjamini_hochberg_step_up_needs_the_running_minimum():
    # Raw scaling gives 0.04 * 2 = 0.08 and 0.041; the step-up takes the
    # minimum from the top, so both are 0.041 and both are rejected at 0.05.
    out = multiple.benjamini_hochberg([0.04, 0.041])
    assert out["adjusted"] == pytest.approx([0.041, 0.041])
    assert out["reject"] == [True, True]


def test_benjamini_hochberg_adjustment_is_monotone_in_the_raw_p():
    rng = np.random.Generator(np.random.PCG64(77))
    for _ in range(100):
        p = rng.random(10) ** 3
        q = np.array(multiple.benjamini_hochberg(p)["adjusted"])
        order = np.argsort(p)
        assert (np.diff(q[order]) >= -1e-15).all()


def test_bca_acceleration_hand_computed():
    # Mean of (1, 2, 3, 10): jackknife means (16 - x) / 3 = 5, 14/3, 13/3, 2,
    # their mean 4, d = 4 - jack = -1, -2/3, -1/3, 2;
    # a = sum d^3 / (6 (sum d^2)^1.5) = (20/3) / (6 (50/9)^1.5).
    expected = (20 / 3) / (6 * (50 / 9) ** 1.5)
    assert resampling.bca_acceleration(
        np.array([1.0, 2.0, 3.0, 10.0]), "mean"
    ) == pytest.approx(expected)
    assert resampling.bca_acceleration(np.array([1.0, 8.0, 9.0, 10.0]), "mean") < 0


def test_bca_moves_the_interval_towards_the_long_tail():
    rng = np.random.Generator(np.random.PCG64(3))
    d = rng.lognormal(0.0, 1.0, 40)
    data = [(0.0, x) for x in d]
    pct = paired.paired_bootstrap(data, seed=1, binary=False, resamples=20_000)
    bca = paired.paired_bootstrap(
        data, seed=1, binary=False, resamples=20_000, method="bca"
    )
    assert bca["high"] > pct["high"] and bca["low"] > pct["low"]


# ---------------------------------------------------------------- 4 versions


def test_every_result_records_the_runtime_versions():
    out = paired.paired_bootstrap(pairs_of(5, 3, 6, 4), seed=0)
    assert out["numpy_version"] == np.__version__
    assert out["algorithm_version"] >= 2
    assert out["bit_generator"] == "PCG64"
    assert an.proportion(3, 10)["numpy_version"] == np.__version__


def test_random_results_agree_within_tolerance_across_random_streams():
    # Bit-identical output holds only for one numpy version; across versions
    # (a different random stream) only Monte Carlo tolerance is promised.
    # Different seeds stand in for a different stream here.
    rng = np.random.Generator(np.random.PCG64(5))
    data = [
        (float(a), float(b))
        for a, b in zip(rng.normal(0, 1, 60), rng.normal(0.4, 1, 60), strict=True)
    ]
    lows = [
        paired.paired_bootstrap(data, seed=s, binary=False, resamples=20_000)["low"]
        for s in range(5)
    ]
    assert max(lows) - min(lows) < 0.03
    ps = [
        an.cochran_q(
            [[1, 0, 1], [0, 0, 1], [1, 1, 0], [1, 0, 0], [0, 1, 1]] * 3,
            seed=s,
            permutations=20_000,
        )["p_permutation"]
        for s in range(5)
    ]
    assert max(ps) - min(ps) < 0.02


# ---------------------------------------------------------------- 5 bootstrap


def test_degenerate_bootstrap_sample_is_flagged():
    for data in (pairs_of(20, 0, 0, 20), pairs_of(0, 0, 40, 0)):
        out = paired.paired_bootstrap(data, seed=0)
        codes = {c["code"] for c in out["caveats"]}
        assert "degenerate_bootstrap" in codes
        assert out["low"] == out["high"]


def test_small_sample_bootstrap_carries_the_coverage_note():
    out = paired.paired_bootstrap(pairs_of(10, 6, 9, 4), seed=0)  # 29 pairs
    assert any(c["code"] == "small_sample" for c in out["caveats"])
    assert "0.91" in out["coverage_note"]
    big = paired.paired_bootstrap(pairs_of(30, 10, 15, 25), seed=0)
    assert not any(c["code"] == "small_sample" for c in big["caveats"])


# ---------------------------------------------------------------- suggestions


def test_cohen_kappa_has_the_envelope():
    out = an.cohen_kappa(["s", "f"], ["s", "s"])
    assert (
        out["schema_version"] == an.SCHEMA_VERSION
        and "caveats" in out
        and "exploratory" in out
    )


def test_rmst_edges_follow_the_documented_contract():
    out = survival.rmst(
        {"a": ([1, 2, float("nan")], [1, 0, 1]), "b": ([], [])}, tau=2.0, seed=0
    )
    assert out["available"] is False
    assert out["dropped"] == 1
    beyond = survival.rmst(
        {"a": ([1, 2], [1, 0]), "b": ([1, 3], [1, 1])}, tau=5.0, seed=0
    )
    assert any(c["code"] == "tau_beyond_follow_up" for c in beyond["caveats"])


def test_mcnemar_without_pairs_gives_no_p_value():
    out = paired.mcnemar([])
    assert out["p_exact"] is None and out["p_mid"] is None
    assert [c["code"] for c in out["caveats"]] == ["empty"]


def test_references_match_the_methods():
    out = an.misjudgement_by_arm(
        {"a": [("success", "failure")], "b": [("success", "success")]}, seed=0
    )
    assert "cohen1960kappa" not in out["references"]
    assert "tri_lbm2025" not in an.posterior_prob_greater(1, 3, 2, 3)["references"]


def test_unused_permutation_helpers_are_gone():
    assert not hasattr(resampling, "sign_flip_pvalue")
    assert not hasattr(resampling, "label_permutation_pvalue")
    assert math.isfinite(1.0)
