"""Two arms run on the same layout cards in the same rounds (paired).

- McNemar's test (McNemar 1947, doi:10.1007/BF02295996) as the exact
  conditional binomial test on the discordant pairs, its mid-p version and
  the asymptotic statistic; Fagerland, Lydersen and Laake (2013,
  doi:10.1186/1471-2288-13-91) recommend the mid-p version over the exact
  conditional one.
- Newcombe's paired score interval for ``p_B - p_A`` (method 10 of Newcombe
  1998, doi:10.1002/(SICI)1097-0258(19981130)17:22<2635::AID-SIM954>3.0.CO;2-C),
  built from Wilson intervals with the continuity-corrected phi.
- The paired bootstrap of the mean difference, resampling whole pairs
  (Efron 1979, doi:10.1214/aos/1176344552).
"""

from __future__ import annotations

import math

import numpy as np

from . import _core, power, resampling
from ._core import caveat, result
from .proportions import wilson_bounds

MODULE = "paired"
# Discordant pairs up to which McNemar's p-value is summed in exact integers.
EXACT_INTEGER_MAX = 2000
SMALL_SAMPLE = 30
# Measured with this library (levi2 review fixes, 2026-10-10): 1000 data
# sets of 30 pairs each, 2000 resamples, seed 20261010; Monte Carlo SE about
# 0.007.
COVERAGE_NOTE = (
    "simulated coverage of the nominal 95% interval with 30 pairs (1000 data "
    "sets, 2000 resamples): percentile 0.93 for normal differences, 0.91 for "
    "exponential (skewed) differences, 0.95 for binary pairs; BCa 0.94, 0.92 "
    "and 0.94"
)


def _cells(pairs) -> tuple[int, int, int, int, int]:
    """(both succeed, only A, only B, neither, dropped) from 0/1 pairs
    ``(a, b)``; a pair with a missing side is dropped and counted."""
    a, b, dropped = _core.clean_binary_pairs(pairs)
    e = int(((a == 1) & (b == 1)).sum())
    f = int(((a == 1) & (b == 0)).sum())
    g = int(((a == 0) & (b == 1)).sum())
    h = int(((a == 0) & (b == 0)).sum())
    return e, f, g, h, dropped


def mcnemar_counts(only_a: int, only_b: int) -> dict:
    """The three McNemar p-values from the discordant counts."""
    m = only_a + only_b
    if m == 0:
        return {"exact": 1.0, "mid_p": 1.0, "asymptotic": 1.0, "statistic": 0.0}
    low = min(only_a, only_b)
    if m <= EXACT_INTEGER_MAX:
        total = 2**m
        tail = sum(math.comb(m, j) for j in range(low + 1))
        exact = min(1.0, 2 * tail / total)
        mid = min(1.0, (2 * tail - math.comb(m, low)) / total)
    else:
        # Big-integer sums grow quadratically; use the log-space binomial.
        cdf = _core.binom_cdf(low, m, 0.5)
        point = float(np.exp(_core.log_binom_pmf(np.array([low]), m, 0.5))[0])
        exact = min(1.0, 2 * cdf)
        mid = min(1.0, 2 * cdf - point)
    stat = (only_a - only_b) ** 2 / m
    return {
        "exact": exact,
        "mid_p": mid,
        "asymptotic": _core.chi2_sf(stat, 1),
        "statistic": stat,
    }


def mcnemar(
    pairs,
    *,
    alpha: float = _core.DEFAULT_ALPHA,
    design_difference: float | None = None,
    design_baseline: float | None = None,
) -> dict:
    """McNemar's tests on paired 0/1 outcomes ``[(a, b), ...]`` (A first).
    ``design_difference`` and ``design_baseline`` are planned values fixed
    before the first trial; they alone decide ``exploratory``."""
    alpha = _core.check_alpha(alpha)
    e, f, g, h, dropped = _cells(pairs)
    n = e + f + g + h
    if n == 0:
        return result(
            "test",
            "mcnemar",
            MODULE,
            references=["mcnemar1947"],
            exploratory=True,
            caveats=[caveat("empty", "no complete pairs")],
            available=False,
            n_pairs=0,
            dropped=dropped,
            cells={"both": 0, "only_a": 0, "only_b": 0, "neither": 0},
            p_exact=None,
            p_mid=None,
            p_asymptotic=None,
            chi_square=None,
            alpha=alpha,
        )
    exploratory, caveats, mdd, basis = power.assess(
        n,
        design="paired",
        design_difference=design_difference,
        design_baseline=design_baseline,
        alpha=alpha,
    )
    if dropped:
        caveats.append(
            caveat(
                "dropped_missing", f"{dropped} pairs had a missing side", count=dropped
            )
        )
    if f + g == 0:
        caveats.append(
            caveat("no_discordant", "no discordant pairs: the test has no information")
        )
    if f + g < 10:
        caveats.append(
            caveat(
                "asymptotic_unreliable",
                "fewer than 10 discordant pairs: read the exact or mid-p value",
            )
        )
    p = mcnemar_counts(f, g)
    return result(
        "test",
        "mcnemar",
        MODULE,
        references=["mcnemar1947", "fagerland2013mcnemar"],
        exploratory=exploratory,
        caveats=caveats,
        available=n > 0,
        n_pairs=n,
        dropped=dropped,
        cells={"both": e, "only_a": f, "only_b": g, "neither": h},
        p_exact=p["exact"],
        p_mid=p["mid_p"],
        p_asymptotic=p["asymptotic"],
        chi_square=p["statistic"],
        alpha=alpha,
        min_detectable_difference=mdd,
        power_basis=basis,
    )


