"""Review findings of the GRASP_ASSIST takeover (independent review 2026-11-16): a stop by any route ends a key
before and between its commands (H1), typed-ahead keys never run (M1), the failure paths after the hand-back are
legal transitions that close the gate again (M2), the gate is one gate end to end and an aborted run cannot be
resumed onto a closed gate (L1), a move that did not arrive holds where it is and locks after N misses (L2), the
sub-steps are paced (L3), a gripper command the client dropped is not recorded (L4), only the interval this port
left is reopened (L5), and a server pose is never paired with another getstate (L6)."""

import pytest
from assist_rig import K, build, press, session
from console_keys import (
    DeadlineKeys as Keys,  # noqa: F401  (the console tests' keyboard)
)
from subtask_rig import committed, notes
from test_console_grasp_assist import POSITION, bound
from test_subtask_grasp_assist import make

from levi.automatic import fr3_bridge as B
from levi.automatic import grasp_assist as ga
from levi.automatic import state_machine as sm


def homes(r):
    return len([t for t in r.trace if t == ("home",)])


def reasons(r):
    return committed(r.orch)


# --- H1: a stop by any route stops the next movement key --------------------------------


def test_a_page_stop_while_a_key_is_awaited_moves_nothing(tmp_path):
    r = make(tmp_path, ["Z", "a"])
    inner = r.gate.assist_key
    calls = []

    def key(view):
        calls.append(1)
        if len(calls) == 1:
            r.orch.stop("page-stop-1")  # registered while terminal D waits for a key
        return inner(view)

    r.gate.assist_key = key
    assert r.orch.run() == "WAIT_HUMAN"
    assert r.rig.env.pose_targets == []  # not one command went out
    assert reasons(r)[-1] == ("GRASP_ASSIST", "WAIT_HUMAN", "grasp_assist_aborted")
    assert homes(r) == 1 and r.robot.holds >= 1
    assert r.rig.robot.admission_snapshot().mode == "closed"
    assert not any(k == "proposal" for k, _ in r.gate.reports)


def test_a_stop_registered_after_the_proposal_is_seen_before_it_is_carried_out(
    tmp_path,
):
    r = make(tmp_path, ["x", "a"])
    # The orchestrator's own check alone (the port's per-command probe is off).
    r.rig.port.stop_probe = None
    inner = r.gate.assist_report

    def report(kind, data):
        inner(kind, data)
        if kind == "proposal" and data["kind"] == "move":
            r.orch.stop("page-stop-2")

    r.gate.assist_report = report
    assert r.orch.run() == "WAIT_HUMAN"
    assert r.rig.env.pose_targets == []


def test_a_stop_between_sub_steps_sends_no_more(tmp_path):
    r = make(tmp_path, ["x", "a"])
    r.rig.env.on_pose = lambda n: r.orch.stop("page-stop-3") if n == 2 else None
    assert r.orch.run() == "WAIT_HUMAN"
    # two sub-steps and the hold at the measured pose; the other two stayed unsent
    assert len(r.rig.env.pose_targets) == 3
    assert reasons(r)[-1][2] == "grasp_assist_aborted" and homes(r) == 1
    assert r.rig.fence.current is None


def test_the_port_checks_the_stop_before_every_command():
    stop = {"flag": False}
    r = build(stop=stop)
    s = session(r)
    r.env.on_pose = lambda n: stop.update(flag=True) if n == 3 else None
    with pytest.raises(ga.AssistStopped):
        press(s, "z")
    assert len(r.env.pose_targets) == 4  # three sub-steps and the hold
    assert r.robot.latch_state() is None and r.fence.current is None
    # A close is not sent either once a stop is registered.
    n = len(r.env.posts)
    with pytest.raises(ga.AssistStopped):
        press(s, "c")
    assert r.env.posts[n:] == [] or all(p[0] == "getstate" for p in r.env.posts[n:])


def test_a_signal_stop_of_the_robot_is_an_abort_not_a_latch():
    r = build()
    s = session(r)
    r.env.on_pose = lambda n: r.robot.emergency_hold() if n == 1 else None
    with pytest.raises(ga.AssistStopped):
        press(s, "z")
    assert len(r.env.pose_targets) == 1
    assert r.robot.latch_state() is None


