"""Reset arbitration (T-C-08, pipeline §6.2-§6.4): after a scene verdict,
start the next forward episode, run the reset policy or ask a person; after
a reset, check the scene again or ask a person.

The rules every strategy keeps (the orchestrator relies on them, and
``check_plan`` refuses a strategy that breaks them):

- only a ``ready`` verdict (``SceneVerdict.may_skip_reset``) starts a
  forward episode without a reset: ``unknown`` and ``unavailable`` never do;
- a reset that reached its horizon, was stopped, or lost its policy goes to
  a person (its rollout is sealed first: the failed rollout is kept);
- a reset whose home failed never continues (the orchestrator locks the run
  in ``FAULT_LOCKED`` / ``home_failed``; no strategy is asked);
- a resume is the operator's (``Orchestrator.resume``), idempotent per
  command id; after it the run goes ``PREFLIGHT -> VERIFY_INITIAL``, a fresh
  initial-state check, never straight back to motion.

``ResetStrategy`` is the seam pipeline §6.4 asks for. v1 has
``SingleResetPolicy`` (one reset policy, ``max_attempts`` between two
forward episodes; the default, the behaviour before T-C-08) and
``HumanAssistedReset`` (no reset policy: every scene that is not ready
waits for a person). ``AtomicSkillSequence`` and ``ScriptedSafeReset`` are
named for later and refused today.
"""

from dataclasses import dataclass
from typing import Protocol

FORWARD = "forward"
RESET = "reset"
WAIT_HUMAN = "wait_human"
# What a reset episode's finalisation may lead to.
VERIFY_AGAIN = "VERIFY_INITIAL"
TO_PERSON = "WAIT_HUMAN"
# Outcomes of a reset after which the scene may be checked again.
RETRYABLE_OUTCOMES = frozenset({"scene_reset_required", "scene_unknown"})
STRATEGIES = ("single_reset_policy", "human_assisted")
LATER = ("atomic_skill_sequence", "scripted_safe_reset")


class StrategyError(ValueError):
    pass


@dataclass(frozen=True)
class ResetPlan:
    """``action``: ``forward`` (skip the reset), ``reset`` (run the reset
    policy) or ``wait_human``; ``reason`` is the journal reason."""

    action: str
    reason: str


class ResetStrategy(Protocol):
    name: str

    def plan(self, decision: str, attempts: int) -> ResetPlan:
        """``decision``: the verdict (``ready``, ``reset_required``,
        ``unknown``, ``unavailable``); ``attempts``: resets already run
        since the last forward episode. Only ``ready`` may start a forward
        episode, also right after an operator's resume (design X2 §1.2:
        the system's own check is the second confirmation)."""
        ...

    def after_reset(self, outcome: str, attempts: int, stop_pending: bool) -> str:
        """``VERIFY_INITIAL`` or ``WAIT_HUMAN`` after a reset episode whose
        finalisation reason is ``outcome``."""
        ...


def _scene_reason(decision: str) -> str:
    return "scene_reset_required" if decision == "reset_required" else "scene_unknown"


# The journal reason of a scene that does not start a forward episode.
scene_reason = _scene_reason


@dataclass(frozen=True)
class SingleResetPolicy:
    """One reset policy. ``on_unknown``: ``reset`` (an unknown or
    unavailable scene runs the reset policy; it is never skipped) or
    ``wait_human``."""

    enabled: bool = True
    max_attempts: int = 1
    on_unknown: str = "reset"
    name: str = "single_reset_policy"

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise StrategyError("enabled is true or false")
        if type(self.max_attempts) is not int or self.max_attempts < 0:
            raise StrategyError("max_attempts is a whole number >= 0")
        if self.on_unknown not in ("reset", "wait_human"):
            raise StrategyError("on_unknown is reset or wait_human")

    def plan(self, decision: str, attempts: int) -> ResetPlan:
        if decision == "ready":
            return ResetPlan(FORWARD, "scene_ready")
        reason = _scene_reason(decision)
        may = (
            self.enabled
            and attempts < self.max_attempts
            and (decision == "reset_required" or self.on_unknown == "reset")
        )
        return ResetPlan(RESET if may else WAIT_HUMAN, reason)

    def after_reset(self, outcome: str, attempts: int, stop_pending: bool) -> str:
        if outcome == "reset_verified":
            # Even with a stop pending: the next scene check consumes it
            # (VERIFY_INITIAL is where a stop waits for a person).
            return VERIFY_AGAIN
        if (
            outcome in RETRYABLE_OUTCOMES
            and attempts < self.max_attempts
            and not stop_pending
        ):
            return VERIFY_AGAIN
        return TO_PERSON


@dataclass(frozen=True)
class HumanAssistedReset:
    """No reset policy: a person puts the scene back whenever it is not
    ready (pipeline §6.4 ``HumanAssistedReset``; the "policy evaluation
    only" mode, AUT-22). After the person's resume the system checks the
    scene again, and only ``ready`` starts the forward episode; anything
    else waits for the person again (a person attesting the scene,
    ``operator_attested`` of design X2, is a later task)."""

    name: str = "human_assisted"

    def plan(self, decision: str, attempts: int) -> ResetPlan:
        if decision == "ready":
            return ResetPlan(FORWARD, "scene_ready")
        return ResetPlan(WAIT_HUMAN, _scene_reason(decision))

    def after_reset(self, outcome: str, attempts: int, stop_pending: bool) -> str:
        return TO_PERSON  # never reached: this strategy runs no reset


def strategy_for(name: str, *, enabled=True, max_attempts=1, on_unknown="reset"):
    if name == "single_reset_policy":
        return SingleResetPolicy(enabled, max_attempts, on_unknown)
    if name == "human_assisted":
        return HumanAssistedReset()
    if name in LATER:
        raise StrategyError(f"{name} is planned (pipeline §6.4), not available in v1")
    raise StrategyError(
        f"unknown reset strategy {name!r}: one of {', '.join(STRATEGIES)}"
    )


DECISIONS = ("ready", "reset_required", "unknown", "unavailable")


def check_plan(strategy, decision: str, attempts: int) -> ResetPlan:
    """The strategy's plan, refused when it breaks the rules above."""
    if decision not in DECISIONS:
        raise StrategyError(f"unknown scene decision {decision!r}")
    found = strategy.plan(decision, attempts)
    if found.action not in (FORWARD, RESET, WAIT_HUMAN):
        raise StrategyError(f"{strategy.name}: unknown action {found.action!r}")
    if found.action == FORWARD and decision != "ready":
        raise StrategyError(
            f"{strategy.name} would skip the reset on a {decision} scene"
        )
    if found.action == FORWARD and found.reason != "scene_ready":
        raise StrategyError(f"{strategy.name}: a forward start is scene_ready")
    if found.action != FORWARD and found.reason != _scene_reason(decision):
        raise StrategyError(f"{strategy.name}: reason {found.reason!r}")
    return found


def check_after(strategy, outcome: str, attempts: int, stop_pending: bool) -> str:
    found = strategy.after_reset(outcome, attempts, stop_pending)
    if found not in (VERIFY_AGAIN, TO_PERSON):
        raise StrategyError(f"{strategy.name}: unknown next state {found!r}")
    # A verified reset always goes to the next scene check (a pending stop
    # is consumed there); a retry never goes on past a stop.
    if found == VERIFY_AGAIN and (
        outcome not in ({"reset_verified"} | RETRYABLE_OUTCOMES)
        or (stop_pending and outcome in RETRYABLE_OUTCOMES)
    ):
        raise StrategyError(f"{strategy.name} would go on after {outcome}")
    return found
