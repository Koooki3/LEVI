"""A measured gripper opening a hair outside [0, 1] is the same opening
(RELAXED[2026-11-17]; was: exactly [0, 1]). Fake env and clock, no I/O."""

import math

import numpy as np
import pytest
from fr3_real_support import HOME, build, step_with, token
from test_fr3_admission_proof import Rig as AdmissionRig

from levi.automatic import fr3_bridge as B
from levi.automatic import grasp_assist as ga


@pytest.mark.parametrize(
    "value,expected",
    [
        (-0.0002, 0.0),
        (-0.02, 0.0),
        (0.0, 0.0),
        (0.37, 0.37),
        (1.0, 1.0),
        (1.0004, 1.0),
        (1.02, 1.0),
    ],
)
def test_clip_gripper_reading_clips_within_the_margin(value, expected):
    assert B.clip_gripper_reading(value) == expected


@pytest.mark.parametrize(
    "value",
    [-0.0201, 1.0201, -1.0, 1.1, math.nan, math.inf, -math.inf, None, "1", True],
)
def test_clip_gripper_reading_rejects_the_rest(value):
    assert B.clip_gripper_reading(value) is None


@pytest.mark.parametrize(
    "route,reply,pos",
    [
        ("open_gripper", "open gripper goal already in flight", 1.0004),
        ("close_gripper", "close gripper goal already in flight", -0.0002),
    ],
)
def test_a_queued_reply_is_confirmed_by_a_reading_a_hair_outside_the_range(
    route, reply, pos
):
    r = build(gripper_confirm_timeout_s=2.0)
    out = step_with(r, route, reply, grip=lambda since: pos)
    assert out.executed == "yes" and r.robot.latched is None
    nxt = r.robot.step(np.array([*HOME, 1.0]), token=token(r, "policy_steps"))
    assert nxt.executed == "yes" and r.robot.latched is None
    assert "gripper_reply_confirmed" in dict(nxt.notes)


@pytest.mark.parametrize("position", [-0.03, 1.03, math.nan, math.inf])
def test_a_reading_beyond_the_margin_or_not_finite_still_locks(position):
    r = build()
    step_with(
        r,
        "open_gripper",
        "open gripper goal already in flight",
        grip=lambda since: position,
    )
    r.robot.hold()
    with pytest.raises(ValueError, match="invalid measured gripper"):
        r.robot.confirm_pending_gripper()
    assert r.robot.latched


@pytest.mark.parametrize(
    "grip,unmet",
    [
        (1.0004, []),
        (1.02, []),
        (1.03, ["gripper_invalid"]),
        (-0.03, ["gripper_invalid"]),
        (math.nan, ["gripper_invalid"]),
        (0.3, ["gripper_not_open"]),
    ],
)
def test_start_pose_reads_a_hair_outside_range_as_open(grip, unmet):
    r = build()
    r.env.grip_fn = lambda since: grip
    result = r.robot.start_pose()
    assert result["unmet"] == unmet and result["ok"] is (not unmet)


def test_home_is_verified_with_a_reading_a_hair_outside_the_range():
    r = build()
    r.env.grip_fn = lambda since: 1.0004
    assert r.robot._home_within(
        r.robot._home_reading(r.robot.client._resolve_home(r.robot.args))
    )
    r.env.grip_fn = lambda since: 1.05
    assert not r.robot._home_within(
        r.robot._home_reading(r.robot.client._resolve_home(r.robot.args))
    )


def _observed(reading):
    rig = AdmissionRig()
    env = rig.env
    original = env.get_observation

    def post(route, payload=None):
        env.http.append(route)
        return {"ok": True, "gripper_pos": reading} if route == "getstate" else {}

    def get_observation():
        env._post("getstate")
        found = original()
        found["robot_state"]["gripper_position"] = reading
        return found

    env._post = post
    env.get_observation = get_observation
    rig.robot.__init__(  # re-install the bridge's hooks over the patched fake
        env,
        rig.robot.guard,
        rig.robot.client,
        rig.robot.args,
        rig.clock,
        rig.fence,
        tolerance_mm=5,
    )
    return rig.robot.observe()


def test_observe_hands_on_one_clipped_number_for_state_and_proof():
    found = _observed(-0.0002)
    assert found["robot_state"]["gripper_position"] == 0.0
    assert found["gripper_measurement"]["value"] == 0.0
    assert found["raw"]["robot_state"]["gripper_position"] == -0.0002  # untouched


def test_observe_keeps_a_valid_reading_as_it_is():
    found = _observed(0.42)
    assert found["robot_state"]["gripper_position"] == 0.42
    assert found["gripper_measurement"]["value"] == 0.42


@pytest.mark.parametrize("reading", [-0.05, 1.05, math.nan])
def test_observe_gives_no_proof_for_an_invalid_reading(reading):
    found = _observed(reading)
    assert found["gripper_measurement"] is None


def _assist_observation(cmd, opening):
    pose = [0.4, 0.0, 0.2, 0.0, 0.0, 0.0, cmd]
    return {
        "pose": pose,
        "state_fresh": True,
        "robot_state": {"gripper_position": opening},
    }


@pytest.mark.parametrize("cmd,opening", [(-0.0002, 1.0004), (1.0003, -0.0001)])
def test_takeover_pose_clips_command_and_opening_a_hair_outside(cmd, opening):
    pose = ga.pose_of(_assist_observation(cmd, opening))
    assert 0.0 <= pose.gripper_cmd <= 1.0 and pose.opening in (0.0, 1.0)


@pytest.mark.parametrize("cmd", [-0.03, 1.03, math.nan])
def test_takeover_pose_still_refuses_a_command_beyond_the_margin(cmd):
    with pytest.raises(ga.AssistFault, match="observation_invalid|is not 0..1"):
        ga.pose_of(_assist_observation(cmd, 1.0))


def test_takeover_pose_drops_an_opening_beyond_the_margin():
    assert ga.pose_of(_assist_observation(1.0, 1.05)).opening is None