def newcombe_paired_bounds(
    e: int, f: int, g: int, h: int, z: float
) -> tuple[float, float]:
    """Method 10 limits for ``theta = (f - g) / n`` in Newcombe's notation
    (e: both +, f: first + only, g: second + only, h: both -), the
    difference of the first classification's rate minus the second's."""
    n = e + f + g + h
    l2, u2 = wilson_bounds(e + f, n, z)
    l3, u3 = wilson_bounds(e + g, n, z)
    p2, p3 = (e + f) / n, (e + g) / n
    dl2, du2 = p2 - l2, u2 - p2
    dl3, du3 = p3 - l3, u3 - p3
    denom = (e + f) * (g + h) * (e + g) * (f + h)
    if denom == 0:
        phi = 0.0
    else:
        num = e * h - f * g
        if e * h > f * g:
            num = max(e * h - f * g - n / 2, 0.0)
        phi = num / math.sqrt(denom)
    theta = (f - g) / n
    delta = math.sqrt(max(0.0, dl2**2 - 2 * phi * dl2 * du3 + du3**2))
    eps = math.sqrt(max(0.0, du2**2 - 2 * phi * du2 * dl3 + dl3**2))
    return max(-1.0, theta - delta), min(1.0, theta + eps)


def newcombe_paired(
    pairs,
    *,
    level: float = _core.DEFAULT_LEVEL,
    design_difference: float | None = None,
    design_baseline: float | None = None,
) -> dict:
    """Newcombe's paired interval for ``p_B - p_A``."""
    level = _core.check_level(level)
    e, f, g, h, dropped = _cells(pairs)
    n = e + f + g + h
    if n == 0:
        return result(
            "estimate",
            "newcombe_paired_score",
            MODULE,
            references=["newcombe1998paired"],
            exploratory=True,
            caveats=[caveat("empty", "no complete pairs")],
            available=False,
            n_pairs=0,
            dropped=dropped,
            difference=None,
            low=None,
            high=None,
        )
    # Newcombe's theta is first-minus-second; with B as the first
    # classification it is p_B - p_A: e = both, f = only B, g = only A.
    low, high = newcombe_paired_bounds(e, g, f, h, _core.z_of(level))
    exploratory, caveats, mdd, basis = power.assess(
        n,
        design="paired",
        design_difference=design_difference,
        design_baseline=design_baseline,
    )
    if dropped:
        caveats.append(
            caveat(
                "dropped_missing", f"{dropped} pairs had a missing side", count=dropped
            )
        )
    return result(
        "estimate",
        "newcombe_paired_score",
        MODULE,
        references=["newcombe1998paired", "wilson1927"],
        exploratory=exploratory,
        caveats=caveats,
        available=True,
        n_pairs=n,
        dropped=dropped,
        level=level,
        difference=(g - f) / n,
        low=low,
        high=high,
        min_detectable_difference=mdd,
        power_basis=basis,
    )


