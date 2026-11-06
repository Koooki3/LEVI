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
- ``robot_type`` becomes ``franka_fr3`` only when the requested one is still
  the default ``generic_arm`` *and* the capture shows these are FR3 joints:
  ``metadata.json`` ``robot_joint_names`` (the teleoperation collector saves
  the ROS joint-state names, in the order of the angles it writes) start with
  ``fr3_joint1..7`` / ``panda_joint1..7``, or no names were recorded (the
  AERI collector, ``levi/automatic/fr3_media.py``) and every angle of every
  demo lies inside the FR3 limits +-0.1 rad. The viewer only drives an FR3
  from joint columns when the robot type names the Franka family, and any
  7-joint arm names its joints ``joint_1..7``; otherwise the columns are still
  written and the robot type stays ``generic_arm``.
"""

import json
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
# FR3 joint limits in radians, as in the model the viewer draws
# (public/robots/fr3/fr3.urdf; tests/test_conversion_joints.py compares them),
# and the viewer's own tolerance for a value beyond a limit.
FR3_LIMITS = (
    (-2.9007, 2.9007),
    (-1.8361, 1.8361),
    (-2.9007, 2.9007),
    (-3.077, -0.1169),
    (-2.8763, 2.8763),
    (0.4398, 4.6216),
    (-3.0508, 3.0508),
)
LIMIT_TOLERANCE = 0.1
FR3_NAMES = [f"fr3_joint{i}" for i in range(1, 8)]
PANDA_NAMES = [f"panda_joint{i}" for i in range(1, 8)]
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
    # metadata.json robot_joint_names (None: not recorded) and whether every
    # angle of the demo, dropped rows included, is inside the FR3 limits.
    names: list[str] | None = None
    within_limits: bool = True

    def select(self, positions) -> "Joints":
        return Joints(
            self.positions[positions].astype(np.float32),
            None
            if self.gripper_width is None
            else self.gripper_width[positions].astype(np.float32),
            self.gripper_problem,
            self.names,
            self.within_limits,
        )

    def fr3_evidence(self) -> tuple[bool, str]:
        """Whether this demo's columns are shown to be FR3 joints 1..7."""
        if self.names:
            first = self.names[:7]
            if first in (FR3_NAMES, PANDA_NAMES):
                return True, f"robot_joint_names start {first[0]}..{first[-1]}"
            return False, f"robot_joint_names start {first}, not fr3_joint1..7"
        if self.within_limits:
            return (
                True,
                f"no joint names recorded, every angle within the FR3 limits +-{LIMIT_TOLERANCE} rad",
            )
        return (
            False,
            f"no joint names recorded and an angle is outside the FR3 limits +-{LIMIT_TOLERANCE} rad",
        )


@dataclass
class Layout:
    """What a whole conversion writes: decided once, for every episode."""

    gripper_width: bool
    gripper_problem: str | None = None
    # Every demo is shown to be FR3 joints 1..7 (Joints.fr3_evidence), and why
    # or why not, naming the first demo that is not.
    fr3: bool = False
    basis: str = ""


def robot_type(requested: str, layout: Layout) -> str:
    """``franka_fr3`` for the default when every demo is shown to be an FR3,
    any explicit choice (and an unproven FR3) unchanged."""
    return (
        FR3_ROBOT_TYPE if requested == DEFAULT_ROBOT_TYPE and layout.fr3 else requested
    )


def robot_type_basis(requested: str, layout: Layout) -> str:
    if requested != DEFAULT_ROBOT_TYPE:
        return f"robot_type {requested!r} was set explicitly and is kept"
    return layout.basis


# What every refusal ends with: the way out.
EXIT = (
    "Set export_joints to false to convert without joint columns, or "
    "exclude this demo (exclude_demos)."
)


def _fail(message: str) -> raw.CaptureError:
    return raw.CaptureError("joint_states", f"{FILE}: {message}. {EXIT}")


def _names(demo) -> list[str] | None:
    """``robot_joint_names`` of the demo's metadata.json, in the order of the
    angle columns (the teleoperation collector writes the first seven of its
    joint-state message); ``None`` when not recorded."""
    try:
        meta = json.loads((demo / "metadata.json").read_text())
    except (OSError, ValueError):
        return None
    names = meta.get("robot_joint_names") if isinstance(meta, dict) else None
    if not isinstance(names, list) or not names:
        return None
    return [str(name) for name in names]


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
            f"(first: row {row}, {POSITION_COLUMNS[column]}); gaps are not filled"
        )
    if (np.abs(angles) > MAX_ABS_RADIANS).any():
        row, column = np.argwhere(np.abs(angles) > MAX_ABS_RADIANS)[0]
        raise _fail(
            f"{POSITION_COLUMNS[column]} is {angles[row, column]:g} at row "
            f"{row}: joint angles must be radians (beyond one turn, +-"
            f"{MAX_ABS_RADIANS:.2f}, is degrees or ticks)"
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
    low_limits, high_limits = np.array(FR3_LIMITS).T
    within = bool(
        (
            (angles >= low_limits - LIMIT_TOLERANCE)
            & (angles <= high_limits + LIMIT_TOLERANCE)
        ).all()
    )
    return Joints(angles, None if problem else width, problem, _names(demo), within)


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
    unproven = [
        (source, why)
        for source, joints in items
        if joints is not None
        for ok, why in [joints.fr3_evidence()]
        if not ok
    ]
    fr3 = not unproven
    named = sum(1 for _, joints in items if joints is not None and joints.names)
    if unproven:
        source, why = unproven[0]
        basis = (
            f"{source}: {why}; the columns are exported, robot_type stays "
            f"{DEFAULT_ROBOT_TYPE}"
        )
    else:
        basis = (
            f"{named} demo(s) with robot_joint_names fr3_joint1..7 / "
            f"panda_joint1..7, {len(items) - named} without names and every "
            f"angle within the FR3 limits +-{LIMIT_TOLERANCE} rad"
        )
    problem = f"{problems[0][0]}: {problems[0][1]}" if problems else None
    return Layout(not problems, problem, fr3, basis)
