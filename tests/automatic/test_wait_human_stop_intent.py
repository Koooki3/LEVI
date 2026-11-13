"""Deterministic stop registration at committed WAIT_HUMAN boundaries.

Only the existing in-process fakes run here. Threads are joined while the
transaction owns its state lock, so the tests force the 50 ms fallback
without sleeps, command retries, sockets or real motion.
"""

import signal
import threading
from types import SimpleNamespace

import pytest
from aeri_harness import build, config

from levi.automatic import control, runner
from levi.automatic import state_machine as sm


@pytest.fixture
def rigs(tmp_path):
    opened = []

    def create(**kwargs):
        rig = build(tmp_path / str(len(opened)), cfg=config(episodes=1), **kwargs)
        opened.append(rig)
        return rig

    yield create
    for rig in reversed(opened):
        rig.orch.crash_hook = None
        rig.orch.listener = None
        rig.orch.close()
        rig.recorder.close()


def concurrent_calls(*calls):
    """Return every result before releasing the caller's transaction lock."""
    barrier = threading.Barrier(len(calls) + 1)
    results = [None] * len(calls)
    errors = []

    def work(index, call):
        try:
            barrier.wait(timeout=2)
            results[index] = call()
        except BaseException as exc:  # noqa: BLE001 - assert worker failures in the owner
            errors.append(exc)

    threads = [
        threading.Thread(target=work, args=(i, call), name=f"fake-stop-{i}")
        for i, call in enumerate(calls)
    ]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=2)
    for thread in threads:
        thread.join(2)
    assert not any(thread.is_alive() for thread in threads)
    assert not errors, errors
    return results


def waiting(rigs):
    rig = rigs(policy={"acquire_fails": True})
    assert rig.orch.run() == "WAIT_HUMAN"
    return rig


def resume(rig):
    return rig.orch.resume(
        "resume-1",
        expected_seq=rig.orch.journal.next_seq,
        environment_handled=True,
        health_rechecked=True,
    )


def driver(rig, monkeypatch):
    """Exercise the actual drive loop; persistence/launch are out of scope."""
    result = runner.Runner(rig.directory, rig.config.plan_sha256)
    result.orch = rig.orch
    result.dry = SimpleNamespace(robot=rig.robot)
    result.started = 0
    monkeypatch.setattr(
        result,
        "finish",
        lambda exit_code, code, detail: runner.RunnerResult(
            exit_code, rig.orch.state, code, detail
        ),
    )
    return result


def completions(rig):
    return [
        event
        for event in rig.orch.journal.events
        if event.record == "committed" and event.to_state == "COMPLETED"
    ]


def assert_stationary(rig):
    assert rig.robot.step_calls == 0
    assert rig.robot.home_calls == 0
    assert sm.check_journal(rig.orch.journal.events) == []


def test_stop_before_wait_commit_is_consumed_without_ending_the_run(rigs):
    rig = rigs(policy={"acquire_fails": True})
    box = {}

    def hook(point):
        tx = rig.orch.journal.open_transaction
        if point == "after_prepared" and tx.to_state == "WAIT_HUMAN":
            box["stop"] = concurrent_calls(lambda: rig.orch.stop("early"))[0]

    rig.orch.crash_hook = hook
    assert rig.orch.run() == "WAIT_HUMAN"
    assert box["stop"].code == "stop_requested"
    assert rig.orch.stop("early").code == "repeated"
    assert not rig.orch.stop_requested()
    assert rig.orch.complete_waiting_stop() is None
    assert not completions(rig)
    assert_stationary(rig)


@pytest.mark.parametrize("pause_at", ["after_committed", "listener"])
def test_late_stop_completes_in_runner_without_resending(rigs, monkeypatch, pause_at):
    rig = rigs(policy={"acquire_fails": True})
    box = {}

    def submit():
        if "stop" not in box:
            box["stop"] = concurrent_calls(lambda: rig.orch.stop("late"))[0]
            box["pending"] = rig.orch.stop_requested()

    if pause_at == "listener":
        rig.orch.listener = lambda state, info: submit() if state == "WAIT_HUMAN" else None
    else:
        rig.orch.crash_hook = lambda point: (
            submit()
            if point == "after_committed" and rig.orch.state == "WAIT_HUMAN"
            else None
        )
    found = driver(rig, monkeypatch).drive(deadline_s=2)
    assert box["stop"].code == "stop_requested" and box["pending"]
    assert found.exit_code == runner.EXIT_OK and found.state == "COMPLETED"
    assert len(completions(rig)) == 1
    assert completions(rig)[0].authority.command_id == "late"
    assert completions(rig)[0].reason == "operator_stop"
    assert rig.orch.stop("late").code == "repeated"
    assert not rig.orch.stop_requested()
    assert_stationary(rig)


