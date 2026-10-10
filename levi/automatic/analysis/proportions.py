"""Success rates of one arm and of two independent arms.

One arm: the Wilson score interval (Wilson 1927, doi:10.1080/01621459.1927.10502953)
is the primary interval and comes from ``levi.live.stats.wilson`` so the
live page and the reports print the same numbers; the exact Clopper-Pearson
interval (Clopper and Pearson 1934, doi:10.1093/biomet/26.4.404) is the
conservative companion. Brown, Cai and DasGupta (2001,
doi:10.1214/ss/1009213286) compare the two.

Two independent arms (no shared layout cards): Fisher's exact test (Fisher
1922, doi:10.2307/2340521), Boschloo's unconditional test with Fisher's
p-value as statistic (Boschloo 1970, doi:10.1111/j.1467-9574.1970.tb00104.x),
Newcombe's hybrid score interval for the difference (method 10 of Newcombe
1998, doi:10.1002/(SICI)1097-0258(19980430)17:8<873::AID-SIM779>3.0.CO;2-I),
and the Agresti-Caffo add-two interval (Agresti and Caffo 2000,
doi:10.1080/00031305.2000.10474560) for comparison.
"""

from __future__ import annotations

import math
from bisect import bisect_right

import numpy as np

from levi.live.stats import wilson as _live_wilson

from . import _core
from ._core import AnalysisInputError, caveat, check_count, result

MODULE = "proportions"


def wilson_bounds(k: int, n: int, z: float) -> tuple[float, float]:
    """The unrounded Wilson score limits. Reports print
    ``levi.live.stats.wilson`` (rounded to 3 digits); interval *compositions*
    (Newcombe's methods) need the unrounded limits, and a test pins this
    function to the live one after rounding."""
    if n == 0:
        raise AnalysisInputError("the Wilson interval needs n > 0")
    p = k / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    scale = 1 + z * z / n
    low = 0.0 if k == 0 else max(0.0, (centre - spread) / scale)
    high = 1.0 if k == n else min(1.0, (centre + spread) / scale)
    return low, high


def clopper_pearson_bounds(k: int, n: int, level: float) -> tuple[float, float]:
    """Exact limits: ``P(X >= k | low) = alpha/2`` and ``P(X <= k | high) =
    alpha/2``, solved by bisection on the exact binomial distribution."""
    alpha = 1 - level
    if n == 0:
        raise AnalysisInputError("the Clopper-Pearson interval needs n > 0")
    if k == 0:
        low = 0.0
    else:
        low = _core.bisect(
            lambda p: 1.0 - _core.binom_cdf(k - 1, n, p), 0.0, 1.0, alpha / 2, True
        )
    if k == n:
        high = 1.0
    else:
        high = _core.bisect(
            lambda p: _core.binom_cdf(k, n, p), 0.0, 1.0, alpha / 2, False
        )
    return low, high


def proportion(k, n, *, level: float = _core.DEFAULT_LEVEL) -> dict:
    """One arm's success rate: Wilson (primary, via ``levi.live.stats``) and
    Clopper-Pearson. ``n = 0`` gives ``available: false`` and no numbers."""
    k, n = check_count(k, n)
    level = _core.check_level(level)
    if n == 0:
        return result(
            "estimate",
            "binomial_proportion",
            MODULE,
            references=["wilson1927", "clopper1934"],
            exploratory=False,
            caveats=[caveat("empty", "no trials in this arm")],
            available=False,
            k=0,
            n=0,
            level=level,
            rate=None,
            wilson=None,
            clopper_pearson=None,
        )
    z = _core.z_of(level)
    live = _live_wilson(k, n, z)
    cp = clopper_pearson_bounds(k, n, level)
    width = live[1] - live[0]
    return result(
        "estimate",
        "binomial_proportion",
        MODULE,
        references=["wilson1927", "clopper1934", "brown2001binomial"],
        exploratory=False,
        caveats=[
            caveat(
                "interval_width",
                f"the {level:.0%} Wilson interval is {width:.2f} wide",
                width=round(width, 3),
            )
        ],
        available=True,
        k=k,
        n=n,
        level=level,
        rate=k / n,
        wilson={"low": live[0], "high": live[1], "rounded_to": 3},
        clopper_pearson={"low": cp[0], "high": cp[1]},
    )


# ------------------------------------------------------------ independent


def _fisher_weights(n1: int, n2: int, total: int) -> tuple[range, list[int]]:
    """Integer hypergeometric weights ``comb(n1, x) comb(n2, total - x)``
    over the support of x given the success margin ``total``."""
    xs = range(max(0, total - n2), min(n1, total) + 1)
    return xs, [math.comb(n1, x) * math.comb(n2, total - x) for x in xs]


