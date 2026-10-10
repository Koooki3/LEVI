"""The AERI state machine's table (levi/automatic/state_machine.py): legal
moves, authority, actions, steps, and the motion fence."""

import itertools
import random
import threading

import pytest

from levi.automatic import state_machine as sm
from levi.domain import aeri

ORCH = {"principal_kind": "orchestrator", "command_id": None}


def check(
    a,
    b,
    reason,
    *,
    who="orchestrator",
    command=None,
    kind="none",
    step=None,
    role=None,
    result=None,
):
    return sm.check_transition(
        a,
        b,
        reason=reason,
        principal_kind=who,
        command_id=command,
        action_kind=kind,
        non_idempotent=kind in sm.NON_IDEMPOTENT_KINDS,
        step=step,
        episode_role=role,
        episode_result=result,
    )


def test_every_state_is_known_and_reachable():
    assert set(sm.STATES) == set(aeri.AERI_STATES) and len(sm.STATES) == 13
    for a, b in sm.TRANSITIONS:
        assert a in sm.STATES and b in sm.STATES
    seen, frontier = {"PREFLIGHT"}, ["PREFLIGHT"]
    while frontier:
        for nxt in sm.successors(frontier.pop()):
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    assert seen == set(sm.STATES)
    assert sm.successors("COMPLETED") == set()


def test_every_reason_and_action_in_the_table_is_in_the_contract():
    for rules in sm.TRANSITIONS.values():
        for rule in rules:
            assert rule.reasons <= set(aeri.TRANSITION_REASONS)
            assert rule.actions <= set(aeri.ACTION_KINDS)


def test_the_normal_path():
    check("PREFLIGHT", "VERIFY_INITIAL", "preflight_passed")
    check(
        "VERIFY_INITIAL",
        "FORWARD_ACTIVE",
        "scene_ready",
        kind="policy_steps",
        step=0,
        role="forward",
    )
    check(
        "FORWARD_ACTIVE",
        "FORWARD_STOPPING",
        "goal_verified",
        kind="hold",
        role="forward",
    )
    check(
        "FORWARD_STOPPING",
        "FORWARD_FINALIZE",
        "goal_verified",
        kind="policy_quiesce",
        role="forward",
    )
    check(
        "FORWARD_FINALIZE",
        "ROBOT_HOME",
        "goal_verified",
        kind="recorder_seal",
        role="forward",
        result=True,
    )
    check(
        "ROBOT_HOME",
        "SCENE_ASSESS",
        "robot_home_reached",
        kind="home",
        step=1,
        role="forward",
    )
    check(
        "SCENE_ASSESS",
        "RESET_ACTIVE",
        "scene_reset_required",
        kind="policy_steps",
        step=0,
        role="reset",
    )
    check(
        "RESET_ACTIVE",
        "RESET_VERIFY",
        "goal_verified",
        kind="policy_quiesce",
        role="reset",
    )
    check(
        "RESET_VERIFY",
        "RESET_FINALIZE",
        "reset_verified",
        kind="recorder_seal",
        role="reset",
    )
    check(
        "RESET_FINALIZE",
        "VERIFY_INITIAL",
        "reset_verified",
        kind="home",
        step=1,
        role="reset",
        result=True,
    )
    check("SCENE_ASSESS", "COMPLETED", "run_completed")


