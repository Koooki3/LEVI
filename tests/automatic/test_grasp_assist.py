"""The GRASP_ASSIST takeover's session on the bridge Robot over an env that models fr3_env (no I/O, no robot):
one refusal test per hard limit, the orientation lock and the quaternion order, the holding facts after close and
lift, the hand-back's state[6] (the command, never the measured opening), the planned holding_fact hand-over
(code path only: refused in jobs until calibration) and a refusal that sends nothing."""

import dataclasses
import math

import pytest
from assist_rig import (
    EULER,
    HELD,
    START,
    K,
    build,
    matrix_to_quat_xyzw,
    press,
    rot,
    session,
)

from levi.automatic import grasp_assist as ga
from levi.automatic import holding as hd
from levi.domain import aeri


def posts(r, route="pose"):
    return [p for p in r.env.posts if p[0] == route]


ENTRY_QUAT = matrix_to_quat_xyzw(rot(*EULER))


# --- orientation: the server quaternion is (qx, qy, qz, qw) ----------------------------


@pytest.mark.parametrize(
    "euler",
    [
        (0.0, 0.0, 0.0),
        (math.pi / 2, 0.0, 0.0),
        (0.0, 0.0, math.pi / 2),
        (0.3, -0.4, 0.5),
        EULER,
        (-3.1, 0.9, 2.2),
    ],
)
def test_euler_and_quaternion_agree_with_an_independent_oracle(euler):
    q = ga.euler_to_quat(*euler)
    oracle = matrix_to_quat_xyzw(rot(*euler))
    assert ga.quat_angle(q, oracle) < 1e-6
    back = ga.quat_to_euler(q)
    assert ga.quat_angle(ga.euler_to_quat(*back), q) < 1e-6


def test_the_quaternion_has_its_scalar_last():
    # 90 degrees about z: (qx, qy, qz, qw) = (0, 0, sin 45, cos 45).
    s = math.sin(math.pi / 4)
    assert ga.euler_to_quat(0, 0, math.pi / 2) == pytest.approx((0, 0, s, s))
    assert ga.quat_to_euler((0, 0, s, s)) == pytest.approx((0, 0, math.pi / 2))
    # Read with the scalar first the same four numbers are another rotation.
    wrong = ga.quat_to_euler((s, s, 0, 0))
    assert wrong != pytest.approx((0, 0, math.pi / 2))


def obs(r):
    return r.robot.observe()


def test_the_entry_orientation_is_the_measured_server_quaternion():
    r = build()
    s = session(r)
    assert ga.quat_angle(s.quat, ENTRY_QUAT) < 1e-6
    assert s.euler == pytest.approx(EULER, abs=1e-5)
    assert "server_pose" in obs(r)  # the bridge hands over the server's own pose


def test_a_pose_whose_quaternion_is_read_in_the_wrong_order_is_a_fault():
    r = build()
    o = obs(r)
    x, y, z, qx, qy, qz, qw = o["server_pose"]
    o["server_pose"] = [x, y, z, qw, qx, qy, qz]  # w first: a misreading
    with pytest.raises(ga.AssistFault) as caught:
        ga.pose_of(o)
    assert caught.value.code == "pose_inconsistent"
    # In the right order it is accepted and its quaternion is the entry's.
    assert ga.quat_angle(ga.pose_of(obs(r)).quat, ENTRY_QUAT) < 1e-6


def test_every_pose_post_carries_the_entry_orientation():
    r = build()
    s = session(r)
    for key in ("x", "Y", "z", "Z"):
        press(s, key)
    assert posts(r), "no pose was sent"
    for _, payload in posts(r):
        assert ga.quat_angle(payload["arr"][3:7], ENTRY_QUAT) < 1e-5


# --- one refusal test per hard limit; a refusal sends nothing and latches nothing -----


def assert_sent_nothing(r, before):
    assert r.env.posts[before:] == [] or all(
        route == "getstate" for route, _ in r.env.posts[before:]
    )
    assert r.robot.latch_state() is None


