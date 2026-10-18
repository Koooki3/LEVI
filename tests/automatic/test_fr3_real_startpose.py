"""The first episode needs the arm at the bound home pose (L2-REAL-STARTPOSE).

Fake env and fake clock; no socket, robot or GPU. Thresholds are the reviewed ones.
"""

import numpy as np
import pytest
from fr3_real_support import HOME, notes, offset_start, run_dir
from test_fr3_bridge import no_network, rig  # noqa: F401

from levi.automatic import fr3_run as R


def test_start_pose_off_home_refuses_to_start_and_names_the_deviation(
    tmp_path, monkeypatch
):
    r = rig(tmp_path, monkeypatch)
    offset_start(r, monkeypatch, 0.05)
    with pytest.raises(ValueError, match=r"start pose.*error_mm=50\.0.*tolerance_mm=5"):
        R.execute(r.job, r.binding, load_modules=lambda b: r.modules)
    routes = [c[1] for c in r.calls if c[0] == "route"]
    assert "pose" not in routes and "clearerr" not in routes  # the arm is not moved
    assert not run_dir(r).exists()  # nothing was started: the run id is not burnt
    assert r.calls[-1][0] == "closed"


def test_start_pose_within_the_home_tolerance_starts_and_logs_the_measurement(
    tmp_path, monkeypatch
):
    r = rig(tmp_path, monkeypatch)
    offset_start(r, monkeypatch, 0.003)
    assert R.execute(r.job, r.binding, load_modules=lambda b: r.modules) == "COMPLETED"
    text = notes(r)["start_pose_checked"]
    assert "error_mm=3.0" in text and "tolerance_mm=5" in text and "gripper=" in text


def test_start_pose_uses_the_same_rotation_tolerance_as_home(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    env_class = r.modules[2].RobotEnv
    init = env_class.__init__

    def init2(self, **kw):
        init(self, **kw)
        self.pose = np.array([*HOME[:5], 0.06, 1.0])

    monkeypatch.setattr(env_class, "__init__", init2)
    with pytest.raises(ValueError, match=r"start pose.*rotation_rad=0\.06"):
        R.execute(r.job, r.binding, load_modules=lambda b: r.modules)


@pytest.mark.parametrize("grip", [0.899, float("nan"), float("inf"), -float("inf")])
def test_start_pose_requires_measured_open_finite_gripper(tmp_path, monkeypatch, grip):
    r = rig(tmp_path, monkeypatch)
    env_class = r.modules[2].RobotEnv
    observe = env_class.get_observation

    def closed(self):
        value = observe(self)
        value["robot_state"]["gripper_position"] = grip
        return value

    monkeypatch.setattr(env_class, "get_observation", closed)
    with pytest.raises(ValueError, match="start pose.*gripper"):
        R.execute(r.job, r.binding, load_modules=lambda b: r.modules)
    assert not any(c[0] == "route" for c in r.calls)
    assert not run_dir(r).exists()
    assert r.calls[-1][0] == "closed"


@pytest.mark.parametrize(
    "dx,rz,grip,unmet",
    [
        (0, 0, 1, []),
        (0.05, 0, 1, ["position_tolerance"]),
        (0, 0.06, 1, ["rotation_tolerance"]),
        (0, 0, 0.3, ["gripper_not_open"]),
        (0, 0, float("nan"), ["gripper_invalid"]),
    ],
)
def test_start_pose_structured_diagnostics_are_one_read_and_zero_motion(
    dx, rz, grip, unmet
):
    import json

    from fr3_real_support import build

    r = build()
    measured = [HOME[0] + dx, *HOME[1:5], rz, 1.0]
    r.env.pose_fn = lambda since: np.array(measured)
    r.env.grip_fn = lambda since: grip
    result = r.robot.start_pose()
    assert result["ok"] is (not unmet)
    assert result["unmet"] == unmet
    assert result["target_pose"] == [*HOME, 1.0]
    assert result["measured_pose"] == measured
    assert result["error_mm"] == pytest.approx(dx * 1000)
    assert result["rotation_rad"] == pytest.approx(rz)
    assert result["tolerance_mm"] == 5
    assert result["rotation_tol_rad"] == 0.05
    assert result["gripper_min"] == 0.9
    assert result["gripper"] == (None if np.isnan(grip) else grip)
    json.dumps(result, allow_nan=False)
    assert r.env.reads == 1 and r.env.routes == ["getstate"]


@pytest.mark.parametrize(
    "case", ["missing_pose", "missing_gripper", "nan_pose", "read_failure"]
)
def test_start_pose_unreadable_diagnostics_refuse_without_motion(case):
    import json

    from fr3_real_support import build

    r = build()
    observe = r.env.get_observation

    def unreadable():
        state = observe()
        if case == "read_failure":
            raise ValueError("camera/state unavailable")
        if case == "missing_pose":
            del state["robot_state"]["cartesian_position"]
        elif case == "missing_gripper":
            del state["robot_state"]["gripper_position"]
        else:
            state["robot_state"]["cartesian_position"][3] = float("nan")
        return state

    r.env.get_observation = unreadable
    result = r.robot.start_pose()
    assert result["ok"] is False
    assert result["unmet"] == ["unreadable"]
    assert result["unreadable_reason"]
    assert result["target_pose"] == [*HOME, 1.0]
    assert result["error_mm"] is None
    assert result["rotation_rad"] is None
    if case in ("missing_pose", "read_failure"):
        assert result["measured_pose"] is None
    elif case == "nan_pose":
        assert result["measured_pose"] == [*HOME[:3], None, *HOME[4:], 1.0]
    else:
        assert result["measured_pose"] == [*HOME, 1.0]
    if case in ("missing_gripper", "read_failure"):
        assert result["gripper"] is None
    else:
        assert result["gripper"] == 1.0
    json.dumps(result, allow_nan=False)
    assert r.env.reads == 1 and r.env.routes == ["getstate"]


def test_start_gate_refusal_explains_object_and_path_checks_without_homing():
    from fr3_real_support import build

    r = build()
    r.env.grip_fn = lambda since: 0.2
    with pytest.raises(ValueError) as found:
        R.start_gate(r.robot)
    message = str(found.value)
    for text in (
        "No motion command was sent",
        "no run ID was consumed",
        "opens the gripper",
        "held object",
        "return path",
        "fault latch",
    ):
        assert text in message
    assert r.env.routes == ["getstate"]