def test_same_id_concurrent_stops_keep_one_intent_and_one_completion(rigs, monkeypatch):
    rig = waiting(rigs)
    with rig.orch._lock:
        answers = concurrent_calls(*(lambda: rig.orch.stop("same") for _ in range(4)))
    assert all(answer.ok and answer.code == "stop_requested" for answer in answers)
    assert sum(answer.repeated for answer in answers) == 3
    assert rig.orch._stop_gen == 1
    assert driver(rig, monkeypatch).drive(deadline_s=2).state == "COMPLETED"
    assert rig.orch.stop("same").code == "repeated"
    assert rig.orch.complete_waiting_stop() is None
    assert len(completions(rig)) == 1
    assert_stationary(rig)


@pytest.mark.parametrize("point", ["before_prepared", "after_committed"])
def test_stop_during_resume_survives_but_cannot_end_a_later_wait(rigs, point):
    rig = waiting(rigs)
    box = {}

    def hook(at):
        if at == point and "stop" not in box:
            box["stop"] = concurrent_calls(lambda: rig.orch.stop("during-resume"))[0]

    rig.orch.crash_hook = hook
    assert resume(rig).ok
    rig.orch.crash_hook = None
    assert box["stop"].code == "stop_requested"
    assert rig.orch.stop_requested()
    assert rig.orch.complete_waiting_stop() is None
    assert rig.orch.run() == "WAIT_HUMAN"
    assert not rig.orch.stop_requested()
    assert rig.orch.stop("during-resume").code == "repeated"
    assert rig.orch.complete_waiting_stop() is None
    assert not completions(rig)
    assert_stationary(rig)


def test_resume_can_override_an_earlier_wait_stop_without_reusing_its_intent(rigs):
    rig = waiting(rigs)
    with rig.orch._lock:
        assert concurrent_calls(lambda: rig.orch.stop("before-resume"))[0].ok
    assert resume(rig).ok
    assert rig.orch.complete_waiting_stop() is None
    assert not rig.orch.stop_requested()
    # The failed policy reaches a fresh wait. The previous wait's stop
    # cannot terminate it; a new command is still needed.
    assert rig.orch.run() == "WAIT_HUMAN"
    assert rig.orch.complete_waiting_stop() is None
    assert not completions(rig)
    assert rig.orch.stop("new-wait-stop").code == "completed"
    assert_stationary(rig)


def test_old_wait_listener_cannot_consume_a_stop_after_reentrant_resume(rigs):
    rig = rigs(policy={"acquire_fails": True})
    box = {}

    def listener(state, info):
        if state == "WAIT_HUMAN" and not box:
            box["entered"] = True
            box["resume"] = resume(rig)
        elif state == "PREFLIGHT" and box.get("entered"):
            box["stop"] = concurrent_calls(lambda: rig.orch.stop("new-cycle"))[0]

    rig.orch.listener = listener
    # Limit the outer loop to its preflight and first scene decision: this
    # exposes the old wait transaction's cleanup before the new safe point.
    rig.orch.run(max_decisions=2)
    rig.orch.listener = None
    assert box["resume"].ok and box["stop"].code == "stop_requested"
    assert rig.orch.state == "PREFLIGHT"
    assert rig.orch.stop_requested()
    assert rig.orch.run() == "WAIT_HUMAN"
    assert rig.orch.stop("new-cycle").code == "repeated"
    assert not completions(rig)
    assert_stationary(rig)


def test_sigterm_in_wait_is_exited_without_stop(rigs, monkeypatch):
    rig = waiting(rigs)
    drive = driver(rig, monkeypatch)
    pump = control.CommandPump(drive)
    drive.pump = pump
    recorded = []
    monkeypatch.setattr(pump, "_finish", lambda value, result: recorded.append(result))
    drive.on_signal(signal.SIGTERM, None)
    pump.tick()
    assert recorded[0]["code"] == "exited_without_stop"
    assert drive.drive(deadline_s=2).state == "WAIT_HUMAN"
    assert rig.orch._stop_gen == 0 and not rig.orch.stop_requested()
    assert not completions(rig)
    assert_stationary(rig)


def test_fault_locked_cannot_complete_a_pending_wait_stop(rigs):
    rig = waiting(rigs)
    with rig.orch._lock:
        assert concurrent_calls(lambda: rig.orch.stop("pending"))[0].ok
    rig.orch._fault("safety_stop", guard=True)
    assert rig.orch.state == "FAULT_LOCKED"
    assert rig.orch.complete_waiting_stop() is None
    assert rig.orch.stop("new-stop").code == "not_running"
    assert rig.orch.run() == "FAULT_LOCKED"
    assert not completions(rig)
    assert_stationary(rig)