def test_the_z_floor_refuses_a_key_below_it():
    r = build(env_kw={"start": (START[0], START[1], 0.06)})
    s = session(r)
    n = len(r.env.posts)
    with pytest.raises(ga.AssistRefused) as caught:
        s.propose(K("Z"))  # 0.06 - 0.01 = 0.05 < 0.05325
    assert caught.value.code == "z_below_floor"
    assert_sent_nothing(r, n)
    # The floor is the binding's: above it the same key is fine.
    r2 = build(env_kw={"start": (START[0], START[1], 0.07)})
    assert session(r2).propose("z-").target[2] == pytest.approx(0.06)


def test_the_floor_is_checked_on_the_target_not_only_on_the_start():
    r = build(env_kw={"start": (START[0], START[1], 0.0635)})
    s = session(r)
    assert s.propose(K("Z")).checks["z_floor_ok"] is True  # 0.0535 >= 0.05325
    press(s, "Z")
    with pytest.raises(ga.AssistRefused) as caught:
        s.propose(K("Z"))
    assert caught.value.code == "z_below_floor"


def test_xy_outside_the_binding_workspace_is_refused():
    r = build(limits=ga.Limits(0.05325, 0.49))
    s = session(r)
    press(s, "x")  # 0.48 -> 0.49: on the edge, inside
    n = len(r.env.posts)
    with pytest.raises(ga.AssistRefused) as caught:
        s.propose(K("x"))
    assert caught.value.code == "xy_outside_workspace"
    assert_sent_nothing(r, n)
    r = build(limits=ga.Limits(0.05325, 0.49), env_kw={"start": (0.0, 0.485, 0.2)})
    with pytest.raises(ga.AssistRefused) as caught:
        session(r).propose(K("y"))  # 0.495 > 0.49
    assert caught.value.code == "xy_outside_workspace"


def test_the_total_travel_from_the_entry_point_is_limited():
    # follow_error_m pinned to the old 5 mm: with the relaxed 8 mm default the
    # first move counts as settled after 3 mm, and this test is about travel.
    r = build(config_kw={"max_travel_m": 0.015, "follow_error_m": 0.005})
    s = session(r)
    press(s, "x")
    n = len(r.env.posts)
    with pytest.raises(ga.AssistRefused) as caught:
        s.propose(K("x"))  # 0.02 m from the entry
    assert caught.value.code == "travel_exceeded"
    assert_sent_nothing(r, n)
    # Going back towards the entry is inside the limit.
    assert s.propose(K("X")).checks["travel_ok"] is True
    # Travel is the distance to the entry, not the path length: out and back is free.
    press(s, "X")
    press(s, "x")
    press(s, "X")


def test_the_key_count_is_limited_and_only_executed_keys_count():
    r = build(config_kw={"max_steps": 2})
    s = session(r)
    press(s, "x")
    with pytest.raises(ga.AssistRefused):
        s.propose("nope")  # an unknown key is refused and costs nothing
    press(s, "X")
    n = len(r.env.posts)
    with pytest.raises(ga.AssistRefused) as caught:
        s.propose(K("x"))
    assert caught.value.code == "max_steps_reached"
    assert_sent_nothing(r, n)
    # Deciding keys are never blocked by the budget.
    assert s.propose("abort").kind == "abort"


def test_a_step_above_two_centimetres_is_refused_whatever_the_config():
    r = build()
    s = session(r)
    n = len(r.env.posts)
    with pytest.raises(ga.AssistRefused) as caught:
        s._propose_move("x+", "move", 0, 0.03)
    assert caught.value.code == "step_too_large"
    assert_sent_nothing(r, n)
    with pytest.raises(ValueError, match="step_m"):
        ga.GraspAssistConfig(enabled=True, step_m=0.03, holding_band=("a b|0.15|0.4",))
    assert ga.MAX_STEP_M == 0.02 == aeri.GRASP_ASSIST_MAX_STEP_M