def test_the_console_wait_ends_on_a_registered_stop(tmp_path):
    b = bound(
        tmp_path,
        [(POSITION, lambda: b.r.orch.stop("page-stop-4"))],
    )
    assert b.r.orch.run() == "WAIT_HUMAN"
    assert b.rig.env.pose_targets == []
    text = b.out.getvalue()
    assert "Takeover abandoned (operator_stop)" in text


# --- M1: typed-ahead keys never run -------------------------------------------------------


def test_keys_queued_before_the_prompt_are_dropped_and_an_a_among_them_aborts(tmp_path):
    b = bound(
        tmp_path,
        # "ZZZa" is already queued when the takeover has opened.
        [("GRASP_ASSIST: you take over", lambda: b.keys.buffer.extend("ZZZa"))],
    )
    assert b.r.orch.run() == "WAIT_HUMAN"
    assert b.rig.env.pose_targets == []
    assert reasons(b.r)[-1][2] == "grasp_assist_aborted"
    assert "discarded and held an a/q" in b.out.getvalue()


def test_keys_typed_while_a_move_runs_are_dropped(tmp_path):
    b = bound(
        tmp_path,
        [
            (POSITION, "x"),
            ("Done: on target", lambda: b.keys.buffer.extend("ZZZ")),
            (POSITION, "a"),
        ],
    )
    assert b.r.orch.run() == "WAIT_HUMAN"
    assert len(b.rig.env.pose_targets) == 4  # only the one move, none of ZZZ
    assert "assist_queued" not in b.out.getvalue()


def test_a_queued_a_is_acted_on_before_the_moves_behind_it(tmp_path):
    b = bound(
        tmp_path,
        [
            (POSITION, "x"),
            ("Done: on target", lambda: b.keys.buffer.extend("ZZZa")),
        ],
    )
    assert b.r.orch.run() == "WAIT_HUMAN"
    assert len(b.rig.env.pose_targets) == 4
    assert reasons(b.r)[-1][2] == "grasp_assist_aborted"


def test_a_queued_q_quits(tmp_path):
    b = bound(
        tmp_path,
        [(POSITION, "x"), ("Done: on target", lambda: b.keys.buffer.extend("Zq"))],
    )
    assert b.r.orch.run() == "WAIT_HUMAN"
    assert len(b.rig.env.pose_targets) == 4 and b.console._quit is True


# --- M2: the failure paths after the hand-back are legal and close the gate again --------


def test_a_stop_right_after_the_handback_waits_for_a_person_with_the_gate_closed(
    tmp_path,
):
    r = make(tmp_path, ["c", "u", "g"])
    inner = r.gate.assist_report

    def report(kind, data):
        inner(kind, data)
        if kind == "handback":
            r.orch.stop("page-stop-5")

    r.gate.assist_report = report
    assert r.orch.run() == "WAIT_HUMAN"  # no exception, no FAULT_LOCKED
    assert reasons(r)[-1] == ("GRASP_ASSIST", "WAIT_HUMAN", "operator_stop")
    assert not [n for n in notes(r.orch, "orchestrator_exception")]
    assert r.rig.robot.admission_snapshot().mode == "closed"
    assert r.robot.holds >= 1
    assert not any(t[0] == "acquire" and t[1].startswith("Reset:") for t in r.trace)
    assert r.rig.fence.current is None or r.rig.fence.current.kind != "policy_steps"


def test_an_unavailable_reset_policy_after_the_handback_waits_for_a_person(tmp_path):
    r = make(tmp_path, ["c", "u", "g"])
    real = r.policy.acquire

    def acquire(role, *, policy_epoch, prompt=None):
        if prompt and prompt.startswith("Reset:"):
            return {"kind": "unavailable", "code": "x", "detail": "y"}
        return real(role, policy_epoch=policy_epoch, prompt=prompt)

    r.policy.acquire = acquire
    assert r.orch.run() == "WAIT_HUMAN"
    assert reasons(r)[-1] == ("GRASP_ASSIST", "WAIT_HUMAN", "policy_error")
    assert r.rig.robot.admission_snapshot().mode == "closed"
    assert not notes(r.orch, "orchestrator_exception")


