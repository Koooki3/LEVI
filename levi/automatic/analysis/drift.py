"""Order effects and environment drift: diagnostics that find problems, never
corrections of results.

1. ``reference_drift``: the reference arm runs in every round; Mann's
   trend test (Mann 1945, doi:10.2307/1907187) on its per-round success
   rates, plus the first-half versus second-half difference with Newcombe's
   interval and Fisher's test.
2. ``arm_time_interaction``: does the B - A difference change between the
   first and second half of the rounds? Permutation test over which rounds
   form the "first half" (exhaustive when there are at most 10^5
   splits, otherwise fixed-seed Monte Carlo).
3. ``carryover``: per arm, success by the arm that ran in the segment just
   before (Williams designs balance this, Williams 1949,
   doi:10.1071/CH9490149), with Fisher's test when there are two strata.
4. ``drift_warning``: raised when any diagnostic has p < 0.05; the report
   then downgrades its conclusions to exploratory.
"""

from __future__ import annotations

import math
from collections import defaultdict
from itertools import combinations, permutations

import numpy as np

from levi.live.stats import wilson as _live_wilson

from . import _core
from ._core import AnalysisInputError, caveat, result
from .proportions import fisher_exact, newcombe_independent

MODULE = "drift"
EXACT_KENDALL_MAX = 8
MAX_SPLITS = 100_000


def mann_kendall_s(values) -> int:
    x = np.asarray(values, dtype=float)
    i, j = np.triu_indices(len(x), k=1)
    return int(np.sign(x[j] - x[i]).sum())


def mann_kendall(values) -> dict:
    """Mann's trend test on a time-ordered sequence (NaN dropped)."""
    x, dropped = _core.clean_floats(values)
    n = len(x)
    caveats = [
        caveat("diagnostic", "a drift diagnostic; it does not correct any result")
    ]
    if dropped:
        caveats.append(
            caveat("dropped_nan", f"{dropped} missing rounds dropped", count=dropped)
        )
    if n < 3:
        return result(
            "test",
            "mann_kendall",
            MODULE,
            references=["mann1945trend"],
            exploratory=True,
            caveats=caveats + [caveat("too_small", "needs at least 3 rounds")],
            available=False,
            n=n,
            s=None,
            p_value=None,
        )
    s = mann_kendall_s(x)
    if n <= EXACT_KENDALL_MAX:
        perms = np.array(list(permutations(x)))
        i, j = np.triu_indices(n, k=1)
        stats = np.sign(perms[:, j] - perms[:, i]).sum(axis=1)
        p = float((np.abs(stats) >= abs(s)).mean())
        mode = "exact"
    else:
        ties = _core.tie_sizes(x)
        var = (
            n * (n - 1) * (2 * n + 5) - (ties * (ties - 1) * (2 * ties + 5)).sum()
        ) / 18
        z = 0.0 if s == 0 or var <= 0 else (abs(s) - 1) / math.sqrt(var)
        p = min(1.0, 2 * _core.norm_sf(z))
        mode = "normal"
    return result(
        "test",
        "mann_kendall",
        MODULE,
        references=["mann1945trend"],
        exploratory=True,
        caveats=caveats,
        available=True,
        n=n,
        s=s,
        mode=mode,
        p_value=p,
    )


def _rounds(rounds) -> list[tuple[int, int]]:
    out = []
    for row in rounds:
        k, n = _core.check_count(*row)
        out.append((k, n))
    return out


def reference_drift(rounds) -> dict:
    """``rounds``: the reference arm's ``(successes, trials)`` per round in
    time order."""
    rows = _rounds(rounds)
    used = [(k, n) for k, n in rows if n > 0]
    trend = mann_kendall([k / n for k, n in used])
    half = len(used) // 2
    first, second = used[:half], used[len(used) - half :]
    k1, n1 = sum(k for k, _ in first), sum(n for _, n in first)
    k2, n2 = sum(k for k, _ in second), sum(n for _, n in second)
    split = None
    fisher = None
    if n1 and n2:
        split = newcombe_independent(k2, n2, k1, n1)
        fisher = fisher_exact(k2, n2, k1, n1)
    ps = [
        p
        for p in (trend.get("p_value"), fisher and fisher.get("p_value"))
        if p is not None
    ]
    return result(
        "diagnostic",
        "reference_drift",
        MODULE,
        references=["mann1945trend", "newcombe1998independent", "fisher1922"],
        exploratory=True,
        caveats=[
            caveat("diagnostic", "a drift diagnostic; it does not correct any result")
        ]
        + (
            [caveat("odd_rounds", "the middle round is left out of the half split")]
            if len(used) % 2
            else []
        ),
        rounds=len(rows),
        trend=trend,
        second_minus_first=split,
        second_vs_first_fisher=fisher,
        warning=any(p < 0.05 for p in ps),
    )


