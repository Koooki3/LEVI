"""Continuous metrics (completion time, steps, smoothness, wall clock).

Tests (rank based, no distribution assumed):

- Wilcoxon's signed-rank test for pairs (Wilcoxon 1945, doi:10.2307/3001968):
  zero differences dropped (Wilcoxon's convention) and counted, ties get
  mid-ranks; the exact null distribution of the rank sum is enumerated by
  dynamic programming over doubled mid-ranks up to 50 non-zero pairs, and
  the tie-corrected normal approximation (continuity corrected) is used
  beyond.
- Mann-Whitney's rank-sum test for independent arms (Mann and Whitney 1947,
  doi:10.1214/aoms/1177730491): exact by dynamic programming up to 60 values
  in total (ties included), tie-corrected normal approximation beyond.

Effect sizes:

- Hodges-Lehmann shift (Hodges and Lehmann 1963, doi:10.1214/aoms/1177704172):
  the median of the Walsh averages (pairs) or of all cross differences
  (independent arms), with a percentile bootstrap interval;
- Cliff's delta (Cliff 1993, doi:10.1037/0033-2909.114.3.494) for independent
  arms: ``P(B > A) - P(B < A)``;
- for pairs, the share of pairs in which B is better (ties count half),
  the "probability of improvement" idea of Agarwal et al. (2021,
  arXiv:2108.13264).
"""

from __future__ import annotations

import math

import numpy as np

from . import _core
from ._core import AnalysisInputError, caveat, result

MODULE = "continuous"
EXACT_SIGNED_RANK_MAX = 50
EXACT_RANK_SUM_MAX = 60
HL_PAIRS_MAX = 3000
HL_CROSS_MAX = 5_000_000
HL_BOOTSTRAP_MAX = 400


def _two_sided_from_counts(
    counts: np.ndarray, support: np.ndarray, centre2: int, observed2: int
):
    """``P(|2S - centre2| >= |observed2 - centre2|)`` from integer counts
    over integer (doubled) sums ``support``."""
    dev = abs(observed2 - centre2)
    mask = np.abs(2 * support - centre2) >= dev
    return float(counts[mask].sum() / counts.sum())


def signed_rank_null(doubled_ranks: list[int]) -> np.ndarray:
    """Counts of each value of the doubled positive-rank sum over all
    ``2**n`` sign patterns (index = doubled sum)."""
    total = sum(doubled_ranks)
    counts = np.zeros(total + 1, dtype=np.int64)
    counts[0] = 1
    for r in doubled_ranks:
        shifted = np.zeros_like(counts)
        shifted[r:] = counts[: total + 1 - r]
        counts = counts + shifted
    return counts


def wilcoxon_signed_rank(pairs) -> dict:
    """Wilcoxon's signed-rank test of ``b - a`` over pairs ``(a, b)``."""
    a, b, dropped = _core.clean_pairs(pairs)
    d = b - a
    zeros = int((d == 0).sum())
    d = d[d != 0]
    n = len(d)
    caveats = [caveat("no_power_model", "secondary metric: treated as exploratory")]
    if dropped:
        caveats.append(
            caveat(
                "dropped_missing", f"{dropped} pairs had a missing side", count=dropped
            )
        )
    if zeros:
        caveats.append(
            caveat("zeros_dropped", f"{zeros} zero differences dropped", count=zeros)
        )
    if n == 0:
        return result(
            "test",
            "wilcoxon_signed_rank",
            MODULE,
            references=["wilcoxon1945"],
            exploratory=True,
            caveats=caveats + [caveat("empty", "no non-zero differences")],
            available=False,
            n=0,
            zeros=zeros,
            dropped=dropped,
            statistic=None,
            p_value=None,
        )
    ranks = _core.midranks(np.abs(d))
    w_plus = float(ranks[d > 0].sum())
    ties = _core.tie_sizes(np.abs(d))
    if n <= EXACT_SIGNED_RANK_MAX:
        doubled = [round(2 * r) for r in ranks]
        counts = signed_rank_null(doubled)
        support = np.arange(len(counts))
        p = _two_sided_from_counts(counts, support, sum(doubled), round(2 * w_plus))
        mode = "exact"
    else:
        mean = n * (n + 1) / 4
        var = (ranks**2).sum() / 4
        z = max(0.0, abs(w_plus - mean) - 0.5) / math.sqrt(var)
        p = min(1.0, 2 * _core.norm_sf(z))
        mode = "normal"
    if len(ties):
        caveats.append(caveat("ties", "tied absolute differences got mid-ranks"))
    return result(
        "test",
        "wilcoxon_signed_rank",
        MODULE,
        references=["wilcoxon1945"],
        exploratory=True,
        caveats=caveats,
        available=True,
        n=n,
        zeros=zeros,
        dropped=dropped,
        statistic=w_plus,
        mode=mode,
        p_value=min(1.0, p),
    )


