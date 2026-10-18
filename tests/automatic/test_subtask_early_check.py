"""subtask.early_check (default off) on fakes: a running subtask stops for the
VLM's done check when a trigger event (the gripper opening) has settled, or
every N steps as the fallback. The episode ends the ordinary way (hold,
quiesce, seal), the same ``done_when`` question is asked on still frames,
true goes to the next subtask, anything else continues the same subtask in a
new episode with the rest of its step budget. No network, robot or GPU.

Written while the arm controller was in use: not run by its author."""

import json

import pytest
from aeri_harness import RUN
from subtask_rig import FORWARD, INVENTORY, REVERSE, build, committed, notes, prompts

from levi.automatic import metrics
from levi.automatic import state_machine as sm
from levi.automatic import subtask_loop as st
from levi.automatic.orchestrator import ConfigError

F1 = f"{RUN}.forward.0001"
F2 = f"{RUN}.forward.0002"
R1 = f"{RUN}.reset.0001"
MAX = 30


def early(**over):
    base = {
        "on_events": True,
        "settle_steps": 2,
        "min_steps": 3,
        "cooldown_steps": 5,
        "max_checks": 2,
    }
    base.update(over)
    return st.EarlyCheck(**base)


def release(step, **extra):
    return {"step": step, "event_type": "gripper_open", **extra}


def rig(tmp_path, *, checks, events=None, check=None, **over):
    over.setdefault("subtask_early_check", early() if check is None else check)
    return build(
        tmp_path,
        checks=checks,
        events=events or {},
        subtask_max_steps=over.pop("subtask_max_steps", MAX),
        **over,
    )


def results(orch):
    return [
        (e.episode_id, e.episode_result.stop_reason)
        for e in orch.journal.events
        if e.record == "committed" and e.episode_result is not None
    ]


# --- off: nothing changes ------------------------------------------------------------


def test_off_by_default_events_change_nothing(tmp_path):
    r = build(tmp_path, events={F1: [release(4)]}, subtask_max_steps=MAX)
    assert r.orch.config.subtask_early_check is None
    assert r.orch.run() == "COMPLETED"
    assert r.robot.step_calls == 4 * MAX
    assert notes(r.orch, st.NOTE_EARLY_CHECK) == []
    assert all("early" not in c for c in notes(r.orch, st.NOTE_CHECK))
    assert prompts(r) == [st.render(a, b) for a, b in FORWARD + REVERSE]


# --- an event makes a check due; true ends the subtask now ------------------------------


def test_a_settled_release_ends_the_subtask_when_the_check_says_done(tmp_path):
    r = rig(
        tmp_path, checks=st.checks(True, True, True, True), events={F1: [release(4)]}
    )
    assert r.orch.run() == "COMPLETED"
    # Due at 4 + settle 2 = 6: six steps, then no further step of that subtask.
    assert r.robot.step_calls == 6 + 3 * MAX
    assert prompts(r) == [st.render(a, b) for a, b in FORWARD + REVERSE]
    (found,) = notes(r.orch, st.NOTE_EARLY_CHECK)
    assert found["trigger"] == "event" and found["event_type"] == "gripper_open"
    assert (found["event_step"], found["step"], found["n"]) == (4, 6, 1)
    assert found["episode_id"] == F1 and found["loop"] == "forward"
    first = notes(r.orch, st.NOTE_CHECK)[0]
    assert first["early"] is True and first["trigger"] == "event"
    assert first["step"] == 6 and first["value"] is True
    assert (F1, "subtask_early_check") in results(r.orch)
    assert sm.check_journal(r.orch.journal.events) == []
    # The ordinary stop path: hold -> quiesce -> seal, then the check.
    path = [c for c in committed(r.orch) if c[2] == "subtask_early_check"]
    assert [(a, b) for a, b, _ in path] == [
        ("FORWARD_ACTIVE", "FORWARD_STOPPING"),
        ("FORWARD_STOPPING", "FORWARD_FINALIZE"),
        ("FORWARD_FINALIZE", "ROBOT_HOME"),
    ]