@pytest.mark.parametrize(
    "reason, who, kind, command",
    [
        ("operator_stop", "operator", "none", "c1"),
        ("operator_stop", "operator", "hold", "c1"),
        ("policy_error", "orchestrator", "none", None),
        ("policy_error", "orchestrator", "hold", None),
    ],
)
def test_the_table_has_the_waits_after_a_hand_back(reason, who, kind, command):
    sm.check_transition(
        "GRASP_ASSIST",
        "WAIT_HUMAN",
        reason=reason,
        principal_kind=who,
        command_id=command,
        action_kind=kind,
        non_idempotent=False,
        step=None,
    )
    # The operator needs a command; the orchestrator cannot stop on its own say.
    with pytest.raises(sm.TransitionRefused):
        sm.check_transition(
            "GRASP_ASSIST",
            "WAIT_HUMAN",
            reason=reason,
            principal_kind="operator" if who == "orchestrator" else "orchestrator",
            command_id=None,
            action_kind=kind,
            non_idempotent=False,
            step=None,
        )


# --- L1: one gate end to end; an aborted run cannot be resumed onto a closed gate --------


def test_the_hand_back_opens_the_one_gate_and_no_policy_step_is_refused(tmp_path):
    r = make(tmp_path, ["c", "u", "g"])
    assert r.orch.run() == "COMPLETED"
    assert r.robot.refused == []  # the fake robot reads the bridge Robot's gate
    assert r.robot.step_calls > 0
    assert r.rig.robot.admission_snapshot().mode == "open"


def test_after_an_abort_the_gate_is_closed_for_the_policy_and_resume_is_refused(
    tmp_path,
):
    r = make(tmp_path, ["x", "a"])
    assert r.orch.run() == "WAIT_HUMAN"
    assert r.robot.admission == "closed"  # the same gate the bridge Robot keeps
    found = r.orch.resume(
        "resume-1",
        expected_seq=r.orch.journal.next_seq,
        environment_handled=True,
        health_rechecked=True,
    )
    assert (found.ok, found.code) == (False, "grasp_assist_new_run_required")
    assert r.orch.state == "WAIT_HUMAN"
    assert all(".forward." in m[1] for m in r.robot.motions)  # no reset step ever ran


def test_a_fault_in_the_takeover_cannot_be_resumed_either(tmp_path):
    r = make(tmp_path, ["c", "g"])
    r.rig.env.fail_route = "close_gripper"
    assert r.orch.run() == "FAULT_LOCKED"
    found = r.orch.resume(
        "resume-2",
        expected_seq=r.orch.journal.next_seq,
        environment_handled=True,
        health_rechecked=True,
    )
    assert found.code == "grasp_assist_new_run_required"


# --- L2: a move that did not arrive holds where it is ----------------------------------


def test_a_move_that_did_not_arrive_re_commands_the_measured_pose():
    r = build()
    s = session(r)
    r.env.stuck = True
    target = s.propose(K("x")).target
    result = s.execute(s.propose(K("x")))
    assert result["ok"] is False and result["code"] == "follow_error"
    last = r.env.pose_targets[-1]
    assert max(abs(a - b) for a, b in zip(last, (0.48, -0.011, 0.236))) < 1e-4
    assert last[0] < target[0] - 0.005  # not the unreached target
    assert len(r.env.pose_targets) == ga.SUBSTEPS + 1  # one hold, in the same window
    assert r.fence.current is None and r.robot.latch_state() is None


def test_the_hold_is_never_below_the_floor():
    r = build(env_kw={"start": (0.48, -0.011, 0.0545)})
    s = session(r)
    r.env.stuck = True
    r.env.blocked_z = 0.04  # pressed through the floor by something
    s.execute(s.propose(K("z")))
    assert r.env.pose_targets[-1][2] >= 0.05325


def test_misses_in_a_row_lock_the_run_and_a_success_resets_the_count():
    r = build()  # two misses (the default)
    s = session(r)
    r.env.stuck = True
    assert press(s, "x")[1]["ok"] is False  # one
    r.env.stuck = False
    assert press(s, "X")[1]["ok"] is True  # reset
    r.env.stuck = True
    assert press(s, "x")[1]["ok"] is False  # one again
    with pytest.raises(ga.AssistFault) as caught:
        press(s, "x")  # two in a row
    assert caught.value.code == "follow_error_repeated"
    one = build(config_kw={"max_follow_misses": 1})
    t = session(one)
    one.env.stuck = True
    with pytest.raises(ga.AssistFault):
        press(t, "x")
    with pytest.raises(ValueError, match="max_follow_misses"):
        ga.GraspAssistConfig(
            enabled=True, max_follow_misses=6, holding_band=("a b|0.15|0.4",)
        )