def test_each_move_is_split_into_small_sub_steps_towards_the_shown_target():
    r = build()
    s = session(r)
    p = s.propose(K("x"))  # shown before anything is sent
    assert posts(r) == []
    assert p.target == pytest.approx((START[0] + 0.01, START[1], START[2]))
    result = s.execute(p)
    sent = [t for t in r.env.pose_targets]
    assert len(sent) == ga.SUBSTEPS
    steps = [b[0] - a[0] for a, b in zip([START, *sent], sent)]
    assert steps == pytest.approx([0.01 / ga.SUBSTEPS] * ga.SUBSTEPS, abs=1e-6)
    assert max(abs(v) for v in steps) <= 0.005
    assert sent[-1] == pytest.approx(p.target)
    assert (
        result["ok"] is True and result["follow_error_m"] <= ga.DEFAULT_FOLLOW_ERROR_M
    )


def test_a_move_that_does_not_arrive_is_reported_held_and_not_latched():
    r = build()
    s = session(r)
    r.env.stuck = True
    result = press(s, "x")[1]
    assert result["ok"] is False and result["code"] == "follow_error"
    assert r.robot.latch_state() is None
    assert r.robot.held is True  # the window ends with a hold
    assert r.fence.current is None  # and no token is left


def test_the_follow_error_limit_is_the_jobs():
    tight = build(config_kw={"follow_error_m": 0.002})
    tight.env.short_x = 0.003  # settles 3 mm short
    result = press(session(tight), "z")[1]
    assert result["ok"] is False and result["code"] == "follow_error"
    assert result["follow_error_m"] == pytest.approx(0.003, abs=1e-5)
    default = build()  # 8 mm (RELAXED[2026-11-17], was 5 mm)
    default.env.short_x = 0.003
    assert press(session(default), "z")[1]["ok"] is True
    assert ga.DEFAULT_FOLLOW_ERROR_M == 0.008 and ga.SETTLE_S == 5.0
    assert ga.DEFAULT_MAX_FOLLOW_MISSES == 3
    seven = build()
    seven.env.short_x = 0.007  # inside 8 mm, was a miss at 5 mm
    assert press(session(seven), "z")[1]["ok"] is True
    nine = build()
    nine.env.short_x = 0.009  # still a miss
    assert press(session(nine), "z")[1]["ok"] is False
    assert tight.robot.latch_state() is None and default.robot.latch_state() is None


# --- the holding facts ----------------------------------------------------------------


def test_close_gives_a_measured_candidate_with_the_command_in_state6():
    r = build()
    s = session(r)
    _, result = press(s, "c")
    fact = s.last_fact
    assert (fact.status, fact.stage) == ("holding_candidate", "close")
    assert fact.source == hd.SOURCE_VERIFIED and fact.is_real
    assert fact.readings[-1].gripper == pytest.approx(HELD, abs=1e-6)
    assert result["fact"] == "holding_candidate"
    # state[6] is the COMMAND (closed: 0.0), the measured opening is another number.
    o = ga.pose_of(obs(r))
    assert o.gripper_cmd == 0.0 and o.opening == pytest.approx(HELD, abs=1e-6)
    # The close went through the client's own step: never a bare close POST.
    assert [p for p in posts(r, "close_gripper")] != []


def test_lift_re_measures_the_candidate_and_verifies_after_the_rise():
    r = build()
    s = session(r)
    press(s, "c")
    z0 = ga.pose_of(obs(r)).xyz[2]
    _, result = press(s, "u")
    assert s.last_fact.status == "holding_verified" and s.last_fact.stage == "lift"
    assert s.last_fact.holding_verified is True and s.last_fact.is_real
    assert s.last_fact.lift_m >= 0.01
    # A move counts as settled within follow_error_m (8 mm by default).
    assert ga.pose_of(obs(r)).xyz[2] - z0 == pytest.approx(0.02, abs=0.008)
    assert result["ok"] is True and result["fact"] == "holding_verified"
    kinds = [code for code, _ in s.events]
    assert kinds.count("holding_fact") >= 3  # close, the lift's own candidate, lift


