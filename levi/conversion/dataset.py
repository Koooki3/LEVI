"""Portable LeRobot v2.1 writer, metadata repair and full-media validation."""

import json
import shutil
from pathlib import Path
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from ..catalog import atomic
from ..diagnostics import diagnose
from ..paths import inside
from ..versions import is_dataset_v2
from . import media, raw


def stats(values):
    a = np.asarray(values, dtype=np.float64)
    return {
        "min": a.min(0).tolist(),
        "max": a.max(0).tolist(),
        "mean": a.mean(0).tolist(),
        "std": a.std(0).tolist(),
        "count": [len(a)],
    }


def aggregate(episodes):
    output = {}
    for key in episodes[0]["stats"]:
        parts = [ep["stats"][key] for ep in episodes if key in ep["stats"]]
        weights = np.array([p["count"][0] for p in parts], float)
        means = np.array([p["mean"] for p in parts])
        sigma = np.array([p["std"] for p in parts])
        w = weights.reshape((-1,) + (1,) * (means.ndim - 1))
        mean = (w * means).sum(0) / weights.sum()
        variance = (w * (sigma**2 + (means - mean) ** 2)).sum(0) / weights.sum()
        output[key] = {
            "min": np.min([p["min"] for p in parts], axis=0).tolist(),
            "max": np.max([p["max"] for p in parts], axis=0).tolist(),
            "mean": mean.tolist(),
            "std": np.sqrt(np.maximum(0, variance)).tolist(),
            "count": [int(weights.sum())],
        }
    return output


def jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in rows
        ),
        encoding="utf-8",
    )


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def convert(source: Path, target: Path, options):
    if target.exists():
        raise ValueError("Dataset destination already exists")
    selected = raw.demos(source, options)
    audits = [raw.audit(d, options) for d in selected]
    bad = [r for r in audits if not r["ok"]]
    if bad:
        raise ValueError(
            "Capture preflight failed: " + json.dumps(bad, ensure_ascii=False)
        )
    target.mkdir(parents=True)
    tasks = {}
    episodes = []
    epstats = []
    provenance = []
    total = 0
    features = {}
    camera_shapes = {}
    for ep, demo in enumerate(selected):
        pose, _, xyz, q, command = raw.load(demo)
        orientation = (
            raw.euler(q)
            if options.orientation == "euler"
            else raw.quaternion_continuous(q)
        )
        state = np.column_stack([xyz, orientation, command]).astype(np.float32)
        action = (
            np.vstack([state[1:], state[-1:]])
            if options.action_mode == "next_state"
            else state.copy()
        )
        n = len(state)
        task = raw.task_text(demo, options)
        tasks.setdefault(task, len(tasks))
        task_id = tasks[task]
        cols = {
            "timestamp": np.arange(n, dtype=np.float32) / options.fps,
            "frame_index": np.arange(n, dtype=np.int64),
            "episode_index": np.full(n, ep, dtype=np.int64),
            "index": np.arange(total, total + n, dtype=np.int64),
            "task_index": np.full(n, task_id, dtype=np.int64),
        }
        table = pa.table(
            {
                **cols,
                "action": pa.array(action.tolist(), type=pa.list_(pa.float32())),
                "observation.state": pa.array(
                    state.tolist(), type=pa.list_(pa.float32())
                ),
            }
        )
        part = target / f"data/chunk-{ep // 1000:03d}/episode_{ep:06d}.parquet"
        part.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, part, compression="snappy")
        eps = {k: stats(v.reshape(-1, 1)) for k, v in cols.items()}
        eps.update({"action": stats(action), "observation.state": stats(state)})
        for camera, key in options.cameras.items():
            dst = target / f"videos/chunk-{ep // 1000:03d}/{key}/episode_{ep:06d}.mp4"
            # Always encode the selected sequence: no codec/FPS metadata guesses.
            media.encode(
                media.decode(raw.camera_path(demo, camera)), dst, options.fps, n
            )
            v = media.inspect(dst, pixels=True)
            shape = [v["height"], v["width"], 3]
            if key in camera_shapes and shape != camera_shapes[key]:
                raise ValueError("Camera resolution changes between episodes")
            camera_shapes[key] = shape
            eps[key] = v["stats"]
            features[key] = {
                "dtype": "video",
                "shape": shape,
                "names": ["height", "width", "channels"],
                "info": {
                    "video.height": v["height"],
                    "video.width": v["width"],
                    "video.codec": v["codec"],
                    "video.pix_fmt": v["pixel_format"],
                    "video.fps": v["fps"],
                    "video.channels": 3,
                    "video.is_depth_map": False,
                    "has_audio": False,
                },
            }
        for k in cols:
            features[k] = {
                "dtype": "float32" if k == "timestamp" else "int64",
                "shape": [1],
                "names": None,
            }
        names = (
            ["x", "y", "z"]
            + (
                ["rx", "ry", "rz"]
                if options.orientation == "euler"
                else ["qx", "qy", "qz", "qw"]
            )
            + ["gripper"]
        )
        for k in ["action", "observation.state"]:
            features[k] = {"dtype": "float32", "shape": [len(names)], "names": names}
        episodes.append({"episode_index": ep, "tasks": [task], "length": n})
        epstats.append({"episode_index": ep, "stats": eps})
        frame_map = demo / "levi_frames.json"
        provenance.append(
            {
                "episode_index": ep,
                "source_demo": demo.relative_to(source).as_posix(),
                "source_frame_ids": json.loads(frame_map.read_text())[
                    "source_frame_ids"
                ]
                if frame_map.exists()
                else pose.frame_index.tolist(),
                "source_capture_timestamps": pose.get(
                    "source_timestamp_sec", pose.timestamp_sec
                ).tolist(),
            }
        )
        total += n
        print(f"Converted episode {ep}: {n} frames", flush=True)
    info = {
        "codebase_version": "v2.1",
        "robot_type": options.robot_type,
        "fps": options.fps,
        "total_episodes": len(episodes),
        "total_frames": total,
        "total_tasks": len(tasks),
        "total_videos": len(episodes) * len(options.cameras),
        "total_chunks": (len(episodes) + 999) // 1000,
        "chunks_size": 1000,
        "splits": {"train": f"0:{len(episodes)}"},
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
        "features": features,
    }
    atomic(target / "meta/info.json", info)
    jsonl(target / "meta/episodes.jsonl", episodes)
    jsonl(target / "meta/episodes_stats.jsonl", epstats)
    jsonl(
        target / "meta/tasks.jsonl",
        [{"task_index": i, "task": text} for text, i in tasks.items()],
    )
    atomic(target / "meta/stats.json", aggregate(epstats))
    jsonl(target / "meta/levi_provenance.jsonl", provenance)
    atomic(
        target / "meta/levi_conversion.json",
        {
            "schema": "levi.conversion.v1",
            "options": options.model_dump(),
            "action_semantics": options.action_mode,
            "rotation_units": "radians"
            if options.orientation == "euler"
            else "unit quaternion xyzw",
            "position_units": "metres",
            "gripper": "command: open=1, close=0; not measured aperture",
            "image_statistics": "all decoded output pixels, RGB normalized to [0,1]",
        },
    )
    return target


