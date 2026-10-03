"""Data-driven scan and classification rules (docs/TRAINING_POOL.md#rules).

Nothing here names a task or a machine; the ``embodiment`` table names the
robots and grippers its metadata evidence points to. The defaults can be
overridden per workspace with ``<workspace>/pool/rules.json``: each top-level
key replaces the default of the same name.
"""

import fnmatch
import json
import re
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    # Folder names never descended into.
    "skip_names": [
        ".git",
        ".venv",
        "venv",
        "node_modules",
        ".cache",
        "__pycache__",
        ".pytest_cache",
        ".pytest_tmp",
        ".mypy_cache",
        ".ruff_cache",
        ".next",
        "wandb",
        "checkpoints",
        "site-packages",
    ],
    # Globs over the path relative to a pool root (``*`` crosses folders).
    "skip_globs": [
        "models",  # model weights at a pool root's top level
        "*/pytest-of-*",
        "*/outputs/LEVI",  # a LEVI workspace's own runtime folder
        "*/agent/datasets",
        "*/.state/tmp",
        "*/.state/logs",
        "*.partial",
        "*/.*.partial",
    ],
    # First match wins; checked before any metadata.
    "path_categories": [
        {"glob": "_archive/*", "category": "archive"},
        {"glob": "*/_archive/*", "category": "archive"},
    ],
    # Formats that are public / third-party data wherever they sit.
    "format_categories": {"droid_raw": "external"},
    # metadata.json / episodes.jsonl ``data_source`` values.
    "rollout_data_sources": ["policy_rollout"],
    "human_data_sources": ["human_teleop", "teleop", "human"],
    # metadata.json ``control_mode`` values meaning a person drove the robot.
    "teleop_control_modes": [
        "pygame",
        "spacemouse",
        "keyboard",
        "teleop",
        "gello",
        "vr",
        "joystick",
    ],
    # Files marking a dataset LEVI produced.
    "levi_markers": [
        "meta/levi_conversion.json",
        "meta/levi_recap.json",
        "meta/levi_view.json",
        ".levi-export.json",
        "pool_export.json",
    ],
    # A LeRobot dataset with no provenance and no data_source.
    "lerobot_default_category": "human",
    # Record-only formats: data files the pool lists but cannot export.
    "unsupported_suffixes": {".pkl": "pickle", ".npz": "npz", ".npy": "npy"},
    # A folder of such files is listed once it holds at least this many bytes.
    "unsupported_min_bytes": 1_000_000,
    # Standard episode folder name of a raw capture.
    "demo_pattern": r"demo_\d+",
    # Evidence for the robot, gripper, action mode and end-effector frame
    # (levi/pool/embodiment.py explains the rule format). Metadata only:
    # no folder or task name is evidence. Raise ``version`` when a rule
    # changes; the scan signature follows the table, so episodes are read again.
    "embodiment": {
        "version": 1,
        "rules": [
            # Gripper: the joint the capture recorded, or the driver that moved it.
            {
                "field": "gripper",
                "value": "robotiq_2f85",
                "in": "metadata",
                "key": "gripper_joint_names",
                "regex": "robotiq_85",
            },
            {
                "field": "gripper",
                "value": "robotiq_2f85",
                "in": "metadata",
                "key": "gripper.joint_name",
                "regex": "robotiq_85",
            },
            {
                "field": "gripper",
                "value": "franka_hand",
                "in": "metadata",
                "key": "gripper_joint_names",
                "regex": r"(fr3|panda)_finger_joint",
            },
            {
                "field": "gripper",
                "value": "franka_hand",
                "in": "metadata",
                "key": "gripper_state_topic",
                "regex": "^/?franka_gripper/",
            },
            {
                "field": "gripper",
                "value": "franka_hand",
                "in": "metadata",
                "key": "gripper_control.move_action_topic",
                "regex": "^/?franka_gripper/",
            },
            # Robot: its joint names.
            {
                "field": "robot",
                "value": "franka_fr3",
                "in": "metadata",
                "key": "robot_joint_names",
                "regex": r"\bfr3_joint\d",
            },
            {
                "field": "robot",
                "value": "franka_panda",
                "in": "metadata",
                "key": "robot_joint_names",
                "regex": r"\bpanda_joint\d",
            },
            # End-effector frame: as a policy server declares it.
            {
                "field": "ee_frame",
                "copy": True,
                "in": "metadata",
                "key": "policy.server_metadata.ee_frame",
            },
            # Action mode. LEVI's conversion records what it wrote; a raw
            # capture has no action column and the pool's export derives the
            # action as the next recorded pose (``next_state``).
            {
                "field": "action_mode",
                "value": "ee_pose_abs_next",
                "in": "conversion",
                "key": "action_semantics",
                "regex": "^next_state$",
            },
            {
                "field": "action_mode",
                "value": "ee_pose_abs_current",
                "in": "conversion",
                "key": "action_semantics",
                "regex": "^state$",
            },
            {
                "field": "action_mode",
                "value": "ee_pose_abs_next",
                "in": "format",
                "regex": "^robot_capture$",
            },
        ],
    },
}

CATEGORIES = ("human", "rollout", "levi", "external", "archive")


def load(pool_dir: Path | None = None) -> dict[str, Any]:
    rules = dict(DEFAULTS)
    if pool_dir is not None:
        path = pool_dir / "rules.json"
        if path.is_file():
            override = json.loads(path.read_text())
            unknown = set(override) - set(DEFAULTS)
            if unknown:
                raise ValueError(f"Unknown pool rules: {sorted(unknown)}")
            rules.update(override)
    return rules


def skipped(relative: str, name: str, rules: dict) -> bool:
    if name in rules["skip_names"]:
        return True
    return any(fnmatch.fnmatchcase(relative, g) for g in rules["skip_globs"])


def path_category(relative: str, rules: dict) -> str | None:
    for rule in rules["path_categories"]:
        if fnmatch.fnmatchcase(relative, rule["glob"]) or fnmatch.fnmatchcase(
            relative + "/", rule["glob"]
        ):
            return rule["category"]
    return None


def normalize_task(text: str | None) -> str:
    """Task identity across folders: underscores to spaces, lower case,
    single spaces (``Pour_water…`` and ``pour_water…`` are one task)."""
    return re.sub(r"\s+", " ", (text or "").replace("_", " ")).strip().lower()


def standard_demo(name: str, rules: dict) -> bool:
    return re.fullmatch(rules["demo_pattern"], name) is not None
