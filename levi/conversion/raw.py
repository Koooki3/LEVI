"""Capture schema, synchronization, pose geometry and source-safe transforms."""

import json
import re
import shutil
from pathlib import Path
import numpy as np
import pandas as pd
import cv2
from . import media
from .options import Options
from ..catalog import atomic

POSE = [
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
GRIP = [
    "timestamp_sec",
    "frame_index",
    "success_flag",
    "source_stamp_sec",
    "finger_left",
    "finger_right",
    "gripper_width",
    "last_gripper_command",
]


def check_tree(root: Path):
    # Reject links for conversion: source snapshots and copies must be self-contained.
    if root.is_symlink() or any(p.is_symlink() for p in root.rglob("*")):
        raise ValueError(
            "Conversion inputs cannot contain symlinks; use a physical dataset copy"
        )


def demos(root: Path, options: Options):
    found = sorted(p for p in root.rglob("demo_*") if p.is_dir())
    relative = {p.relative_to(root).as_posix(): p for p in found}
    unknown = set(options.exclude_demos) - relative.keys()
    if unknown:
        raise ValueError(f"Unknown excluded demo paths: {sorted(unknown)}")
    result = [p for key, p in relative.items() if key not in options.exclude_demos]
    if not result:
        raise ValueError("No demo_* captures selected")
    return result


def read_csv(path: Path, columns=None):
    frame = pd.read_csv(path)
    if columns and not set(columns).issubset(frame.columns):
        # Only treat an entirely numeric first row as a legacy headerless schema.
        first = path.open().readline().strip().split(",")
        try:
            float(first[0])
            float(first[1])
        except (ValueError, IndexError):
            raise ValueError(f"Missing required columns in {path.name}") from None
        if len(first) != len(columns):
            raise ValueError(f"Wrong column count in {path.name}")
        frame = pd.read_csv(path, header=None, names=columns)
    if frame.empty:
        raise ValueError(f"Empty CSV: {path.name}")
    return frame


def load(demo: Path):
    pose = read_csv(demo / "end_effector_pose.csv", POSE)
    grip = read_csv(demo / "gripper_state.csv", GRIP)
    for label, frame in [("pose", pose), ("gripper", grip)]:
        ids = pd.to_numeric(frame.frame_index, errors="raise").to_numpy(float)
        if (
            not np.isfinite(ids).all()
            or (ids < 0).any()
            or (ids != np.floor(ids)).any()
            or (np.diff(ids) <= 0).any()
        ):
            raise ValueError(
                f"{label}: frame IDs must be finite, integer, unique and increasing"
            )
        for col in ["timestamp_sec", "source_stamp_sec"]:
            values = pd.to_numeric(frame[col], errors="raise").to_numpy(float)
            if not np.isfinite(values).all() or (np.diff(values) < 0).any():
                raise ValueError(f"{label}: invalid {col}")
        if not (pd.to_numeric(frame.success_flag, errors="raise") == 1).all():
            raise ValueError(f"{label}: failed source samples")
    if len(pose) < 2 or not np.array_equal(pose.frame_index, grip.frame_index):
        raise ValueError("Pose/gripper frame IDs must match exactly (no silent join)")
    if not np.allclose(pose.timestamp_sec, grip.timestamp_sec, rtol=0, atol=1e-3):
        raise ValueError("Pose/gripper capture timestamps are not aligned")
    xyz = pose[["px", "py", "pz"]].to_numpy(float)
    q = pose[["qx", "qy", "qz", "qw"]].to_numpy(float)
    norm = np.linalg.norm(q, axis=1)
    if not np.isfinite(xyz).all() or not np.isfinite(q).all() or (norm < 1e-8).any():
        raise ValueError("Nonfinite pose or zero quaternion")
    q = q / norm[:, None]
    command = grip.last_gripper_command.astype(str).str.strip().str.lower()
    if not command.isin(["open", "close"]).all():
        raise ValueError("Unknown gripper command; expected open or close")
    for extra in demo.glob("*.csv"):
        if extra.name in ("end_effector_pose.csv", "gripper_state.csv", "events.csv"):
            continue
        frame = read_csv(extra)
        if "frame_index" in frame and not np.array_equal(
            frame.frame_index, pose.frame_index
        ):
            raise ValueError(f"Frame alignment mismatch: {extra.name}")
    return pose, grip, xyz, q, (command == "open").to_numpy(float)


def euler(q):
    x, y, z, w = q.T
    angles = np.stack(
        [
            np.arctan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y)),
            np.arcsin(np.clip(2 * (w * y - z * x), -1, 1)),
            np.arctan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)),
        ],
        axis=1,
    )
    return np.unwrap(angles, axis=0)


