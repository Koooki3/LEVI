"""Shared plumbing of the analysis library: the result envelope, input
cleaning and the few distribution functions the standard library lacks.

Everything here is a pure function of its arguments. There is no module
state, no cache and no global random generator: every function that draws
random numbers takes an explicit ``seed`` and builds its own
``numpy.random.Generator`` (PCG64), so the same input gives the same output
in any process and two threads never share a generator.
"""

from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np

SCHEMA_VERSION = "levi.aeri.analysis.v1"
IMPLEMENTATION_VERSION = 1
DEFAULT_LEVEL = 0.95
DEFAULT_ALPHA = 0.05
# Upper bounds that keep a call's work and memory bounded whatever the input.
MAX_RESAMPLES = 100_000
# Elements gathered per resampling chunk (about 32 MB of float64).
CHUNK_ELEMENTS = 4_000_000

_NORMAL = NormalDist()


class AnalysisInputError(ValueError):
    """The input cannot be analysed as given (wrong shape, infinite values,
    impossible counts). NaN is never an error: it is dropped and counted."""


def caveat(code: str, message: str, **detail) -> dict:
    """One machine-readable caveat. ``code`` is stable; ``message`` is plain
    English for logs; report templates translate by ``code``."""
    return {"code": code, "message": message, **detail}


def result(
    kind: str,
    method: str,
    module: str,
    *,
    references: tuple[str, ...] | list[str] = (),
    exploratory: bool,
    caveats: list[dict] | None = None,
    **fields,
) -> dict:
    """The envelope every public function returns: plain JSON types only."""
    out = {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "method": method,
        "implementation": f"levi.automatic.analysis.{module}@{IMPLEMENTATION_VERSION}",
        "references": list(references),
        "exploratory": bool(exploratory),
        "caveats": list(caveats or []),
    }
    out.update({k: jsonable(v) for k, v in fields.items()})
    return out


def jsonable(value):
    """numpy scalars and arrays to plain Python; NaN and inf to None."""
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, np.ndarray):
        return [jsonable(v) for v in value.tolist()]
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        value = float(value)
        return value if math.isfinite(value) else None
    return value


# ------------------------------------------------------------------ inputs


def check_level(level: float) -> float:
    level = float(level)
    if not 0.5 <= level < 1:
        raise AnalysisInputError(f"confidence level must be in [0.5, 1), got {level}")
    return level


def check_alpha(alpha: float) -> float:
    alpha = float(alpha)
    if not 0 < alpha < 1:
        raise AnalysisInputError(f"alpha must be in (0, 1), got {alpha}")
    return alpha


def check_count(k, n) -> tuple[int, int]:
    """``k`` successes in ``n`` trials as validated ints."""
    for name, v in (("k", k), ("n", n)):
        if isinstance(v, bool) or not isinstance(v, (int, np.integer)):
            if isinstance(v, (float, np.floating)) and float(v).is_integer():
                continue
            raise AnalysisInputError(f"{name} must be a whole number, got {v!r}")
    k, n = int(k), int(n)
    if n < 0 or k < 0 or k > n:
        raise AnalysisInputError(f"need 0 <= k <= n, got k={k}, n={n}")
    return k, n


def clean_floats(values, name: str = "values") -> tuple[np.ndarray, int]:
    """A 1-D float array without NaN, and how many NaN were dropped.
    Infinite values raise: they are a bug upstream, not a measurement."""
    arr = np.asarray(list(values) if not isinstance(values, np.ndarray) else values)
    if arr.dtype == object:
        arr = np.array([np.nan if v is None else v for v in arr.tolist()], dtype=float)
    arr = np.asarray(arr, dtype=float).reshape(-1)
    nan = np.isnan(arr)
    if np.isinf(arr).any():
        raise AnalysisInputError(f"{name} contains an infinite value")
    return arr[~nan], int(nan.sum())


def clean_pairs(pairs, name: str = "pairs") -> tuple[np.ndarray, np.ndarray, int]:
    """Paired values ``[(a, b), ...]``: two float arrays with every pair that
    has a NaN or None on either side removed, and the count removed."""
    rows = list(pairs)
    a = np.array([np.nan if r[0] is None else r[0] for r in rows], dtype=float)
    b = np.array([np.nan if r[1] is None else r[1] for r in rows], dtype=float)
    if np.isinf(a).any() or np.isinf(b).any():
        raise AnalysisInputError(f"{name} contains an infinite value")
    keep = ~(np.isnan(a) | np.isnan(b))
    return a[keep], b[keep], int((~keep).sum())


def clean_binary_pairs(
    pairs, name: str = "pairs"
) -> tuple[np.ndarray, np.ndarray, int]:
    """Paired 0/1 outcomes; True/False, 0/1 and 0.0/1.0 are accepted, a pair
    with None or NaN on either side is dropped and counted, anything else
    raises."""
    a, b, dropped = clean_pairs(pairs, name)
    for side in (a, b):
        if not np.isin(side, (0.0, 1.0)).all():
            raise AnalysisInputError(f"{name} must hold 0/1 outcomes")
    return a.astype(np.int64), b.astype(np.int64), dropped


