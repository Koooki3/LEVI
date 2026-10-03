"""Training pool: robot, gripper, action mode and end-effector frame, read from
metadata only (never from folder or task names), and the export that refuses
to mix grippers."""

import json

import pytest

from levi.pool import embodiment, rules

ROBOTIQ_JOINTS = ["robotiq_85_left_knuckle_joint"]
FRANKA_JOINTS = ["fr3_finger_joint1", "fr3_finger_joint2"]
FR3 = [f"fr3_joint{i}" for i in range(1, 8)]


def read(meta=None, fmt="robot_capture", **more):
    return embodiment.read({"metadata": meta or {}, **more}, fmt)


# ------------------------------------------------------------------ units


def test_robotiq_capture_with_a_policy_frame():
    got = read(
        {
            "gripper_joint_names": ROBOTIQ_JOINTS,
            "robot_joint_names": FR3,
            "policy": {"server_metadata": {"ee_frame": "franka_hand_tcp"}},
        }
    )
    assert got["gripper"] == "robotiq_2f85"
    assert got["robot"] == "franka_fr3"
    assert got["ee_frame"] == "franka_hand_tcp"
    # A raw capture has no action column; the pool export derives the next pose.
    assert got["action_mode"] == "ee_pose_abs_next"
    assert "gripper_joint_names" in got["evidence"]["gripper"]
    assert "policy.server_metadata.ee_frame" in got["evidence"]["ee_frame"]


def test_franka_hand_capture_from_joints_or_the_gripper_driver_topic():
    by_joints = read({"gripper_joint_names": FRANKA_JOINTS})
    assert by_joints["gripper"] == "franka_hand"
    by_topic = read({"gripper_state_topic": "/franka_gripper/joint_states"})
    assert by_topic["gripper"] == "franka_hand"
    # Both say the same thing: still one value.
    both = read(
        {
            "gripper_joint_names": FRANKA_JOINTS,
            "gripper_state_topic": "/franka_gripper/joint_states",
        }
    )
    assert both["gripper"] == "franka_hand"


def test_spacemouse_capture_names_its_gripper_in_other_keys():
    got = read({"gripper": {"joint_name": "robotiq_85_left_knuckle_joint"}})
    assert got["gripper"] == "robotiq_2f85"


def test_nothing_recorded_is_unknown_not_a_guess():
    got = read({"task_description": "pick up the robotiq cube", "task_folder": "x"})
    assert got["gripper"] == got["robot"] == got["ee_frame"] == "unknown"
    assert got["evidence"].get("gripper") is None
    # No metadata and no format evidence at all.
    empty = embodiment.read({}, "lerobot")
    assert {empty[f] for f in embodiment.FIELDS} == {"unknown"}


def test_names_of_folders_and_tasks_are_not_evidence():
    got = embodiment.read(
        {"metadata": {"task_description": "robotiq franka_hand"}},
        "robot_capture",
        path="/data/robotiq_collection/franka_hand_task/demo_0000",
    )
    assert got["gripper"] == "unknown"


def test_conflicting_evidence_is_unknown_and_says_so():
    got = read(
        {
            "gripper_joint_names": ROBOTIQ_JOINTS,
            "gripper_state_topic": "/franka_gripper/joint_states",
        }
    )
    assert got["gripper"] == "unknown"
    assert "conflict" in got["evidence"]["gripper"]
    assert "robotiq_2f85" in got["evidence"]["gripper"]
    assert "franka_hand" in got["evidence"]["gripper"]


def test_action_mode_from_a_levi_conversion_record():
    nxt = embodiment.read({"conversion": {"action_semantics": "next_state"}}, "lerobot")
    assert nxt["action_mode"] == "ee_pose_abs_next"
    same = embodiment.read({"conversion": {"action_semantics": "state"}}, "lerobot")
    assert same["action_mode"] == "ee_pose_abs_current"
    # A LeRobot dataset with no record of its action says nothing.
    plain = embodiment.read(
        {"info": {"features": {"action": {"names": ["x", "y", "z"]}}}}, "lerobot"
    )
    assert plain["action_mode"] == "unknown"


