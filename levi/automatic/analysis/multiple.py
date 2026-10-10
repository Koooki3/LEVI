"""More than two arms: omnibus tests within blocks and multiplicity control.

Omnibus tests, both computed per block (a block = one layout card in one
round, every arm once):

- Cochran's Q for 0/1 outcomes (Cochran 1950, doi:10.1093/biomet/37.3-4.256);
- Friedman's rank test for continuous outcomes (Friedman 1937,
  doi:10.1080/01621459.1937.10503522).

Their p-values come from permuting the arm labels within each block
(fixed seed, at most 10^5 draws); the chi-square approximation is reported
beside it for reference only.

Multiplicity (input: a list of p-values, output: adjusted p-values and
decisions):

- Holm (1979, Scandinavian Journal of Statistics 6(2):65-70): strong
  family-wise error control under any dependence; the default for the
  pre-registered primary pairwise family;
- Bonferroni (Dunn 1961, doi:10.1080/01621459.1961.10482090): the same
  guarantee, less power; used where a compact letter display needs a single
  threshold;
- Benjamini-Hochberg (1995, doi:10.1111/j.2517-6161.1995.tb02031.x): false
  discovery rate for the exploratory secondary family. The original proof
  assumes independent tests; metrics measured on the same trials are
  dependent, so it is a screening device here and is labelled as such.
"""

from __future__ import annotations

import numpy as np

from . import _core
from ._core import AnalysisInputError, caveat, result

MODULE = "multiple"
MAX_PERMUTATIONS = 100_000


def _pvalues(pvalues) -> tuple[np.ndarray, list[int], int]:
    raw = list(pvalues)
    arr = np.array([np.nan if p is None else p for p in raw], dtype=float)
    if np.isinf(arr).any():
        raise AnalysisInputError("p-values must be finite")
    keep = [i for i, p in enumerate(arr) if not np.isnan(p)]
    vals = arr[keep]
    if ((vals < 0) | (vals > 1)).any():
        raise AnalysisInputError("p-values must lie in [0, 1]")
    return vals, keep, len(raw)


def _adjusted(method: str, refs, applicability: str, pvalues, alpha, fn) -> dict:
    alpha = _core.check_alpha(alpha)
    vals, keep, total = _pvalues(pvalues)
    adjusted = [None] * total
    reject = [None] * total
    if len(vals):
        adj = fn(vals)
        for slot, a in zip(keep, adj, strict=True):
            adjusted[slot] = float(a)
            reject[slot] = bool(a <= alpha)
    caveats = [caveat("applicability", applicability)]
    dropped = total - len(vals)
    if dropped:
        caveats.append(
            caveat(
                "dropped_missing",
                f"{dropped} missing p-values were left out of the family",
                count=dropped,
            )
        )
    return result(
        "adjustment",
        method,
        MODULE,
        references=refs,
        exploratory=method == "benjamini_hochberg",
        caveats=caveats,
        alpha=alpha,
        family_size=len(vals),
        adjusted=adjusted,
        reject=reject,
    )


def _holm(p: np.ndarray) -> np.ndarray:
    m = len(p)
    order = np.argsort(p, kind="mergesort")
    stepped = np.maximum.accumulate((m - np.arange(m)) * p[order])
    out = np.empty(m)
    out[order] = np.minimum(1.0, stepped)
    return out


def _bh(p: np.ndarray) -> np.ndarray:
    m = len(p)
    order = np.argsort(p, kind="mergesort")
    scaled = p[order] * m / np.arange(1, m + 1)
    stepped = np.minimum.accumulate(scaled[::-1])[::-1]
    out = np.empty(m)
    out[order] = np.minimum(1.0, stepped)
    return out


def holm(pvalues, *, alpha: float = _core.DEFAULT_ALPHA) -> dict:
    """Holm's step-down adjusted p-values; ``reject`` at ``alpha``."""
    return _adjusted(
        "holm",
        ["holm1979"],
        "family-wise error control in the strong sense under any dependence",
        pvalues,
        alpha,
        _holm,
    )


def bonferroni(pvalues, *, alpha: float = _core.DEFAULT_ALPHA) -> dict:
    """Bonferroni-adjusted p-values ``min(1, m p)``."""
    return _adjusted(
        "bonferroni",
        ["dunn1961bonferroni"],
        "family-wise error control under any dependence; never more powerful than Holm",
        pvalues,
        alpha,
        lambda p: np.minimum(1.0, p * len(p)),
    )


def benjamini_hochberg(pvalues, *, alpha: float = _core.DEFAULT_ALPHA) -> dict:
    """Benjamini-Hochberg adjusted p-values (q-values) at FDR ``alpha``."""
    return _adjusted(
        "benjamini_hochberg",
        ["benjamini1995fdr"],
        "false discovery rate; proven for independent tests, used here for screening only",
        pvalues,
        alpha,
        _bh,
    )


# ------------------------------------------------------------- omnibus


def _blocks(matrix, binary: bool) -> tuple[np.ndarray, int]:
    """Rows = blocks, columns = arms; rows with a NaN are dropped and counted."""
    rows = [list(r) for r in matrix]
    if not rows:
        return np.zeros((0, 0)), 0
    width = len(rows[0])
    if any(len(r) != width for r in rows):
        raise AnalysisInputError("every block needs one value per arm")
    arr = np.array([[np.nan if v is None else v for v in r] for r in rows], dtype=float)
    if np.isinf(arr).any():
        raise AnalysisInputError("values must be finite")
    keep = ~np.isnan(arr).any(axis=1)
    arr = arr[keep]
    if binary and not np.isin(arr, (0.0, 1.0)).all():
        raise AnalysisInputError("Cochran's Q needs 0/1 outcomes")
    return arr, int((~keep).sum())


