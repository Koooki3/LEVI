"""The pre-registered "LEVI 2.0 is better" criteria of the three-arm experiment.

The experiment compares arm A (``direct``), arm B (``prior_levi``) and arm C
(``levi2``); the teacher is a reference, not a party to the claim. The rules
are written down before any result (EXPERIMENT-3ARM-PLAN §4) and coded here so
they are applied, not re-read:

- **C against B is "better"** when quality is non-inferior (the lower end of the
  paired difference interval is at least ``-margin``), at least one cost metric
  is significantly better (its interval of C minus B lies below 0), and no cost
  metric is significantly worse (its interval lies above 0). External tokens
  are the exception: escalating to the teacher always adds some, so they are
  not counted as worse if the caller states that C's external tokens per unit
  of quality stay below the teacher arm's.
- **C against A is "better"** when the lower end of the quality interval is
  above 0, or when quality is non-inferior and cost is lower (same rule as
  above) and reliability (legal-output rate, calibration) is better.
- **Overall**: C is better in a majority of the task families against both A
  and B, and in no family is its quality significantly worse than any arm.

An interval too wide to decide is ``undetermined`` (未定), never "better": with
six or seven confirm problems that will often be the honest answer. A missing
interval is also undetermined, not a quiet "no" or "yes". A verdict of "not
better" is only given when the intervals that exist already decide it.

The functions take intervals of paired differences ``C - other`` (computed by
whoever can see the problems one by one, as for ``quality_delta_ci_vs_teacher``)
and return a ``Verdict``. They read no record, no dataset and no model.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from .compare import _non_inferior

BETTER = "better"
NOT_BETTER = "not_better"
UNDETERMINED = "undetermined"
VERDICTS = (BETTER, NOT_BETTER, UNDETERMINED)
ACHIEVED = "achieved"
PARTIAL = "partial"

# A cost or reliability metric against one arm.
_BETTER, _WORSE, _NEITHER, _UNKNOWN = "better", "worse", "neither", "unknown"


@dataclass(frozen=True)
class Verdict:
    verdict: str
    reasons: tuple[str, ...]


def _interval(value, name):
    """``(low, high)`` or ``None``; anything else is a caller's mistake."""
    if value is None:
        return None
    try:
        low, high = value
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be [low, high] or None") from None
    for end in (low, high):
        if isinstance(end, bool) or not isinstance(end, (int, float)):
            raise TypeError(f"{name} must hold numbers")
        if not math.isfinite(end):
            raise ValueError(f"{name} must be finite")
    if low > high:
        raise ValueError(f"{name} must have low <= high")
    return float(low), float(high)


def _cost_state(interval) -> str:
    """Cost of C minus the other arm, smaller is better: significantly better
    if the whole interval is below 0, worse if it is above 0."""
    if interval is None:
        return _UNKNOWN
    low, high = interval
    if high < 0:
        return _BETTER
    if low > 0:
        return _WORSE
    return _NEITHER


def _reliability_state(interval) -> str:
    """Reliability of C minus the other arm, larger is better."""
    if interval is None:
        return _UNKNOWN
    low, high = interval
    if low > 0:
        return _BETTER
    if high < 0:
        return _WORSE
    return _NEITHER


def _cost_states(
    costs: Mapping, external_costs, external_ok: bool | None
) -> dict[str, str]:
    states = {}
    for name, raw in costs.items():
        state = _cost_state(_interval(raw, f"costs[{name}]"))
        if name in external_costs and state == _WORSE:
            # Escalation adds external tokens by design; it is a cost only
            # if the efficiency condition against the teacher arm fails.
            state = {True: _NEITHER, False: _WORSE, None: _UNKNOWN}[external_ok]
        states[name] = state
    return states


def _cost_summary(states: Mapping[str, str]):
    """(better names, worse names, unknown names)."""

    def pick(want):
        return sorted(n for n, s in states.items() if s == want)

    return pick(_BETTER), pick(_WORSE), pick(_UNKNOWN)


def _all_clear(any_metric: bool, worse, unknown, better) -> bool | None:
    """Is there a gain on the metrics and no loss? ``True`` if something is
    significantly better and nothing worse; ``False`` if something is worse, or
    every interval exists and none is better; ``None`` if not decidable (no
    metric at all, or a missing interval that could still change the answer)."""
    if not any_metric:
        return None
    if worse:
        return False
    if unknown:
        return None
    return bool(better)


def better_than_prior_levi(
    quality_delta_ci,
    margin: float,
    costs: Mapping[str, object],
    *,
    higher_is_better: bool = True,
    external_costs: tuple[str, ...] = ("external_tokens",),
    external_efficiency_ok: bool | None = None,
) -> Verdict:
    """Is C better than B? ``quality_delta_ci`` is the interval of C - B in
    quality; ``costs`` maps a cost metric (smaller is better) to the interval of
    C - B (``None`` = not available). ``external_efficiency_ok`` says whether
    C's external tokens per unit of quality are below the teacher arm's: it
    decides how the metrics named in ``external_costs`` count when they are
    significantly higher than B's (True: not a cost; False: a cost; None:
    unknown)."""
    if not isinstance(margin, (int, float)) or isinstance(margin, bool) or margin < 0:
        raise ValueError("margin must be a number >= 0")
    quality = _non_inferior(
        _interval(quality_delta_ci, "quality_delta_ci"), higher_is_better, margin
    )
    states = _cost_states(costs, external_costs, external_efficiency_ok)
    better, worse, unknown = _cost_summary(states)
    reasons = [f"quality non-inferior: {quality}"]
    reasons += [f"cost better: {n}" for n in better]
    reasons += [f"cost worse: {n}" for n in worse]
    reasons += [f"cost unknown: {n}" for n in unknown]
    if quality == "no" or worse:
        return Verdict(NOT_BETTER, tuple(reasons))
    if quality == "yes" and not unknown and better:
        return Verdict(BETTER, tuple(reasons))
    if quality == "yes" and not unknown and costs:
        return Verdict(NOT_BETTER, (*reasons, "no cost is significantly better"))
    return Verdict(UNDETERMINED, tuple(reasons))


