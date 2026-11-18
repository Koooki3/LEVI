"""The yaw keys (``r`` / ``R``) of the GRASP_ASSIST takeover, on the bridge Robot over the env fake: the turn is
sent as sub-steps like a move, rx and ry stay at their entry values, the step and the total turn are capped (config
and refusals before anything is sent), the binding's rz range refuses, the orientation drift check compares with
the CURRENT target orientation, and a yaw key counts toward ``max_steps``."""

import math
from itertools import pairwise

import pytest
from assist_rig import EULER, MAX_XY, MIN_Z, START, K, press, session
from assist_rig import build as rig_build

from levi.automatic import grasp_assist as ga
from levi.domain import aeri


def build(**kw):
    """The rig with the yaw keys on (they are off unless the job enables them)."""
    kw["config_kw"] = {"yaw_enabled": True, **kw.get("config_kw", {})}
    return rig_build(**kw)


def euler_of(r):
    """The measured Euler angles once the arm has had time to finish its turn (a move
    is ``ok`` as soon as the orientation is within the 0.05 rad tolerance)."""
    r.clock.advance(int(2e9))
    return ga.pose_of(r.port.observe()).euler


def sent(r):
    return len([p for p in r.env.posts if p[0] == "pose"])


# --- the keys and the settings ---------------------------------------------------------------


def test_r_and_capital_r_are_the_yaw_keys_and_count_as_actions():
    assert ga.KEY_CHARS["r"] == "r+" and ga.KEY_CHARS["R"] == "r-"
    assert ga.YAW_KEYS == {"r+", "r-"} and ga.YAW_KEYS <= ga.ACTING_KEYS
    assert ga.YAW_KEYS <= set(ga.KEYS)


def test_the_yaw_settings_have_their_defaults_and_hard_caps():
    cfg = ga.GraspAssistConfig()
    assert (cfg.yaw_step_rad, cfg.max_yaw_rad) == (0.15, 1.57) and not cfg.yaw_enabled
    assert ga.MAX_YAW_STEP_RAD == aeri.GRASP_ASSIST_MAX_YAW_STEP_RAD == 0.35
    assert ga.MAX_YAW_RAD == aeri.GRASP_ASSIST_MAX_YAW_RAD == 1.6
    assert ga.GraspAssistConfig(yaw_step_rad=0.35, max_yaw_rad=1.6).yaw_step_rad == 0.35
    for bad in (0, -0.1, 0.351, float("nan"), True, "0.1"):
        with pytest.raises(ValueError, match="yaw_step_rad"):
            ga.GraspAssistConfig(yaw_step_rad=bad)
    for bad in (0, -1, 1.61, float("inf"), False):
        with pytest.raises(ValueError, match="max_yaw_rad"):
            ga.GraspAssistConfig(max_yaw_rad=bad)


def test_the_yaw_settings_stay_out_of_the_plan_until_they_differ_from_the_default():
    assert not {"yaw_enabled", "yaw_step_rad", "max_yaw_rad"} & set(
        ga.GraspAssistConfig().describe()
    )
    cfg = ga.GraspAssistConfig(yaw_step_rad=0.1, max_yaw_rad=1.0)
    assert cfg.describe()["yaw_step_rad"] == 0.1
    assert cfg.describe()["max_yaw_rad"] == 1.0


def test_the_job_model_caps_the_yaw_settings_too():
    part = aeri.JobSubtaskGraspAssist
    assert part(yaw_step_rad=0.35, max_yaw_rad=1.6).yaw_step_rad == 0.35
    with pytest.raises(ValueError):
        part(yaw_step_rad=0.36)
    with pytest.raises(ValueError):
        part(max_yaw_rad=1.7)


# --- the turn ----------------------------------------------------------------------------------


def test_a_yaw_key_turns_rz_one_step_and_nothing_else():
    r = build()
    s = session(r)
    before = sent(r)
    p, result = press(s, "r")
    assert result["ok"] is True and p.kind == "yaw"
    # Sent as sub-steps, like a move; the position is held.
    assert sent(r) - before == ga.SUBSTEPS
    rx, ry, rz = euler_of(r)
    assert rx == pytest.approx(EULER[0], abs=1e-5)
    assert ry == pytest.approx(EULER[1], abs=1e-5)
    assert rz == pytest.approx(EULER[2] + 0.15, abs=1e-5)
    assert r.env._xyz() == pytest.approx(START, abs=1e-6)
    assert s.steps == 1 and s.view()["yaw_rad"] == pytest.approx(0.15, abs=1e-5)
    # Upper case is the other way, one step back to the entry.
    press(s, "R")
    assert euler_of(r)[2] == pytest.approx(EULER[2], abs=1e-5)
    press(s, "R")
    assert euler_of(r)[2] == pytest.approx(EULER[2] - 0.15, abs=1e-5)
    assert s.steps == 3


