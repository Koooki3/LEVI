"""Synthetic captures for reset-export tests.

Every video frame carries its own row number in a grey block (decode it with
``marker``), so a test can read from the exported video which source frame
every output frame is. The wrist camera shows an object between two fingers
that, after the release, stays, settles a little lower, or leaves.
"""

import json
from pathlib import Path

import cv2
import numpy as np
import pandas as pd

from levi.conversion import media

W, H = 128, 96
N = 40
GRASP, RELEASE, OPEN_DONE, REST_LAST = 10, 30, 34, 37  # rows (see make_demo)


def stamp(frame, row: int):
    """Draw the row number as six black/white 8x8 cells in the top-left corner."""
    for bit in range(6):
        frame[2:10, 2 + 10 * bit : 10 + 10 * bit] = 255 if (row >> bit) & 1 else 0
    return frame


def marker(frame) -> int:
    """The row number a frame was drawn with (it survives H.264)."""
    return sum(
        (1 << bit) * int(frame[2:10, 2 + 10 * bit : 10 + 10 * bit].mean() > 127)
        for bit in range(6)
    )


def _texture(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = rng.integers(40, 200, (H // 4, W // 4, 3), dtype=np.uint8)
    return cv2.resize(base, (W, H), interpolation=cv2.INTER_CUBIC)


def _disc(frame, cx, cy, r, seed=3):
    # An orange disc with a smooth pattern (survives a change of size).
    yy0, xx0 = np.mgrid[-r : r + 1, -r : r + 1]
    shade = (45 * np.sin(xx0 / max(r, 1) * 5) * np.cos(yy0 / max(r, 1) * 4)).astype(
        np.int16
    )
    patch = np.clip(
        np.array([0, 90, 190], dtype=np.int16) + shade[:, :, None], 0, 255
    ).astype(np.uint8)  # orange (BGR)
    yy, xx = np.mgrid[-r : r + 1, -r : r + 1]
    inside = xx**2 + yy**2 <= r * r
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            y, x = cy + dy, cx + dx
            if inside[dy + r, dx + r] and 0 <= y < H and 0 <= x < W:
                frame[y, x] = patch[dy + r, dx + r]


def wrist(row: int, scenario: str, seed: int = 1) -> np.ndarray:
    frame = _texture(seed).copy()
    opening = (
        float(np.clip((row - RELEASE) / (OPEN_DONE - RELEASE), 0, 1))
        if row >= RELEASE
        else 0.0
    )
    closed = GRASP + 2 <= row < RELEASE
    gap = 24 if row < GRASP else (14 if closed else int(14 + 22 * opening))
    if row >= GRASP + 3:  # the object is between the fingers from the grasp on
        cx, cy, r = W // 2, int(0.45 * H), 16
        if row >= RELEASE:
            t = float(np.clip((row - RELEASE) / (OPEN_DONE - RELEASE), 0, 1))
            if scenario.startswith("late_"):
                # Still between the open fingers when they finish opening, the
                # fall comes after: rows OPEN_DONE+1 .. OPEN_DONE+3.
                t = float(np.clip((row - OPEN_DONE) / 3, 0, 1))
                scenario = scenario[5:]
            if scenario == "drop":
                cy, r = int(cy + 9 * t), int(r - 3 * t)
            elif scenario == "in_reach":
                cy = int(cy + 4 * t)
            elif scenario == "escaped":
                cx, cy, r = int(cx + 44 * t), int(cy + 30 * t), int(r - 6 * t)
        _disc(frame, cx, cy, r)
    else:
        _disc(frame, 20, 70, 10)
    cv2.rectangle(
        frame,
        (W // 2 - gap - 10, int(0.6 * H)),
        (W // 2 - gap, H - 1),
        (15, 15, 15),
        -1,
    )
    cv2.rectangle(
        frame,
        (W // 2 + gap, int(0.6 * H)),
        (W // 2 + gap + 10, H - 1),
        (15, 15, 15),
        -1,
    )
    if row > REST_LAST:  # the arm lifts: the whole view changes
        frame = np.roll(frame, 17 * (row - REST_LAST), axis=1)
    return stamp(frame, row)


def side(row: int, seed: int = 2) -> np.ndarray:
    frame = _texture(seed).copy()
    cv2.circle(frame, (20 + 2 * row, 60), 5, (0, 200, 255), -1)
    return stamp(frame, row)


def write_video(path: Path, frames) -> None:
    media.encode(iter(frames), path, 10, len(frames))


def make_demo(
    demo: Path,
    scenario: str = "in_place",
    *,
    arm_leaves: bool = False,
    n: int = N,
    task: str = "put the object in the bowl",
    rollout: str | None = "success",
    start=(0.4, 0.0, 0.2),
    end_override=None,
    seed: int = 0,
    leave_at: int | None = None,
    no_grasp: bool = False,
):
    """One raw capture: approach, grasp at GRASP, carry, release at RELEASE,
    still until REST_LAST, then the arm lifts (or, with ``arm_leaves``, at
    once). ``scenario``: what the object does after the release."""
    demo.mkdir(parents=True)
    t = np.arange(n) / 10 + 1000
    xyz = np.zeros((n, 3))
    xyz[:, 2] = 0.2
    xyz[:GRASP] = np.linspace(start, (0.3, 0.1, 0.1), GRASP)
    xyz[GRASP:RELEASE] = np.linspace((0.3, 0.1, 0.1), (0.1, 0.3, 0.12), RELEASE - GRASP)
    xyz[RELEASE:] = (0.1, 0.3, 0.12)
    lift_from = RELEASE + 1 if arm_leaves else REST_LAST + 1
    if leave_at is not None:
        lift_from = leave_at
    for i in range(lift_from, n):
        xyz[i] = (0.1, 0.3, 0.12 + 0.03 * (i - lift_from + 1))
    if end_override is not None:
        xyz[-1] = end_override
    pose = pd.DataFrame(
        {
            "timestamp_sec": t,
            "frame_index": np.arange(n),
            "success_flag": 1,
            "source_stamp_sec": t,
            "px": xyz[:, 0],
            "py": xyz[:, 1],
            "pz": xyz[:, 2],
            "qx": 0.0,
            "qy": 0.0,
            "qz": 0.0,
            "qw": 1.0,
        }
    )
    pose.to_csv(demo / "end_effector_pose.csv", index=False)
    cmd = ["open"] * GRASP + ["close"] * (RELEASE - GRASP) + ["open"] * (n - RELEASE)
    if no_grasp:
        cmd = ["open"] * n
    width = np.full(n, 0.084)
    if not no_grasp:
        width[GRASP : GRASP + 3] = [0.07, 0.05, 0.03]
        width[GRASP + 3 : RELEASE] = 0.03
        width[RELEASE : OPEN_DONE + 1] = np.linspace(
            0.03, 0.084, OPEN_DONE - RELEASE + 1
        )
    pd.DataFrame(
        {
            "timestamp_sec": t,
            "frame_index": np.arange(n),
            "success_flag": 1,
            "source_stamp_sec": t,
            "finger_left": width / 2,
            "finger_right": width / 2,
            "gripper_width": width,
            "last_gripper_command": cmd,
        }
    ).to_csv(demo / "gripper_state.csv", index=False)
    pose[["timestamp_sec", "frame_index"]].to_csv(demo / "frames.csv", index=False)
    pd.DataFrame({"timestamp_sec": [t[-1]], "event": ["stop_demo"]}).to_csv(
        demo / "events.csv", index=False
    )
    meta = {
        "task_description": task,
        "gripper_joint_names": ["robotiq_85_left_knuckle_joint"],
        "created_at": "2026-08-01T10:00:00",
        "stopped_at": "2026-08-01T10:00:00",
        "frame_count": n,
        "collection_freq_hz": 10.0,
    }
    if rollout:
        meta.update(
            data_source="policy_rollout",
            control_mode="policy_rollout",
            eval={"outcome": rollout},
            success_flag_final=int(rollout == "success"),
            policy={"checkpoint_dir": "/models/pi05_test_step10"},
        )
    else:
        meta.update(control_mode="pygame", success_flag_final=0)
    (demo / "metadata.json").write_text(json.dumps(meta))
    write_video(
        demo / "wrist_camera.mp4", [wrist(i, scenario, 1 + 10 * seed) for i in range(n)]
    )
    write_video(demo / "side_camera.mp4", [side(i, 2 + 10 * seed) for i in range(n)])
    return demo


def make_record(
    demo: Path,
    *,
    start=(0.1, 0.3, 0.18),
    anchor=(0.1, 0.3, 0.12),
    m: int = 30,
    task: str = "record a reset",
    grasp: bool = True,
    visual: bool = True,
    reach: bool = True,
):
    """A recording that begins at ``start`` (where a forward episode ended),
    goes down to the object, grasps it (unless ``grasp`` is False), brings it to
    ``anchor`` and stays there; its wrist camera shows the hold the forward
    episode's hold frame shows (unless ``visual`` is False). Frame stamps are
    33 + row, so they cannot be mistaken for forward rows."""
    demo.mkdir(parents=True)
    t = np.arange(m) / 10 + 2000
    xyz = np.zeros((m, 3))
    obj = np.array([0.12, 0.31, 0.11])
    xyz[:10] = np.linspace(start, obj, 10)
    xyz[10:14] = obj
    goal = np.array(anchor) if reach else np.array(anchor) + (0.2, 0, 0)
    xyz[14:25] = np.linspace(obj, goal, 11)
    xyz[25:] = goal
    pose = pd.DataFrame(
        {
            "timestamp_sec": t,
            "frame_index": np.arange(m),
            "success_flag": 1,
            "source_stamp_sec": t,
            "px": xyz[:, 0],
            "py": xyz[:, 1],
            "pz": xyz[:, 2],
            "qx": 0.0,
            "qy": 0.0,
            "qz": 0.0,
            "qw": 1.0,
        }
    )
    pose.to_csv(demo / "end_effector_pose.csv", index=False)
    cmd = (
        ["open"] * 11
        + (["close"] if grasp else ["open"]) * 1
        + (["close"] if grasp else ["open"]) * (m - 12)
    )
    cmd = cmd[:m]
    width = np.full(m, 0.084)
    if grasp:
        width[11:14] = [0.06, 0.04, 0.03]
        width[14:] = 0.03
    pd.DataFrame(
        {
            "timestamp_sec": t,
            "frame_index": np.arange(m),
            "success_flag": 1,
            "source_stamp_sec": t,
            "finger_left": width / 2,
            "finger_right": width / 2,
            "gripper_width": width,
            "last_gripper_command": cmd,
        }
    ).to_csv(demo / "gripper_state.csv", index=False)
    pose[["timestamp_sec", "frame_index"]].to_csv(demo / "frames.csv", index=False)
    pd.DataFrame({"timestamp_sec": [t[-1]], "event": ["stop_demo"]}).to_csv(
        demo / "events.csv", index=False
    )
    (demo / "metadata.json").write_text(
        json.dumps(
            {
                "task_description": task,
                "gripper_joint_names": ["robotiq_85_left_knuckle_joint"],
                "created_at": "2026-08-02T10:00:00",
                "stopped_at": "2026-08-02T10:00:00",
                "frame_count": m,
                "collection_freq_hz": 10.0,
                "data_source": "policy_rollout",
                "control_mode": "policy_rollout",
                "eval": {"outcome": "success"},
                "success_flag_final": 1,
                "policy": {"checkpoint_dir": "/models/pi05_test_step10"},
            }
        )
    )
    other = np.random.default_rng(9).integers(0, 255, (H, W, 3), dtype=np.uint8)

    def hand(i):
        frame = wrist(RELEASE - 1, "in_place") if (visual and i >= 13) else other.copy()
        return stamp(frame, 33 + i)

    write_video(demo / "wrist_camera.mp4", [hand(i) for i in range(m)])
    write_video(
        demo / "side_camera.mp4", [stamp(_texture(2).copy(), 33 + i) for i in range(m)]
    )
    return demo
