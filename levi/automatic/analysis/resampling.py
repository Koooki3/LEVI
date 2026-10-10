"""Bootstrap intervals and permutation p-values with explicit seeds.

Bootstrap (Efron 1979, doi:10.1214/aos/1176344552): the percentile interval
by default, BCa on request (Efron 1987, doi:10.1080/01621459.1987.10478410).
Permutation tests: exhaustive sign flips for up to 20 pairs, otherwise a
fixed-seed Monte Carlo with at most 10^5 draws; the Monte Carlo p-value is
``(1 + hits) / (1 + draws)`` so it is never 0.

Bounded work: the number of resamples is capped at ``MAX_RESAMPLES``; data
with at most 16 distinct values and the mean statistic are resampled
through multinomial category counts (cost independent of n); everything
else is resampled in chunks of ``CHUNK_ELEMENTS`` gathered values.
"""

from __future__ import annotations

from itertools import product
from statistics import NormalDist

import numpy as np

from . import _core
from ._core import AnalysisInputError

STATISTICS = ("mean", "median")
EXACT_SIGN_FLIP_MAX = 20
MAX_PERMUTATIONS = 100_000
BCA_MAX_N = 5000
_NORMAL = NormalDist()


def _stat(values: np.ndarray, statistic: str, axis=None):
    if statistic == "mean":
        return values.mean(axis=axis)
    if statistic == "median":
        return np.median(values, axis=axis)
    raise AnalysisInputError(f"statistic must be one of {STATISTICS}")