def test_repeated_misses_lock_the_run(tmp_path):
    r = make(tmp_path, ["x", "x", "a"])
    r.rig.env.stuck = True
    assert r.orch.run() == "FAULT_LOCKED"
    assert notes(r.orch, "grasp_assist_fault")


# --- L3: the sub-steps are paced ------------------------------------------------------------


def test_the_sub_steps_are_sent_a_period_apart():
    r = build()
    s = session(r)
    press(s, "z")
    gaps = [b - a for a, b in zip(r.env.pose_times, r.env.pose_times[1:])]
    assert len(gaps) == ga.SUBSTEPS - 1
    assert all(abs(g - ga.SUBSTEP_PERIOD_S * 1e9) < 1e6 for g in gaps)
    assert ga.SUBSTEP_PERIOD_S == pytest.approx(0.1)


# --- L4: a gripper command the client dropped is not recorded --------------------------------


def test_a_command_the_client_rate_limits_is_sent_again_until_it_is_taken():
    r = build()
    s = session(r)
    press(s, "c")
    # An open straight after: the client's own limit (0.75 s) drops the first try.
    press(s, "o")
    assert r.env.last_gripper_cmd == 1.0 and s.commanded == 1.0
    assert len([p for p in r.env.posts if p[0] == "open_gripper"]) == 1


def test_a_command_that_is_never_taken_is_refused_and_not_recorded():
    r = build(env_kw={"gripper_sleep": 1000.0})
    s = session(r)
    press(s, "c")
    n = len(r.env.posts)
    with pytest.raises(ga.AssistRefused) as caught:
        press(s, "o")
    assert caught.value.code == "gripper_command_not_taken"
    assert s.commanded == 0.0 and r.env.last_gripper_cmd == 0.0
    assert [p for p in r.env.posts[n:] if p[0] == "open_gripper"] == []


def test_a_move_is_refused_while_the_clients_command_is_not_the_sessions():
    r = build()
    s = session(r)
    press(s, "c")
    r.env.last_gripper_cmd = 1.0  # the client dropped the close, the session thinks 0.0
    n = len(r.env.pose_targets)
    with pytest.raises(ga.AssistRefused) as caught:
        s.propose(K("x"))
    assert caught.value.code == "gripper_state_mismatch"
    assert len(r.env.pose_targets) == n


# --- L5: only the interval this port left is reopened -------------------------------------------


def test_the_port_reopens_the_interval_it_left_after_its_windows():
    r = build()
    r.port.begin()
    for label in ("x+", "close"):
        with r.port.window(label):
            pass
    r.port.end(reopen=True)
    assert r.robot.admission_snapshot().mode == "open"


def test_a_gate_someone_else_closed_afresh_is_not_reopened_by_the_port():
    r = build()
    r.port.begin()
    with r.port.window("x+"):
        pass
    r.robot.reopen_admission()  # somebody reopened it ...
    r.robot.stop_admission()  # ... and closed it again: another interval
    with pytest.raises(B.AdmissionRefused):
        r.port.end(reopen=True)
    assert r.robot.admission_snapshot().mode == "closed"
    # A port that never began holds no interval at all.
    other = build()
    other.robot.stop_admission()
    with pytest.raises(B.AdmissionRefused, match="no closed interval"):
        other.port.end(reopen=True)


# --- L6: a server pose is never paired with another getstate ---------------------------------------


def test_the_server_pose_comes_with_its_own_getstate_only():
    r = build()
    assert "server_pose" in r.robot.observe()

    def another_thread():
        r.env.from_xyz[0] += 0.002  # the arm moved ...
        r.env._post("getstate")  # ... and another reader's getstate came in between
        r.env.after_state = None

    r.env.after_state = another_thread
    found = r.robot.observe()
    assert "server_pose" not in found  # left out, never the wrong one
    # The session works without it (the quaternion is the Euler angles') and no fault.
    assert ga.pose_of(found).xyz[0] == pytest.approx(0.48, abs=1e-5)


