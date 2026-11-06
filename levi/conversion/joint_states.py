"""Measured joint angles and gripper width of a robot capture, for export.

A capture written by the FR3 collectors holds ``robot_joint_states.csv`` (the
seven joint angles of every frame, ``joint_1_pos`` .. ``joint_7_pos``, radians)
next to ``end_effector_pose.csv``, and ``gripper_state.csv`` carries the
measured ``gripper_width`` (metres). The conversion used to read neither, so a
converted dataset had only the end-effector pose and could not drive a 3D
replay of the arm. This module reads both, row for row with the pose file,
and refuses data it cannot trust: nothing is filled, interpolated or zeroed.

Layout written to the dataset (the viewer's ``src/lib/replay/fr3-columns.ts``
reads the names; ``tests/test_conversion_joints.py`` re-derives its rules
from that file):

- ``observation.joint_positions``: float32, shape [7], names ``joint_1`` ..
  ``joint_7``, radians;
- ``observation.gripper_width``: float32, shape [1], metres, the *measured*
  aperture. The gripper channel of ``observation.state`` / ``action`` stays
  the open/close command;
- ``robot_type`` becomes ``franka_fr3`` when the requested one is still the
  default ``generic_arm``, because the viewer only drives an FR3 from joint
  columns when the robot type names the FR3 family (any 7-joint arm names its
  joints ``joint_1..7``).
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import raw

FILE = "robot_joint_states.csv"
POSITION_COLUMNS = [f"joint_{i}_pos" for i in range(1, 8)]
JOINT_FEATURE = "observation.joint_positions"
JOINT_NAMES = [f"joint_{i}" for i in range(1, 8)]
GRIPPER_FEATURE = "observation.gripper_width"
GRIPPER_NAMES = ["gripper_width"]
DEFAULT_ROBOT_TYPE = "generic_arm"  # Options.robot_type default
FR3_ROBOT_TYPE = "franka_fr3"
# Joint angles are radians. FR3 joints stay within about +-3.1 (joint 6 reaches
# 4.62), so a value beyond one full turn is degrees or ticks, never a pose.
MAX_ABS_RADIANS = 2 * np.pi
# Measured aperture in metres: a 2F-85 opens 0.085, a Franka hand 0.08.
# Millimetres (85) or a 0..1 fraction would silently mislabel the unit.
GRIPPER_WIDTH_RANGE = (-0.005, 0.2)


@dataclass
class Joints:
    """One demo's measured columns, one row per capture row (``select`` keeps
    the rows a conversion retains)."""

    positions: np.ndarray  # (rows, 7) radians
    gripper_width: np.ndarray | None  # (rows,) metres, None when unusable
    gripper_problem: str | None = None

    def select(self, positions) -> "Joints":
        return Joints(
            self.positions[positions].astype(np.float32),
            None
            if self.gripper_width is None
            else self.gripper_width[positions].astype(np.float32),
            self.gripper_problem,
        )


@dataclass
class Layout:
    """What a whole conversion writes: decided once, for every episode."""

    gripper_width: bool
    gripper_problem: str | None = None


def robot_type(requested: str) -> str:
    """``franka_fr3`` for the default, any explicit choice unchanged."""
    return FR3_ROBOT_TYPE if requested == DEFAULT_ROBOT_TYPE else requested


def _fail(message: str) -> raw.CaptureError:
    return raw.CaptureError("joint_states", f"{FILE}: {message}")


def read(demo, pose: pd.DataFrame, grip: pd.DataFrame) -> Joints | None:
    """The demo's joint angles and gripper width, or ``None`` when it has no
    joint source (no ``robot_joint_states.csv``, or one without any
    ``joint_N_pos`` column: another layout, not read). A source that is there
    but incomplete, misaligned or implausible raises ``CaptureError``."""
    path = demo / FILE
    if not path.is_file():
        return None
    frame = raw.read_csv(path)
    present = [c for c in POSITION_COLUMNS if c in frame.columns]
    if not present:
        return None
    if len(present) != len(POSITION_COLUMNS):
        missing = [c for c in POSITION_COLUMNS if c not in present]
        raise _fail(
            f"has {', '.join(present)} but not {', '.join(missing)}; "
            "all seven joint angles are required"
        )
    if len(frame) != len(pose):
        raise _fail(
            f"has {len(frame)} rows but end_effector_pose.csv has {len(pose)}; "
            "rows are matched one to one, never joined or padded"
        )
    if "frame_index" not in frame.columns:
        raise _fail("has no frame_index column, so its rows cannot be matched")
    ids = pd.to_numeric(frame.frame_index, errors="coerce").to_numpy(float)
    if not np.array_equal(ids, pose.frame_index.to_numpy(float)):
        raise _fail("frame IDs differ from end_effector_pose.csv")
    if "timestamp_sec" in frame.columns and not np.allclose(
        pd.to_numeric(frame.timestamp_sec, errors="coerce"),
        pose.timestamp_sec,
        rtol=0,
        atol=1e-3,
    ):
        raise _fail("capture timestamps differ from end_effector_pose.csv")
    angles = (
        frame[POSITION_COLUMNS].apply(pd.to_numeric, errors="coerce").to_numpy(float)
    )
    bad = ~np.isfinite(angles)
    if bad.any():
        row, column = np.argwhere(bad)[0]
        raise _fail(
            f"{int(bad.sum())} joint angle(s) are missing or not finite "
            f"(first: row {row}, {POSITION_COLUMNS[column]}); gaps are not "
            "filled. Fix the capture, exclude the demo, or set export_joints "
            "to false"
        )
    if (np.abs(angles) > MAX_ABS_RADIANS).any():
        row, column = np.argwhere(np.abs(angles) > MAX_ABS_RADIANS)[0]
        raise _fail(
            f"{POSITION_COLUMNS[column]} is {angles[row, column]:g} at row "
            f"{row}: joint angles must be radians (beyond one turn, +-"
            f"{MAX_ABS_RADIANS:.2f}, is degrees or ticks). Set export_joints "
            "to false to convert without them"
        )
    width = pd.to_numeric(grip.gripper_width, errors="coerce").to_numpy(float)
    low, high = GRIPPER_WIDTH_RANGE
    problem = None
    if not np.isfinite(width).all():
        problem = (
            "gripper_state.csv gripper_width is missing or not finite "
            f"(first: row {int(np.flatnonzero(~np.isfinite(width))[0])})"
        )
    elif width.min() < low or width.max() > high:
        problem = (
            f"gripper_state.csv gripper_width spans {width.min():g}..{width.max():g}, "
            f"outside {low:g}..{high:g} m: not metres?"
        )
    return Joints(angles, None if problem else width, problem)


def settle(items: list[tuple[str, Joints | None]]) -> Layout | None:
    """The layout for a whole conversion from every planned episode, or
    ``None`` when no episode has a joint source (the output is then exactly
    what it was before joints were exported). A dataset's features are the
    same for every episode, so a source present in only some episodes is
    refused rather than padded."""
    have = [source for source, joints in items if joints is not None]
    if not have:
        return None
    lacking = [source for source, joints in items if joints is None]
    if lacking:
        shown = ", ".join(lacking[:3]) + (
            f" (+{len(lacking) - 3} more)" if len(lacking) > 3 else ""
        )
        raise ValueError(
            f"{FILE} is present in {len(have)} of {len(items)} episodes but "
            f"missing (or in another layout) in {shown}: joint columns cannot "
            "be written for only some episodes, and gaps are not filled. "
            "Exclude those demos (exclude_demos) or set export_joints to false"
        )
    problems = [
        (source, joints.gripper_problem)
        for source, joints in items
        if joints is not None and joints.gripper_problem
    ]
    if problems:
        source, problem = problems[0]
        return Layout(False, f"{source}: {problem}")
    return Layout(True)
