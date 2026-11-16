"""The admission gate of the GRASP_ASSIST takeover (``Robot.assist_under_closed_gate``), modelled on the home-only
window: the policy gate stays closed, exactly four routes pass, only for the key's own ``assist`` token and only
from the thread that opened the window; a gripper command is never a bare POST (no move_gripper)."""

import threading

import numpy as np
import pytest
from assist_rig import EULER, START, build

from levi.automatic import fr3_assist
from levi.automatic import fr3_bridge as B
from levi.automatic import grasp_assist as ga
from levi.automatic import state_machine as sm

HOLD = [*START, *EULER, 1.0]


def assist_token(r, *, kind="assist", ttl_s=60):
    return r.fence.issue(
        sm.MotionToken(
            run_id="r",
            transaction_id=f"tx-{kind}",
            control_epoch=1,
            kind=kind,
            episode_id="r.reset.0001",
            policy_epoch=1,
            expires_mono_ns=r.clock.now() + int(ttl_s * 1e9),
        )
    )


def closed(r):
    r.robot.stop_admission()
    return r


def routes(r):
    return [route for route, _ in r.env.posts if route != "getstate"]


def test_the_window_opens_only_from_a_closed_gate():
    r = build()
    token = assist_token(r)
    with (
        pytest.raises(B.AdmissionRefused, match="not closed"),
        r.robot.assist_under_closed_gate(token=token),
    ):
        pass
    assert routes(r) == []


@pytest.mark.parametrize(
    "bad",
    ["policy_steps", "home"],
)
def test_only_an_assist_token_opens_it(bad):
    r = closed(build())
    token = assist_token(r, kind=bad)
    with (
        pytest.raises(B.AdmissionRefused, match="not an assist token"),
        r.robot.assist_under_closed_gate(token=token),
    ):
        pass
    assert r.robot.admission_snapshot().mode == "closed"


def test_a_stale_stopped_or_latched_robot_does_not_open_it():
    r = closed(build())
    token = assist_token(r)
    r.fence.revoke()  # a later transaction revoked it
    with (
        pytest.raises(B.AdmissionRefused, match="entry refused"),
        r.robot.assist_under_closed_gate(token=token),
    ):
        pass
    r = closed(build())
    token = assist_token(r)
    r.robot.emergency_hold()
    with (
        pytest.raises(B.AdmissionRefused, match="operator stop"),
        r.robot.assist_under_closed_gate(token=token),
    ):
        pass
    r = closed(build())
    token = assist_token(r)
    r.robot.latched = "latched"
    with (
        pytest.raises(B.AdmissionRefused, match="latch"),
        r.robot.assist_under_closed_gate(token=token),
    ):
        pass
    r = closed(build())
    token = assist_token(r, ttl_s=1)
    r.clock.advance(2_000_000_000)
    with (
        pytest.raises(B.AdmissionRefused, match="expired"),
        r.robot.assist_under_closed_gate(token=token),
    ):
        pass


def test_a_step_inside_the_window_passes_and_the_gate_closes_after_it():
    r = closed(build())
    before = r.robot.admission_snapshot()
    token = assist_token(r)
    with r.robot.assist_under_closed_gate(token=token) as step:
        assert r.robot.admission_snapshot().mode == B.GATE_ASSIST_ONLY
        result = step(np.array(HOLD))
    assert result.executed == "yes"
    assert routes(r) == ["clearerr", "pose"]
    after = r.robot.admission_snapshot()
    assert after.mode == "closed" and after.generation > before.generation
    assert (after.assist_pose_released, after.assist_gripper_released) == (2, 0)
    assert after.assist_gate_rejected == 0
    assert r.robot.held is True
    # The policy's own counters did not move: the window has its own.
    assert (after.pose_attempts, after.gripper_attempts) == (
        before.pose_attempts,
        before.gripper_attempts,
    )


def run_route_in_step(r, extra):
    """One assist step whose env also POSTs ``extra`` (what a rewritten client could do)."""
    original = r.env.step

    def step(action):
        result = original(action)
        r.env._post(extra)
        return result

    r.env.step = step
    token = assist_token(r)
    with r.robot.assist_under_closed_gate(token=token) as do:
        return do(np.array(HOLD))


@pytest.mark.parametrize("route", ["open_gripper", "close_gripper"])
def test_the_gripper_routes_pass_inside_the_send_path(route):
    r = closed(build())
    # The routes the gate lets through; the reply text is the server's own.
    original = r.env.step

    def step(action):
        result = original(action)
        result["gripper_response"] = {
            "command": route,
            "response": r.env._post(route),
        }
        return result

    r.env.step = step
    token = assist_token(r)
    with r.robot.assist_under_closed_gate(token=token) as do:
        found = do(np.array(HOLD))
    assert found.executed == "yes"
    assert route in routes(r)


@pytest.mark.parametrize("route", ["move_gripper", "close_gripper_slow"])
def test_move_gripper_and_the_slow_close_are_never_admitted(route):
    r = closed(build())
    found = run_route_in_step(r, route)
    assert found.executed in ("no", "unknown")
    assert route not in routes(r)  # refused locally, never reached the server
    assert r.robot.admission_snapshot().assist_gate_rejected >= 1


def test_a_route_outside_the_send_path_is_refused_even_if_it_is_on_the_list():
    r = closed(build())
    token = assist_token(r)
    with r.robot.assist_under_closed_gate(token=token):
        for route in (
            "pose",
            "clearerr",
            "open_gripper",
            "close_gripper",
            "move_gripper",
        ):
            with pytest.raises(B.AdmissionRefused):
                r.env._post(route)
    assert routes(r) == []