def test_the_check_is_asked_only_after_hold_quiesce_and_seal(tmp_path):
    """R6/R7(b) still hold with early checks on: no VLM question while a
    policy may infer."""
    states = []
    holder = []

    def on_call(kind, info):
        states.append(holder[0].orch.state)

    r = rig(
        tmp_path,
        checks=st.checks(False, True, True, True, True),
        events={F1: [release(4)]},
        on_call=on_call,
    )
    holder.append(r)
    assert r.orch.run() == "COMPLETED"
    assert states and not set(states) & sm.POLICY_MAY_INFER
    assert not set(states) & sm.MOTION_STATES
    # The VLM slept again before the continued episode's policy acquired.
    assert all(awake is False for awake in r.policy.awake_at_acquire)


# --- not done: the same subtask continues with the rest of its budget --------------------


@pytest.mark.parametrize("answer", [False, None])
def test_false_or_null_continues_the_same_subtask(tmp_path, answer):
    r = rig(
        tmp_path,
        checks=[st.check_answer(answer), *st.checks(True, True, True, True)],
        events={F1: [release(4)]},
    )
    assert r.orch.run() == "COMPLETED"
    f0 = st.render(*FORWARD[0])
    assert prompts(r)[:3] == [f0, f0, st.render(*FORWARD[1])]
    # 6 steps, then the remaining 24 in the continued episode.
    assert r.robot.step_calls == 6 + (MAX - 6) + 3 * MAX
    (cont,) = notes(r.orch, st.NOTE_CONTINUED)
    assert cont["after"] == F1 and cont["segment"] == 2
    assert cont["value"] is answer
    assert (cont["used_steps"], cont["remaining_steps"]) == (6, MAX - 6)
    started = notes(r.orch, st.NOTE_STARTED)
    assert "segment" not in started[0]
    assert started[1]["segment"] == 2 and started[1]["continues"] == F1
    assert started[1]["attempt"] == 1 and started[1]["max_steps"] == MAX - 6
    # Not a retry: no retry note, the loop counts two subtasks.
    assert notes(r.orch, st.NOTE_RETRY) == []
    done = notes(r.orch, st.NOTE_LOOP_DONE)
    assert [(d["loop"], d["subtasks"], d["retries"]) for d in done] == [
        ("forward", 2, 0),
        ("reverse", 2, 0),
    ]
    assert metrics.report(r.orch.journal.events)["subtasks"]["subtasks"] == 4
    # Both episodes ran, both are the round's members.
    assert r.orch.sub.round_episodes[:2] == [F1, F2]
    described = list(r.recorder.described.values())
    assert described[1]["segment"] == 2 and described[1]["continues"] == F1
    assert sm.check_journal(r.orch.journal.events) == []


def test_an_unavailable_vlm_at_an_early_check_continues(tmp_path):
    """The second wake (the first is for the inventory and the plan, the
    second for the early check) fails: the subtask goes on; the ordinary
    check at the budget's end still decides."""

    class SecondWakeFails(st.FakeVlmPower):
        def wake(self):
            ok = super().wake()
            if self.wakes == 2:
                self.awake = False
                return False
            return ok

    r = rig(
        tmp_path,
        checks=st.checks(True, True, True, True),
        events={F1: [release(4)]},
        power=SecondWakeFails(awake=False),
    )
    assert r.orch.run() == "COMPLETED"
    assert notes(r.orch, st.NOTE_VLM_WAKE_FAILED)
    (cont,) = notes(r.orch, st.NOTE_CONTINUED)
    assert cont["value"] is None
    # The early check was counted though not asked.
    assert notes(r.orch, st.NOTE_EARLY_CHECK)[0]["n"] == 1
    assert all("early" not in c for c in notes(r.orch, st.NOTE_CHECK))


def test_the_end_check_after_a_continued_episode_can_still_retry(tmp_path):
    r = rig(
        tmp_path,
        checks=st.checks(False, False, True, True, True, True),
        events={F1: [release(4)]},
    )
    assert r.orch.run() == "COMPLETED"
    retry = notes(r.orch, st.NOTE_RETRY)
    assert [(x["index"], x["attempt"]) for x in retry] == [(0, 2)]
    started = notes(r.orch, st.NOTE_STARTED)
    # The retry is a new attempt: full budget, first segment again.
    assert "segment" not in started[2] and started[2]["attempt"] == 2


# --- stop and safety keep their priority -------------------------------------------------