def validate(root: Path):
    """Native checks plus full video decoding and v2 row/media/metadata contracts."""
    report = diagnose(root, max_episodes=0, decode_video=False)
    info = json.loads((root / "meta/info.json").read_text())
    failures = []
    videos = 0
    for path in sorted((root / "videos").rglob("*.mp4")):
        try:
            inside(path, root)
            v = media.inspect(path)
            videos += 1
            if abs(v["fps"] - info["fps"]) > 0.01:
                failures.append(f"Video FPS mismatch: {path.relative_to(root)}")
            if is_dataset_v2(info.get("codebase_version")):
                ep = int(path.stem.rsplit("_", 1)[-1])
                data = inside(
                    info["data_path"].format(
                        episode_chunk=ep // info["chunks_size"], episode_index=ep
                    ),
                    root,
                )
                if pq.read_metadata(data).num_rows != v["frames"]:
                    failures.append(
                        f"Video/Parquet length mismatch: {path.relative_to(root)}"
                    )
                feature = info["features"].get(path.parent.name, {})
                if feature.get("shape") != [v["height"], v["width"], 3]:
                    failures.append(
                        f"Video resolution mismatch: {path.relative_to(root)}"
                    )
        except Exception as exc:
            failures.append(str(exc))
    if is_dataset_v2(info.get("codebase_version")):
        task_rows = read_jsonl(root / "meta/tasks.jsonl")
        tasks = {r["task_index"] for r in task_rows}
        eps = read_jsonl(root / "meta/episodes.jsonl")
        if len(eps) != info["total_episodes"] or len(tasks) != info["total_tasks"]:
            failures.append("Metadata counts disagree")
        cursor = 0
        for ep in eps:
            i = ep["episode_index"]
            path = inside(
                info["data_path"].format(
                    episode_chunk=i // info["chunks_size"], episode_index=i
                ),
                root,
            )
            try:
                data = pq.read_table(path).to_pandas()
                n = len(data)
                if (
                    n != ep["length"]
                    or not np.array_equal(data.frame_index, np.arange(n))
                    or not np.array_equal(data["index"], np.arange(cursor, cursor + n))
                ):
                    failures.append(f"Episode {i}: index/length contract violation")
                if not np.allclose(
                    data.timestamp, np.arange(n) / info["fps"], atol=1e-4, rtol=0
                ):
                    failures.append(f"Episode {i}: timestamps must be frame_index/FPS")
                if not set(data.task_index) <= tasks:
                    failures.append(f"Episode {i}: unknown task index")
                for key, f in info["features"].items():
                    if f["dtype"] == "video":
                        video = inside(
                            info["video_path"].format(
                                episode_chunk=i // info["chunks_size"],
                                episode_index=i,
                                video_key=key,
                            ),
                            root,
                        )
                        if not video.is_file():
                            failures.append(f"Episode {i}: missing camera {key}")
                cursor += n
            except Exception as exc:
                failures.append(str(exc))
    failures += [r["message"] for r in report["results"] if r["status"] == "fail"]
    return {
        "ok": not failures,
        "failures": failures,
        "decoded_videos": videos,
        "diagnostics": report,
        "method": "Native schema checks plus complete video decode; no training framework dependency",
    }


