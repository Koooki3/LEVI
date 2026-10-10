"""The compatibility path to the live service (T-C-03): the online
judgement's answers (interface C5) mapped to ``levi.aeri.judgement.v1``,
and the AERI states written as the evaluation client's session states
(interface C2).

Nothing in ``levi.live`` is changed or monkeypatched; only its public
constants are read (``online.RESULT_SCHEMA``).

**C5 → judgement.** By ``outcome`` and ``undecided``, never by ``reading``
alone (an episode-level veto turns a ``supported`` reading into a failure):

| C5 ``status`` | condition | result |
| --- | --- | --- |
| ``ok`` | ``undecided`` true (whatever the outcome) | ``unknown`` / ``model_undecided`` |
| ``ok`` | ``outcome=success`` | ``confirmed`` |
| ``ok`` | ``outcome=failure`` | ``rejected`` |
| ``unavailable`` | reason ``gate_closed``/``gate_pending``/``busy``/``service_busy`` | ``Unavailable`` retryable: a degradation, not an error |
| ``unavailable`` | ``cold_start``/``vllm_starting``/``no_room``/``wake_failed``/``vllm_failed``/``shutting_down`` | ``Unavailable``, not retryable |
| ``error`` | ``timeout:`` | ``Unavailable(timeout)``: a timeout is never ``unknown`` |
| ``error`` | ``invalid_answer:``/``model_error:``/``internal_error:``/``invalid_request:`` | ``Unavailable(invalid_answer/model_error/provider_error/contract_violation)`` |
| anything else | | ``Unavailable(contract_violation)`` (fail closed) |

The online judgement reports a timeout as ``status=error``, not
``unavailable``: the reason's prefix decides, not the status.

**AERI state → C2 session state.** Only the client's seven states are ever
written (a new name would pass through the live reader unchanged and open
the GPU gate). A state in which a policy may be inferring is ``running``,
whatever else is true: the reset policy's active state included (written to
the reset role's file). Faking ``waiting_reset`` there would let the
background model run beside the reset policy.
"""

import re

from levi.domain import aeri

from ..state_machine import POLICY_MAY_INFER

LEGACY_STATES = (
    "standby",
    "homing",
    "running",
    "waiting_reset",
    "fault",
    "stopped",
    "finished",
)
LEGACY_OF = {
    "PREFLIGHT": "standby",
    "VERIFY_INITIAL": "waiting_reset",
    "FORWARD_ACTIVE": "running",
    "FORWARD_STOPPING": "running",
    "FORWARD_FINALIZE": "waiting_reset",
    "ROBOT_HOME": "homing",
    "SCENE_ASSESS": "waiting_reset",
    "RESET_ACTIVE": "running",
    "RESET_VERIFY": "waiting_reset",
    "RESET_FINALIZE": "homing",
    "WAIT_HUMAN": "waiting_reset",
    "FAULT_LOCKED": "fault",
    "COMPLETED": "finished",
}
# Whose session file is active in each state; the other says ``standby``.
ACTIVE_ROLE = {
    "RESET_ACTIVE": "reset",
    "RESET_VERIFY": "reset",
    "RESET_FINALIZE": "reset",
}
RETRYABLE = {
    "gate_closed": "gate_closed",
    "gate_pending": "gate_closed",
    "busy": "busy",
    "service_busy": "busy",
}
NOT_RETRYABLE = {
    "cold_start": "cold_start",
    "vllm_starting": "cold_start",
    "no_room": "no_room",
    "wake_failed": "provider_error",
    "vllm_failed": "provider_error",
    "shutting_down": "shutting_down",
}
ERRORS = {
    "timeout": "timeout",
    "invalid_answer": "invalid_answer",
    "model_error": "model_error",
    "internal_error": "provider_error",
    "invalid_request": "contract_violation",
}
_PREFIX = re.compile(r"^\s*([a-z_]+)\s*:")


def legacy_state(state: str, *, stopped: bool = False) -> str:
    """The C2 ``state`` of the active role's file in AERI ``state``
    (``stopped``: the run ended by an operator's stop)."""
    if state == "COMPLETED" and stopped:
        return "stopped"
    found = LEGACY_OF[state]
    assert found in LEGACY_STATES
    if state in POLICY_MAY_INFER:
        assert found == "running", state
    return found


def legacy_sessions(state: str, *, stopped: bool = False) -> dict:
    """``{"forward": c2_state, "reset": c2_state}`` for both role files."""
    active = ACTIVE_ROLE.get(state, "forward")
    value = legacy_state(state, stopped=stopped)
    other = "standby" if value not in ("fault", "finished", "stopped") else value
    return {active: value, ("reset" if active == "forward" else "forward"): other}


def reason_code(reason) -> str | None:
    """``"gate_closed: ..."`` -> ``"gate_closed"``."""
    if not isinstance(reason, str):
        return None
    found = _PREFIX.match(reason)
    return found.group(1) if found else None


def _unavailable(
    *, run_id, request_id, code, retryable, detail, produced_ns, clock_domain
):
    return aeri.validate(
        {
            "schema": aeri.SCHEMAS["judgement"],
            "minor": aeri.MINOR,
            "run_id": run_id,
            "emitted_wall_ns": 0,
            "kind": "unavailable",
            "request_id": request_id,
            "code": code,
            "retryable": retryable,
            "retry_after_ms": None,
            "detail": str(detail or "")[:300],
            "produced_ns": produced_ns,
            "clock_domain": clock_domain,
        },
        "judgement",
    )