def quaternion_continuous(q):
    q = q.copy()
    for i in range(1, len(q)):
        if np.dot(q[i - 1], q[i]) < 0:
            q[i] *= -1
    return q


def keep_positions(xyz, q, grip, options):
    # Compare with last retained frame: accumulated slow motion must survive.
    protected = {0, len(xyz) - 1}
    for i in np.flatnonzero(np.diff(grip) != 0) + 1:
        protected.update(
            range(
                max(0, i - options.gripper_margin),
                min(len(xyz), i + options.gripper_margin + 1),
            )
        )
    keep = [0]
    for i in range(1, len(xyz)):
        last = keep[-1]
        rotation = 2 * np.arccos(np.clip(abs(np.dot(q[last], q[i])), 0, 1))
        if (
            i in protected
            or grip[i] != grip[last]
            or np.linalg.norm(xyz[i] - xyz[last]) >= options.xyz_threshold
            or rotation >= options.rotation_threshold
        ):
            keep.append(i)
    return np.array(keep, dtype=int)


def camera_path(demo, key):
    for name in [key + ".mp4", key + "_raw.avi"]:
        path = demo / name
        if path.exists():
            try:
                media.probe(path)
                return path
            except Exception as exc:
                if name.endswith("_raw.avi"):
                    raise ValueError(f"Unreadable camera {key}: {exc}") from exc
    raise ValueError(f"Missing camera: {key}")


def audit(demo: Path, options: Options):
    rec = {"demo": demo.name, "errors": [], "warnings": [], "cameras": {}}
    try:
        pose, grip, xyz, q, command = load(demo)
        rec["frames"] = len(pose)
        rec["retained_frames"] = len(keep_positions(xyz, q, command, options))
        if not (np.diff(command) != 0).any():
            rec["warnings"].append("Gripper does not change command")
        longest = run = 0
        for same in np.diff(pose.source_stamp_sec) == 0:
            run = run + 1 if same else 0
            longest = max(longest, run)
        if longest >= options.stale_run:
            rec["errors"].append(f"Stale state source timestamp run: {longest}")
        for key in options.cameras:
            v = media.inspect(camera_path(demo, key))
            rec["cameras"][key] = v
            if v["frames"] != len(pose):
                rec["errors"].append(
                    f"{key}: {v['frames']} video frames vs {len(pose)} CSV rows"
                )
            if abs(v["fps"] - options.fps) > 0.01:
                rec["errors"].append(
                    f"{key}: video FPS {v['fps']} differs from target {options.fps}; run fps normalization"
                )
            if v["span"] < options.frozen_threshold:
                rec["errors"].append(f"{key}: frozen camera (span {v['span']:.4f})")
        metadata = demo / "metadata.json"
        events = demo / "events.csv"
        if metadata.exists():
            m = json.loads(metadata.read_text())
            if m.get("camera_stalled"):
                rec["errors"].append("Capture metadata records camera stall")
            if m.get("frame_count", len(pose)) != len(pose):
                rec["errors"].append("Capture metadata frame_count mismatch")
            if options.require_complete and not m.get("stopped_at"):
                rec["errors"].append("Capture has no stopped_at marker")
        elif options.require_complete:
            rec["errors"].append("Missing capture metadata.json")
        if events.exists():
            ev = pd.read_csv(events)
            names = set(ev.get("event", []))
            if "camera_stalled" in names:
                rec["errors"].append("Capture contains camera_stalled event")
            if options.require_complete and "stop_demo" not in names:
                rec["errors"].append("Missing stop_demo event")
        elif options.require_complete:
            rec["errors"].append("Missing events.csv")
    except Exception as exc:
        rec["errors"].append(str(exc))
    rec["ok"] = not rec["errors"]
    return rec