def _half_difference(rows: np.ndarray, first: np.ndarray) -> float:
    """(B - A in the other rounds) - (B - A in ``first``), pooled rates."""

    def diff(sel):
        ka, na, kb, nb = rows[sel].sum(axis=0)
        return kb / nb - ka / na

    second = np.ones(len(rows), dtype=bool)
    second[first] = False
    return float(diff(second) - diff(first))


def arm_time_interaction(rounds, *, seed: int) -> dict:
    """``rounds``: ``(k_a, n_a, k_b, n_b)`` per round in time order."""
    seed = _core.check_seed(seed)
    rows = []
    for r in rounds:
        ka, na = _core.check_count(r[0], r[1])
        kb, nb = _core.check_count(r[2], r[3])
        if na and nb:
            rows.append((ka, na, kb, nb))
    caveats = [
        caveat("diagnostic", "a drift diagnostic; it does not correct any result")
    ]
    m = len(rows)
    if m < 4:
        return result(
            "test",
            "arm_time_interaction",
            MODULE,
            references=[],
            exploratory=True,
            caveats=caveats
            + [caveat("too_small", "needs at least 4 rounds with both arms")],
            available=False,
            rounds=m,
            p_value=None,
            warning=False,
        )
    arr = np.array(rows, dtype=float)
    half = m // 2
    observed = _half_difference(arr, np.arange(half))
    total = math.comb(m, half)
    tol = 1e-12
    if total <= MAX_SPLITS:
        stats = [
            _half_difference(arr, np.array(c)) for c in combinations(range(m), half)
        ]
        mode = "exact"
        p = float(np.mean(np.abs(stats) >= abs(observed) - tol))
    else:
        rng = _core.generator(seed)
        draws = 20_000
        hits = sum(
            abs(_half_difference(arr, rng.permutation(m)[:half])) >= abs(observed) - tol
            for _ in range(draws)
        )
        p = (1 + hits) / (1 + draws)
        mode = "monte_carlo"
    return result(
        "test",
        "arm_time_interaction",
        MODULE,
        references=[],
        exploratory=True,
        caveats=caveats,
        available=True,
        rounds=m,
        statistic=observed,
        mode=mode,
        p_value=p,
        seed=seed,
        warning=p < 0.05,
    )


def carryover(trials, *, level: float = _core.DEFAULT_LEVEL) -> dict:
    """``trials``: mappings with ``arm``, ``previous_arm`` (None for the
    first segment) and ``success`` (bool; None is left out)."""
    z = _core.z_of(_core.check_level(level))
    table: dict = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for row in trials:
        if row.get("arm") is None:
            raise AnalysisInputError("every trial needs an arm")
        if row.get("success") is None:
            continue
        cell = table[str(row["arm"])][str(row.get("previous_arm"))]
        cell[0] += int(bool(row["success"]))
        cell[1] += 1
    arms = {}
    warning = False
    for arm, strata in sorted(table.items()):
        entry = {
            "by_previous_arm": {
                prev: {"k": k, "n": n, "rate": k / n, "wilson": _live_wilson(k, n, z)}
                for prev, (k, n) in sorted(strata.items())
            }
        }
        real = {p: v for p, v in strata.items() if p != "None"}
        if len(real) == 2:
            (k1, n1), (k2, n2) = (real[p] for p in sorted(real))
            test = fisher_exact(k1, n1, k2, n2)
            entry["fisher_p"] = test["p_value"]
            warning = warning or test["p_value"] < 0.05
        arms[arm] = entry
    return result(
        "diagnostic",
        "carryover",
        MODULE,
        references=["williams1949", "fisher1922", "wilson1927"],
        exploratory=True,
        caveats=[
            caveat(
                "diagnostic", "a carryover diagnostic; it does not correct any result"
            )
        ],
        arms=arms,
        warning=warning,
    )


def drift_warning(*diagnostics: dict) -> dict:
    """One flag over any number of drift diagnostics."""
    raised = [d.get("method") for d in diagnostics if d.get("warning")]
    return result(
        "diagnostic",
        "drift_warning",
        MODULE,
        references=[],
        exploratory=True,
        caveats=[
            caveat("downgrade", "a raised warning makes every conclusion exploratory")
        ]
        if raised
        else [],
        warning=bool(raised),
        raised_by=raised,
    )