def _predicates(answer: dict) -> list:
    """The final-state spec's two answer fields as required predicates
    (``unclear`` or missing: not read)."""
    state = answer.get("object_state")
    stable = answer.get("stable")
    return [
        {
            "name": "object_state",
            "value": None
            if state in (None, "unclear")
            else state == "resting_at_destination",
            "required": True,
            "evidence_refs": [],
        },
        {
            "name": "stable",
            "value": None if stable in (None, "unclear") else stable == "yes",
            "required": True,
            "evidence_refs": [],
        },
    ]


def from_c5(
    body: dict,
    *,
    run_id: str,
    request_id: str,
    episode_id: str,
    episode_role: str,
    target: str,
    observed_from_step: int,
    observed_through_step: int,
    observed_through_ns: int,
    produced_ns: int,
    clock_domain: str,
    valid_ms: int = 5_000,
):
    """An online judgement result (``levi.online.judge.result.v1``) as a
    validated ``Judgement`` or ``JudgementUnavailable``. Times are C's own
    (the online judgement reports none on the robot's clock)."""
    from levi.live import online

    common = {
        "run_id": run_id,
        "request_id": request_id,
        "produced_ns": produced_ns,
        "clock_domain": clock_domain,
    }
    if not isinstance(body, dict) or body.get("schema") != online.RESULT_SCHEMA:
        return _unavailable(
            code="contract_violation",
            retryable=False,
            detail="not an online judgement result",
            **common,
        )
    status = body.get("status")
    reason = body.get("reason")
    code = reason_code(reason)
    if status == "unavailable":
        if code in RETRYABLE:
            return _unavailable(
                code=RETRYABLE[code], retryable=True, detail=reason, **common
            )
        mapped = NOT_RETRYABLE.get(code, "provider_error")
        return _unavailable(code=mapped, retryable=False, detail=reason, **common)
    if status == "error":
        mapped = ERRORS.get(code, "provider_error")
        return _unavailable(code=mapped, retryable=False, detail=reason, **common)
    if status != "ok":
        return _unavailable(
            code="contract_violation",
            retryable=False,
            detail=f"status {status!r}",
            **common,
        )
    outcome = body.get("outcome")
    undecided = body.get("undecided")
    reading = body.get("reading")
    spec = body.get("spec") or {}
    if (
        outcome not in ("success", "failure")
        or not isinstance(undecided, bool)
        or reading not in ("supported", "contradicted", "unknown")
    ):
        return _unavailable(
            code="invalid_answer",
            retryable=False,
            detail="outcome, undecided or reading missing",
            **common,
        )
    if (str(spec.get("id")), str(spec.get("version"))) not in aeri.BUILTIN_SPECS:
        return _unavailable(
            code="not_supported",
            retryable=False,
            detail=f"spec {spec.get('id')}@{spec.get('version')}",
            **common,
        )
    answer = body.get("answer")
    predicates = _predicates(answer if isinstance(answer, dict) else {})
    decision, unknown_reason, vetoes = decide(outcome, undecided, predicates)
    model = body.get("model")
    try:
        elapsed_ms = max(0, int(float(body.get("elapsed_s") or 0) * 1000))
    except (TypeError, ValueError, OverflowError):
        elapsed_ms = 0
    message = {
        "schema": aeri.SCHEMAS["judgement"],
        "minor": aeri.MINOR,
        "run_id": run_id,
        "emitted_wall_ns": 0,
        "kind": "judgement",
        "judgement_id": f"c5-{request_id}",
        "request_id": request_id,
        "episode_id": episode_id,
        "episode_role": episode_role,
        "target": target,
        "subgoal": None,
        "event_ids": [],
        "decision": decision,
        "unknown_reason": unknown_reason,
        "predicate_results": predicates,
        "vetoes": vetoes,
        "observed_from_step": observed_from_step,
        "observed_through_step": observed_through_step,
        "observed_through_ns": observed_through_ns,
        "produced_ns": produced_ns,
        "valid_until_ns": produced_ns + valid_ms * 1_000_000,
        "clock_domain": clock_domain,
        "spec_id": str(spec.get("id")),
        "spec_version": str(spec.get("version")),
        "provider": "vlm",
        "model_name": str(model)[:200] if model else "unknown",
        "model_digest": None,
        "confidence_kind": "none",
        "calibrated_probability": None,
        "calibration_ref": None,
        "cost": {"elapsed_ms": min(elapsed_ms, aeri.INT64_MAX)},
        "legacy_c5": {"reading": reading, "outcome": outcome, "undecided": undecided},
    }
    try:
        # The contract re-checks the mapping against legacy_c5: a wrong
        # mapping is refused here, never let through without its audit copy.
        return aeri.validate(message, "judgement", specs=aeri.BUILTIN_SPECS)
    except aeri.AeriError as exc:
        return _unavailable(
            code="contract_violation",
            retryable=False,
            detail=f"mapping refused ({exc.code}): reading={reading}, "
            f"outcome={outcome}, undecided={undecided}",
            **common,
        )


def decide(outcome: str, undecided: bool, predicates: list) -> tuple:
    """``(decision, unknown_reason, vetoes)`` of a C5 answer: undecided
    first (whatever the outcome), then the outcome; a decided success whose
    answer fields do not both support it is never a confirmation."""
    if undecided:
        return "unknown", "model_undecided", []
    if outcome == "success":
        if not all(p["value"] is True for p in predicates):
            return "unknown", "conflicting_predicates", []
        return "confirmed", None, []
    if not any(p["value"] is False for p in predicates):
        # A failure with both fields fine is the rule's episode-level veto.
        return "rejected", None, [{"id": "c5-rule", "state": "confirmed"}]
    return "rejected", None, []