def image_files(demo, key):
    # Natural numeric sorting avoids frame_10 preceding frame_2.
    natural = lambda p: [
        int(v) if v.isdigit() else v.lower() for v in re.split(r"(\d+)", p.name)
    ]
    return sorted(
        (
            p
            for p in (demo / key).iterdir()
            if p.suffix.lower() in (".png", ".jpg", ".jpeg")
        ),
        key=natural,
    )


def read_images(files, positions):
    for i in positions:
        frame = cv2.imread(str(files[int(i)]))
        if frame is None:
            raise ValueError(f"Unreadable image: {files[int(i)].name}")
        yield frame


def sample_positions(n, source_fps, target_fps):
    if target_fps > source_fps + 0.01:
        raise ValueError(
            "Upsampling capture frames is not supported; choose target FPS <= source FPS"
        )
    positions = np.arange(0, n, source_fps / target_fps).astype(int)
    if len(positions) < 2:
        raise ValueError("Resampling leaves fewer than two frames")
    return np.unique(positions)


def transform_demo(source: Path, target: Path, positions, options, use_images=False):
    pose, _, _, _, _ = load(source)
    ids = pose.frame_index.to_numpy()[positions]
    target.mkdir(parents=True, exist_ok=False)
    for item in source.iterdir():
        if item.name in options.cameras or item.suffix.lower() in (".mp4", ".avi"):
            continue
        if item.is_file():
            shutil.copy2(item, target / item.name)
        elif item.is_dir():
            shutil.copytree(item, target / item.name)
    for csv in source.glob("*.csv"):
        frame = read_csv(
            csv,
            POSE
            if csv.name == "end_effector_pose.csv"
            else GRIP
            if csv.name == "gripper_state.csv"
            else None,
        )
        if "frame_index" not in frame:
            continue
        if csv.name == "events.csv":
            # Event rows map to the next retained frame; original fields remain recoverable.
            old = pd.to_numeric(frame.frame_index, errors="raise").to_numpy()
            new = np.clip(np.searchsorted(ids, old), 0, len(ids) - 1)
            frame["source_frame_index"] = old
            frame["frame_index"] = new
        else:
            frame = frame.iloc[positions].copy()
            if "source_frame_index" not in frame:
                frame["source_frame_index"] = frame.frame_index
            frame["frame_index"] = np.arange(len(frame))
        if "timestamp_sec" in frame:
            if "source_timestamp_sec" not in frame:
                frame["source_timestamp_sec"] = frame.timestamp_sec
            frame["timestamp_sec"] = frame.frame_index / options.fps
        frame.to_csv(target / csv.name, index=False)
    for camera in options.cameras:
        if use_images:
            files = image_files(source, camera)
            if len(files) != len(pose):
                raise ValueError(f"{camera}: image and CSV counts differ")
            frames = read_images(files, positions)
        else:
            frames = media.selected_video(camera_path(source, camera), positions)
        media.encode(frames, target / (camera + ".mp4"), options.fps, len(positions))
    metadata = (
        json.loads((source / "metadata.json").read_text())
        if (source / "metadata.json").exists()
        else {}
    )
    metadata.update(frame_count=len(positions), fps=options.fps)
    atomic(target / "metadata.json", metadata)
    atomic(
        target / "levi_frames.json",
        {
            "schema": "levi.frames.v1",
            "source_positions": positions.tolist(),
            "source_frame_ids": pose.get("source_frame_index", pose.frame_index)
            .iloc[positions]
            .tolist(),
            "fps": options.fps,
        },
    )


def task_text(demo, options):
    path = demo.parent / "task_description.txt"
    text = (
        path.read_text(encoding="utf-8").strip() if path.exists() else demo.parent.name
    )
    return options.task_map.get(text, text)


def copy_task(source, destination, options):
    destination.parent.mkdir(parents=True, exist_ok=True)
    text = task_text(source, options)
    (destination.parent / "task_description.txt").write_text(text, encoding="utf-8")