@pytest.mark.parametrize(
    "case",
    [
        # Straight back to motion from a human state.
        ("WAIT_HUMAN", "FORWARD_ACTIVE", "scene_ready", {}, "E_ILLEGAL"),
        ("FAULT_LOCKED", "VERIFY_INITIAL", "human_resumed", {}, "E_ILLEGAL"),
        # Leaving FAULT_LOCKED without an operator's command.
        ("FAULT_LOCKED", "PREFLIGHT", "human_resumed", {}, "E_AUTHORITY"),
        (
            "FAULT_LOCKED",
            "PREFLIGHT",
            "human_resumed",
            {"who": "operator"},
            "E_AUTHORITY",
        ),
        (
            "WAIT_HUMAN",
            "PREFLIGHT",
            "human_resumed",
            {"who": "orchestrator"},
            "E_AUTHORITY",
        ),
        # A recovery only re-enters FAULT_LOCKED.
        (
            "FAULT_LOCKED",
            "PREFLIGHT",
            "human_resumed",
            {"who": "recovery"},
            "E_AUTHORITY",
        ),
        # The safety guard only locks.
        (
            "FORWARD_ACTIVE",
            "FORWARD_STOPPING",
            "goal_verified",
            {"who": "safety_guard", "kind": "hold", "role": "forward"},
            "E_AUTHORITY",
        ),
        # A wrong reason.
        ("PREFLIGHT", "VERIFY_INITIAL", "goal_verified", {}, "E_REASON"),
        # A physical action without a step, or claimed idempotent.
        (
            "VERIFY_INITIAL",
            "FORWARD_ACTIVE",
            "scene_ready",
            {"kind": "policy_steps", "role": "forward"},
            "E_STEP",
        ),
        (
            "ROBOT_HOME",
            "SCENE_ASSESS",
            "robot_home_reached",
            {"kind": "home", "role": "forward"},
            "E_STEP",
        ),
        # The wrong action, the wrong role.
        (
            "VERIFY_INITIAL",
            "FORWARD_ACTIVE",
            "scene_ready",
            {"kind": "hold", "role": "forward"},
            "E_ACTION",
        ),
        (
            "VERIFY_INITIAL",
            "FORWARD_ACTIVE",
            "scene_ready",
            {"kind": "policy_steps", "step": 0, "role": "reset"},
            "E_EPISODE",
        ),
        # An episode result only where it belongs.
        (
            "FORWARD_FINALIZE",
            "ROBOT_HOME",
            "goal_verified",
            {"kind": "recorder_seal", "role": "forward", "result": False},
            "E_RESULT",
        ),
        (
            "ROBOT_HOME",
            "SCENE_ASSESS",
            "robot_home_reached",
            {"kind": "home", "step": 1, "role": "forward", "result": True},
            "E_RESULT",
        ),
        # Nothing after COMPLETED.
        ("COMPLETED", "FAULT_LOCKED", "safety_stop", {}, "E_ILLEGAL"),
    ],
)
def test_refused_moves(case):
    a, b, reason, kw, code = case
    with pytest.raises(sm.TransitionRefused) as exc:
        check(a, b, reason, **kw)
    assert exc.value.code == code


def test_non_idempotent_flag_is_required_for_physical_actions():
    with pytest.raises(sm.TransitionRefused) as exc:
        sm.check_transition(
            "ROBOT_HOME",
            "SCENE_ASSESS",
            reason="robot_home_reached",
            principal_kind="orchestrator",
            command_id=None,
            action_kind="home",
            non_idempotent=False,
            step=1,
            episode_role="forward",
        )
    assert exc.value.code == "E_ACTION"


def test_fault_locked_is_reachable_from_every_live_state_and_left_only_by_a_command():
    for state in sm.STATES:
        if state in ("COMPLETED", "FAULT_LOCKED"):
            continue
        check(state, "FAULT_LOCKED", "safety_stop", who="safety_guard")
        check(state, "FAULT_LOCKED", "recovery_ambiguous", who="recovery")
    check("FAULT_LOCKED", "FAULT_LOCKED", "recovery_ambiguous", who="recovery")
    check("FAULT_LOCKED", "PREFLIGHT", "human_resumed", who="operator", command="c-1")
    assert sm.successors("FAULT_LOCKED") == {"PREFLIGHT", "FAULT_LOCKED"}
    assert sm.successors("WAIT_HUMAN") == {"PREFLIGHT", "COMPLETED", "FAULT_LOCKED"}


def test_motion_enters_only_from_a_scene_decision():
    into_motion = {a for (a, b) in sm.TRANSITIONS if b in sm.MOTION_STATES}
    assert into_motion == {"VERIFY_INITIAL", "SCENE_ASSESS"}
    for (a, b), rules in sm.TRANSITIONS.items():
        if b in sm.MOTION_STATES:
            assert all(
                r.actions == {"policy_steps"} and r.principals == {"orchestrator"}
                for r in rules
            )


def test_random_walk_never_finds_an_illegal_move():
    """10^4 random proposals: whatever the table accepts is an edge of it,
    with an allowed principal and action; nothing leaves the human states
    except an operator's command or a recovery that stays locked."""
    rng = random.Random(20261010)
    principals = ("orchestrator", "operator", "safety_guard", "recovery")
    state, accepted = "PREFLIGHT", 0
    for _ in range(10_000):
        to = rng.choice(sm.STATES)
        reason = rng.choice(aeri.TRANSITION_REASONS)
        who = rng.choice(principals)
        kind = rng.choice(aeri.ACTION_KINDS)
        command = rng.choice((None, "cmd-1"))
        edges = [b for (a, b) in sm.TRANSITIONS if a == state]
        if edges and rng.random() < 0.6:
            # Half the proposals follow the table, with one field possibly
            # wrong, so the walk gets everywhere.
            to = rng.choice(edges)
            rule = rng.choice(sm.TRANSITIONS[(state, to)])
            reason = rng.choice(sorted(rule.reasons))
            who = rng.choice(sorted(rule.principals))
            kind = rng.choice(sorted(rule.actions))
            if rng.random() < 0.3:
                who = rng.choice(principals)
        try:
            rule = sm.check_transition(
                state,
                to,
                reason=reason,
                principal_kind=who,
                command_id=command,
                action_kind=kind,
                non_idempotent=kind in sm.NON_IDEMPOTENT_KINDS,
                step=rng.choice((None, 0, 1)),
                episode_role=rng.choice((None, "forward", "reset")),
            )
        except sm.TransitionRefused:
            continue
        accepted += 1
        assert rule in sm.TRANSITIONS[(state, to)]
        assert (
            who in rule.principals and reason in rule.reasons and kind in rule.actions
        )
        if state in sm.HUMAN_STATES and to != "FAULT_LOCKED":
            assert who == "operator" and command is not None
        if to in sm.MOTION_STATES:
            assert who == "orchestrator" and kind == "policy_steps"
        state = to if to != "COMPLETED" else "PREFLIGHT"
    assert accepted > 1000