def test_a_stop_during_the_early_check_wins(tmp_path):
    holder = []

    def on_call(kind, info):
        if kind == "check" and not holder[1]:
            holder[1] = True
            holder[0].orch.stop("stop-early")

    holder = [None, False]
    r = rig(
        tmp_path,
        checks=st.checks(False),
        events={F1: [release(4)]},
        on_call=on_call,
    )
    holder[0] = r
    assert r.orch.run() == "WAIT_HUMAN"
    waits = [c for c in committed(r.orch) if c[1] == "WAIT_HUMAN"]
    assert waits[-1][2] == "operator_stop"
    assert r.robot.step_calls == 6
    assert len(prompts(r)) == 1
    assert notes(r.orch, st.NOTE_CONTINUED) == []


@pytest.mark.parametrize("fault", ["latch", "red"])
def test_a_latch_or_red_guard_during_the_early_check_locks_before_any_step(
    tmp_path, fault
):
    holder = []

    def on_call(kind, info):
        if kind == "check":
            robot = holder[0].robot
            if fault == "latch":
                robot.latched = "test latch"
            else:
                robot.red_light = True

    r = rig(
        tmp_path,
        checks=st.checks(False),
        events={F1: [release(4)]},
        on_call=on_call,
    )
    holder.append(r)
    r.orch.run()
    assert r.orch.state in ("FAULT_LOCKED", "WAIT_HUMAN")
    # Nothing moved after the check.
    assert r.robot.step_calls == 6


def test_the_operator_verdict_before_the_trigger_wins(tmp_path):
    r = rig(tmp_path, checks=st.checks(True, True), events={F1: [release(4)]})
    original = r.robot.step

    def step(action, *, token):
        if r.robot.step_calls == 2 and r.orch.ctx.verdict is None:
            r.orch.episode_verdict("v-s", "success")
        return original(action, token=token)

    r.robot.step = step
    r.orch.run()
    assert notes(r.orch, st.NOTE_EARLY_CHECK) == []
    assert (F1, "operator_verdict") in results(r.orch)


# --- pacing ------------------------------------------------------------------------------


def test_max_checks_caps_the_early_checks_of_an_attempt(tmp_path):
    r = rig(
        tmp_path,
        checks=st.checks(False, True, True, True, True),
        events={F1: [release(4)], F2: [release(2), release(12)]},
        check=early(max_checks=1),
    )
    assert r.orch.run() == "COMPLETED"
    assert len(notes(r.orch, st.NOTE_EARLY_CHECK)) == 1
    assert r.robot.step_calls == 6 + (MAX - 6) + 3 * MAX


def test_a_retracted_release_makes_no_check(tmp_path):
    r = rig(
        tmp_path,
        checks=st.checks(True, True, True, True),
        events={F1: [release(4, id="ev-a"), {**release(5), "retracts": "ev-a"}]},
    )
    assert r.orch.run() == "COMPLETED"
    assert notes(r.orch, st.NOTE_EARLY_CHECK) == []
    assert r.robot.step_calls == 4 * MAX


def test_other_event_types_make_no_check(tmp_path):
    r = rig(
        tmp_path,
        checks=st.checks(True, True, True, True),
        events={F1: [{"step": 4, "event_type": "object_settled"}]},
    )
    assert r.orch.run() == "COMPLETED"
    assert notes(r.orch, st.NOTE_EARLY_CHECK) == []


def test_the_interval_is_the_fallback_without_events(tmp_path):
    one = [FORWARD[0]]
    back = [REVERSE[0]]
    r = build(
        tmp_path,
        plans=[st.plan_answer(one), st.plan_answer(back)],
        checks=st.checks(False, False, True, False, False, True),
        subtask_max_steps=100,
        subtask_early_check=st.EarlyCheck(
            every_steps=40, min_steps=3, cooldown_steps=20, max_checks=3
        ),
    )
    assert r.orch.run() == "COMPLETED"
    found = notes(r.orch, st.NOTE_EARLY_CHECK)
    assert [(x["loop"], x["trigger"], x["step"]) for x in found] == [
        ("forward", "interval", 40),
        ("forward", "interval", 80),
        ("reverse", "interval", 40),
        ("reverse", "interval", 80),
    ]
    # 40 + 40 + 20 steps per loop: the budget is never exceeded.
    assert r.robot.step_calls == 2 * 100
    assert [x["remaining_steps"] for x in notes(r.orch, st.NOTE_CONTINUED)] == [
        60,
        20,
        60,
        20,
    ]