def paired_bootstrap(
    pairs,
    *,
    seed: int,
    resamples: int = 10_000,
    level: float = _core.DEFAULT_LEVEL,
    statistic: str = "mean",
    method: str = "percentile",
    binary: bool = True,
    design_difference: float | None = None,
    design_baseline: float | None = None,
) -> dict:
    """Bootstrap interval of ``statistic(b - a)`` resampling whole pairs.
    For 0/1 outcomes (``binary``) the mean difference is ``p_B - p_A``.

    The percentile interval is liberal in small samples (``coverage_note``
    gives the simulated coverage); a sample whose differences all take one
    value gives a zero-width interval, flagged ``degenerate_bootstrap``:
    use McNemar's test and Newcombe's interval for such binary data."""
    level = _core.check_level(level)
    seed = _core.check_seed(seed)
    if binary:
        a, b, dropped = _core.clean_binary_pairs(pairs)
    else:
        a, b, dropped = _core.clean_pairs(pairs)
    diffs = (b - a).astype(float)
    n = len(diffs)
    if n == 0:
        return result(
            "estimate",
            f"paired_bootstrap_{method}",
            MODULE,
            references=["efron1979bootstrap"],
            exploratory=True,
            caveats=[caveat("empty", "no complete pairs")],
            available=False,
            n_pairs=0,
            dropped=dropped,
            seed=seed,
            estimate=None,
            low=None,
            high=None,
        )
    capped = _core.check_resamples(resamples)
    reps = resampling.bootstrap_distribution(diffs, statistic, capped, seed)
    low, high, notes = resampling.interval(diffs, reps, statistic, level, method)
    basis = None
    if binary:
        exploratory, caveats, mdd, basis = power.assess(
            n,
            design="paired",
            design_difference=design_difference,
            design_baseline=design_baseline,
        )
    else:
        exploratory, mdd = True, None
        caveats = [
            caveat("no_power_model", "continuous metric: treated as exploratory")
        ]
    caveats += notes
    if capped < int(resamples):
        caveats.append(caveat("resamples_capped", f"resamples capped at {capped}"))
    if np.unique(diffs).size == 1 or float(np.var(reps)) == 0.0:
        caveats.append(
            caveat(
                "degenerate_bootstrap",
                "every difference takes one value: the bootstrap interval has zero "
                "width and cannot be trusted; use an exact method (McNemar's test, "
                "Newcombe's interval) instead",
            )
        )
    if n < SMALL_SAMPLE:
        caveats.append(
            caveat(
                "small_sample",
                f"below {SMALL_SAMPLE} pairs the percentile bootstrap covers less "
                "than its nominal level (see coverage_note)",
            )
        )
    if dropped:
        caveats.append(
            caveat(
                "dropped_missing", f"{dropped} pairs had a missing side", count=dropped
            )
        )
    refs = ["efron1979bootstrap"] + (["efron1987bca"] if method == "bca" else [])
    return result(
        "estimate",
        f"paired_bootstrap_{method}",
        MODULE,
        references=refs,
        exploratory=exploratory,
        caveats=caveats,
        available=True,
        n_pairs=n,
        dropped=dropped,
        level=level,
        statistic=statistic,
        seed=seed,
        resamples=capped,
        estimate=float(resampling._stat(diffs, statistic)),
        low=low,
        high=high,
        min_detectable_difference=mdd,
        power_basis=basis,
        coverage_note=COVERAGE_NOTE,
    )


def unpaired_bootstrap(
    a,
    b,
    *,
    seed: int,
    resamples: int = 10_000,
    level: float = _core.DEFAULT_LEVEL,
    statistic: str = "mean",
) -> dict:
    """Percentile bootstrap of ``statistic(b) - statistic(a)`` resampling
    each arm on its own (independent arms, e.g. a rate or a median)."""
    level = _core.check_level(level)
    seed = _core.check_seed(seed)
    xa, da = _core.clean_floats(a, "a")
    xb, db = _core.clean_floats(b, "b")
    if len(xa) == 0 or len(xb) == 0:
        return result(
            "estimate",
            "unpaired_bootstrap_percentile",
            MODULE,
            references=["efron1979bootstrap"],
            exploratory=True,
            caveats=[caveat("empty", "an arm has no values")],
            available=False,
            estimate=None,
            low=None,
            high=None,
            seed=seed,
        )
    capped = _core.check_resamples(resamples)
    # Two independent streams derived from one seed.
    ra = resampling.bootstrap_distribution(xa, statistic, capped, seed)
    rb = resampling.bootstrap_distribution(xb, statistic, capped, seed + 1)
    reps = rb - ra
    alpha = 1 - level
    lo, hi = np.quantile(reps, [alpha / 2, 1 - alpha / 2])
    caveats = [caveat("no_power_model", "treated as exploratory")]
    if da or db:
        caveats.append(
            caveat("dropped_nan", f"{da + db} NaN values dropped", count=da + db)
        )
    return result(
        "estimate",
        "unpaired_bootstrap_percentile",
        MODULE,
        references=["efron1979bootstrap"],
        exploratory=True,
        caveats=caveats,
        available=True,
        n_a=len(xa),
        n_b=len(xb),
        dropped=da + db,
        level=level,
        statistic=statistic,
        seed=seed,
        resamples=capped,
        estimate=float(
            resampling._stat(xb, statistic) - resampling._stat(xa, statistic)
        ),
        low=float(lo),
        high=float(hi),
    )