def test_another_thread_cannot_use_the_window():
    r = closed(build())
    token = assist_token(r)
    seen = {}
    with r.robot.assist_under_closed_gate(token=token) as step:

        def other():
            seen["result"] = step(np.array(HOLD))

        thread = threading.Thread(target=other)
        thread.start()
        thread.join()
    assert seen["result"].executed == "no"
    assert routes(r) == []


def test_another_token_cannot_use_the_window():
    r = closed(build())
    token = assist_token(r)
    with r.robot.assist_under_closed_gate(token=token):
        other = sm.MotionToken(
            run_id="r",
            transaction_id="tx-other",
            control_epoch=1,
            kind="assist",
            episode_id="r.reset.0001",
            policy_epoch=1,
            expires_mono_ns=r.clock.now() + 10**12,
        )
        result = r.robot._assist_step(np.array(HOLD), other)
    assert result.executed == "no" and routes(r) == []


def test_a_policy_step_and_a_home_stay_refused_while_the_window_is_open():
    r = closed(build())
    token = assist_token(r)
    policy = sm.MotionToken(
        run_id="r",
        transaction_id="tx-policy",
        control_epoch=1,
        kind="policy_steps",
        episode_id="r.reset.0001",
        policy_epoch=1,
        expires_mono_ns=r.clock.now() + 10**12,
    )
    with r.robot.assist_under_closed_gate(token=token):
        assert r.robot.step(np.array(HOLD), token=policy).executed == "no"
        home = r.robot.home_under_closed_gate(token=token)
        assert home.executed == "no"
        with pytest.raises(B.AdmissionRefused, match="assist-only window open"):
            r.robot.reopen_admission()
        snap = r.robot.admission_snapshot()
        with pytest.raises(B.AdmissionRefused, match="assist-only window open"):
            r.robot.reopen_admission_if(
                generation=snap.generation, closed_from_ns=snap.closed_from_ns or 0
            )
    assert routes(r) == []


def test_nested_windows_are_refused_and_do_not_end_the_first():
    r = closed(build())
    token = assist_token(r)
    with r.robot.assist_under_closed_gate(token=token) as step:
        with (
            pytest.raises(B.AdmissionRefused, match="already open"),
            r.robot.assist_under_closed_gate(token=token),
        ):
            pass
        assert r.robot.admission_snapshot().mode == B.GATE_ASSIST_ONLY
        assert step(np.array(HOLD)).executed == "yes"


def test_after_the_window_the_old_token_moves_nothing():
    r = closed(build())
    token = assist_token(r)
    with r.robot.assist_under_closed_gate(token=token) as step:
        pass
    assert step(np.array(HOLD)).executed == "no"
    assert routes(r) == []


def test_the_gate_returns_to_closed_even_when_the_body_raises():
    r = closed(build())
    token = assist_token(r)
    with (
        pytest.raises(RuntimeError, match="boom"),
        r.robot.assist_under_closed_gate(token=token),
    ):
        raise RuntimeError("boom")
    snap = r.robot.admission_snapshot()
    assert snap.mode == "closed" and snap.closed_from_ns is not None


def test_an_unacknowledged_reply_latches_the_robot():
    r = closed(build())
    r.env.fail_route = "pose"
    token = assist_token(r)
    with r.robot.assist_under_closed_gate(token=token) as step:
        found = step(np.array(HOLD))
    assert found.executed == "unknown" and r.robot.latch_state()


# --- the port: one token per key, with an expiry; the gate reopens only for a hand-back ---


def test_the_port_issues_one_token_per_key_and_revokes_it():
    r = build()
    r.port.begin()
    seen = []
    for label in ("x+", "x-", "close"):
        with r.port.window(label):
            seen.append(r.fence.current)
            assert r.fence.current.kind == "assist"
        assert r.fence.current is None
    assert len({t.transaction_id for t in seen}) == 3
    assert all(t.expires_mono_ns > r.clock.now() for t in seen)


def test_a_key_that_outlives_its_token_is_a_fault_not_a_motion():
    r = build(token_s=0.5)
    r.port.begin()
    with pytest.raises(ga.AssistFault) as caught, r.port.window("x+"):
        r.clock.advance(1_000_000_000)
        r.port.pose_step(START, EULER, 1.0)
    assert caught.value.code == "command_failed"
    assert r.env.pose_targets == []


def test_the_port_reopens_the_gate_only_as_the_closed_interval_it_left():
    r = build()
    r.port.begin()
    assert r.robot.admission_snapshot().mode == "closed"
    r.port.end(reopen=False)
    assert r.robot.admission_snapshot().mode == "closed"
    r.port.end(reopen=True)
    assert r.robot.admission_snapshot().mode == "open"
    with pytest.raises(B.AdmissionRefused):
        r.port.end(reopen=True)  # already open: not a closed interval


def test_the_port_needs_a_bridge_robot():
    r = build()
    with pytest.raises(TypeError):
        fr3_assist.Fr3AssistPort(
            object(),
            r.fence,
            r.clock,
            run_id="r",
            episode_id=lambda: "e",
            control_epoch=lambda: 1,
            policy_epoch=lambda: 1,
        )


def test_a_gripper_command_goes_through_the_clients_step_not_a_bare_post():
    r = build()
    r.port.begin()
    with r.port.window("close"):
        r.port.gripper_step(0.0, START, EULER)
    assert [route for route in routes(r)] == ["clearerr", "pose", "close_gripper"]
    assert r.env.last_gripper_cmd == 0.0  # the policy's state[6] now reports the close
    with pytest.raises(ga.AssistFault), r.port.window("x"):
        r.port.gripper_step(0.5, START, EULER)