def fisher_pvalues(n1: int, n2: int, *, two_sided: str = "minlike") -> np.ndarray:
    """Fisher's two-sided p-value of every table, as an ``(n1+1, n2+1)``
    array indexed by (successes in arm 1, successes in arm 2).

    ``minlike`` sums the probabilities of all tables no more likely than the
    observed one (exact integer comparison, no tolerance needed);
    ``doubling`` doubles the smaller one-sided tail. Each p-value is one
    correctly rounded division of two integers, so equal rationals give
    equal floats."""
    if two_sided not in ("minlike", "doubling"):
        raise AnalysisInputError("two_sided must be 'minlike' or 'doubling'")
    out = np.ones((n1 + 1, n2 + 1))
    for total in range(n1 + n2 + 1):
        xs, ws = _fisher_weights(n1, n2, total)
        denom = math.comb(n1 + n2, total)
        if two_sided == "minlike":
            order = sorted(ws)
            cum = []
            running = 0
            for w in order:
                running += w
                cum.append(running)
            for x, w in zip(xs, ws, strict=True):
                p = cum[bisect_right(order, w) - 1] / denom
                out[x, total - x] = min(1.0, p)
        else:
            left = []
            running = 0
            for w in ws:
                running += w
                left.append(running)
            for i, x in enumerate(xs):
                low = left[i]
                high = denom - (left[i - 1] if i else 0)
                out[x, total - x] = min(1.0, 2 * min(low, high) / denom)
    return out


def fisher_exact(k1, n1, k2, n2, *, two_sided: str = "minlike") -> dict:
    """Fisher's exact test for two independent arms (``k`` successes of
    ``n`` each). Reports the two-sided p-value and both one-sided ones."""
    k1, n1 = check_count(k1, n1)
    k2, n2 = check_count(k2, n2)
    if n1 == 0 or n2 == 0:
        return _unavailable_two("fisher_exact", ["fisher1922"], "an arm has no trials")
    total = k1 + k2
    xs, ws = _fisher_weights(n1, n2, total)
    denom = math.comb(n1 + n2, total)
    i = k1 - xs.start
    p = float(fisher_pvalues(n1, n2, two_sided=two_sided)[k1, k2])
    less = sum(ws[: i + 1]) / denom
    greater = sum(ws[i:]) / denom
    return result(
        "test",
        f"fisher_exact_{two_sided}",
        MODULE,
        references=["fisher1922"],
        exploratory=True,
        caveats=[_unpaired_caveat()],
        available=True,
        k1=k1,
        n1=n1,
        k2=k2,
        n2=n2,
        p_value=p,
        p_arm1_lower=less,
        p_arm1_higher=greater,
    )


def boschloo_exact(k1, n1, k2, n2, *, grid: int = 1000) -> dict:
    """Boschloo's unconditional test: the p-value is the largest, over the
    common success probability ``pi``, of the probability of a table whose
    two-sided Fisher p-value is at most the observed one. The maximum is
    taken on ``grid`` equally spaced points and refined locally by golden
    section around the best grid point."""
    k1, n1 = check_count(k1, n1)
    k2, n2 = check_count(k2, n2)
    if n1 == 0 or n2 == 0:
        return _unavailable_two(
            "boschloo_exact", ["boschloo1970"], "an arm has no trials"
        )
    pvals = fisher_pvalues(n1, n2)
    region = (pvals <= pvals[k1, k2]).astype(float)

    def size(pi: float) -> float:
        return float(_core.binom_pmf(n1, pi) @ region @ _core.binom_pmf(n2, pi))

    grid = max(10, int(grid))
    pis = (np.arange(grid) + 0.5) / grid
    values = [size(float(pi)) for pi in pis]
    best = int(np.argmax(values))
    lo = float(pis[max(0, best - 1)])
    hi = float(pis[min(grid - 1, best + 1)])
    best_value = values[best]
    ratio = (math.sqrt(5) - 1) / 2
    a, b = lo, hi
    for _ in range(60):
        c = b - ratio * (b - a)
        d = a + ratio * (b - a)
        if size(c) > size(d):
            b = d
        else:
            a = c
    best_value = max(best_value, size((a + b) / 2))
    p = min(1.0, best_value)
    return result(
        "test",
        "boschloo_exact",
        MODULE,
        references=["boschloo1970", "fisher1922"],
        exploratory=True,
        caveats=[_unpaired_caveat()],
        available=True,
        k1=k1,
        n1=n1,
        k2=k2,
        n2=n2,
        p_value=p,
        fisher_p_value=float(pvals[k1, k2]),
        nuisance_grid=grid,
    )


