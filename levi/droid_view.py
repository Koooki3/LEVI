"""A read-only, video-aligned LeRobot browsing view of DROID raw episodes.

DROID MP4s in this raw release declare mostly 60 FPS while trajectory samples
arrive around 14 Hz. Their packet PTS cannot be treated as capture time. The
view keeps every shared camera frame, stream-copies H.264 into a nominal 14.3
FPS viewing clock, and records the original control timestamps separately.
This is a browsing/annotation clock, not a claim of exact acquisition timing.
"""

import math
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import h5py
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from . import catalog
from .conversion import dataset, media, registry
from .conversion.engine import fingerprint
from .conversion.episodes import SourceEpisode
from .conversion.inputs.droid_raw import CAMERAS, camera_paths, metadata
from .conversion.progress import Progress

VIEW_FPS = 14.3
CAMERA_FEATURES = {name: feature for name, (_, feature) in CAMERAS.items()}


def _retime(source: Path, target: Path, frames: int, source_rate: float) -> dict:
    """Copy compressed packets, shorten excess tail frames and verify the PTS."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if not math.isfinite(source_rate) or source_rate <= 0:
        raise ValueError(f"Unknown source rate: {source}")
    command = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-n",
        "-itsscale",
        repr(source_rate / VIEW_FPS),
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-frames:v",
        str(frames),
        "-c:v",
        "copy",
        "-an",
        "-video_track_timescale",
        "600600",
        "-movflags",
        "+faststart",
        str(target),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=300)
        info = media.probe(target)
        if info["declared_frames"] != frames:
            raise ValueError(
                f"Frame count after remux: {info['declared_frames']} vs {frames}"
            )
        times = np.asarray(media.packet_times(target))
        if len(times) != frames:
            raise ValueError("Missing packet timestamps after remux")
        error = float(np.max(np.abs((times - times[0]) - np.arange(frames) / VIEW_FPS)))
        if error > 0.11:
            raise ValueError(f"Video clock irregular by {error:.3f}s: {source}")
        return {**info, "max_clock_error_seconds": error}
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def _numeric(f: h5py.File, key: str, rows: int, width: int) -> np.ndarray:
    if key not in f:
        raise ValueError(f"Missing DROID field: {key}")
    arr = np.asarray(f[key][:rows], dtype=np.float32)
    if arr.shape != (rows, width) or not np.isfinite(arr).all():
        raise ValueError(f"Invalid DROID field {key}: {arr.shape}")
    return arr


def _vector(f: h5py.File, key: str, rows: int) -> np.ndarray:
    if key not in f:
        raise ValueError(f"Missing DROID field: {key}")
    arr = np.asarray(f[key][:rows], dtype=np.float32)
    if arr.shape != (rows,) or not np.isfinite(arr).all():
        raise ValueError(f"Invalid DROID field {key}: {arr.shape}")
    return arr


def _episode(
    source: Path, root: Path, ep: int, item: SourceEpisode, offset: int, task_id: int
):
    demo = Path(item.path)
    meta = metadata(demo)
    paths = camera_paths(demo, meta)
    probes = {name: media.probe(path) for name, path in paths.items()}
    lengths = [int(probe["declared_frames"] or 0) for probe in probes.values()]
    with h5py.File(demo / "trajectory.h5", "r") as f:
        h5_rows = int(f["observation/robot_state/joint_positions"].shape[0])
        rows = min(h5_rows, *lengths)
        if (
            rows < 2
            or h5_rows - rows > 3
            or any(length - rows > 3 for length in lengths)
        ):
            raise ValueError(
                f"{demo.name}: HDF5/video frame mismatch: {h5_rows}, {lengths}"
            )
        control = np.asarray(
            f["observation/timestamp/control/step_start"][:rows], dtype=np.int64
        )
        if control.shape != (rows,) or np.any(np.diff(control) <= 0):
            raise ValueError(f"{demo.name}: control timestamps are not increasing")
        state = np.column_stack(
            [
                _numeric(f, "observation/robot_state/joint_positions", rows, 7),
                _vector(f, "observation/robot_state/gripper_position", rows),
            ]
        )
        action = np.column_stack(
            [
                _numeric(f, "action/joint_position", rows, 7),
                _vector(f, "action/gripper_position", rows),
            ]
        )
    view_t = np.arange(rows, dtype=np.float32) / VIEW_FPS
    source_t = (control - control[0]).astype(np.float64) / 1000
    table = pa.table(
        {
            "timestamp": view_t,
            "frame_index": np.arange(rows, dtype=np.int64),
            "episode_index": np.full(rows, ep, dtype=np.int64),
            "index": np.arange(offset, offset + rows, dtype=np.int64),
            "task_index": np.full(rows, task_id, dtype=np.int64),
            "observation.state": pa.array(state.tolist(), type=pa.list_(pa.float32())),
            "action": pa.array(action.tolist(), type=pa.list_(pa.float32())),
            "source_control_timestamp_sec": source_t,
        }
    )
    part = root / f"data/chunk-{ep // 1000:03d}/episode_{ep:06d}.parquet"
    part.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, part, compression="snappy")
    video_info = {}
    try:
        for name, path in paths.items():
            key = CAMERA_FEATURES[name]
            output = root / f"videos/chunk-{ep // 1000:03d}/{key}/episode_{ep:06d}.mp4"
            video_info[key] = _retime(path, output, rows, probes[name]["fps"])
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    row = {
        "episode_index": ep,
        "tasks": [item.task],
        "length": rows,
        "source_demo": item.source_id,
        "levi_outcome": item.outcome,
        "levi_outcome_source": "droid_raw_metadata",
        "lab": meta.get("building"),
        "levi_task_unspecified": item.metadata.get("task_unspecified", False),
    }
    provenance = {
        "episode_index": ep,
        "source_demo": item.source_id,
        "source_positions": list(range(rows)),
        "source_frame_ids": list(range(rows)),
        "source_capture_timestamps": source_t.tolist(),
        "source_unix_time_ms": control.tolist(),
        "view_clock_fps": VIEW_FPS,
        "max_source_to_view_error_seconds": float(np.max(np.abs(source_t - view_t))),
        "dropped_hdf5_tail_rows": h5_rows - rows,
        "dropped_video_tail_frames": {
            CAMERA_FEATURES[name]: probes[name]["declared_frames"] - rows
            for name in paths
        },
    }
    stats = {
        "timestamp": dataset.stats(view_t),
        "frame_index": dataset.stats(np.arange(rows)),
        "episode_index": dataset.stats(np.full(rows, ep)),
        "index": dataset.stats(np.arange(offset, offset + rows)),
        "task_index": dataset.stats(np.full(rows, task_id)),
        "observation.state": dataset.stats(state),
        "action": dataset.stats(action),
        "source_control_timestamp_sec": dataset.stats(source_t),
    }
    return row, provenance, {"episode_index": ep, "stats": stats}, video_info


def build(source: Path, target: Path, options, progress_path=None) -> dict:
    """Build all selected episodes without writing to DROID originals."""
    fmt = registry.INPUT_FORMATS["droid_raw"]
    progress = Progress(progress_path, ["Inspect", "Build DROID view", "Publish"])
    report = fmt.inspect(source, options, progress)
    if report.failed:
        raise ValueError("; ".join(f"{r.id}: {r.detail}" for r in report.failed))
    episodes = fmt.episodes(source, options)
    if len(episodes) != len(report.episodes):
        raise ValueError("A browsing view must include every DROID episode")
    tasks = {task: i for i, task in enumerate(dict.fromkeys(e.task for e in episodes))}
    offsets = []
    cursor = 0
    for item in episodes:
        offsets.append(cursor)
        cursor += int(item.frames or 0)
    if target.exists():
        raise ValueError("Browsing view target already exists")
    target.mkdir(parents=True)
    rows = [None] * len(episodes)
    progress.stage("Build DROID view", len(episodes))
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            pending = {
                pool.submit(
                    _episode, source, target, ep, item, offsets[ep], tasks[item.task]
                ): ep
                for ep, item in enumerate(episodes)
            }
            for future in as_completed(pending):
                ep = pending[future]
                rows[ep] = future.result()
                progress.advance(episodes[ep].source_id)
        # The HDF5 trajectory may have one more row than each MP4; output
        # offsets must reflect the rows actually retained, not metadata estimates.
        actual_offset = 0
        for ep, (row, _, _, _) in enumerate(rows):
            path = target / f"data/chunk-{ep // 1000:03d}/episode_{ep:06d}.parquet"
            table = pq.read_table(path)
            index = np.arange(
                actual_offset, actual_offset + row["length"], dtype=np.int64
            )
            table = table.set_column(
                table.schema.get_field_index("index"), "index", pa.array(index)
            )
            pq.write_table(table, path, compression="snappy")
            rows[ep][2]["stats"]["index"] = dataset.stats(index)
            actual_offset += row["length"]
        ep_rows = [part[0] for part in rows]
        provenance = [part[1] for part in rows]
        epstats = [part[2] for part in rows]
        features = {
            key: {
                "dtype": "float64"
                if key == "source_control_timestamp_sec"
                else "float32"
                if key == "timestamp"
                else "int64",
                "shape": [1],
                "names": None,
            }
            for key in (
                "timestamp",
                "frame_index",
                "episode_index",
                "index",
                "task_index",
                "source_control_timestamp_sec",
            )
        }
        names = [f"joint_{i}" for i in range(7)] + ["gripper"]
        for key in ("observation.state", "action"):
            features[key] = {"dtype": "float32", "shape": [8], "names": names}
        shapes = {}
        for ep, (_, _, _, videos) in enumerate(rows):
            for key, video in videos.items():
                shape = [video["height"], video["width"], 3]
                shapes.setdefault(key, {})[ep] = shape
        for key, episode_shapes in shapes.items():
            default_shape = max(
                (shape for shape in episode_shapes.values()),
                key=lambda shape: sum(
                    value == shape for value in episode_shapes.values()
                ),
            )
            features[key] = {
                "dtype": "video",
                "shape": default_shape,
                "names": ["height", "width", "channels"],
                "info": {
                    "video.codec": "h264",
                    "video.fps": VIEW_FPS,
                    "video.is_depth_map": False,
                    "has_audio": False,
                    "levi.variable_resolution": len(
                        {tuple(s) for s in episode_shapes.values()}
                    )
                    > 1,
                },
            }
        info = {
            "codebase_version": "v2.1",
            "robot_type": "franka_panda",
            "fps": VIEW_FPS,
            "total_episodes": len(episodes),
            "total_frames": actual_offset,
            "total_tasks": len(tasks),
            "total_videos": 3 * len(episodes),
            "total_chunks": (len(episodes) + 999) // 1000,
            "chunks_size": 1000,
            "splits": {"train": f"0:{len(episodes)}"},
            "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
            "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
            "features": features,
        }
        catalog.atomic(target / "meta/info.json", info)
        dataset.jsonl(target / "meta/episodes.jsonl", ep_rows)
        dataset.jsonl(
            target / "meta/tasks.jsonl",
            [{"task_index": i, "task": task} for task, i in tasks.items()],
        )
        dataset.jsonl(target / "meta/episodes_stats.jsonl", epstats)
        catalog.atomic(target / "meta/stats.json", dataset.aggregate(epstats))
        dataset.jsonl(target / "meta/levi_provenance.jsonl", provenance)
        dataset.jsonl(
            target / "meta/levi_droid_video_shapes.jsonl",
            [
                {
                    "episode_index": ep,
                    "shapes": {key: shapes[key][ep] for key in shapes},
                }
                for ep in range(len(episodes))
            ],
        )
        catalog.atomic(
            target / "meta/levi_view.json",
            {
                "schema": "levi.view.v1",
                "input_format": "droid_raw",
                "source_root": str(source),
                "source_fingerprint": fingerprint(source),
                "view_fps": VIEW_FPS,
                "clock_note": "Uniform viewing clock; source control timestamps preserved in parquet/provenance",
                "source_time_error_max_seconds": max(
                    row["max_source_to_view_error_seconds"] for row in provenance
                ),
                "excluded": {},
                "skipped": {},
            },
        )
        progress.stage("Publish", 1)
        progress.advance("view")
        progress.finish()
        return {
            "ok": True,
            "dataset_path": str(target),
            "episodes": len(episodes),
            "frames": actual_offset,
            "view": catalog.read(target / "meta/levi_view.json", {}),
        }
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
