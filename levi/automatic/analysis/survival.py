"""Time to success with right censoring.

The event is "success" and the time is the success step (or seconds). A
failed episode or one that ran to the step budget is censored at its last
step; one stopped by a fault or the operator is censored where it stopped.
Stopping and succeeding are competing events; treating stops as censoring
is an approximation and every result says so.

- Kaplan-Meier product-limit estimate (Kaplan and Meier 1958,
  doi:10.1080/01621459.1958.10501452) with Greenwood's variance and a
  log(-log) interval; ``incidence = 1 - S(t)`` is the curve to draw.
- Log-rank test for k arms (Mantel 1966, Cancer Chemotherapy Reports
  50(3):163-170), chi-square with k - 1 degrees of freedom.
- Restricted mean survival time up to a common horizon ``tau`` (Royston and
  Parmar 2013, doi:10.1186/1471-2288-13-152): the area under S(t) on
  [0, tau]; the difference between arms gets a percentile bootstrap interval
  (each arm resampled on its own, fixed seed). It needs no proportional
  hazards when every arm shares the horizon.
"""

from __future__ import annotations

import math

import numpy as np

from . import _core
from ._core import AnalysisInputError, caveat, result

MODULE = "survival"
COMPETING = caveat(
    "competing_events",
    "stops by fault or operator are treated as censoring; they are competing events",
)


def _clean(times, events) -> tuple[np.ndarray, np.ndarray, int]:
    t = list(times)
    e = list(events)
    if len(t) != len(e):
        raise AnalysisInputError("times and events need the same length")
    tt = np.array([np.nan if v is None else v for v in t], dtype=float)
    ee = np.array([np.nan if v is None else float(v) for v in e], dtype=float)
    if np.isinf(tt).any():
        raise AnalysisInputError("times must be finite")
    keep = ~(np.isnan(tt) | np.isnan(ee))
    tt, ee = tt[keep], ee[keep]
    if (tt < 0).any():
        raise AnalysisInputError("times must be non-negative")
    if not np.isin(ee, (0.0, 1.0)).all():
        raise AnalysisInputError("events must be 0/1 (1 = success observed)")
    return tt, ee.astype(np.int64), int((~keep).sum())


def km_arrays(t: np.ndarray, e: np.ndarray):
    """Distinct event times, numbers at risk and events there, S(t) and the
    Greenwood sum, vectorised (O(n log n))."""
    ts = np.sort(t)
    times, events = np.unique(t[e == 1], return_counts=True)
    at_risk = len(t) - np.searchsorted(ts, times, side="left")
    with np.errstate(divide="ignore", invalid="ignore"):
        surv = np.cumprod(1 - events / at_risk)
        terms = np.where(
            at_risk > events, events / (at_risk * (at_risk - events)), np.inf
        )
    return times, at_risk, events, surv, np.cumsum(terms)


def km_table(t: np.ndarray, e: np.ndarray) -> list[dict]:
    """One row per distinct event time: at risk, events, S(t), Greenwood SE."""
    rows = []
    for time, r, d, s, g in zip(*km_arrays(t, e), strict=True):
        se = float(s * math.sqrt(g)) if math.isfinite(g) else None
        rows.append(
            {
                "time": float(time),
                "at_risk": int(r),
                "events": int(d),
                "survival": float(s),
                "greenwood": float(g),
                "se": se,
            }
        )
    return rows


def kaplan_meier(times, events, *, level: float = _core.DEFAULT_LEVEL) -> dict:
    """The Kaplan-Meier curve of one arm."""
    level = _core.check_level(level)
    t, e, dropped = _clean(times, events)
    z = _core.z_of(level)
    caveats = [COMPETING]
    if dropped:
        caveats.append(
            caveat(
                "dropped_missing",
                f"{dropped} episodes without time or event",
                count=dropped,
            )
        )
    steps = []
    for row in km_table(t, e):
        s, g = row["survival"], row["greenwood"]
        low = high = None
        if 0 < s < 1 and math.isfinite(g) and g > 0:
            # log(-log) interval: always inside (0, 1).
            spread = z * math.sqrt(g) / abs(math.log(s))
            low = s ** math.exp(spread)
            high = s ** math.exp(-spread)
        elif s == 0:
            low = high = 0.0
        steps.append(
            {
                **{k: row[k] for k in ("time", "at_risk", "events", "survival", "se")},
                "incidence": 1 - s,
                "low": low,
                "high": high,
            }
        )
    median = next((r["time"] for r in steps if r["survival"] <= 0.5), None)
    return result(
        "estimate",
        "kaplan_meier",
        MODULE,
        references=["kaplan1958"],
        exploratory=True,
        caveats=caveats,
        available=len(t) > 0,
        n=len(t),
        events=int(e.sum()),
        censored=int(len(t) - e.sum()),
        dropped=dropped,
        level=level,
        steps=steps,
        median_time=median,
    )


