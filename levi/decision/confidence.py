"""Confidence from an injected logprobs structure. No model call, no service.

Closed-field answers (boolean, enum, option letter): locate the token that starts the
field's value in the strict-JSON output, take its top alternatives, keep the ones that
begin an allowed value, and normalise. Anything that cannot be located or does not add
up is *unknown*, which the router escalates. A ``confidence`` field the model wrote about
itself is never read.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

from .contracts import Source, TypedAnswer

SUM_TOLERANCE = 1e-6


@dataclass(frozen=True)
class TokenLogprob:
    """One generated token: its text and the top alternatives {token: logprob}."""

    token: str
    top: Mapping[str, float]


@dataclass(frozen=True)
class ClosedFieldConfidence:
    value: str  # the allowed value with the largest probability
    probs: Mapping[str, float]  # renormalised over the allowed values
    p_raw: float  # probs[value]
    out_of_set_mass: (
        float  # probability mass on alternatives that start no allowed value
    )


def _value_offset(content: str, field: str) -> int | None:
    marker = f'"{field}"'
    if content.count(marker) != 1:
        return None
    i = content.index(marker) + len(marker)
    while i < len(content) and content[i] in " \t\r\n":
        i += 1
    if i >= len(content) or content[i] != ":":
        return None
    i += 1
    while i < len(content) and content[i] in " \t\r\n":
        i += 1
    return i


def _starts(token: str, allowed: str) -> bool:
    t = token.strip().strip('"')
    return bool(t) and allowed.startswith(t)


def closed_field(
    content: str, tokens: Sequence[TokenLogprob], field: str, allowed: Iterable[str]
) -> ClosedFieldConfidence | None:
    """Probability of each allowed value of ``field``, or None when it is unknown.

    None covers: no logprobs, tokens that do not concatenate to ``content``, a field
    that is missing or repeated, no token at the value start, an alternative that is a
    prefix of two allowed values, no allowed value seen, or probabilities above 1.
    """
    allowed = tuple(allowed)
    if not tokens or not allowed or len(set(allowed)) != len(allowed):
        return None
    if "".join(t.token for t in tokens) != content:
        return None
    start = _value_offset(content, field)
    if start is None:
        return None
    pos = 0
    chosen = None
    for t in tokens:
        end = pos + len(t.token)
        if end > start:
            chosen = t
            break
        pos = end
    if chosen is None or not chosen.top:
        return None
    # The first token of the value may begin with the opening quote of a string value.
    mass = dict.fromkeys(allowed, 0.0)
    seen: set[str] = set()
    for alt, lp in chosen.top.items():
        if not isinstance(lp, (int, float)) or math.isnan(lp) or lp > 0:
            return None
        if alt in seen:
            return None
        seen.add(alt)
        hits = [a for a in allowed if _starts(alt, a)]
        if len(hits) > 1:
            return None
        if hits:
            mass[hits[0]] += math.exp(lp)
    total_in = sum(mass.values())
    if total_in <= 0.0 or total_in > 1.0 + SUM_TOLERANCE:
        return None
    probs = {a: m / total_in for a, m in mass.items()}
    best = max(allowed, key=lambda a: (probs[a], -allowed.index(a)))
    return ClosedFieldConfidence(
        value=best,
        probs=probs,
        p_raw=probs[best],
        out_of_set_mass=max(0.0, 1.0 - min(total_in, 1.0)),
    )


def typed_answer(
    conf: ClosedFieldConfidence | None,
    *,
    abstain_values: Iterable[str] = ("null",),
    vetoes: Iterable[str] = (),
    out_of_distribution: bool = False,
) -> TypedAnswer:
    """Wrap a closed-field result for the router.

    Unknown confidence stays None; a winning value in ``abstain_values`` is an abstention.
    """
    if conf is None:
        return TypedAnswer(None, None, None, None, tuple(vetoes), out_of_distribution)
    return TypedAnswer(
        None if conf.value in tuple(abstain_values) else conf.value,
        conf.p_raw,
        Source.LOGPROBS,
        conf.out_of_set_mass,
        tuple(vetoes),
        out_of_distribution,
    )


def sample_agreement(samples: Sequence[object]) -> tuple[object, float] | None:
    """Open structures: the modal canonical sample and its share, or None for no samples.

    Callers pass already-canonicalised, hashable samples (their own equality rule).
    """
    if not samples:
        return None
    value, n = Counter(samples).most_common(1)[0]
    return value, n / len(samples)


# A veto check gets the caller's signal context and returns a reason, or None for "no
# contradiction". The rules belong to the caller (gripper closed yet "released", ...).
VetoCheck = Callable[[object], str | None]


def run_vetoes(checks: Iterable[VetoCheck], context: object) -> tuple[str, ...]:
    """Reasons from every check. A check that raises counts as a veto (fail closed)."""
    out = []
    for check in checks:
        try:
            reason = check(context)
        except Exception as exc:  # noqa: BLE001 - any failure of a safety check is a veto
            reason = f"veto_check_error:{type(exc).__name__}"
        if reason:
            out.append(str(reason))
    return tuple(out)
