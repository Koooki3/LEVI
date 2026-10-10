"""The AERI run's state machine: which of the 13 states may follow which,
for what reason, on whose authority and with which action; and the motion
fence that lets a robot move only under the current token.

Pure rules, no I/O: the orchestrator (``orchestrator.py``) asks
``check_transition`` before it prepares a transaction in the journal
(``journal.py``), and ``check_journal`` re-reads a whole journal against the
same table (used by the tests, and by anyone auditing a run).

The table (``TRANSITIONS``) follows the design's state list (X1, pipeline
§4.2): the normal path

    PREFLIGHT -> VERIFY_INITIAL -> FORWARD_ACTIVE -> FORWARD_STOPPING
    -> FORWARD_FINALIZE -> ROBOT_HOME -> SCENE_ASSESS -> FORWARD_ACTIVE ...
    SCENE_ASSESS / VERIFY_INITIAL -> RESET_ACTIVE -> RESET_VERIFY
    -> RESET_FINALIZE -> VERIFY_INITIAL (home first, then a fresh check)
    ... -> COMPLETED

plus, from every state that is not ``COMPLETED`` or ``FAULT_LOCKED``, a move
to ``FAULT_LOCKED`` (orchestrator, safety guard or recovery). ``WAIT_HUMAN``
and ``FAULT_LOCKED`` are left only by an operator's command (``command_id``
set) to ``PREFLIGHT``, never straight back to where the run was.

Every transition names one action. ``policy_steps`` (entering an active
state: the episode's steps are authorised once) and ``home`` are physical
and not idempotent: they must say ``non_idempotent`` and carry a ``step``
the orchestrator assigns (the journal refuses an idempotency key it has
seen, in any epoch, after any recovery).
"""

import threading
from dataclasses import dataclass

from levi.domain import aeri

STATES = aeri.AERI_STATES
TERMINAL = frozenset({"COMPLETED"})
HUMAN_STATES = frozenset({"WAIT_HUMAN", "FAULT_LOCKED"})
# States in which the robot moves under a policy-steps token.
MOTION_STATES = frozenset({"FORWARD_ACTIVE", "RESET_ACTIVE"})
# States in which a policy may still be inferring (a chunk may be in flight
# until the quiesce is acknowledged): the GPU gate must be closed.
POLICY_MAY_INFER = frozenset({"FORWARD_ACTIVE", "FORWARD_STOPPING", "RESET_ACTIVE"})
# Physical actions the FR3 server cannot deduplicate.
NON_IDEMPOTENT_KINDS = frozenset({"policy_steps", "home"})
# Which role each active state runs.
ROLE_OF = {"FORWARD_ACTIVE": "forward", "RESET_ACTIVE": "reset"}

FORWARD_STOPS = frozenset(
    {
        "goal_verified",
        "horizon_exhausted",
        "operator_stop",
        "policy_error",
        "watchdog_timeout",
    }
)
RESET_STOPS = frozenset(
    {
        "goal_verified",
        "reset_horizon_exhausted",
        "operator_stop",
        "policy_error",
        "watchdog_timeout",
    }
)
RESET_OUTCOMES = frozenset(
    {
        "reset_verified",
        "scene_reset_required",
        "scene_unknown",
        "reset_horizon_exhausted",
        "operator_stop",
        "policy_error",
        "watchdog_timeout",
    }
)
TO_HUMAN = frozenset(
    {
        "scene_reset_required",
        "scene_unknown",
        "preflight_failed",
        "policy_error",
        "contract_violation_limit",
        "operator_stop",
        "reset_horizon_exhausted",
    }
)
FAULT_REASONS = {
    "orchestrator": frozenset(
        {
            "safety_stop",
            "recorder_failed",
            "home_failed",
            "policy_error",
            "watchdog_timeout",
            "preflight_failed",
        }
    ),
    "safety_guard": frozenset({"safety_stop", "watchdog_timeout"}),
    "recovery": frozenset({"recovery_ambiguous"}),
}
# Transitions whose committed line carries the episode result; besides
# these, a move from an episode state to FAULT_LOCKED may carry one (the
# episode ended by a fault, task_outcome unknown).
RESULT_TRANSITIONS = frozenset(
    {
        ("FORWARD_FINALIZE", "ROBOT_HOME"),
        ("RESET_FINALIZE", "VERIFY_INITIAL"),
        ("RESET_FINALIZE", "WAIT_HUMAN"),
    }
)

ORCH = frozenset({"orchestrator"})
ORCH_OR_OPERATOR = frozenset({"orchestrator", "operator"})
OPERATOR = frozenset({"operator"})


@dataclass(frozen=True)
class Rule:
    reasons: frozenset
    principals: frozenset
    actions: frozenset
    command: bool = False  # the operator's command_id is required
    role: str | None = None  # the episode role the transition must name


def _r(reasons, principals, actions, **kw) -> Rule:
    return Rule(frozenset(reasons), frozenset(principals), frozenset(actions), **kw)