def logrank(groups: dict) -> dict:
    """Log-rank test over ``{arm: (times, events)}`` (two or more arms)."""
    names = list(groups)
    if len(names) < 2:
        raise AnalysisInputError("the log-rank test needs at least two arms")
    data = {}
    dropped = 0
    for name in names:
        times, events = groups[name]
        t, e, d = _clean(times, events)
        data[name] = (t, e)
        dropped += d
    k = len(names)
    all_t = np.concatenate([data[n][0] for n in names])
    all_e = np.concatenate([data[n][1] for n in names])
    observed = np.zeros(k)
    expected = np.zeros(k)
    cov = np.zeros((k, k))
    event_times = np.unique(all_t[all_e == 1])
    sorted_t = [np.sort(data[n][0]) for n in names]
    sorted_ev = [np.sort(data[n][0][data[n][1] == 1]) for n in names]
    risk_all = np.array(
        [len(st) - np.searchsorted(st, event_times, side="left") for st in sorted_t],
        dtype=float,
    )
    dead_all = np.array(
        [
            np.searchsorted(se, event_times, side="right")
            - np.searchsorted(se, event_times, side="left")
            for se in sorted_ev
        ],
        dtype=float,
    )
    for i in range(len(event_times)):
        risk, dead = risk_all[:, i], dead_all[:, i]
        r, d = risk.sum(), dead.sum()
        observed += dead
        expected += d * risk / r
        if r > 1:
            frac = risk / r
            cov += d * (r - d) / (r - 1) * (np.diag(frac) - np.outer(frac, frac))
    caveats = [
        COMPETING,
        caveat("no_power_model", "secondary metric: treated as exploratory"),
    ]
    if dropped:
        caveats.append(
            caveat(
                "dropped_missing",
                f"{dropped} episodes without time or event",
                count=dropped,
            )
        )
    diff = (observed - expected)[: k - 1]
    sub = cov[: k - 1, : k - 1]
    if all_e.sum() == 0 or np.linalg.matrix_rank(sub) < k - 1:
        return result(
            "test",
            "logrank",
            MODULE,
            references=["mantel1966"],
            exploratory=True,
            caveats=caveats
            + [caveat("no_information", "no events or a degenerate arm")],
            available=False,
            arms=names,
            observed=observed,
            expected=expected,
            statistic=None,
            p_value=None,
        )
    stat = float(diff @ np.linalg.solve(sub, diff))
    return result(
        "test",
        "logrank",
        MODULE,
        references=["mantel1966"],
        exploratory=True,
        caveats=caveats,
        available=True,
        arms=names,
        observed=observed,
        expected=expected,
        statistic=stat,
        df=k - 1,
        p_value=_core.chi2_sf(stat, k - 1),
    )


def rmst_value(t: np.ndarray, e: np.ndarray, tau: float) -> float:
    """Area under the Kaplan-Meier step function on ``[0, tau]``."""
    times, _, _, surv, _ = km_arrays(t, e)
    keep = times < tau
    times, surv = times[keep], surv[keep]
    edges = np.concatenate([[0.0], times, [tau]])
    heights = np.concatenate([[1.0], surv])
    return float((heights * np.diff(edges)).sum())


def rmst(
    groups: dict,
    *,
    tau: float,
    seed: int,
    resamples: int = 2000,
    level: float = _core.DEFAULT_LEVEL,
    reference: str | None = None,
) -> dict:
    """RMST per arm up to ``tau`` and the difference of each arm from
    ``reference`` (default: the first arm) with a bootstrap interval.

    With "success" as the event, a *smaller* RMST means faster success."""
    level = _core.check_level(level)
    seed = _core.check_seed(seed)
    if not (isinstance(tau, (int, float)) and math.isfinite(tau) and tau > 0):
        raise AnalysisInputError("tau must be a positive finite horizon")
    names = list(groups)
    if not names:
        raise AnalysisInputError("no arms")
    reference = names[0] if reference is None else reference
    if reference not in groups:
        raise AnalysisInputError(f"unknown reference arm {reference!r}")
    resamples = min(_core.check_resamples(resamples), 20_000)
    data = {}
    for name in names:
        t, e, _ = _clean(*groups[name])
        if len(t) == 0:
            raise AnalysisInputError(f"arm {name!r} has no episodes")
        data[name] = (t, e)
    rng = _core.generator(seed)
    boots = {}
    for name in names:
        t, e = data[name]
        n = len(t)
        boots[name] = np.array(
            [
                rmst_value(t[idx], e[idx], tau)
                for idx in (rng.integers(0, n, n) for _ in range(resamples))
            ]
        )
    alpha = 1 - level
    arms = {
        name: {"rmst": rmst_value(*data[name], tau), "n": len(data[name][0])}
        for name in names
    }
    diffs = {}
    for name in names:
        if name == reference:
            continue
        reps = boots[name] - boots[reference]
        lo, hi = np.quantile(reps, [alpha / 2, 1 - alpha / 2])
        diffs[name] = {
            "difference": arms[name]["rmst"] - arms[reference]["rmst"],
            "low": float(lo),
            "high": float(hi),
        }
    return result(
        "estimate",
        "rmst",
        MODULE,
        references=["royston2013rmst", "kaplan1958", "efron1979bootstrap"],
        exploratory=True,
        caveats=[
            COMPETING,
            caveat("no_power_model", "secondary metric: treated as exploratory"),
        ],
        available=True,
        tau=float(tau),
        level=level,
        seed=seed,
        resamples=resamples,
        reference=reference,
        arms=arms,
        differences=diffs,
    )