def newcombe_independent(k1, n1, k2, n2, *, level: float = _core.DEFAULT_LEVEL) -> dict:
    """Newcombe's hybrid score interval (his method 10) for ``p1 - p2``
    built from the two Wilson intervals."""
    k1, n1 = check_count(k1, n1)
    k2, n2 = check_count(k2, n2)
    level = _core.check_level(level)
    if n1 == 0 or n2 == 0:
        return _unavailable_two(
            "newcombe_hybrid_score", ["newcombe1998independent"], "an arm has no trials"
        )
    z = _core.z_of(level)
    p1, p2 = k1 / n1, k2 / n2
    l1, u1 = wilson_bounds(k1, n1, z)
    l2, u2 = wilson_bounds(k2, n2, z)
    d = p1 - p2
    low = d - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
    high = d + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
    return result(
        "estimate",
        "newcombe_hybrid_score",
        MODULE,
        references=["newcombe1998independent", "wilson1927"],
        exploratory=True,
        caveats=[_unpaired_caveat()],
        available=True,
        k1=k1,
        n1=n1,
        k2=k2,
        n2=n2,
        level=level,
        difference=d,
        low=max(-1.0, low),
        high=min(1.0, high),
    )


def agresti_caffo(k1, n1, k2, n2, *, level: float = _core.DEFAULT_LEVEL) -> dict:
    """The add-two interval for ``p1 - p2``: one success and one failure are
    added to each arm before the Wald formula."""
    k1, n1 = check_count(k1, n1)
    k2, n2 = check_count(k2, n2)
    level = _core.check_level(level)
    z = _core.z_of(level)
    a, b = (k1 + 1) / (n1 + 2), (k2 + 1) / (n2 + 2)
    se = math.sqrt(a * (1 - a) / (n1 + 2) + b * (1 - b) / (n2 + 2))
    d = a - b
    return result(
        "estimate",
        "agresti_caffo_add_two",
        MODULE,
        references=["agresti2000addtwo"],
        exploratory=True,
        caveats=[_unpaired_caveat()],
        available=True,
        k1=k1,
        n1=n1,
        k2=k2,
        n2=n2,
        level=level,
        difference=(k1 / n1 if n1 else 0.0) - (k2 / n2 if n2 else 0.0),
        adjusted_difference=d,
        low=max(-1.0, d - z * se),
        high=min(1.0, d + z * se),
    )


def posterior_prob_greater(k_a, n_a, k_b, n_b) -> dict:
    """Descriptive only, not a test: ``P(p_B > p_A)`` under independent
    uniform priors, i.e. Beta(1 + k, 1 + n - k) posteriors, computed exactly
    by the finite sum for integer Beta parameters (no sampling)."""
    k_a, n_a = check_count(k_a, n_a)
    k_b, n_b = check_count(k_b, n_b)
    aa, ba = 1 + k_a, 1 + n_a - k_a
    ab, bb = 1 + k_b, 1 + n_b - k_b

    def lbeta(x: float, y: float) -> float:
        return math.lgamma(x) + math.lgamma(y) - math.lgamma(x + y)

    terms = [
        lbeta(aa + i, ba + bb) - math.log(bb + i) - lbeta(1 + i, bb) - lbeta(aa, ba)
        for i in range(ab)
    ]
    top = max(terms)
    prob = math.exp(top) * sum(math.exp(t - top) for t in terms)
    return result(
        "estimate",
        "beta_posterior_prob_greater",
        MODULE,
        references=["kressgazit2024", "tri_lbm2025"],
        exploratory=True,
        caveats=[
            caveat(
                "descriptive_only",
                "a posterior probability under uniform priors; not a test",
            )
        ],
        available=True,
        k_a=k_a,
        n_a=n_a,
        k_b=k_b,
        n_b=n_b,
        prior="uniform Beta(1, 1)",
        prob_b_greater=min(1.0, max(0.0, prob)),
    )


def _unpaired_caveat() -> dict:
    return caveat(
        "unpaired",
        "arms compared without shared layout cards; initial conditions are not paired",
    )


def _unavailable_two(method: str, refs: list[str], why: str) -> dict:
    return result(
        "test",
        method,
        MODULE,
        references=refs,
        exploratory=True,
        caveats=[caveat("empty", why)],
        available=False,
        p_value=None,
    )
