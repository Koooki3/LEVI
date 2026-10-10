"""Cross-cutting properties: interval coverage by fixed-seed Monte Carlo,
symmetries, monotonicity and edge cases not covered per module."""

from itertools import pairwise

import numpy as np
import pytest

from levi.automatic.analysis import (
    continuous,
    drift,
    failures,
    paired,
    proportions,
    survival,
)


def test_newcombe_intervals_reach_nominal_coverage_by_monte_carlo():
    rng = np.random.Generator(np.random.PCG64(31))
    n, p1, p2 = 30, 0.6, 0.35
    hits = 0
    for _ in range(4000):
        out = proportions.newcombe_independent(
            int(rng.binomial(n, p1)), n, int(rng.binomial(n, p2)), n
        )
        hits += out["low"] <= p1 - p2 <= out["high"]
    assert 0.93 <= hits / 4000 <= 0.975

    # Paired: correlated pairs, rho about 0.3.
    pa, pb, rho = 0.5, 0.7, 0.3
    p11 = pa * pb + rho * np.sqrt(pa * (1 - pa) * pb * (1 - pb))
    cells = [p11, pa - p11, pb - p11, 1 - pa - pb + p11]
    hits = 0
    for draw in rng.multinomial(n, cells, size=3000):
        e, f, g, h = (int(v) for v in draw)
        pairs = [(1, 1)] * e + [(1, 0)] * f + [(0, 1)] * g + [(0, 0)] * h
        out = paired.newcombe_paired(pairs)
        hits += out["low"] <= pb - pa <= out["high"]
    assert 0.93 <= hits / 3000 <= 0.975


def test_two_arm_intervals_are_antisymmetric_in_the_arms():
    for k1, n1, k2, n2 in ((3, 10, 8, 12), (0, 7, 7, 7), (15, 40, 9, 35)):
        for fn in (proportions.newcombe_independent, proportions.agresti_caffo):
            ab, ba = fn(k1, n1, k2, n2), fn(k2, n2, k1, n1)
            assert ab["low"] == pytest.approx(-ba["high"])
            assert ab["high"] == pytest.approx(-ba["low"])
    assert proportions.agresti_caffo(0, 0, 1, 2)["available"] is False
    assert proportions.posterior_prob_greater(0, 0, 0, 0)[
        "prob_b_greater"
    ] == pytest.approx(0.5)


def test_signed_rank_p_is_invariant_to_negation_and_order():
    rng = np.random.Generator(np.random.PCG64(4))
    d = rng.normal(0.4, 1, 18)
    base = continuous.wilcoxon_signed_rank([(0.0, x) for x in d])["p_value"]
    assert continuous.wilcoxon_signed_rank([(0.0, -x) for x in d])[
        "p_value"
    ] == pytest.approx(base)
    assert continuous.wilcoxon_signed_rank([(0.0, x) for x in d[::-1]])[
        "p_value"
    ] == pytest.approx(base)
    assert continuous.improvement_share([], higher_is_better=True)["available"] is False


def test_kaplan_meier_is_non_increasing_and_rmst_is_bounded():
    rng = np.random.Generator(np.random.PCG64(6))
    t = rng.integers(1, 50, 200)
    e = rng.integers(0, 2, 200)
    steps = survival.kaplan_meier(t, e)["steps"]
    surv = [s["survival"] for s in steps]
    assert all(a >= b for a, b in pairwise(surv))
    for tau in (5.0, 20.0, 49.0):
        value = survival.rmst_value(t.astype(float), e, tau)
        assert 0 < value <= tau
    later = survival.rmst_value(t.astype(float) + 3, e, 49.0)
    assert later > survival.rmst_value(t.astype(float), e, 49.0)


def test_mann_kendall_reversal_flips_the_sign():
    x = [0.2, 0.5, 0.4, 0.7, 0.6, 0.9, 0.8, 1.0, 0.95, 1.1]
    forward, backward = drift.mann_kendall(x), drift.mann_kendall(x[::-1])
    assert forward["s"] == -backward["s"]
    assert forward["p_value"] == pytest.approx(backward["p_value"])


def test_failure_mode_counts_add_up():
    rng = np.random.Generator(np.random.PCG64(8))
    trials = [
        {
            "arm": str(rng.integers(0, 3)),
            "success": bool(rng.random() < 0.5),
            "stop_reason": str(rng.choice(["budget", "fault", "operator"])),
            "failure_mode": str(rng.choice(["drop", "miss", ""])),
        }
        for _ in range(300)
    ]
    out = failures.failure_modes(trials)
    for arm in out["arms"].values():
        assert sum(v["k"] for v in arm["by_stop_reason"].values()) == arm["failures"]
        assert sum(v["k"] for v in arm["by_failure_mode"].values()) == arm["failures"]
    assert sum(a["trials"] for a in out["arms"].values()) == 300