def test_each_sub_step_turns_less_than_the_bridge_limit():
    r = build(config_kw={"yaw_step_rad": 0.35})
    s = session(r)
    press(s, "r")
    quats = r.env.pose_quats[-ga.SUBSTEPS :]
    angles = [ga.quat_angle(a, b) for a, b in pairwise(quats)]
    assert max(angles) < 0.35 / ga.SUBSTEPS + 1e-6
    assert ga.quat_angle(quats[-1], ga.euler_to_quat(*EULER)) == pytest.approx(
        0.35, abs=1e-5
    )


def test_a_yaw_proposal_is_shown_before_it_is_sent():
    r = build()
    s = session(r)
    before = sent(r)
    p = s.propose(K("r"))
    assert sent(r) == before
    d = p.describe()
    assert d["kind"] == "yaw" and d["euler"][2] == pytest.approx(
        EULER[2] + 0.15, abs=1e-4
    )
    assert d["checks"]["yaw_ok"] and d["checks"]["rz_range_ok"]


# --- limits: refused before anything is sent ---------------------------------------------------


def test_the_total_turn_is_capped_at_max_yaw_rad_and_the_refusal_sends_nothing():
    r = build(config_kw={"max_yaw_rad": 0.3})
    s = session(r)
    press(s, "r")
    press(s, "r")
    before = sent(r)
    with pytest.raises(ga.AssistRefused) as e:
        s.propose(K("r"))
    assert e.value.code == "yaw_exceeded" and sent(r) == before
    assert s.refused == 1 and s.steps == 2
    # The other way is still open.
    press(s, "R")
    press(s, "R")
    press(s, "R")
    press(s, "R")  # -0.3 from the entry
    with pytest.raises(ga.AssistRefused, match="yaw_exceeded"):
        s.propose(K("R"))


def test_a_step_above_the_hard_cap_is_refused():
    r = build()
    s = session(r)
    before = sent(r)
    with pytest.raises(ga.AssistRefused) as e:
        s._propose_yaw("r+", 0.36)
    assert e.value.code == "yaw_step_too_large" and sent(r) == before
    with pytest.raises(ga.AssistRefused, match="yaw_step_too_large"):
        s._propose_yaw("r+", float("nan"))


def test_rz_outside_the_binding_range_is_refused_before_anything_is_sent():
    # The entry rz is -0.884: -1.0 is the range's lower edge, a step (0.15) is -1.034.
    r = build(limits=ga.Limits(MIN_Z, MAX_XY, (-1.0, 0.77)))
    s = session(r)
    before = sent(r)
    with pytest.raises(ga.AssistRefused) as e:
        s.propose(K("R"))
    assert e.value.code == "rz_outside_range" and sent(r) == before
    press(s, "r")  # the other way is inside
    assert euler_of(r)[2] == pytest.approx(EULER[2] + 0.15, abs=1e-5)


def test_the_range_edge_itself_is_allowed():
    edge = EULER[2] - 0.15
    r = build(limits=ga.Limits(MIN_Z, MAX_XY, (edge, 0.77)))
    s = session(r)
    press(s, "R")
    assert euler_of(r)[2] == pytest.approx(edge, abs=1e-5)
    with pytest.raises(ga.AssistRefused, match="rz_outside_range"):
        s.propose(K("R"))


def test_the_limits_read_the_bindings_rz_range():
    safety = {"min_z": 0.05325, "max_xy": 0.85, "rz_range": [-2.44, 0.77]}
    assert ga.Limits.from_safety(safety).rz_range == (-2.44, 0.77)
    assert ga.Limits.from_safety({"min_z": 0.05, "max_xy": 0.8}).rz_range is None
    with pytest.raises(ValueError, match="rz_range"):
        ga.Limits(0.05, 0.8, (1.0, 0.0))


def test_a_yaw_key_counts_toward_max_steps():
    r = build(config_kw={"max_steps": 2})
    s = session(r)
    press(s, "r")
    press(s, "x")
    before = sent(r)
    with pytest.raises(ga.AssistRefused) as e:
        s.propose(K("r"))
    assert e.value.code == "max_steps_reached" and sent(r) == before


def test_a_refused_yaw_key_is_not_an_executed_step():
    r = build(config_kw={"max_yaw_rad": 0.15})
    s = session(r)
    press(s, "r")
    with pytest.raises(ga.AssistRefused):
        s.propose(K("r"))
    assert s.steps == 1 and s.keys == 2 and s.refused == 1


# --- the orientation check follows the target --------------------------------------------------