# --- the motion fence ------------------------------------------------------------------


def token(tx="r:tx1", kind="policy_steps", expires=10_000):
    return sm.MotionToken("r", tx, 1, kind, "r.forward.0001", 1, expires)


def test_fence_accepts_only_the_current_unexpired_token_of_its_kind():
    fence = sm.MotionFence()
    current = fence.issue(token())
    fence.check(current, "policy_steps", 0)
    for bad, kind, now, code in (
        (None, "policy_steps", 0, "no_token"),
        (token("r:tx0"), "policy_steps", 0, "revoked"),
        (current, "home", 0, "wrong_kind"),
        (current, "policy_steps", 10_000, "expired"),
    ):
        with pytest.raises(sm.MotionRefused) as exc:
            fence.check(bad, kind, now)
        assert exc.value.code == code
    fence.revoke()
    with pytest.raises(sm.MotionRefused):
        fence.check(current, "policy_steps", 0)


def test_a_late_thread_holding_an_old_token_cannot_move():
    fence = sm.MotionFence()
    old = fence.issue(token("r:tx1"))
    allowed = []
    barrier = threading.Barrier(9)

    def late():
        barrier.wait()
        for _ in range(200):
            try:
                fence.check(old, "policy_steps", 0)
                allowed.append(1)
            except sm.MotionRefused:
                pass

    threads = [threading.Thread(target=late) for _ in range(8)]
    for thread in threads:
        thread.start()
    barrier.wait()
    fence.revoke()
    fence.issue(token("r:tx2"))
    for thread in threads:
        thread.join()
    # Once revoked, the old token is refused for good.
    with pytest.raises(sm.MotionRefused):
        fence.check(old, "policy_steps", 0)
    assert fence.refused + len(allowed) == 8 * 200 + 1


def test_tables_are_closed_over_the_contract_enums():
    pairs = list(itertools.product(sm.STATES, repeat=2))
    assert len(pairs) == 169
    assert set(sm.TRANSITIONS) <= set(pairs)


def test_robot_home_and_operator_rules():
    check(
        "ROBOT_HOME",
        "SCENE_ASSESS",
        "robot_home_reached",
        kind="home",
        step=1,
        role="forward",
    )
    with pytest.raises(sm.TransitionRefused) as exc:
        check(
            "ROBOT_HOME",
            "SCENE_ASSESS",
            "goal_verified",
            kind="home",
            step=1,
            role="forward",
        )
    assert exc.value.code == "E_REASON"
    # A person takes over where the arm stands only on an operator's command.
    check("ROBOT_HOME", "WAIT_HUMAN", "operator_stop", who="operator", command="s-1")
    with pytest.raises(sm.TransitionRefused):
        check("ROBOT_HOME", "WAIT_HUMAN", "operator_stop")
    # An operator always names its command, also on shared rules.
    with pytest.raises(sm.TransitionRefused) as exc:
        check(
            "FORWARD_ACTIVE",
            "FORWARD_STOPPING",
            "operator_stop",
            who="operator",
            kind="hold",
            role="forward",
        )
    assert exc.value.code == "E_AUTHORITY"
    # An episode ended by a fault may carry its result, or not.
    for result in (True, False):
        check(
            "FORWARD_ACTIVE",
            "FAULT_LOCKED",
            "safety_stop",
            who="safety_guard",
            kind="hold",
            result=result,
        )
    with pytest.raises(sm.TransitionRefused):
        check("ROBOT_HOME", "FAULT_LOCKED", "home_failed", result=True)
    # A run that stopped with no step discards its recording.
    check(
        "FORWARD_FINALIZE",
        "ROBOT_HOME",
        "operator_stop",
        who="operator",
        command="s-1",
        kind="recorder_abort",
        role="forward",
        result=True,
    )