def _table() -> dict:
    t: dict[tuple[str, str], list[Rule]] = {}

    def add(a, b, rule):
        t.setdefault((a, b), []).append(rule)

    quiet = ("none", "notify")
    add("PREFLIGHT", "VERIFY_INITIAL", _r({"preflight_passed"}, ORCH, {"none"}))
    for start in ("VERIFY_INITIAL", "SCENE_ASSESS"):
        add(
            start,
            "FORWARD_ACTIVE",
            _r({"scene_ready"}, ORCH, {"policy_steps"}, role="forward"),
        )
        add(
            start,
            "RESET_ACTIVE",
            _r(
                {"scene_reset_required", "scene_unknown"},
                ORCH,
                {"policy_steps"},
                role="reset",
            ),
        )
        add(start, "COMPLETED", _r({"run_completed"}, ORCH, {"none"}))
    for start in ("PREFLIGHT", "VERIFY_INITIAL", "SCENE_ASSESS"):
        add(start, "WAIT_HUMAN", _r(TO_HUMAN, ORCH_OR_OPERATOR, quiet))
    add(
        "FORWARD_ACTIVE",
        "FORWARD_STOPPING",
        _r(FORWARD_STOPS, ORCH_OR_OPERATOR, {"hold"}, role="forward"),
    )
    add(
        "FORWARD_STOPPING",
        "FORWARD_FINALIZE",
        _r(FORWARD_STOPS, ORCH_OR_OPERATOR, {"policy_quiesce"}, role="forward"),
    )
    add(
        "FORWARD_FINALIZE",
        "ROBOT_HOME",
        # recorder_abort: an episode that ran no step keeps no recording.
        _r(
            FORWARD_STOPS,
            ORCH_OR_OPERATOR,
            {"recorder_seal", "recorder_abort"},
            role="forward",
        ),
    )
    # The home motion is this transaction's side effect.
    add(
        "ROBOT_HOME",
        "SCENE_ASSESS",
        _r({"robot_home_reached"}, ORCH_OR_OPERATOR, {"home"}, role="forward"),
    )
    # After an operator's stop, a person may take over where the arm stands
    # (configured, or when the episode ran no step).
    add(
        "ROBOT_HOME",
        "WAIT_HUMAN",
        _r({"operator_stop"}, OPERATOR, {"none"}, command=True),
    )
    add(
        "RESET_ACTIVE",
        "RESET_VERIFY",
        _r(RESET_STOPS, ORCH_OR_OPERATOR, {"policy_quiesce"}, role="reset"),
    )
    add(
        "RESET_VERIFY",
        "RESET_FINALIZE",
        _r(
            RESET_OUTCOMES,
            ORCH_OR_OPERATOR,
            {"recorder_seal", "recorder_abort"},
            role="reset",
        ),
    )
    add(
        "RESET_FINALIZE",
        "VERIFY_INITIAL",
        _r(
            {"reset_verified", "scene_reset_required", "scene_unknown"},
            ORCH,
            {"home"},
            role="reset",
        ),
    )
    add(
        "RESET_FINALIZE",
        "WAIT_HUMAN",
        _r(
            RESET_OUTCOMES - {"reset_verified"},
            ORCH_OR_OPERATOR,
            {"home"},
            role="reset",
        ),
    )
    # After an operator's stop, a person takes over without the reset's home
    # (the episode ran no step, or staying put is configured).
    add(
        "RESET_FINALIZE",
        "WAIT_HUMAN",
        _r({"operator_stop"}, OPERATOR, {"none"}, command=True, role="reset"),
    )
    for start in ("WAIT_HUMAN", "FAULT_LOCKED"):
        add(start, "PREFLIGHT", _r({"human_resumed"}, OPERATOR, {"none"}, command=True))
    add(
        "WAIT_HUMAN",
        "COMPLETED",
        _r({"operator_stop"}, OPERATOR, {"none"}, command=True),
    )
    add(
        "FAULT_LOCKED",
        "FAULT_LOCKED",
        _r({"recovery_ambiguous", "journal_corrupt"}, {"recovery"}, {"none"}),
    )
    for start in STATES:
        if start in TERMINAL or start == "FAULT_LOCKED":
            continue
        for principal, reasons in FAULT_REASONS.items():
            actions = {"none"} if principal == "recovery" else {"none", "hold"}
            add(start, "FAULT_LOCKED", _r(reasons, {principal}, actions))
    return t


TRANSITIONS = _table()


