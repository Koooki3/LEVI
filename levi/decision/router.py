"""Route one local answer: person, external panel or local. Pure and ordered.

Order (the first rule that holds decides):

1. physical safety (static table; unlisted kind is physical) -> human
2. confidence unknown, abstention, out-of-set mass over the limit, physical veto,
   out-of-distribution flag -> escalate
3. calibrated p >= tau -> local (label ``automatic``); otherwise -> escalate
   (label ``agent-reviewed`` once the panel answers)

External unavailable: the item waits (``pending_external``). It gets no label, no
local fallback and no person.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import safety
from .calibration import ESC_ALL, Calibration, RunKey, apply_temperature
from .contracts import (
    EscalationRequest,
    EscalationResponse,
    LabelTier,
    Reason,
    Route,
    RouteDecision,
    Source,
    State,
    TypedAnswer,
)

DEFAULT_MAX_OUT_OF_SET_MASS = 0.05


def route(
    answer: TypedAnswer,
    kind: str,
    context,
    calibration: Calibration,
    run_key: RunKey,
    *,
    max_out_of_set_mass: float = DEFAULT_MAX_OUT_OF_SET_MASS,
) -> RouteDecision:
    """Decide who answers. Refuses (``KeyMismatch``) if the calibration is not for this run."""
    calibration.require_key(run_key)
    if safety.is_physical(kind, context):
        return RouteDecision(Route.HUMAN, Reason.PHYSICAL_SAFETY, None)
    if answer.p_raw is None or answer.source is None:
        return RouteDecision(
            Route.ESCALATE, Reason.CONFIDENCE_UNKNOWN, LabelTier.AGENT_REVIEWED
        )
    if answer.value is None:
        return RouteDecision(Route.ESCALATE, Reason.ABSTAINED, LabelTier.AGENT_REVIEWED)
    if answer.source is Source.LOGPROBS and answer.out_of_set_mass is None:
        return RouteDecision(
            Route.ESCALATE, Reason.CONFIDENCE_UNKNOWN, LabelTier.AGENT_REVIEWED
        )
    if (
        answer.out_of_set_mass is not None
        and answer.out_of_set_mass > max_out_of_set_mass
    ):
        return RouteDecision(
            Route.ESCALATE, Reason.OUT_OF_SET_MASS, LabelTier.AGENT_REVIEWED
        )
    if answer.vetoes:
        return RouteDecision(
            Route.ESCALATE, Reason.PHYSICAL_VETO, LabelTier.AGENT_REVIEWED
        )
    if answer.out_of_distribution:
        return RouteDecision(
            Route.ESCALATE, Reason.OUT_OF_DISTRIBUTION, LabelTier.AGENT_REVIEWED
        )
    if calibration.tau == ESC_ALL:
        return RouteDecision(
            Route.ESCALATE, Reason.ESCALATE_ALL, LabelTier.AGENT_REVIEWED
        )
    p_cal = apply_temperature(answer.p_raw, calibration.temperature)
    if p_cal >= calibration.tau:
        return RouteDecision(
            Route.LOCAL, Reason.AT_OR_ABOVE_THRESHOLD, LabelTier.AUTOMATIC, p_cal
        )
    return RouteDecision(
        Route.ESCALATE, Reason.BELOW_THRESHOLD, LabelTier.AGENT_REVIEWED, p_cal
    )


@dataclass(frozen=True)
class Outcome:
    state: State
    label_tier: LabelTier | None
    answer: object | None


def finalize(
    decision: RouteDecision,
    local_value: object,
    request: EscalationRequest | None = None,
    response: EscalationResponse | None = None,
) -> Outcome:
    """What is written for the item. Only a valid panel answer or a local route yields a label."""
    if decision.route is Route.HUMAN:
        return Outcome(State.WITH_HUMAN, None, None)
    if decision.route is Route.LOCAL:
        return Outcome(State.ANSWERED_LOCAL, LabelTier.AUTOMATIC, local_value)
    # Escalated: the local value is never used from here on.
    if request is None or response is None or response.problems(request):
        return Outcome(State.PENDING_EXTERNAL, None, None)
    return Outcome(State.ANSWERED_EXTERNAL, LabelTier.AGENT_REVIEWED, response.answer)


__all__ = ["DEFAULT_MAX_OUT_OF_SET_MASS", "Outcome", "finalize", "route"]
