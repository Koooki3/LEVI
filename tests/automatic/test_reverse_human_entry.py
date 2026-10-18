"""Scoped human reset requests; in-process fakes only. No real services."""

import threading

import pytest
from subtask_rig import build, notes


def request(orch, command="human-reset", **over):
    scope = orch.reverse_human_scope()
    assert scope is not None
    scope.update(over)
    return orch.reverse_human_request(command, **scope)


def reverse_results(orch):
    return [
        e.episode_result
        for e in orch.journal.events
        if e.record == "committed"
        and e.episode_role == "reset"
        and e.episode_result is not None
    ]


@pytest.mark.parametrize("where", ["chunk", "step", "verify", "check"])
@pytest.mark.parametrize("home", [False, True])
def test_current_reverse_request_stops_without_a_verdict(tmp_path, where, home):
    r = build(tmp_path, home_after_operator_stop=home)
    orch = r.orch
    seen = []
    homes = []
    original_home = r.robot.home

    def home_call(**kwargs):
        homes.append(orch.ctx.role)
        return original_home(**kwargs)

    r.robot.home = home_call

    def take():
        if orch.ctx.role == "reset" and not seen:
            seen.append((orch.ctx.steps_done, request(orch)))

    original_chunk = orch._chunk

    def chunk(*args, **kwargs):
        value = original_chunk(*args, **kwargs)
        if where == "chunk":
            take()
        return value

    orch._chunk = chunk
    original_step = r.robot.step

    def step(*args, **kwargs):
        if where == "step":
            take()  # this step has already been admitted
        return original_step(*args, **kwargs)

    r.robot.step = step
    original_check = orch._check_subtask

    def check(ctx, **kwargs):  # early= since subtask.early_check
        value = original_check(ctx, **kwargs)
        if where == "check":
            take()  # at the VLM completion boundary, before window closure
        return value

    orch._check_subtask = check

    def listener(state, info):
        if where == "verify" and state == "RESET_VERIFY":
            take()

    orch.listener = listener
    assert orch.run() == "WAIT_HUMAN"
    assert len(seen) == 1 and seen[0][1].ok
    result = reverse_results(orch)[-1]
    assert result.stop_reason == "operator_stop"
    assert result.task_outcome == "unknown"
    assert result.scene_reset == "unknown"
    assert ("reset" in homes) == (home and where != "chunk")
    expected_steps = {"chunk": 0, "step": 1, "verify": 12, "check": 12}
    assert r.robot.step_calls == 24 + expected_steps[where]
    assert len(notes(orch, "reverse_human_requested")) == 1
    assert orch.reverse_human_scope() is None


def test_payload_identity_duplicates_and_stale_requests_do_not_register(tmp_path):
    r = build(tmp_path)
    orch = r.orch
    saved = []

    def listener(state, info):
        if state != "RESET_ACTIVE":
            return
        scope = orch.reverse_human_scope()
        before = (orch._stop_gen, dict(orch._stops))
        for field, value in (
            ("run_id", "wrong"),
            ("episode_id", "old"),
            ("round_no", 999),
            ("policy_epoch", 999),
            ("round_no", True),
            ("policy_epoch", True),
        ):
            assert not orch.reverse_human_request(
                "bad", **dict(scope, **{field: value})
            ).ok
            assert (orch._stop_gen, orch._stops) == before
        first = orch.reverse_human_request("human-reset", **scope)
        assert first.ok
        generation = orch._stop_gen
        assert orch.reverse_human_request("human-reset", **scope) == first
        assert not orch.reverse_human_request("human-reset", **scope, by="other").ok
        assert orch._stop_gen == generation
        saved.append((scope, first))

    orch.listener = listener
    assert orch.run() == "WAIT_HUMAN"
    scope, first = saved[0]
    before = (orch._stop_gen, dict(orch._stops))
    assert orch.reverse_human_request("human-reset", **scope) == first
    assert not orch.reverse_human_request("late", **scope).ok
    assert (orch._stop_gen, orch._stops) == before
    assert orch.state == "WAIT_HUMAN"  # never the generic stop's COMPLETED
    resumed = orch.resume(
        "human-resume",
        expected_seq=orch.journal.next_seq,
        environment_handled=True,
        health_rechecked=True,
    )
    assert resumed.ok and orch.state == "PREFLIGHT"
    assert orch.sub.phase == "idle" and orch.sub.plan == []
    assert not orch.reverse_human_request("old-after-resume", **scope).ok