def rank_sum_null(doubled_ranks: list[int], size: int) -> np.ndarray:
    """Counts of each doubled rank sum of a ``size``-subset of the pooled
    doubled ranks (index = doubled sum)."""
    total = sum(doubled_ranks)
    table = np.zeros((size + 1, total + 1), dtype=np.int64)
    table[0, 0] = 1
    for r in doubled_ranks:
        for j in range(size, 0, -1):
            table[j, r:] += table[j - 1, : total + 1 - r]
    return table[size]


def mann_whitney(a, b) -> dict:
    """Mann-Whitney's test; ``u_b`` counts pairs with B above A (ties half)."""
    xa, da = _core.clean_floats(a, "a")
    xb, db = _core.clean_floats(b, "b")
    na, nb = len(xa), len(xb)
    caveats = [caveat("no_power_model", "secondary metric: treated as exploratory")]
    if da or db:
        caveats.append(
            caveat("dropped_nan", f"{da + db} NaN values dropped", count=da + db)
        )
    if na == 0 or nb == 0:
        return result(
            "test",
            "mann_whitney",
            MODULE,
            references=["mann1947whitney"],
            exploratory=True,
            caveats=caveats + [caveat("empty", "an arm has no values")],
            available=False,
            n_a=na,
            n_b=nb,
            u_b=None,
            p_value=None,
        )
    pooled = np.concatenate([xa, xb])
    ranks = _core.midranks(pooled)
    rank_b = float(ranks[na:].sum())
    u_b = rank_b - nb * (nb + 1) / 2
    big_n = na + nb
    if big_n <= EXACT_RANK_SUM_MAX:
        doubled = [round(2 * r) for r in ranks]
        counts = rank_sum_null(doubled, nb)
        support = np.arange(len(counts))
        centre2 = nb * (big_n + 1)
        p = _two_sided_from_counts(counts, support, centre2, round(2 * rank_b))
        mode = "exact"
    else:
        ties = _core.tie_sizes(pooled)
        var = (
            na
            * nb
            / 12
            * ((big_n + 1) - ((ties**3 - ties).sum()) / (big_n * (big_n - 1)))
        )
        z = max(0.0, abs(u_b - na * nb / 2) - 0.5) / math.sqrt(var) if var > 0 else 0.0
        p = min(1.0, 2 * _core.norm_sf(z)) if var > 0 else 1.0
        mode = "normal"
    return result(
        "test",
        "mann_whitney",
        MODULE,
        references=["mann1947whitney"],
        exploratory=True,
        caveats=caveats,
        available=True,
        n_a=na,
        n_b=nb,
        u_b=u_b,
        mode=mode,
        p_value=min(1.0, p),
    )


def cliffs_delta(a, b) -> dict:
    """Cliff's delta ``P(B > A) - P(B < A)`` by sorting (O(n log n))."""
    xa, da = _core.clean_floats(a, "a")
    xb, db = _core.clean_floats(b, "b")
    caveats = []
    if da or db:
        caveats.append(
            caveat("dropped_nan", f"{da + db} NaN values dropped", count=da + db)
        )
    if len(xa) == 0 or len(xb) == 0:
        return result(
            "estimate",
            "cliffs_delta",
            MODULE,
            references=["cliff1993"],
            exploratory=True,
            caveats=caveats + [caveat("empty", "an arm has no values")],
            available=False,
            delta=None,
        )
    sa = np.sort(xa)
    below = np.searchsorted(sa, xb, side="left").sum()
    above = (len(sa) - np.searchsorted(sa, xb, side="right")).sum()
    delta = (int(below) - int(above)) / (len(xa) * len(xb))
    return result(
        "estimate",
        "cliffs_delta",
        MODULE,
        references=["cliff1993"],
        exploratory=True,
        caveats=caveats,
        available=True,
        n_a=len(xa),
        n_b=len(xb),
        delta=delta,
    )