# --- N1: a stop never leaves the tool pressing on its unreached setpoint ------------------


def test_a_stop_during_the_miss_settle_still_holds_at_the_measured_pose():
    r = build()
    flag = {"stop": False}
    r.port.stop_probe = lambda: flag["stop"]
    s = session(r)
    r.env.blocked_z = 0.233  # something under the tool
    inner = s._settled

    def settled(target):
        found = inner(target)
        flag["stop"] = True  # the stop lands as the settle ends
        return found

    s._settled = settled
    n0 = len(r.env.pose_targets)
    result = press(s, "Z")[1]
    assert result["ok"] is False and result["code"] == "follow_error"
    last = r.env.pose_targets[-1]
    assert len(r.env.pose_targets) == n0 + ga.SUBSTEPS + 1  # the hold was sent
    assert last[2] == pytest.approx(0.233, abs=1e-4)  # the measured pose, not 0.226
    assert r.robot.latch_state() is None and r.fence.current is None


def test_a_stop_part_way_holds_the_tool_where_it_is_not_at_the_next_setpoint():
    stop = {"flag": False}
    r = build(stop=stop)
    s = session(r)
    r.env.on_pose = lambda n: stop.update(flag=True) if n == 2 else None
    with pytest.raises(ga.AssistStopped):
        press(s, "Z")
    sent = r.env.pose_targets
    assert len(sent) == 3  # two sub-steps and the hold
    # The hold is the measured pose (the arm lags the second setpoint), not a setpoint.
    assert sent[-1] == pytest.approx(tuple(r.env.from_xyz))
    assert sent[-1][2] > sent[1][2] and sent[-1] != sent[0]
    assert r.robot.latch_state() is None


def test_a_signal_stop_sends_nothing_more_and_latches_nothing():
    r = build()
    s = session(r)
    r.env.on_pose = lambda n: r.robot.emergency_hold() if n == 2 else None
    with pytest.raises(ga.AssistStopped):
        press(s, "Z")
    assert len(r.env.pose_targets) == 2  # the bridge sends nothing under its stop flag
    assert r.robot.latch_state() is None


def test_the_hold_setpoint_is_clamped_to_the_floor_and_the_square():
    r = build()
    s = session(r)
    assert s._clip((0.0, 0.0, 0.01))[2] == pytest.approx(0.05325)
    assert s._clip((5.0, -5.0, 0.2)) == (0.85, -0.85, 0.2)


# --- N3: after a hand-back that waits for a person the text says a new run is needed -------


def test_every_wait_out_of_the_takeover_says_a_new_run_is_needed(tmp_path):
    from levi.automatic import console as C
    from levi.automatic import console_text as T

    r = make(tmp_path / "a", ["c", "u", "g"])
    inner = r.gate.assist_report

    def report(kind, data):
        inner(kind, data)
        if kind == "handback":
            r.orch.stop("page-stop-n3")

    r.gate.assist_report = report
    assert r.orch.run() == "WAIT_HUMAN"
    assert reasons(r)[-1][2] == "operator_stop"  # the journal's own reason
    unavailable = make(tmp_path / "b", ["c", "u", "g"])
    real = unavailable.policy.acquire
    unavailable.policy.acquire = lambda role, *, policy_epoch, prompt=None: (
        {"kind": "unavailable", "code": "x", "detail": "y"}
        if prompt and prompt.startswith("Reset:")
        else real(role, policy_epoch=policy_epoch, prompt=prompt)
    )
    assert unavailable.orch.run() == "WAIT_HUMAN"
    aborted = make(tmp_path / "c", ["x", "a"])
    assert aborted.orch.run() == "WAIT_HUMAN"
    for run in (r, unavailable, aborted):
        reason = C.wait_reason(run.orch)
        assert reason == "grasp_assist_aborted"
        _, todo = C.WAIT_REASONS[reason]
        assert "cannot go on" in T.Texts("en")(todo)
        assert "不能续跑" in T.Texts("zh")(todo)
    # Another wait keeps its own text (an ordinary stop in a forward loop).
    plain = make(tmp_path / "d", [], assist=False)
    plain.orch.stop("early-stop")
    plain.orch.run()
    assert C.wait_reason(plain.orch) != "grasp_assist_aborted"
