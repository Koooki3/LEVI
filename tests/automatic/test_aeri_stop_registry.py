"""Regression tests for the final review of the AERI orchestrator
(levi2/recon/review-C2-final.md, F1 and F2): a stop that arrives while a
resume is in progress is never cleared by that resume; a stop that took
effect is consumed, so sending it again is ``repeated`` (it never ends the
run) and the next resume writes no false ``stop_command_lost``."""

import threading
import time

import pytest
from aeri_harness import build, committed, config

from levi.automatic import state_machine as sm


def waiting(tmp_path):
    """A run waiting for a person (no policy at the first episode)."""
    r = build(tmp_path, cfg=config(episodes=1), policy={"acquire_fails": True})
    assert r.orch.run() == "WAIT_HUMAN"
    r.policy.acquire_fails = False
    return r


def resume(r, command="c-1"):
    return r.orch.resume(
        command,
        expected_seq=r.orch.journal.next_seq,
        environment_handled=True,
        health_rechecked=True,
    )


def assert_stop_kept(r, found):
    assert found.ok and found.code == "stop_requested"
    assert r.orch.run() == "WAIT_HUMAN"
    assert r.robot.step_calls == 0 and r.robot.home_calls == 0
    assert committed(r.orch)[-1][1:] == ("WAIT_HUMAN", "operator_stop")
    assert sm.check_journal(r.orch.journal.events) == []


# --- F1: a stop during a resume survives it -------------------------------------------------


def test_a_stop_while_the_resume_holds_the_lock_survives(tmp_path):
    """The resume's prepare is slow: stop() gives up on the lock after its
    short wait and only registers."""
    r = waiting(tmp_path)
    original = r.orch.journal.prepare
    box = {}

    def prepare(to, *args, **kwargs):
        if to == "PREFLIGHT" and "stop" not in box:
            thread = threading.Thread(
                target=lambda: box.setdefault("stop", r.orch.stop("s-race"))
            )
            thread.start()
            time.sleep(0.2)
            thread.join(5)
        return original(to, *args, **kwargs)

    r.orch.journal.prepare = prepare
    assert resume(r).ok
    assert_stop_kept(r, box["stop"])


def test_a_stop_that_waits_for_the_resume_s_lock_survives(tmp_path):
    """No sleep: stop() gets the lock once the resume releases it."""
    r = waiting(tmp_path)
    original = r.orch.journal.prepare
    box = {}

    def prepare(to, *args, **kwargs):
        if to == "PREFLIGHT" and "thread" not in box:
            box["thread"] = threading.Thread(
                target=lambda: box.setdefault("stop", r.orch.stop("s-race"))
            )
            box["thread"].start()
            time.sleep(0.01)  # the stop is registered and waits for the lock
        return original(to, *args, **kwargs)

    r.orch.journal.prepare = prepare
    assert resume(r).ok
    box["thread"].join(5)
    assert_stop_kept(r, box["stop"])


def test_a_stop_between_the_resume_s_commit_and_its_end_survives(tmp_path):
    r = waiting(tmp_path)
    box = {}

    def hook(point):
        if point == "after_committed" and "stop" not in box:
            box["stop"] = r.orch.stop("s-race")  # same thread: the lock is ours

    r.orch.crash_hook = hook
    assert resume(r).ok
    r.orch.crash_hook = None
    assert_stop_kept(r, box["stop"])


def test_a_stop_before_the_resume_is_cleared_by_it_and_noted(tmp_path):
    """The resume is the operator's later word; the stop it overrides is
    recorded as lost (the one honest use of that note)."""
    r = build(tmp_path, cfg=config(episodes=1), policy={"acquire_fails": True})
    r.orch.run()
    # A stop that could not take effect: registered while the lock was busy.
    r.orch._lock.acquire()
    try:
        holder = threading.Thread(target=lambda: r.orch.stop("s-old"))
        holder.start()
        holder.join(5)
    finally:
        r.orch._lock.release()
    r.policy.acquire_fails = False
    assert resume(r).ok
    assert r.orch.run() == "COMPLETED"
    notes = [e.note for e in r.orch.journal.events if e.record == "note"]
    assert any(n.code == "stop_command_lost" and "s-old" in n.detail for n in notes)


# --- F2: a stop that took effect is consumed ------------------------------------------------


@pytest.fixture
def stopped(tmp_path):
    """A stop at step 3 (default: home, then wait for a person)."""
    r = build(tmp_path, cfg=config(episodes=2))
    original = r.robot.step

    def step(action, *, token):
        if r.robot.step_calls == 3:
            r.orch.stop("s1")
        return original(action, token=token)

    r.robot.step = step
    assert r.orch.run() == "WAIT_HUMAN"
    r.robot.step = original
    assert committed(r.orch)[-1] == ("SCENE_ASSESS", "WAIT_HUMAN", "operator_stop")
    return r


def test_the_same_stop_again_after_it_took_effect_is_repeated(stopped):
    again = stopped.orch.stop("s1")
    assert again.ok and again.repeated and again.code == "repeated"
    assert stopped.orch.state == "WAIT_HUMAN"  # never ends the run


def test_a_resume_after_a_stop_that_took_effect_notes_nothing_lost(stopped):
    assert resume(stopped).ok
    assert stopped.orch.run() == "COMPLETED"
    codes = [e.note.code for e in stopped.orch.journal.events if e.record == "note"]
    assert "stop_command_lost" not in codes
    # The run went on: the stop no longer stops anything.
    assert stopped.orch.completed_forward == 2


def test_a_consumed_stop_id_after_a_resume_is_a_used_id(stopped):
    assert resume(stopped).ok
    found = stopped.orch.stop("s1")
    assert not found.ok and found.code == "command_used"


def test_two_stops_are_both_recorded_when_consumed(tmp_path):
    r = build(tmp_path, cfg=config(episodes=1))
    original = r.robot.step

    def step(action, *, token):
        if r.robot.step_calls == 3:
            r.orch.stop("s-a")
            r.orch.stop("s-b")
        return original(action, token=token)

    r.robot.step = step
    assert r.orch.run() == "WAIT_HUMAN"
    notes = [e.note for e in r.orch.journal.events if e.record == "note"]
    assert any(n.code == "stop_commands_merged" and "s-b" in n.detail for n in notes)
    assert r.orch.stop("s-b").code == "repeated"
    assert r.orch.state == "WAIT_HUMAN"