def test_dict_values_are_not_matched_and_copied_values_are_sanitized():
    # A dict under a matched key is no evidence (only text, numbers, lists of them).
    assert read({"gripper_joint_names": {"a": "robotiq_85"}})["gripper"] == "unknown"
    got = read({"policy": {"server_metadata": {"ee_frame": "  Franka Hand TCP!! "}}})
    assert got["ee_frame"] == "franka_hand_tcp"
    long = read({"policy": {"server_metadata": {"ee_frame": "x" * 200}}})
    assert long["ee_frame"] == "unknown"  # not a frame name


def test_rules_are_data_versioned_and_overridable(tmp_path):
    defaults = rules.load()
    table = defaults["embodiment"]
    assert isinstance(table["version"], int) and table["rules"]
    assert {r["field"] for r in table["rules"]} <= set(embodiment.FIELDS)
    custom = {
        "embodiment": {
            "version": 7,
            "rules": [
                {
                    "field": "gripper",
                    "value": "my_gripper",
                    "in": "metadata",
                    "key": "end_tool",
                    "regex": "^pinch$",
                }
            ],
        }
    }
    (tmp_path / "rules.json").write_text(json.dumps(custom))
    mine = rules.load(tmp_path)
    got = embodiment.read(
        {"metadata": {"end_tool": "pinch"}}, "robot_capture", rules=mine
    )
    assert got["gripper"] == "my_gripper"
    # The default evidence no longer applies.
    other = embodiment.read(
        {"metadata": {"gripper_joint_names": ROBOTIQ_JOINTS}},
        "robot_capture",
        rules=mine,
    )
    assert other["gripper"] == "unknown"
    # The scan signature follows the table, so a changed table is read again.
    assert embodiment.signature(defaults) != embodiment.signature(mine)
    assert embodiment.signature(defaults) == embodiment.signature(rules.load())


def test_a_malformed_rule_is_refused(tmp_path):
    (tmp_path / "rules.json").write_text(
        json.dumps(
            {"embodiment": {"version": 1, "rules": [{"field": "colour", "value": "x"}]}}
        )
    )
    with pytest.raises(ValueError, match="embodiment"):
        embodiment.read({}, "robot_capture", rules=rules.load(tmp_path))


# ---------------------------------------------------- the mix of grippers


def test_gripper_mix_conditions():
    mix = embodiment.gripper_mix
    only = mix(["robotiq_2f85"] * 3)
    assert only["problem"] is None and only["counts"] == {"robotiq_2f85": 3}
    assert mix(["unknown"] * 4)["problem"] is None  # old data keeps working
    assert mix([None, "unknown"])["counts"] == {"unknown": 2}
    two = mix(["robotiq_2f85", "franka_hand", "franka_hand"])
    assert two["problem"] == "mixed_known"
    assert two["known"] == ["franka_hand", "robotiq_2f85"]
    odd = mix(["robotiq_2f85", "unknown"])
    assert odd["problem"] == "known_and_unknown" and odd["unknown"] == 1
    # The recipe names unknown on purpose: that is a choice.
    assert (
        mix(["robotiq_2f85", "unknown"], chosen=["robotiq_2f85", "unknown"])["problem"]
        is None
    )
    # Two known grippers need the explicit permission, naming both is not enough.
    assert (
        mix(["robotiq_2f85", "franka_hand"], chosen=["robotiq_2f85", "franka_hand"])[
            "problem"
        ]
        == "mixed_known"
    )
    assert mix(["robotiq_2f85", "franka_hand"], allow=True)["problem"] is None
    assert mix(["robotiq_2f85", "franka_hand"], allow=True)["mixed"] is True
    assert mix(["robotiq_2f85", "robotiq_2f85"], allow=True)["mixed"] is False
