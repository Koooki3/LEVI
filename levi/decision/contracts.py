"""Typed records of the decision layer (``levi.decision.*.v1``).

Unknown is ``None``, never 0. There is no self-reported confidence source: the
router never branches on a number the model wrote about itself.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

REQUEST_CONTRACT = "levi.decision.escalation_request.v1"
RESPONSE_CONTRACT = "levi.decision.escalation_response.v1"
CALIBRATION_CONTRACT = "levi.decision.calibration.v1"
ROUTE_CONTRACT = "levi.decision.route.v1"
PANEL_PROTOCOL = "two_reviewers_one_adjudicator.v1"
TOKEN_FIELDS = ("input_new", "output", "cache_read", "cache_write")


class Context(str, Enum):
    OFFLINE = "offline"
    LIVE = "live"


class Safety(str, Enum):
    PHYSICAL = "physical"
    NON_PHYSICAL = "non_physical"


class Route(str, Enum):
    LOCAL = "local"
    ESCALATE = "escalate"
    HUMAN = "human"


class Reason(str, Enum):
    PHYSICAL_SAFETY = "physical_safety"
    CONFIDENCE_UNKNOWN = "confidence_unknown"
    ABSTAINED = "abstained"
    OUT_OF_SET_MASS = "out_of_set_mass"
    PHYSICAL_VETO = "physical_veto"
    OUT_OF_DISTRIBUTION = "out_of_distribution"
    ESCALATE_ALL = "escalate_all"
    BELOW_THRESHOLD = "below_threshold"
    AT_OR_ABOVE_THRESHOLD = "at_or_above_threshold"


class Source(str, Enum):
    """Where a confidence came from. Self-report is deliberately absent."""

    LOGPROBS = "logprobs"
    SAMPLE_AGREEMENT = "sample_agreement"
    SIGNAL_CHECK = "signal_check"


class LabelTier(str, Enum):
    """Only two tiers exist; a human tier is never written by this layer."""

    AUTOMATIC = "automatic"
    AGENT_REVIEWED = "agent-reviewed"


class State(str, Enum):
    ANSWERED_LOCAL = "answered_local"
    ANSWERED_EXTERNAL = "answered_external"
    PENDING_EXTERNAL = "pending_external"
    WITH_HUMAN = "with_human"


@dataclass(frozen=True)
class TypedAnswer:
    """One local answer with the evidence the router may use.

    ``value`` None is an abstention. ``p_raw`` None is unknown confidence.
    """

    value: object
    p_raw: float | None
    source: Source | None = None
    out_of_set_mass: float | None = None
    vetoes: tuple[str, ...] = ()
    out_of_distribution: bool = False

    def __post_init__(self):
        if self.p_raw is not None and not (
            isinstance(self.p_raw, (int, float))
            and math.isfinite(self.p_raw)
            and 0.0 <= self.p_raw <= 1.0
        ):
            raise ValueError("p_raw must be a finite number in [0, 1] or None")
        if self.out_of_set_mass is not None and not (
            math.isfinite(self.out_of_set_mass) and 0.0 <= self.out_of_set_mass <= 1.0
        ):
            raise ValueError("out_of_set_mass must be in [0, 1] or None")
        if self.source is not None and not isinstance(self.source, Source):
            raise ValueError("source must be a Source")


@dataclass(frozen=True)
class RouteDecision:
    route: Route
    reason: Reason
    label_tier: (
        LabelTier | None
    )  # LOCAL -> automatic; ESCALATE -> agent-reviewed once answered
    p_cal: float | None = None
    contract: str = ROUTE_CONTRACT


@dataclass(frozen=True)
class EscalationRequest:
    """What the external panel may see. Local answer and confidence are absent on purpose."""

    request_id: str
    question_kind: str
    spec_id: str
    messages: tuple
    response_schema: dict
    signals: dict = field(default_factory=dict)
    contract: str = REQUEST_CONTRACT
    panel_protocol: str = PANEL_PROTOCOL


@dataclass(frozen=True)
class TokenRow:
    role: str
    input_new: int | None
    output: int | None
    cache_read: int | None
    cache_write: int | None
    exact: bool

    def __post_init__(self):
        for name in TOKEN_FIELDS:
            v = getattr(self, name)
            if v is not None and (
                not isinstance(v, int) or isinstance(v, bool) or v < 0
            ):
                raise ValueError(
                    f"{name} must be a non-negative int or None (unknown is not 0)"
                )


@dataclass(frozen=True)
class EscalationResponse:
    request_id: str
    answer: object
    reviewer_a: object
    reviewer_b: object
    adjudicator: object | None = None
    tokens: tuple[TokenRow, ...] = ()
    label_tier: LabelTier = LabelTier.AGENT_REVIEWED
    contract: str = RESPONSE_CONTRACT

    def problems(self, request: EscalationRequest) -> list[str]:
        """Why this response is not an answer to ``request`` (empty when it is)."""
        out = []
        if self.contract != RESPONSE_CONTRACT:
            out.append("contract")
        if self.request_id != request.request_id:
            out.append("request_id")
        if self.label_tier is not LabelTier.AGENT_REVIEWED:
            out.append("label_tier")
        if self.reviewer_a != self.reviewer_b and self.adjudicator is None:
            out.append(
                "adjudication_missing"
            )  # reviewers disagree: an adjudicator is required
        return out