def clean_binary(values, name: str = "values") -> tuple[np.ndarray, int]:
    arr, dropped = clean_floats(values, name)
    if not np.isin(arr, (0.0, 1.0)).all():
        raise AnalysisInputError(f"{name} must hold 0/1 outcomes")
    return arr.astype(np.int64), dropped


def check_seed(seed) -> int:
    if isinstance(seed, bool) or not isinstance(seed, (int, np.integer)):
        raise AnalysisInputError("seed must be an int (randomness is always explicit)")
    if int(seed) < 0:
        raise AnalysisInputError("seed must be non-negative")
    return int(seed)


def generator(seed: int) -> np.random.Generator:
    return np.random.Generator(np.random.PCG64(check_seed(seed)))


def check_resamples(count) -> int:
    count = int(count)
    if count < 1:
        raise AnalysisInputError("the number of resamples must be at least 1")
    return min(count, MAX_RESAMPLES)


# ------------------------------------------------------------ distributions


def z_of(level: float) -> float:
    """The two-sided standard-normal critical value for ``level``."""
    return _NORMAL.inv_cdf(0.5 + check_level(level) / 2)


def norm_sf(x: float) -> float:
    """Upper tail of the standard normal, accurate far into the tail."""
    return 0.5 * math.erfc(x / math.sqrt(2.0))


def chi2_sf(x: float, df: int) -> float:
    """Upper tail of the chi-square distribution with integer ``df``, from
    the closed forms of the regularised gamma function at integer and
    half-integer shape (no series truncation)."""
    if df < 1 or int(df) != df:
        raise AnalysisInputError("chi-square df must be a positive integer")
    if x <= 0:
        return 1.0
    half = x / 2.0
    if df % 2 == 0:
        term, total = 1.0, 1.0
        for i in range(1, df // 2):
            term *= half / i
            total += term
        return min(1.0, math.exp(-half) * total)
    total = math.erfc(math.sqrt(half))
    term = math.sqrt(half) * math.exp(-half) / math.gamma(1.5)
    for i in range(1, (df + 1) // 2):
        total += term
        term *= half / (i + 0.5)
    return min(1.0, total)


def log_binom_pmf(k: np.ndarray, n: int, p: float) -> np.ndarray:
    """log P(X = k) for X ~ Binomial(n, p), elementwise; -inf off support."""
    k = np.asarray(k, dtype=float)
    out = np.full(k.shape, -np.inf)
    ok = (k >= 0) & (k <= n)
    kk = k[ok]
    lc = (
        math.lgamma(n + 1)
        - np.vectorize(math.lgamma, otypes=[float])(kk + 1)
        - np.vectorize(math.lgamma, otypes=[float])(n - kk + 1)
    )
    if p <= 0:
        out[ok] = np.where(kk == 0, 0.0, -np.inf)
    elif p >= 1:
        out[ok] = np.where(kk == n, 0.0, -np.inf)
    else:
        out[ok] = lc + kk * math.log(p) + (n - kk) * math.log1p(-p)
    return out


def binom_pmf(n: int, p: float) -> np.ndarray:
    """The whole Binomial(n, p) probability vector (length n + 1)."""
    return np.exp(log_binom_pmf(np.arange(n + 1), n, p))


def binom_cdf(k: int, n: int, p: float) -> float:
    """P(X <= k), X ~ Binomial(n, p), summing exact terms (``math.comb``)."""
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    if p <= 0:
        return 1.0
    if p >= 1:
        return 0.0
    # Sum in log space to stay finite for large n.
    terms = log_binom_pmf(np.arange(k + 1), n, p)
    top = terms.max()
    return float(min(1.0, math.exp(top) * np.exp(terms - top).sum()))


def bisect(
    fn, lo: float, hi: float, target: float, increasing: bool, tol=1e-12
) -> float:
    """The root of ``fn(x) = target`` on ``[lo, hi]`` for a monotone ``fn``."""
    for _ in range(200):
        mid = (lo + hi) / 2
        value = fn(mid)
        if (value < target) == increasing:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return (lo + hi) / 2


def midranks(values: np.ndarray) -> np.ndarray:
    """Ranks starting at 1, ties sharing their average rank."""
    values = np.asarray(values, dtype=float)
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(len(values), dtype=float)
    sorted_vals = values[order]
    i = 0
    n = len(values)
    while i < n:
        j = i
        while j + 1 < n and sorted_vals[j + 1] == sorted_vals[i]:
            j += 1
        ranks[order[i : j + 1]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def tie_sizes(values: np.ndarray) -> np.ndarray:
    _, counts = np.unique(np.asarray(values, dtype=float), return_counts=True)
    return counts[counts > 1]
