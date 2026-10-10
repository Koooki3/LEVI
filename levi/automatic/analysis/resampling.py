"""Bootstrap intervals with explicit seeds.

Bootstrap (Efron 1979, doi:10.1214/aos/1176344552): the percentile interval
by default, BCa on request (Efron 1987, doi:10.1080/01621459.1987.10478410).

Bounded work: the number of resamples is capped at ``MAX_RESAMPLES``; data
with at most 16 distinct values and the mean statistic are resampled
through multinomial category counts (cost independent of n); everything
else is resampled in chunks of ``CHUNK_ELEMENTS`` gathered values.
"""

from __future__ import annotations

from statistics import NormalDist

import numpy as np

from . import _core
from ._core import AnalysisInputError

STATISTICS = ("mean", "median")
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


def bca_acceleration(values: np.ndarray, statistic: str) -> float:
    """The BCa acceleration ``a = sum d^3 / (6 (sum d^2)^1.5)`` with
    ``d = mean(jackknife) - jackknife``; positive for a right-skewed mean."""
    jack = _jackknife(np.asarray(values, dtype=float), statistic)
    diff = jack.mean() - jack
    denom = 6 * (diff**2).sum() ** 1.5
    return float((diff**3).sum() / denom) if denom > 0 else 0.0


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
    accel = bca_acceleration(values, statistic)
    limits = []
    for q in (alpha / 2, 1 - alpha / 2):
        zq = _NORMAL.inv_cdf(q)
        adj = _NORMAL.cdf(z0 + (z0 + zq) / (1 - accel * (z0 + zq)))
        limits.append(float(np.quantile(replicates, min(1.0, max(0.0, adj)))))
    return limits[0], limits[1], notes