def test_a_move_after_a_yaw_is_not_drift_against_the_entry_orientation():
    r = build()
    s = session(r)
    press(s, "r")
    press(s, "r")  # 0.3 rad away from the entry, far above the 0.05 tolerance
    _, result = press(s, "x")
    assert result["ok"] is True
    r.clock.advance(int(2e9))
    assert ga.quat_angle(
        ga.pose_of(r.port.observe()).quat, ga.euler_to_quat(*EULER)
    ) == pytest.approx(0.3, abs=1e-3)


def test_a_tool_that_leaves_the_current_target_orientation_is_drift():
    r = build()
    s = session(r)
    press(s, "r")
    # The arm is turned back to the entry orientation by something else: against
    # the CURRENT target (entry + 0.15) that is 0.15 rad of drift.
    r.env.target_quat = r.env.from_quat = list(
        ga.euler_to_quat(*EULER)  # entry orientation
    )
    r.env.cmd_ns = None
    r.env.stuck_yaw = True  # the next move does not bring it back either
    _, result = press(s, "x")
    assert result["ok"] is False and result["code"] == "orientation_drift"


def test_drift_inside_the_tolerance_of_the_target_still_passes():
    r = build()
    s = session(r)
    press(s, "r")
    small = ga.euler_to_quat(EULER[0], EULER[1], EULER[2] + 0.15 + 0.03)
    r.env.target_quat = r.env.from_quat = list(small)
    r.env.stuck_yaw = True
    _, result = press(s, "x")
    assert result["ok"] is True


def test_a_yaw_that_never_arrives_is_reported_after_the_settle_wait():
    r = build()
    s = session(r)
    r.env.stuck_yaw = True
    began = r.clock.now()
    _, result = press(s, "r")
    assert result["ok"] is False and result["code"] == "orientation_drift"
    assert (r.clock.now() - began) / 1e9 >= ga.SETTLE_S - 1e-6
    assert s.misses == 1


def test_a_slowly_turning_arm_is_waited_for_before_the_orientation_is_checked():
    r = build(env_kw={"tau": 2.0})
    s = session(r)
    _, result = press(s, "r")
    assert result["ok"] is True
    assert euler_of(r)[2] == pytest.approx(EULER[2] + 0.15, abs=1e-5)


def test_a_stop_during_a_yaw_holds_at_the_measured_orientation_not_the_target():
    stop = {"flag": False}
    r = build(stop=stop)
    s = session(r)
    r.env.on_pose = lambda n: stop.update(flag=n >= 2)
    with pytest.raises(ga.AssistStopped):
        press(s, "r")
    # Two sub-steps went out; the hold's setpoint is where the arm was, rx and ry the
    # entry's, and it is the session's target now.
    held = r.env.pose_quats[-1]
    measured = ga.pose_of(r.port.observe()).quat
    assert ga.quat_angle(held, measured) < ga.ORIENTATION_TOL_RAD
    assert s.euler[:2] == pytest.approx(EULER[:2], abs=1e-5)
    assert ga.quat_angle(s.quat, ga.euler_to_quat(*s.euler)) < 1e-9
    assert ga.quat_angle(held, s.quat) < 1e-6
    # The sent turn is on the budget all the same.
    assert s.yaw_reach == pytest.approx(0.15 * 2 / ga.SUBSTEPS, abs=1e-5)


def test_a_yaw_that_never_arrives_is_held_at_the_measured_yaw_not_left_twisting():
    r = build()
    s = session(r)
    r.env.stuck_yaw = True  # a finger on the rim: the orientation never follows
    _, result = press(s, "r")
    assert result["ok"] is False and result["code"] == "orientation_drift"
    # The last setpoint the arm was given is the measured orientation (the entry's), not
    # the 0.15 rad target the impedance controller would keep pressing towards.
    entry = ga.euler_to_quat(*EULER)
    assert ga.quat_angle(r.env.pose_quats[-1], entry) < ga.ORIENTATION_TOL_RAD
    assert s.euler[2] == pytest.approx(EULER[2], abs=ga.ORIENTATION_TOL_RAD)
    # ... and the next key does not stack on the old target: it is one step from the
    # measured yaw, not 0.30 rad from the entry.
    r.env.stuck_yaw = False
    p = s.propose(K("r"))
    assert p.euler[2] - EULER[2] == pytest.approx(0.15, abs=ga.ORIENTATION_TOL_RAD)