def test_a_reverse_subtask_ends_early_too(tmp_path):
    r = rig(
        tmp_path,
        checks=st.checks(True, True, True, True),
        events={R1: [release(4)]},
    )
    assert r.orch.run() == "COMPLETED"
    (found,) = notes(r.orch, st.NOTE_EARLY_CHECK)
    assert found["loop"] == "reverse" and found["episode_id"] == R1
    assert (R1, "subtask_early_check") in results(r.orch)
    assert r.robot.step_calls == 3 * MAX + 6
    assert sm.check_journal(r.orch.journal.events) == []


def test_a_reverse_subtask_continues_after_false(tmp_path):
    r = rig(
        tmp_path,
        checks=st.checks(True, True, False, True, True),
        events={R1: [release(4)]},
    )
    assert r.orch.run() == "COMPLETED"
    (cont,) = notes(r.orch, st.NOTE_CONTINUED)
    assert cont["loop"] == "reverse" and cont["after"] == R1
    assert r.robot.step_calls == 2 * MAX + 6 + (MAX - 6) + MAX
    # The continuation's result committed on RESET_FINALIZE -> SCENE_ASSESS.
    assert ("RESET_FINALIZE", "SCENE_ASSESS", "subtask_continue") in committed(r.orch)
    assert sm.check_journal(r.orch.journal.events) == []


# --- the pure parts ------------------------------------------------------------------------


def test_early_due_rules():
    c = early(min_steps=5, settle_steps=2, cooldown_steps=4, max_checks=2)
    pend = {"event_type": "gripper_open", "event_id": "e", "step": 6}

    def due(step, **kw):
        args = {"budget": 30, "checks": 0, "last_check": None, "pending": pend}
        args.update(kw)
        return st.early_due(c, step=step, **args)

    assert due(7) is None  # not settled
    assert due(8)["trigger"] == "event"
    assert due(4, pending={**pend, "step": 1}) is None  # before min_steps
    assert due(27) is None  # within cooldown of the budget's end
    assert due(8, last_check=6) is None  # within cooldown of the last check
    assert due(8, checks=2) is None  # the cap
    assert due(8, pending=None) is None
    interval = st.EarlyCheck(every_steps=40, min_steps=0, cooldown_steps=10)
    assert st.early_due(
        interval, step=40, budget=200, checks=0, last_check=None, pending=None
    ) == {"trigger": "interval", "every_steps": 40, "step": 40}
    assert (
        st.early_due(
            interval, step=79, budget=200, checks=1, last_check=40, pending=None
        )
        is None
    )


@pytest.mark.parametrize(
    "over, words",
    [
        ({}, "on_events"),
        ({"every_steps": 39}, "every_steps"),
        ({"on_events": True, "event_types": ("nope",)}, "event_types"),
        ({"on_events": True, "event_types": ()}, "event_types"),
        ({"on_events": True, "max_checks": 0}, "max_checks"),
        ({"on_events": True, "max_checks": 21}, "max_checks"),
        ({"on_events": True, "cooldown_steps": 0}, "cooldown_steps"),
        ({"on_events": "yes"}, "on_events"),
    ],
)
def test_bad_early_check_settings_are_refused(over, words):
    with pytest.raises(ValueError, match=words):
        st.EarlyCheck(**over)


def test_the_option_belongs_to_subtask_loop_only():
    from aeri_harness import config

    with pytest.raises(ConfigError, match="subtask_early_check"):
        config(subtask_early_check=early())


def test_a_dict_setting_becomes_an_early_check(tmp_path):
    r = build(
        tmp_path,
        subtask_early_check={"on_events": True, "event_types": ["gripper_open"]},
    )
    assert isinstance(r.orch.config.subtask_early_check, st.EarlyCheck)
    assert r.orch.config.subtask_early_check.event_types == ("gripper_open",)


def test_new_codes_are_registered():
    from levi.domain import aeri

    assert st.R_EARLY_CHECK in aeri.TRANSITION_REASONS
    assert st.R_EARLY_CHECK in aeri.STOP_REASONS
    assert st.R_EARLY_CHECK in sm.FORWARD_STOPS
    assert {st.NOTE_EARLY_CHECK, st.NOTE_CONTINUED} <= set(st.NOTES)
    assert metrics.ended_by(st.R_EARLY_CHECK) == "early_stop"


def test_the_inventory_fixture_is_unchanged():
    # The rig's whitelist is what these tests' prompts assume.
    assert json.loads(INVENTORY)["objects"][0]["id"] == "pink_plate_1"