def test_lift_without_a_close_or_on_an_empty_gripper_moves_nothing():
    r = build()
    s = session(r)
    with pytest.raises(ga.AssistRefused) as caught:
        s.propose(K("u"))
    assert caught.value.code == "lift_needs_close"
    r = build(env_kw={"object_opening": 0.01})
    s = session(r)
    press(s, "c")
    assert s.last_fact.status == "empty"
    n = len(posts(r))
    p = s.propose(K("u"))
    with pytest.raises(ga.AssistRefused) as caught:
        s.execute(p)
    assert caught.value.code == "lift_needs_holding_candidate"
    assert len(posts(r)) == n  # nothing was lifted


def test_the_measured_opening_is_shown_and_the_fact_status_with_it():
    r = build()
    s = session(r)
    view = s.view()
    assert view["opening"] == pytest.approx(1.0) and view["fact"] is None
    assert view["gripper_cmd"] == 1.0
    press(s, "c")
    view = s.view()
    assert view["opening"] == pytest.approx(HELD, abs=1e-6)
    assert view["fact"] == "holding_candidate" and view["gripper_cmd"] == 0.0
    assert view["steps"] == 1 and view["max_steps"] == 40


# --- the hand-back: g is refused without a positive fact; state[6] is the command -------


@pytest.mark.parametrize(
    "setup, code",
    [
        ([], "no_holding_fact"),
        (["c"], None),  # a candidate: allowed
    ],
)
def test_handback_needs_a_fact(setup, code):
    r = build()
    s = session(r)
    for key in setup:
        press(s, key)
    if code is None:
        assert s.propose("handback").kind == "handback"
    else:
        with pytest.raises(ga.AssistRefused) as caught:
            s.propose("handback")
        assert caught.value.code == code


def test_g_is_refused_on_an_empty_gripper():
    r = build(env_kw={"object_opening": 0.01})
    s = session(r)
    press(s, "c")
    with pytest.raises(ga.AssistRefused) as caught:
        s.propose("handback")
    assert caught.value.code == "holding_fact_empty"
    # A person can still abort.
    assert s.propose("abort").kind == "abort"


def test_g_is_refused_on_an_open_gripper():
    r = build()
    s = session(r)
    press(s, "c")
    press(s, "o")
    assert s.last_fact.status == "open"
    with pytest.raises(ga.AssistRefused) as caught:
        s.propose("handback")
    assert caught.value.code == "holding_fact_open"


def test_g_is_refused_on_a_fact_nobody_could_prove():
    r = build(env_kw={"object_opening": 0.6})  # between the band and open
    s = session(r)
    press(s, "c")
    assert (s.last_fact.status, s.last_fact.reason) == (
        "unknown",
        "between_band_and_open",
    )
    with pytest.raises(ga.AssistRefused) as caught:
        s.propose("handback")
    assert caught.value.code == "holding_fact_unproven"


def test_g_re_measures_at_the_key_and_refuses_if_the_object_is_gone():
    r = build()
    s = session(r)
    press(s, "c")
    r.env.object_opening = 0.01
    r.env._grip(0.01)  # the object slipped out after the fact
    with pytest.raises(ga.AssistRefused) as caught:
        s.propose("handback")
    assert caught.value.code == "holding_fact_empty"


def test_the_policy_state6_of_the_handback_is_the_command_not_the_measured_opening():
    r = build()
    s = session(r)
    press(s, "c")
    press(s, "u")
    proposal = s.propose("handback")
    record = s.finalize_handback(proposal)
    assert record["policy_state6"] == 0.0
    assert record["opening"] == pytest.approx(HELD, abs=1e-6)
    assert record["policy_state6"] != record["opening"]
    assert record["env_reset"] is False and record["homed"] is False
    assert record["by"] == "operator" and record["fact_status"] == "holding_candidate"


def test_a_state6_that_is_not_the_issued_command_refuses_the_handback():
    r = build()
    s = session(r)
    press(s, "c")
    r.env.last_gripper_cmd = 1.0  # the client never recorded the close
    with pytest.raises(ga.AssistRefused) as caught:
        s.propose("handback")
    assert caught.value.code == "handback_state_mismatch"


def test_handback_sends_no_motion_and_does_not_home():
    r = build()
    s = session(r)
    press(s, "c")
    n = len(posts(r))
    s.finalize_handback(s.propose("handback"))
    assert len(posts(r)) == n
    assert not any(route in ("jointreset", "startimp") for route, _ in r.env.posts)