def bootstrap_distribution(
    values: np.ndarray, statistic: str, resamples: int, seed: int
) -> np.ndarray:
    """``resamples`` bootstrap replicates of ``statistic(values)``."""
    values = np.asarray(values, dtype=float)
    n = len(values)
    if n == 0:
        raise AnalysisInputError("cannot bootstrap an empty sample")
    resamples = _core.check_resamples(resamples)
    rng = _core.generator(seed)
    levels, counts = np.unique(values, return_counts=True)
    if statistic == "mean" and len(levels) <= 16:
        draws = rng.multinomial(n, counts / n, size=resamples)
        return draws @ levels / n
    out = np.empty(resamples)
    rows = max(1, _core.CHUNK_ELEMENTS // n)
    for start in range(0, resamples, rows):
        stop = min(resamples, start + rows)
        idx = rng.integers(0, n, size=(stop - start, n))
        out[start:stop] = _stat(values[idx], statistic, axis=1)
    return out


def _jackknife(values: np.ndarray, statistic: str) -> np.ndarray:
    n = len(values)
    if statistic == "mean":
        return (values.sum() - values) / (n - 1)
    return np.array([_stat(np.delete(values, i), statistic) for i in range(n)])


def interval(
    values: np.ndarray,
    replicates: np.ndarray,
    statistic: str,
    level: float,
    method: str,
) -> tuple[float | None, float | None, list[dict]]:
    """Percentile or BCa limits from bootstrap replicates."""
    alpha = 1 - level
    notes = []
    if method == "percentile":
        lo, hi = np.quantile(replicates, [alpha / 2, 1 - alpha / 2])
        return float(lo), float(hi), notes
    if method != "bca":
        raise AnalysisInputError("method must be 'percentile' or 'bca'")
    n = len(values)
    if n > BCA_MAX_N and statistic != "mean":
        notes.append(
            _core.caveat(
                "bca_skipped",
                f"BCa needs a jackknife; n={n} exceeds {BCA_MAX_N}, percentile used",
            )
        )
        lo, hi = np.quantile(replicates, [alpha / 2, 1 - alpha / 2])
        return float(lo), float(hi), notes
    theta = float(_stat(values, statistic))
    below = (replicates < theta).mean() + 0.5 * (replicates == theta).mean()
    if below <= 0 or below >= 1 or n < 3:
        notes.append(
            _core.caveat("bca_degenerate", "BCa undefined here; percentile used")
        )
        lo, hi = np.quantile(replicates, [alpha / 2, 1 - alpha / 2])
        return float(lo), float(hi), notes
    z0 = _NORMAL.inv_cdf(float(below))
    jack = _jackknife(values, statistic)
    diff = jack.mean() - jack
    denom = 6 * (diff**2).sum() ** 1.5
    accel = float((diff**3).sum() / denom) if denom > 0 else 0.0
    limits = []
    for q in (alpha / 2, 1 - alpha / 2):
        zq = _NORMAL.inv_cdf(q)
        adj = _NORMAL.cdf(z0 + (z0 + zq) / (1 - accel * (z0 + zq)))
        limits.append(float(np.quantile(replicates, min(1.0, max(0.0, adj)))))
    return limits[0], limits[1], notes


def sign_flip_pvalue(
    diffs: np.ndarray, *, statistic: str = "mean", seed: int = 0
) -> tuple[float, str, int]:
    """Two-sided p-value of ``|statistic(diffs)|`` under random sign flips:
    exhaustive for up to 20 differences, Monte Carlo (``MAX_PERMUTATIONS``
    draws) beyond. Returns ``(p, mode, draws)``."""
    diffs = np.asarray(diffs, dtype=float)
    n = len(diffs)
    if n == 0:
        raise AnalysisInputError("no differences")
    observed = abs(float(_stat(diffs, statistic)))
    tol = 1e-12 * max(1.0, observed)
    if n <= EXACT_SIGN_FLIP_MAX:
        hits = 0
        total = 0
        # Chunk the 2**n sign patterns 2**12 at a time.
        head = min(n, 12)
        tail = n - head
        head_signs = np.array(list(product((1.0, -1.0), repeat=head)))
        for rest in product((1.0, -1.0), repeat=tail):
            signs = np.hstack([head_signs, np.tile(rest, (len(head_signs), 1))])
            stats = np.abs(_stat(signs * diffs, statistic, axis=1))
            hits += int((stats >= observed - tol).sum())
            total += len(signs)
        return hits / total, "exact", total
    rng = _core.generator(seed)
    hits = 0
    rows = max(1, _core.CHUNK_ELEMENTS // n)
    for start in range(0, MAX_PERMUTATIONS, rows):
        stop = min(MAX_PERMUTATIONS, start + rows)
        signs = rng.choice((-1.0, 1.0), size=(stop - start, n))
        stats = np.abs(_stat(signs * diffs, statistic, axis=1))
        hits += int((stats >= observed - tol).sum())
    return (1 + hits) / (1 + MAX_PERMUTATIONS), "monte_carlo", MAX_PERMUTATIONS


def label_permutation_pvalue(
    a: np.ndarray, b: np.ndarray, *, statistic: str = "mean", seed: int = 0
) -> tuple[float, int]:
    """Two-sided p-value of ``|stat(b) - stat(a)|`` under random relabelling
    of the pooled sample (``MAX_PERMUTATIONS`` Monte Carlo draws)."""
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    if len(a) == 0 or len(b) == 0:
        raise AnalysisInputError("both samples need values")
    pooled = np.concatenate([a, b])
    n, na = len(pooled), len(a)
    observed = abs(float(_stat(b, statistic) - _stat(a, statistic)))
    tol = 1e-12 * max(1.0, observed)
    rng = _core.generator(seed)
    hits = 0
    rows = max(1, _core.CHUNK_ELEMENTS // n)
    for start in range(0, MAX_PERMUTATIONS, rows):
        stop = min(MAX_PERMUTATIONS, start + rows)
        perm = rng.permuted(np.tile(pooled, (stop - start, 1)), axis=1)
        stats = np.abs(
            _stat(perm[:, na:], statistic, axis=1)
            - _stat(perm[:, :na], statistic, axis=1)
        )
        hits += int((stats >= observed - tol).sum())
    return (1 + hits) / (1 + MAX_PERMUTATIONS), MAX_PERMUTATIONS