def test_a_hold_does_not_reset_the_yaw_budget():
    r = build(config_kw={"max_yaw_rad": 0.15})
    s = session(r)
    r.env.stuck_yaw = True
    press(s, "r")  # sent 0.15, did not arrive, held at the measured yaw
    assert s.yaw_reach == pytest.approx(0.15, abs=1e-5)
    r.env.stuck_yaw = False
    press(s, "r")  # from the measured yaw, within the total
    assert s.yaw_reach <= 0.15 + 1e-9 and s.view()["yaw_rad"] <= 0.15 + 1e-4
    # The reach is the furthest ever sent: it never shrinks.
    press(s, "R")
    assert s.yaw_reach == pytest.approx(0.15, abs=1e-4)


def test_the_measured_yaw_hold_stays_inside_the_binding_range():
    r = build(limits=ga.Limits(0.05325, 0.85, (EULER[2] - 0.1, EULER[2] + 0.2)))
    s = session(r)
    r.env.stuck_yaw = True
    r.env.from_quat = r.env.target_quat = list(
        ga.euler_to_quat(EULER[0], EULER[1], EULER[2] + 0.5)  # measured far outside
    )
    _, result = press(s, "r")
    assert result["ok"] is False
    assert r.limits.rz_ok(s.euler[2])
    # Held at the nearest edge (+0.2), not thrown back to the entry yaw (0.5 away).
    assert s.euler[2] == pytest.approx(EULER[2] + 0.2, abs=1e-6)


@pytest.mark.parametrize("over", [0.003, 0.05])
def test_a_hold_just_outside_the_range_stays_next_to_the_measured_yaw(over):
    # Review N1: a measured yaw a little past the interval's edge must be held at the
    # edge, at most ``over`` from the measured yaw, never at the entry yaw.
    hi = EULER[2] + 0.3
    r = build(limits=ga.Limits(0.05325, 0.85, (EULER[2] - 0.1, hi)))
    s = session(r)
    r.env.stuck_yaw = True
    r.env.from_quat = r.env.target_quat = list(
        ga.euler_to_quat(EULER[0], EULER[1], hi + over)
    )
    _, result = press(s, "r")
    assert result["ok"] is False
    assert r.limits.rz_ok(s.euler[2])
    assert abs(s.euler[2] - (hi + over)) <= over + 1e-6


def test_rz_nearest_across_pi():
    lim = ga.Limits(0.05, 0.85, (2.9, -2.9 + 2 * math.pi))  # an interval across +-pi
    assert lim.rz_nearest(3.1) == 3.1
    assert lim.rz_nearest(2.8) == pytest.approx(2.9)
    assert lim.rz_nearest(-2.8) == pytest.approx(-2.9)


# --- the yaw keys are off unless the job turns them on ----------------------------------------


def test_without_yaw_enabled_the_keys_are_refused_and_nothing_is_sent():
    r = rig_build()  # a job as it was reviewed before the yaw keys existed
    assert r.cfg.yaw_enabled is False
    s = session(r)
    before = sent(r)
    for key in ("r", "R"):
        with pytest.raises(ga.AssistRefused) as e:
            s.propose(K(key))
        assert e.value.code == "yaw_not_enabled"
    assert sent(r) == before and s.steps == 0 and s.refused == 2
    # Every other key works as it did.
    _, result = press(s, "x")
    assert result["ok"] is True


def test_enabling_the_yaw_keys_shows_in_the_plan_and_the_job_model():
    cfg = ga.GraspAssistConfig(yaw_enabled=True)
    assert cfg.describe()["yaw_enabled"] is True
    assert aeri.JobSubtaskGraspAssist(yaw_enabled=True).yaw_enabled is True
    for bad in ("yes", 1):
        with pytest.raises(ValueError):
            ga.GraspAssistConfig(yaw_enabled=bad)
    with pytest.raises(ValueError):
        aeri.JobSubtaskGraspAssist(yaw_enabled="yes")


# --- rz_range as the binding compares it ---------------------------------------------------------


def test_an_rz_range_across_plus_minus_pi_is_read_modulo_two_pi():
    entry_rz = (
        math.pi - 0.1 + 0.0
    )  # 3.04: inside (3.0, 3.5), the range as a binding gives it
    r = build(
        env_kw={"euler": (EULER[0], EULER[1], entry_rz)},
        limits=ga.Limits(MIN_Z, MAX_XY, (3.0, 3.5)),
    )
    s = session(r)
    press(s, "r")  # 3.19
    press(s, "r")  # 3.34 (the client reports it as -2.94)
    with pytest.raises(ga.AssistRefused) as e:
        for _ in range(3):
            press(s, "r")  # 3.49 is inside, 3.64 is not
    assert e.value.code == "rz_outside_range"
    assert ga.Limits(0.1, 0.8, (3.0, 3.5)).rz_ok(-2.94)
    assert not ga.Limits(0.1, 0.8, (3.0, 3.5)).rz_ok(-2.7)
