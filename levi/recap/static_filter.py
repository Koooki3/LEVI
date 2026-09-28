"""The static-pose frame filter a RECAP value model's training data went through.

The FR3 RECAP datasets (``sft_recap`` = ``lerobot_fr3_filtered_robotiq_v3``
and ``rollouts_recap``) were built from raw captures filtered by
``data_collection_robotiq/scripts/filter_static_pose_frames.py`` (5 mm /
0.01 rad; about a quarter of all frames dropped). A value model trained on them
has never seen those near-static frames, and its returns and N-step
advantages count *kept* steps: N = 10 spans ten kept frames. Labelling an
unfiltered view the same way therefore needs the same filter.

This module transcribes that script's decision exactly (``load_aligned_states``,
``choose_frames_to_drop``, ``protected_frames`` and ``build_keep_plan`` with
their CSV reading, Euler conversion and command-based gripper channel), so the
kept frames are the ones the training pipeline would have kept. It only reads
the raw capture's CSVs; it decides, it does not rewrite anything. LEVI's own
conversion filter (``levi/conversion/raw.py::keep_positions``, compared with
the last *kept* frame) is a different rule and is not used here.

Rule (drop the later frame of an adjacent pair of pose rows when all hold):
gripper command unchanged (|Δ| <= gripper_epsilon), Euler L2 delta (wrapped)
<= euler_threshold_rad, XYZ distance < xyz_threshold_m; frames within
``gripper_protect_margin`` rows of a gripper command flip are never dropped.
Rows of ``frames.csv`` whose ``frame_index`` is dropped are removed; the
returned positions index the rows (= video frames) that remain.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any, Sequence

import numpy as np

RULE = "fr3_static_pose_v3"
SOURCE = "data_collection_robotiq/scripts/filter_static_pose_frames.py"
POSE_COLUMNS = [
    "timestamp_sec",
    "frame_index",
    "success_flag",
    "source_stamp_sec",
    "px",
    "py",
    "pz",
    "qx",
    "qy",
    "qz",
    "qw",
]
GRIPPER_COLUMNS = [
    "timestamp_sec",
    "frame_index",
    "success_flag",
    "source_stamp_sec",
    "finger_left",
    "finger_right",
    "gripper_width",
    "last_gripper_command",
]
FRAMES_COLUMNS = ["timestamp_sec", "frame_index", "success_flag", "wrist_video", "side_video"]


def quaternion_to_euler(qx: float, qy: float, qz: float, qw: float):
    sinr_cosp = 2 * (qw * qx + qy * qz)
    cosr_cosp = 1 - 2 * (qx * qx + qy * qy)
    roll = math.atan2(sinr_cosp, cosr_cosp)
    sinp = 2 * (qw * qy - qz * qx)
    if abs(sinp) >= 1:
        pitch = math.copysign(math.pi / 2, sinp)
    else:
        pitch = math.asin(sinp)
    siny_cosp = 2 * (qw * qz + qx * qy)
    cosy_cosp = 1 - 2 * (qy * qy + qz * qz)
    yaw = math.atan2(siny_cosp, cosy_cosp)
    return float(roll), float(pitch), float(yaw)


def wrap_angle_delta(delta: np.ndarray) -> np.ndarray:
    return (delta + np.pi) % (2 * np.pi) - np.pi


def gripper_command_to_binary(command: object) -> float:
    return 0.0 if str(command).strip().lower() == "close" else 1.0


def read_csv_rows(csv_path: Path, fallback_columns: Sequence[str]):
    with Path(csv_path).open("r", newline="", encoding="utf-8") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            has_header = csv.Sniffer().has_header(sample) if sample.strip() else False
        except csv.Error:
            has_header = True
        if has_header:
            reader = csv.DictReader(f)
            return list(reader.fieldnames or []), list(reader)
        reader = csv.DictReader(f, fieldnames=fallback_columns)
        return list(fallback_columns), list(reader)


def load_aligned_states(demo_dir: Path) -> list[tuple[int, np.ndarray]]:
    pose_path = Path(demo_dir) / "end_effector_pose.csv"
    gripper_path = Path(demo_dir) / "gripper_state.csv"
    if not pose_path.exists() or not gripper_path.exists():
        return []
    _, pose_rows = read_csv_rows(pose_path, POSE_COLUMNS)
    _, gripper_rows = read_csv_rows(gripper_path, GRIPPER_COLUMNS)
    gripper_by_frame = {row.get("frame_index"): row for row in gripper_rows}
    states: list[tuple[int, np.ndarray]] = []
    for pose_row in sorted(
        pose_rows, key=lambda row: int(float(row.get("frame_index", "0") or 0))
    ):
        gripper_row = gripper_by_frame.get(pose_row.get("frame_index"))
        if gripper_row is None:
            continue
        try:
            frame_index = int(float(pose_row["frame_index"]))
            roll, pitch, yaw = quaternion_to_euler(
                float(pose_row["qx"]),
                float(pose_row["qy"]),
                float(pose_row["qz"]),
                float(pose_row["qw"]),
            )
            gripper = gripper_command_to_binary(gripper_row.get("last_gripper_command"))
            state = np.asarray(
                [
                    float(pose_row["px"]),
                    float(pose_row["py"]),
                    float(pose_row["pz"]),
                    roll,
                    pitch,
                    yaw,
                    gripper,
                ],
                dtype=np.float64,
            )
        except Exception:  # noqa: BLE001 -- the script skips unreadable rows
            continue
        if np.all(np.isfinite(state)):
            states.append((frame_index, state))
    return states


def protected_frames(aligned_states, margin: int) -> set[int]:
    protected: set[int] = set()
    for position in range(1, len(aligned_states)):
        if aligned_states[position][1][6] == aligned_states[position - 1][1][6]:
            continue
        low = max(0, position - margin)
        high = min(len(aligned_states), position + margin + 1)
        protected.update(aligned_states[index][0] for index in range(low, high))
    return protected


def choose_frames_to_drop(
    aligned_states,
    xyz_threshold_m: float,
    euler_threshold_rad: float,
    gripper_epsilon: float,
    gripper_protect_margin: int = 2,
) -> tuple[set[int], int, int]:
    drop: set[int] = set()
    total_pairs = matched_pairs = 0
    for (_prev_frame, prev_state), (cur_frame, cur_state) in zip(
        aligned_states[:-1], aligned_states[1:]
    ):
        total_pairs += 1
        xyz_distance = float(np.linalg.norm(cur_state[:3] - prev_state[:3]))
        euler_distance = float(
            np.linalg.norm(wrap_angle_delta(cur_state[3:6] - prev_state[3:6]))
        )
        gripper_delta = abs(float(cur_state[6] - prev_state[6]))
        if (
            gripper_delta <= gripper_epsilon
            and euler_distance <= euler_threshold_rad
            and xyz_distance < xyz_threshold_m
        ):
            matched_pairs += 1
            drop.add(cur_frame)
    drop -= protected_frames(aligned_states, gripper_protect_margin)
    return drop, total_pairs, matched_pairs


def frame_rows(demo_dir: Path) -> list[int]:
    """``frame_index`` of every ``frames.csv`` row, in file order (= video
    frame order); the aligned pose rows when there is no frames.csv."""
    frames_path = Path(demo_dir) / "frames.csv"
    if frames_path.exists():
        _, rows = read_csv_rows(frames_path, FRAMES_COLUMNS)
        return [int(float(r["frame_index"])) for r in rows if r.get("frame_index")]
    return [frame for frame, _ in load_aligned_states(demo_dir)]


def kept_positions(demo_dir: Path, params: dict[str, Any]) -> dict[str, Any]:
    """The row positions (0-based, = video frame positions) the training
    filter keeps for one raw demo, with its counts."""
    aligned = load_aligned_states(demo_dir)
    frames = frame_rows(demo_dir)
    if len(aligned) < 2:
        # The script skips such a demo entirely (no output episode).
        return {
            "keep": list(range(len(frames))),
            "frames": len(frames),
            "skipped": "not enough aligned states",
        }
    drop, total_pairs, matched_pairs = choose_frames_to_drop(
        aligned,
        float(params["xyz_threshold_m"]),
        float(params["euler_threshold_rad"]),
        float(params["gripper_epsilon"]),
        int(params["gripper_protect_margin"]),
    )
    keep = [pos for pos, frame in enumerate(frames) if frame not in drop]
    out: dict[str, Any] = {
        "keep": keep,
        "frames": len(frames),
        "pairs": total_pairs,
        "matched_pairs": matched_pairs,
    }
    if len(keep) < int(params.get("min_frames", 2)):
        out["skipped"] = f"fewer than {params.get('min_frames', 2)} frames kept"
    return out