def better_than_direct(
    quality_delta_ci,
    margin: float,
    costs: Mapping[str, object],
    reliability: Mapping[str, object],
    *,
    higher_is_better: bool = True,
    external_costs: tuple[str, ...] = ("external_tokens",),
    external_efficiency_ok: bool | None = None,
) -> Verdict:
    """Is C better than A? ``quality_delta_ci`` is the interval of C - A;
    ``costs`` as in ``better_than_prior_levi``; ``reliability`` maps a metric
    where larger is better (legal-output rate, calibration) to the interval of
    C - A."""
    if not isinstance(margin, (int, float)) or isinstance(margin, bool) or margin < 0:
        raise ValueError("margin must be a number >= 0")
    interval = _interval(quality_delta_ci, "quality_delta_ci")
    if interval is None:
        wins = None
    else:
        low, high = interval if higher_is_better else (-interval[1], -interval[0])
        wins = True if low > 0 else False if high <= 0 else None
    quality = _non_inferior(interval, higher_is_better, margin)
    states = _cost_states(costs, external_costs, external_efficiency_ok)
    better, worse, unknown = _cost_summary(states)
    cost_lower = _all_clear(bool(costs), worse, unknown, better)
    rel = {
        n: _reliability_state(_interval(v, f"reliability[{n}]"))
        for n, v in reliability.items()
    }
    reliable = _all_clear(
        bool(rel),
        sorted(n for n, s in rel.items() if s == _WORSE),
        sorted(n for n, s in rel.items() if s == _UNKNOWN),
        sorted(n for n, s in rel.items() if s == _BETTER),
    )
    second = {"yes": True, "no": False}.get(quality)  # None = undetermined/unknown
    parts = [second, cost_lower, reliable]
    second_path = False if False in parts else None if None in parts else True
    reasons = [
        f"quality gain (lower end > 0): {wins}",
        f"quality non-inferior: {quality}",
        f"cost lower: {cost_lower}",
        f"reliability better: {reliable}",
    ]
    if wins is True or second_path is True:
        return Verdict(BETTER, tuple(reasons))
    if wins is False and second_path is False:
        return Verdict(NOT_BETTER, tuple(reasons))
    return Verdict(UNDETERMINED, tuple(reasons))


def quality_significantly_worse(quality_delta_ci, higher_is_better: bool = True):
    """Is C's quality significantly worse than another arm's (interval of C -
    other entirely on the worse side of 0)? ``None`` if there is no interval."""
    interval = _interval(quality_delta_ci, "quality_delta_ci")
    if interval is None:
        return None
    worse_end = interval[1] if higher_is_better else -interval[0]
    return worse_end < 0


def any_quality_worse(quality_delta_cis, higher_is_better: bool = True):
    """``Family.quality_worse_than_any`` from the intervals of C - each arm
    (A, B and, if present, T): ``True`` if any is significantly worse, ``None``
    if none is but some interval is missing, else ``False``."""
    answers = [
        quality_significantly_worse(c, higher_is_better) for c in quality_delta_cis
    ]
    return (
        True if True in answers else None if None in answers or not answers else False
    )


@dataclass(frozen=True)
class Family:
    """One task family's verdicts: C against A and against B, and whether C's
    quality is significantly worse than any arm (``True``/``False``/``None``
    unknown; any arm that is ``True`` makes it ``True``)."""

    vs_direct: Verdict
    vs_prior_levi: Verdict
    quality_worse_than_any: bool | None


def _family_state(family: Family) -> str:
    pair = (family.vs_direct.verdict, family.vs_prior_levi.verdict)
    if NOT_BETTER in pair:
        return NOT_BETTER
    return BETTER if pair == (BETTER, BETTER) else UNDETERMINED


def overall(families: Mapping[str, Family]) -> Verdict:
    """The summary claim. ``achieved`` needs a majority (more than half) of the
    families better than both A and B and no family whose quality is
    significantly worse than any arm; ``partial`` when that is decided against
    (a family is worse, or a majority cannot be reached); ``undetermined`` when
    intervals too wide or missing still leave either outcome possible."""
    if not families:
        return Verdict(UNDETERMINED, ("no task family",))
    for name, family in families.items():
        for verdict in (family.vs_direct, family.vs_prior_levi):
            if verdict.verdict not in VERDICTS:
                raise ValueError(f"family {name}: unknown verdict")
    total = len(families)
    states = {name: _family_state(f) for name, f in families.items()}
    better = sorted(n for n, s in states.items() if s == BETTER)
    open_ = sorted(n for n, s in states.items() if s == UNDETERMINED)
    worse = sorted(n for n, f in families.items() if f.quality_worse_than_any is True)
    unknown_worse = sorted(
        n for n, f in families.items() if f.quality_worse_than_any is None
    )
    reasons = [
        f"better than both A and B: {better} of {total}",
        f"undetermined: {open_}",
        f"quality significantly worse than an arm: {worse}",
        f"quality worse-ness not known: {unknown_worse}",
    ]
    if worse:
        return Verdict(PARTIAL, tuple(reasons))
    if 2 * len(better) > total and not unknown_worse:
        return Verdict(ACHIEVED, tuple(reasons))
    if 2 * (len(better) + len(open_)) > total:
        return Verdict(UNDETERMINED, tuple(reasons))
    return Verdict(PARTIAL, tuple(reasons))