def cochran_q_statistic(x: np.ndarray, vectorised: bool = False):
    """``Q = (k-1) [k sum C_j^2 - N^2] / (k N - sum R_i^2)`` over a
    blocks-by-arms 0/1 matrix; NaN when every block is all-0 or all-1.
    With ``vectorised`` returns ``(x, score)`` where ``score`` maps stacked
    column totals to Q (the row terms do not change under permutation)."""
    _, k = x.shape
    row = x.sum(axis=1)
    total = row.sum()
    denom = k * total - (row**2).sum()

    def score(col):
        if denom == 0:
            return np.full(np.shape(col)[:-1], np.nan)
        return (k - 1) * (k * (col**2).sum(axis=-1) - total**2) / denom

    if vectorised:
        return x, score
    return float(score(x.sum(axis=0)))


def friedman_statistic(x: np.ndarray, vectorised: bool = False):
    """Friedman's chi-square with the tie correction, over within-block
    mid-ranks of a blocks-by-arms matrix; NaN when every block is all ties.
    With ``vectorised`` returns ``(ranks, score)`` (see Cochran's Q)."""
    b, k = x.shape
    ranks = np.vstack([_core.midranks(r) for r in x])
    ties = sum(((t**3) - t).sum() for t in (_core.tie_sizes(r) for r in x))
    denom = b * k * (k + 1) - ties / (k - 1)

    def score(rsum):
        if denom == 0:
            return np.full(np.shape(rsum)[:-1], np.nan)
        return 12 * ((rsum - b * (k + 1) / 2) ** 2).sum(axis=-1) / denom

    if vectorised:
        return ranks, score
    return float(score(ranks.sum(axis=0)))


def _omnibus(name, refs, stat_fn, matrix, binary, seed, permutations) -> dict:
    seed = _core.check_seed(seed)
    x, dropped = _blocks(matrix, binary)
    b, k = x.shape if x.size else (0, 0)
    caveats = [caveat("no_power_model", "omnibus test: treated as exploratory")]
    if dropped:
        caveats.append(
            caveat(
                "dropped_missing",
                f"{dropped} blocks had a missing value",
                count=dropped,
            )
        )
    if b < 2 or k < 2:
        return result(
            "test",
            name,
            MODULE,
            references=refs,
            exploratory=True,
            caveats=caveats
            + [caveat("too_small", "needs at least 2 blocks and 2 arms")],
            available=False,
            blocks=int(b),
            arms=int(k),
            dropped=dropped,
            statistic=None,
            p_permutation=None,
            p_chi_square=None,
            seed=seed,
        )
    observed = stat_fn(x)
    if np.isnan(observed):
        caveats.append(
            caveat("no_information", "every block is constant: no information")
        )
        return result(
            "test",
            name,
            MODULE,
            references=refs,
            exploratory=True,
            caveats=caveats,
            available=False,
            blocks=int(b),
            arms=int(k),
            dropped=dropped,
            statistic=None,
            p_permutation=1.0,
            p_chi_square=1.0,
            seed=seed,
        )
    draws = min(int(permutations), MAX_PERMUTATIONS)
    rng = _core.generator(seed)
    # Within-block permutation leaves every block's values (so its row sum,
    # ranks and ties) unchanged; only the column totals move. Score the
    # permuted column totals of ``base`` vectorised, in bounded chunks.
    base, score = stat_fn(x, vectorised=True)
    tol = 1e-9 * max(1.0, abs(observed))
    hits = 0
    per_chunk = max(1, _core.CHUNK_ELEMENTS // base.size)
    for start in range(0, draws, per_chunk):
        count = min(per_chunk, draws - start)
        stack = rng.permuted(np.broadcast_to(base, (count, *base.shape)).copy(), axis=2)
        hits += int((score(stack.sum(axis=1)) >= observed - tol).sum())
    p_perm = (1 + hits) / (1 + draws)
    return result(
        "test",
        name,
        MODULE,
        references=refs,
        exploratory=True,
        caveats=caveats,
        available=True,
        blocks=int(b),
        arms=int(k),
        dropped=dropped,
        statistic=observed,
        df=k - 1,
        p_permutation=p_perm,
        permutations=draws,
        p_chi_square=_core.chi2_sf(observed, k - 1),
        seed=seed,
    )


def cochran_q(matrix, *, seed: int, permutations: int = 20_000) -> dict:
    """Cochran's Q over blocks (rows) by arms (columns) of 0/1 outcomes."""
    return _omnibus(
        "cochran_q",
        ["cochran1950q"],
        cochran_q_statistic,
        matrix,
        True,
        seed,
        permutations,
    )


def friedman(matrix, *, seed: int, permutations: int = 20_000) -> dict:
    """Friedman's test over blocks (rows) by arms (columns) of values."""
    return _omnibus(
        "friedman",
        ["friedman1937"],
        friedman_statistic,
        matrix,
        False,
        seed,
        permutations,
    )