@pytest.mark.parametrize("where", ["step", "seal", "check"])
def test_registration_does_not_wait_for_state_lock_or_adapter(tmp_path, where):
    r = build(tmp_path, home_after_operator_stop=False)
    orch = r.orch
    entered, release = threading.Event(), threading.Event()
    outcome = []

    def block():
        if orch.ctx.role == "reset":
            entered.set()
            assert release.wait(3)

    target, name = (
        (r.robot, "step")
        if where == "step"
        else (r.recorder, "seal")
        if where == "seal"
        else (orch, "_check_subtask")
    )
    original = getattr(target, name)

    def blocked(*args, **kwargs):
        block()
        return original(*args, **kwargs)

    setattr(target, name, blocked)
    worker = threading.Thread(target=lambda: outcome.append(orch.run()), daemon=True)
    worker.start()
    try:
        assert entered.wait(3)
        # seal runs inside _tx's state lock. This call must still return.
        reply = []
        registrar = threading.Thread(
            target=lambda: reply.append(request(orch)), daemon=True
        )
        registrar.start()
        registrar.join(1)
        assert not registrar.is_alive()
        assert reply[0].ok
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert outcome == ["WAIT_HUMAN"]
    assert reverse_results(orch)[-1].scene_reset == "unknown"


def test_failed_seal_closes_window_and_locks(tmp_path):
    r = build(tmp_path)
    orch = r.orch
    original = r.recorder.seal
    saved = []

    def seal(*args, **kwargs):
        if orch.ctx.role == "reset":
            saved.append(orch.reverse_human_scope())
            assert request(orch).ok
            raise RuntimeError("seal failed")
        return original(*args, **kwargs)

    r.recorder.seal = seal
    assert orch.run() == "FAULT_LOCKED"
    assert orch.reverse_human_scope() is None
    before = (orch._stop_gen, dict(orch._stops))
    assert not orch.reverse_human_request("after-fault", **saved[0]).ok
    assert (orch._stop_gen, orch._stops) == before


def test_closed_old_window_cannot_stop_next_reverse_episode(tmp_path):
    r = build(tmp_path)
    orch = r.orch
    saved = []

    def listener(state, info):
        if state == "RESET_ACTIVE":
            scope = orch.reverse_human_scope()
            if saved:
                before = (orch._stop_gen, dict(orch._stops))
                assert not orch.reverse_human_request("late-old", **saved[0]).ok
                assert (orch._stop_gen, orch._stops) == before
            saved.append(scope)

    orch.listener = listener
    assert orch.run() == "COMPLETED"
    assert len(saved) == 2
    assert not notes(orch, "reverse_human_requested")


@pytest.mark.parametrize("home", [False, True])
@pytest.mark.parametrize("guard", ["red", "latch"])
@pytest.mark.parametrize("when", ["check", "seal", "zero_step"])
def test_reverse_stop_safety_wins_even_when_no_home_is_due(tmp_path, home, guard, when):
    r = build(tmp_path, home_after_operator_stop=home)
    orch = r.orch
    homes = []
    original_home = r.robot.home

    def home_call(**kwargs):
        homes.append(orch.ctx.role)
        return original_home(**kwargs)

    r.robot.home = home_call

    def trip():
        if guard == "red":
            r.robot.health = lambda: {"state": "red"}
        else:
            r.robot.latch_state = lambda: True

    original_check = orch._check_subtask

    def check(ctx, **kwargs):  # early= since subtask.early_check
        value = original_check(ctx, **kwargs)
        if ctx.role == "reset" and when == "check":
            assert request(orch).ok
            trip()
        return value

    orch._check_subtask = check

    def listener(state, info):
        if state == "RESET_ACTIVE" and when == "zero_step":
            assert request(orch).ok
        if state == "RESET_FINALIZE" and when in ("seal", "zero_step"):
            if when == "seal":
                assert request(orch).ok
            else:
                assert orch.ctx.steps_done == 0
            trip()

    orch.listener = listener
    assert orch.run() == "FAULT_LOCKED"
    assert orch.fault_reason == "safety_stop"
    assert "reset" not in homes
    assert orch.reverse_human_scope() is None


def test_failed_quiescence_closes_request_window(tmp_path):
    r = build(tmp_path)
    orch = r.orch
    original = r.policy.quiesce

    def quiesce(**kwargs):
        if orch.ctx.role == "reset":
            assert request(orch).ok
            return {"ok": False}
        return original(**kwargs)

    r.policy.quiesce = quiesce
    assert orch.run() == "FAULT_LOCKED"
    assert orch.reverse_human_scope() is None


def test_resume_requires_an_independent_round_release(tmp_path):
    r = build(tmp_path, episodes=2, pose_ok=True)
    orch = r.orch

    def listener(state, info):
        if state == "RESET_ACTIVE":
            assert request(orch).ok

    orch.listener = listener
    assert orch.run() == "WAIT_HUMAN"
    acquired = len(r.policy.acquired)
    releases = []

    def refuse(round_no, total):
        releases.append((round_no, total))
        return "refused"

    r.gate.release_round = refuse
    assert orch.resume(
        "resume",
        expected_seq=orch.journal.next_seq,
        environment_handled=True,
        health_rechecked=True,
    ).ok
    assert orch.state == "PREFLIGHT" and orch.sub.phase == "idle"
    assert orch.run() == "WAIT_HUMAN"
    assert releases == [(2, 2)]
    assert len(r.policy.acquired) == acquired