def repair(source, target, options, stage):
    info = json.loads((source / "meta/info.json").read_text())
    if not is_dataset_v2(info.get("codebase_version")):
        raise ValueError(
            "Metadata repair currently accepts v2 datasets; v3 can be viewed and validated"
        )
    tasks = read_jsonl(source / "meta/tasks.jsonl")
    episodes = read_jsonl(source / "meta/episodes.jsonl")
    updates = []
    unmapped = []
    for row in tasks:
        old = row["task"]
        new = options.task_map.get(old, old)
        if old != new:
            updates.append({"old": old, "new": new})
        elif "_" in old and " " not in old and old not in options.task_map:
            unmapped.append(old)
    if stage.startswith("tasks"):
        result = {"ok": not unmapped, "updates": updates, "unmapped": unmapped}
        if stage == "tasks-preview":
            return result
        if unmapped:
            raise ValueError(
                "Unmapped task identifiers: " + json.dumps(unmapped, ensure_ascii=False)
            )
    shutil.copytree(source, target)
    if stage == "timestamps":
        # FPS override must describe the real video rate; never relabel incompatible video.
        for p in (source / "videos").rglob("*.mp4"):
            if abs(media.probe(p)["fps"] - options.fps) > 0.01:
                raise ValueError(
                    "Timestamp repair FPS differs from video; normalize raw capture FPS first"
                )
        epstats = (
            read_jsonl(source / "meta/episodes_stats.jsonl")
            if (source / "meta/episodes_stats.jsonl").exists()
            else []
        )
        stats_map = {r["episode_index"]: r for r in epstats}
        for ep in episodes:
            i = ep["episode_index"]
            p = inside(
                info["data_path"].format(
                    episode_chunk=i // info["chunks_size"], episode_index=i
                ),
                target,
            )
            table = pq.read_table(p)
            old = table["timestamp"].to_pylist()
            atomic(target / f"meta/levi_original_timestamps/{i:06d}.json", old)
            new = np.arange(table.num_rows, dtype=np.float32) / options.fps
            table = table.set_column(
                table.schema.get_field_index("timestamp"), "timestamp", pa.array(new)
            )
            pq.write_table(table, p, compression="snappy")
            if i in stats_map:
                stats_map[i]["stats"]["timestamp"] = stats(new.reshape(-1, 1))
        info["fps"] = options.fps
        atomic(target / "meta/info.json", info)
        if epstats:
            jsonl(target / "meta/episodes_stats.jsonl", epstats)
            atomic(target / "meta/stats.json", aggregate(epstats))
        elif (target / "meta/stats.json").exists():
            # Avoid leaving misleading aggregate stats when episode stats cannot be refreshed.
            stored = json.loads((target / "meta/stats.json").read_text())
            stored.pop("timestamp", None)
            atomic(target / "meta/stats.json", stored)
    else:
        for row in tasks:
            row["task"] = options.task_map.get(row["task"], row["task"])
        for row in episodes:
            row["tasks"] = [options.task_map.get(t, t) for t in row.get("tasks", [])]
        jsonl(target / "meta/tasks.jsonl", tasks)
        jsonl(target / "meta/episodes.jsonl", episodes)
    return {"ok": True, "dataset_path": str(target)}
