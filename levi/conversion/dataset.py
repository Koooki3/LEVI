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
    """Convert an already-staged capture (every frame at ``options.fps``)
    into a dataset at ``target``: every row kept, no filtering. Runs the
    single-pass pipeline, so H.264 videos are stream-copied, not re-encoded."""
    from . import registry
    from .inputs.robot_capture import camera_info

    options = options.model_copy(
        update={"timing": "retime", "filter_static": False, "target": "lerobot_v21"}
    )
    for demo in raw.demos(source, options):
        for camera in options.cameras:
            try:
                rate = camera_info(demo, camera, options)["fps"]
            except ValueError:
                continue  # reported by the preflight
            if abs(rate - options.fps) > 0.01:
                raise ValueError(
                    f"Capture preflight failed: {demo.name}/{camera} video FPS "
                    f"{rate:g} differs from target {options.fps:g}; run fps normalization"
                )
    return Path(registry.export(source, target, options)["dataset_path"])


def validate(root: Path, measured: dict | None = None):
    """Native checks plus full video decoding and v2 row/media/metadata contracts.

    ``measured`` maps a video's path relative to ``root`` to the result of a
    full ``media.inspect`` decode already done by the caller (the conversion
    pipeline decodes every output once for its statistics); those files are
    not decoded a second time."""
    measured = measured or {}
    report = diagnose(root, max_episodes=0, decode_video=False)
    info = json.loads((root / "meta/info.json").read_text())
    failures = []
    videos = 0
    for path in sorted((root / "videos").rglob("*.mp4")):
        try:
            inside(path, root)
            v = measured.get(path.relative_to(root).as_posix()) or media.inspect(path)
            videos += 1
            # Measured from packet timestamps: the container's average rate
            # also counts the last frame's display time, which a lossless
            # retime cannot rescale.
            interval = media.frame_interval(path)
            measured_fps = 1 / interval if interval else v["fps"]
            if abs(measured_fps - info["fps"]) > 0.01:
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
        except Exception as exc:  # noqa: BLE001
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
            except Exception as exc:  # noqa: BLE001
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