# --- the planned holding_fact hand-over: code path in place, refused in jobs ------------


@pytest.fixture
def auto(monkeypatch):
    monkeypatch.setattr(aeri, "UNENABLED_HANDOFFS", ())
    return build(config_kw={"handoff": "holding_fact"})


def test_the_holding_fact_handoff_needs_a_verified_measured_fact(auto):
    s = session(auto)
    press(s, "c")
    assert s.auto_ready() is False  # a candidate is not enough
    with pytest.raises(ga.AssistRefused) as caught:
        s.propose("handback")
    assert caught.value.code == "holding_not_verified"
    press(s, "u")
    assert s.auto_ready() is True
    proposal = s.propose("handback")
    assert s.handback_record(proposal)["by"] == "holding_fact"


def test_the_holding_fact_handoff_refuses_a_fact_that_is_not_a_verified_measurement(
    auto,
):
    s = session(auto)
    press(s, "c")
    press(s, "u")
    assert s.auto_ready() is True
    s.last_fact = dataclasses.replace(s.last_fact, source=hd.SOURCE_RECORDED)
    assert s.auto_ready() is False
    s.last_fact = dataclasses.replace(s.last_fact, source=hd.SOURCE_SYNTHETIC)
    assert s.auto_ready() is False


def test_the_operator_handoff_is_never_automatic():
    r = build()
    s = session(r)
    press(s, "c")
    press(s, "u")
    assert s.last_fact.status == "holding_verified"
    assert s.auto_ready() is False


# --- the sample id / run binding is on the facts ---------------------------------------


def test_the_facts_carry_the_run_and_the_band():
    r = build()
    s = session(r)
    press(s, "c")
    record = s.last_fact.record()
    assert record["run_id"] == "r" and record["sample_id"] == "r:assist:1"
    assert record["band"] == [0.15, 0.4] and record["object_id"] == "purple eggplant"


# --- the real entry builds the port only when the job asks for the takeover ------------


def test_the_entry_builds_the_port_only_for_an_enabled_takeover():
    from types import SimpleNamespace as NS

    from levi.automatic import fr3_run as R
    from levi.automatic import grasp_assist as ga2

    r = build()
    safety = {"min_z": 0.05325, "max_xy": 0.85}
    off = NS(run_id="r", subtask_grasp_assist=None)
    disabled = NS(
        run_id="r",
        subtask_grasp_assist=ga2.GraspAssistConfig(
            enabled=False, holding_band=("a b|0.15|0.4",)
        ),
    )
    for cfg in (off, disabled):
        assert (
            R.grasp_assist_parts(cfg, r.robot, r.fence, r.clock, safety, lambda: None)
            == {}
        )
    cfg = NS(run_id="r", subtask_grasp_assist=r.cfg)
    orch = NS(
        numbers={"reset": 2},
        policy_epoch=0,
        journal=NS(control_epoch=7),
    )
    rig = R.grasp_assist_parts(cfg, r.robot, r.fence, r.clock, safety, lambda: orch)[
        "grasp_assist"
    ]
    # The bridge Robot itself, the binding's workspace, the next reset episode.
    assert rig.port.robot is r.robot
    assert rig.limits == ga2.Limits(0.05325, 0.85)
    r.port = rig.port
    r.port.begin()
    with r.port.window("x+"):
        token = r.fence.current
    assert (token.episode_id, token.control_epoch, token.policy_epoch) == (
        "r.reset.0003",
        7,
        1,
    )
    with pytest.raises(ga2.AssistFault, match="not up yet"):
        R.grasp_assist_parts(cfg, r.robot, r.fence, r.clock, safety, lambda: None)[
            "grasp_assist"
        ].port.window("x+").__enter__()
    # A binding without a floor cannot run a takeover.
    with pytest.raises(ValueError, match="min_z"):
        R.grasp_assist_parts(
            cfg, r.robot, r.fence, r.clock, {"min_z": 0, "max_xy": 0.85}, lambda: orch
        )