class TransitionRefused(Exception):
    """``code``: E_ILLEGAL (no such move), E_REASON, E_AUTHORITY, E_ACTION,
    E_STEP, E_EPISODE, E_RESULT."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def legal(from_state: str, to_state: str) -> bool:
    return (from_state, to_state) in TRANSITIONS


def successors(state: str) -> set:
    return {b for (a, b) in TRANSITIONS if a == state}


def check_transition(
    from_state: str,
    to_state: str,
    *,
    reason: str,
    principal_kind: str,
    command_id: str | None,
    action_kind: str,
    non_idempotent: bool,
    step: int | None,
    episode_role: str | None = None,
    episode_result: bool | None = None,
) -> Rule:
    """The rule that allows this move, or ``TransitionRefused``.
    ``episode_result``: None skips that check (a prepared line has no
    result yet); True/False says whether the commit carries one."""
    rules = TRANSITIONS.get((from_state, to_state))
    if not rules:
        raise TransitionRefused("E_ILLEGAL", f"{from_state} -> {to_state}")
    if action_kind in NON_IDEMPOTENT_KINDS:
        if not non_idempotent:
            raise TransitionRefused("E_ACTION", f"{action_kind} is not idempotent")
        if step is None:
            raise TransitionRefused(
                "E_STEP",
                f"a {action_kind} action needs a step the orchestrator assigns",
            )
    failures = []
    # A fault ends an episode that may not have its result yet: optional there.
    optional = to_state == "FAULT_LOCKED" and from_state in aeri.EPISODE_STATES
    for rule in rules:
        if principal_kind not in rule.principals:
            failures.append(("E_AUTHORITY", f"{principal_kind} may not move here"))
        elif (rule.command or principal_kind == "operator") and command_id is None:
            failures.append(("E_AUTHORITY", "an operator's command_id is required"))
        elif reason not in rule.reasons:
            failures.append(("E_REASON", f"reason {reason}"))
        elif action_kind not in rule.actions:
            failures.append(("E_ACTION", f"action {action_kind}"))
        elif rule.role is not None and episode_role != rule.role:
            failures.append(("E_EPISODE", f"needs a {rule.role} episode"))
        else:
            wants = (from_state, to_state) in RESULT_TRANSITIONS
            if episode_result is not None and episode_result != wants and not optional:
                failures.append(
                    (
                        "E_RESULT",
                        (
                            "an episode result belongs exactly on "
                            "FORWARD_FINALIZE->ROBOT_HOME, RESET_FINALIZE->next "
                            "and (optionally) episode->FAULT_LOCKED"
                        ),
                    )
                )
                continue
            return rule
    code, detail = failures[-1]
    raise TransitionRefused(code, f"{from_state} -> {to_state}: {detail}")


def check_journal(events) -> list[str]:
    """Every prepared and committed transition of a journal checked against
    the table; the problems found (empty: the journal keeps the rules)."""
    problems = []
    prepared = None
    for event in events:
        if event.record == "prepared":
            prepared = event
            try:
                check_transition(
                    event.from_state,
                    event.to_state,
                    reason=event.reason,
                    principal_kind=event.authority.principal_kind,
                    command_id=event.authority.command_id,
                    action_kind=event.action.kind,
                    non_idempotent=event.action.non_idempotent,
                    step=event.action.step,
                    episode_role=event.episode_role,
                )
            except TransitionRefused as exc:
                problems.append(f"line {event.sequence_no}: {exc}")
        elif event.record == "committed" and prepared is not None:
            wants = (event.from_state, event.to_state) in RESULT_TRANSITIONS
            optional = (
                event.to_state == "FAULT_LOCKED"
                and event.from_state in aeri.EPISODE_STATES
            )
            if (event.episode_result is not None) != wants and not optional:
                problems.append(
                    f"line {event.sequence_no}: episode result on "
                    f"{event.from_state} -> {event.to_state}"
                )
            prepared = None
    return problems


# --- the motion fence -------------------------------------------------------------


@dataclass(frozen=True)
class MotionToken:
    """Permission to move: one transaction, one control epoch, one kind of
    motion, until ``expires_mono_ns`` on the orchestrator's clock."""

    run_id: str
    transaction_id: str
    control_epoch: int
    kind: str  # "policy_steps" or "home"
    episode_id: str
    policy_epoch: int | None
    expires_mono_ns: int


class MotionRefused(Exception):
    """``code``: no_token, revoked, expired, wrong_kind."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class MotionFence:
    """The one current motion token. The orchestrator issues it after the
    journal holds the transaction and revokes it before anything else is
    prepared; a robot adapter asks ``check`` before every command. A token
    kept by a late thread or a late chunk is refused once revoked. Thread
    safe."""

    def __init__(self):
        self._lock = threading.Lock()
        self._current: MotionToken | None = None
        self.refused = 0

    def issue(self, token: MotionToken) -> MotionToken:
        with self._lock:
            self._current = token
            return token

    def revoke(self) -> None:
        with self._lock:
            self._current = None

    @property
    def current(self) -> MotionToken | None:
        with self._lock:
            return self._current

    def check(self, token, kind: str, now_ns: int) -> None:
        with self._lock:
            problem = None
            if token is None:
                problem = ("no_token", "")
            elif token != self._current:
                problem = ("revoked", token.transaction_id)
            elif token.kind != kind:
                problem = ("wrong_kind", f"{token.kind} is not {kind}")
            elif now_ns >= token.expires_mono_ns:
                problem = ("expired", token.transaction_id)
            if problem is not None:
                self.refused += 1
                raise MotionRefused(*problem)