def improvement_share(pairs, *, higher_is_better: bool) -> dict:
    """Share of pairs in which B is better than A (a tie counts half)."""
    a, b, dropped = _core.clean_pairs(pairs)
    n = len(a)
    if n == 0:
        return result(
            "estimate",
            "paired_improvement_share",
            MODULE,
            references=["agarwal2021rliable"],
            exploratory=True,
            caveats=[caveat("empty", "no complete pairs")],
            available=False,
            share=None,
        )
    sign = 1.0 if higher_is_better else -1.0
    d = sign * (b - a)
    share = ((d > 0).sum() + 0.5 * (d == 0).sum()) / n
    return result(
        "estimate",
        "paired_improvement_share",
        MODULE,
        references=["agarwal2021rliable"],
        exploratory=True,
        caveats=[]
        if not dropped
        else [caveat("dropped_missing", f"{dropped} pairs dropped", count=dropped)],
        available=True,
        n_pairs=n,
        higher_is_better=higher_is_better,
        share=float(share),
    )


def hl_paired(d: np.ndarray) -> float:
    """Median of the Walsh averages ``(d_i + d_j) / 2``, ``i <= j``."""
    i, j = np.triu_indices(len(d))
    return float(np.median((d[i] + d[j]) / 2))


def hl_shift(a: np.ndarray, b: np.ndarray) -> float:
    """Median of all cross differences ``b_j - a_i``."""
    return float(np.median(np.subtract.outer(b, a)))


def hodges_lehmann(
    a,
    b=None,
    *,
    paired: bool,
    seed: int,
    resamples: int = 2000,
    level: float = _core.DEFAULT_LEVEL,
) -> dict:
    """Hodges-Lehmann shift of B over A with a percentile bootstrap
    interval. ``paired=True`` takes pairs ``[(a, b), ...]`` as ``a``."""
    level = _core.check_level(level)
    seed = _core.check_seed(seed)
    caveats = [caveat("no_power_model", "secondary metric: treated as exploratory")]
    rng = _core.generator(seed)
    resamples = min(_core.check_resamples(resamples), 20_000)
    if paired:
        if b is not None:
            raise AnalysisInputError("paired=True takes the pairs as the only argument")
        xa, xb, dropped = _core.clean_pairs(a)
        d = xb - xa
        n = len(d)
        if n == 0:
            return _hl_empty(caveats, "paired")
        if n > HL_PAIRS_MAX:
            return _hl_empty(
                caveats
                + [
                    caveat("too_large", f"n={n} above {HL_PAIRS_MAX} Walsh-average cap")
                ],
                "paired",
            )
        estimate = hl_paired(d)
        boot = None
        if n <= HL_BOOTSTRAP_MAX:
            boot = np.array(
                [hl_paired(d[rng.integers(0, n, n)]) for _ in range(resamples)]
            )
        sizes = {"n_pairs": n}
    else:
        xa, da = _core.clean_floats(a, "a")
        xb, db = _core.clean_floats(b, "b")
        dropped = da + db
        if len(xa) == 0 or len(xb) == 0:
            return _hl_empty(caveats, "two_sample")
        if len(xa) * len(xb) > HL_CROSS_MAX:
            return _hl_empty(
                caveats
                + [caveat("too_large", f"more than {HL_CROSS_MAX} cross differences")],
                "two_sample",
            )
        estimate = hl_shift(xa, xb)
        boot = None
        if max(len(xa), len(xb)) <= HL_BOOTSTRAP_MAX:
            boot = np.array(
                [
                    hl_shift(
                        xa[rng.integers(0, len(xa), len(xa))],
                        xb[rng.integers(0, len(xb), len(xb))],
                    )
                    for _ in range(resamples)
                ]
            )
        sizes = {"n_a": len(xa), "n_b": len(xb)}
    if dropped:
        caveats.append(
            caveat("dropped_missing", f"{dropped} values dropped", count=dropped)
        )
    low = high = None
    if boot is None:
        caveats.append(
            caveat(
                "interval_skipped",
                f"bootstrap interval skipped above {HL_BOOTSTRAP_MAX} values",
            )
        )
    else:
        alpha = 1 - level
        low, high = (float(v) for v in np.quantile(boot, [alpha / 2, 1 - alpha / 2]))
    return result(
        "estimate",
        "hodges_lehmann_" + ("paired" if paired else "two_sample"),
        MODULE,
        references=["hodges1963", "efron1979bootstrap"],
        exploratory=True,
        caveats=caveats,
        available=True,
        **sizes,
        dropped=dropped,
        level=level,
        seed=seed,
        resamples=resamples if boot is not None else 0,
        estimate=estimate,
        low=low,
        high=high,
    )


def _hl_empty(caveats, kind) -> dict:
    return result(
        "estimate",
        f"hodges_lehmann_{kind}",
        MODULE,
        references=["hodges1963"],
        exploratory=True,
        caveats=caveats
        + (
            []
            if any(c["code"] == "too_large" for c in caveats)
            else [caveat("empty", "no values")]
        ),
        available=False,
        estimate=None,
        low=None,
        high=None,
    )
