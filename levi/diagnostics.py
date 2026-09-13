"""Version-aware, inspectable diagnostics. Heuristics are not training guarantees."""

import json
from pathlib import Path
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from .paths import inside

from .versions import SUPPORTED_DATASET_VERSIONS, normalize_dataset_version

SUPPORTED_VERSIONS = set(SUPPORTED_DATASET_VERSIONS)


CHECKS = [
    "metadata",
    "temporal",
    "action",
    "video",
    "distribution",
    "episodes",
    "features",
    "training",
    "anomaly",
    "portability",
]


def _finite_number(value, fallback=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return number if np.isfinite(number) else fallback


def _task_texts(value):
    if isinstance(value, str):
        value = value.strip()
        return [value] if value else []
    if isinstance(value, (list, tuple, set)):
        output = []
        for item in value:
            output.extend(_task_texts(item))
        return output
    return []


def _task_metadata_summary(path):
    """Return unique task labels and indices from either metadata encoding."""
    if not path.exists():
        return set(), set()
    try:
        if path.suffix == ".parquet":
            rows = pq.read_table(path).to_pylist()
        else:
            rows = []
            for line in path.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except Exception:  # noqa: BLE001
        return set(), set()

    labels = set()
    indices = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in (
            "task",
            "tasks",
            "task_name",
            "name",
            "instruction",
            "__index_level_0__",
        ):
            labels.update(_task_texts(row.get(key)))
        value = row.get("task_index")
        if value is not None:
            try:
                index = int(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if index >= 0:
                indices.add(index)
    return labels, indices


def diagnose(root: Path, max_episodes=20, checks=None, decode_video=False):
    chosen = checks or CHECKS
    info = json.loads(inside("meta/info.json", root).read_text())
    fps = _finite_number(info.get("fps", 0))
    total_episodes = max(0, int(_finite_number(info.get("total_episodes", 0))))
    total_frames = max(0, int(_finite_number(info.get("total_frames", 0))))
    total_tasks = max(0, int(_finite_number(info.get("total_tasks", 0))))
    try:
        episode_limit = max(0, int(max_episodes or 0))
    except (TypeError, ValueError, OverflowError):
        episode_limit = 0
    results = []
    frames = 0
    episodes = {}
    flagged = set()

    def add(check, status, message, episode=None):
        if check in chosen:
            results.append(
                {
                    "check": check,
                    "status": status,
                    "message": message,
                    "episode": episode,
                }
            )
            if episode is not None and status in ("warn", "fail"):
                flagged.add(int(episode))

    raw_version = info.get("codebase_version", "")
    version = normalize_dataset_version(raw_version)
    display_version = str(raw_version)
    add(
        "metadata",
        "pass" if version else "fail",
        f"codebase_version = {display_version}"
        + (
            f" (normalized {version})" if version and display_version != version else ""
        ),
    )
    if fps <= 0:
        add("metadata", "fail", "fps must be positive")
    task_candidates = [
        root / "meta/tasks.parquet",
        root / "meta/tasks.jsonl",
    ]
    task_summaries = [
        (path, *_task_metadata_summary(path))
        for path in task_candidates
        if path.exists()
    ]
    if task_summaries:
        task_path, task_labels, task_indices = max(
            task_summaries,
            key=lambda item: len(item[1]) or len(item[2]),
        )
    else:
        task_path = task_candidates[0]
        task_labels, task_indices = set(), set()
    task_count = len(task_labels) or len(task_indices)
    add(
        "metadata",
        "pass"
        if task_path.exists() and (task_count > 0 or total_tasks == 0)
        else "fail"
        if total_tasks > 0
        else "pass",
        f"Task metadata: {task_path.name}"
        + (f" ({task_count} unique task(s))" if task_count else ""),
    )
    if task_path.exists() and total_tasks > 0 and task_count != total_tasks:
        add(
            "metadata",
            "fail",
            f"Task total: read {task_count}, metadata {total_tasks}",
        )
    files = sorted((root / "data").rglob("*.parquet"))
    if not files:
        add("metadata", "fail", "No data parquet files")
    for path in files:
        inside(path, root)
        try:
            table = pq.read_table(path)
            data = table.to_pandas()
        except Exception as exc:
            add("portability", "fail", f"{path.name}: {exc}")
            continue
        if "episode_index" not in data:
            add("features", "fail", f"{path.name}: missing episode_index")
            continue
        episode_values = pd.to_numeric(data["episode_index"], errors="coerce")
        invalid_episode = (
            episode_values.isna()
            | (~np.isfinite(episode_values.to_numpy(dtype=float)))
            | (episode_values % 1 != 0)
            | (episode_values < 0)
        )
        if invalid_episode.any():
            add("features", "fail", f"{path.name}: invalid episode_index values")
        valid = ~invalid_episode
        data = data.loc[valid].copy()
        data["episode_index"] = episode_values.loc[valid].astype("int64")
        for ep, part in data.groupby("episode_index", sort=True):
            ep = int(ep)
            if episode_limit and ep not in episodes and len(episodes) >= episode_limit:
                continue
            episodes.setdefault(ep, []).append(part)
        if episode_limit and len(episodes) >= episode_limit:
            # v3 episodes may cross shards: continue scanning to finish selected episodes.
            continue
    for ep, parts in episodes.items():
        data = pd.concat(parts, ignore_index=True)
        frames += len(data)
        if len(data) < 2:
            add("episodes", "fail", "Episode has fewer than two frames", ep)
        missing = set(info.get("features", {})) - set(data.columns)
        missing = {
            k
            for k in missing
            if info["features"][k].get("dtype") not in ("video", "image")
        }
        if missing:
            add(
                "features", "fail", "Missing columns: " + ", ".join(sorted(missing)), ep
            )
        if "timestamp" in data:
            ts = pd.to_numeric(data["timestamp"], errors="coerce").to_numpy(dtype=float)
            delta = np.diff(ts)
            if not np.isfinite(ts).all() or (delta <= 0).any():
                add(
                    "temporal",
                    "fail",
                    "Non-finite, repeated or reversed timestamps",
                    ep,
                )
            elif fps > 0 and (np.abs(delta - 1 / fps) > max(1e-4, 0.1 / fps)).any():
                add(
                    "temporal",
                    "warn",
                    "Timestamp gaps differ from nominal FPS by more than 10%",
                    ep,
                )
        else:
            add("temporal", "fail", "Missing timestamp column", ep)
        if "frame_index" in data:
            frame_index = pd.to_numeric(data["frame_index"], errors="coerce").to_numpy(
                dtype=float
            )
            if not np.isfinite(frame_index).all() or (np.diff(frame_index) != 1).any():
                add("temporal", "warn", "Non-consecutive frame indices", ep)
        for key in ("action", "observation.state"):
            if key not in data:
                continue
            try:
                values = np.stack(data[key]).astype(float).reshape(len(data), -1)
            except (ValueError, TypeError):
                add("features", "fail", f"{key}: inconsistent numeric shape", ep)
                continue
            shape = info.get("features", {}).get(key, {}).get("shape")
            try:
                expected_size = int(np.prod(shape)) if shape else None
            except (TypeError, ValueError, OverflowError):
                expected_size = None
                add("features", "fail", f"{key}: invalid shape metadata", ep)
            if expected_size is not None and expected_size != values.shape[1]:
                add("features", "fail", f"{key}: shape disagrees with metadata", ep)
            if not np.isfinite(values).all():
                add("distribution", "fail", f"{key}: NaN or infinity", ep)
                continue
            if len(values) > 1:
                diff = np.diff(values, axis=0)
                mean = np.mean(np.abs(diff), axis=1)
                if key == "action":
                    if np.max(np.abs(diff)) < 1e-8:
                        add("action", "warn", "Frozen actions throughout episode", ep)
                    if mean.std() > 0 and (mean > mean.mean() + 8 * mean.std()).any():
                        add(
                            "action",
                            "warn",
                            "Sudden action jump above mean + 8 standard deviations",
                            ep,
                        )
                    stuck = np.flatnonzero(np.ptp(values, axis=0) < 1e-8).tolist()
                    if stuck:
                        add(
                            "anomaly",
                            "warn",
                            f"Constant actuator dimensions (may be intentional): {stuck}",
                            ep,
                        )
            std = values.std(axis=0)
            outliers = int(
                (
                    np.abs(values - values.mean(axis=0)) > 10 * np.maximum(std, 1e-12)
                ).sum()
            )
            if outliers:
                add(
                    "distribution",
                    "warn",
                    f"{key}: {outliers} values beyond 10 standard deviations",
                    ep,
                )
    for key, feature in info.get("features", {}).items():
        if feature.get("dtype") != "video":
            continue
        if not decode_video:
            add("video", "skip", f"{key}: decoding not requested")
            continue
        import cv2

        videos = sorted((root / "videos").rglob("*.mp4"))
        videos = [p for p in videos if key in p.parts]
        if not videos:
            add("video", "fail", f"{key}: no local video files")
        for path in videos[: episode_limit or None]:
            inside(path, root)
            cap = cv2.VideoCapture(str(path))
            count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            rate = cap.get(cv2.CAP_PROP_FPS)
            samples = []
            for position in sorted({0, max(0, count // 2), max(0, count - 1)}):
                cap.set(cv2.CAP_PROP_POS_FRAMES, position)
                ok, frame = cap.read()
                if not ok:
                    add("video", "fail", f"{path.name}: cannot decode frame {position}")
                else:
                    samples.append(cv2.resize(frame, (32, 32)).astype(float))
            cap.release()
            if rate > 0 and fps > 0 and abs(rate - fps) > 0.1:
                add(
                    "video",
                    "warn",
                    f"{path.name}: video FPS {rate:.3f}, metadata FPS {fps}",
                )
            if len(samples) > 1 and all(
                np.mean(np.abs(s - samples[0])) < 0.1 for s in samples[1:]
            ):
                add("video", "warn", f"{path.name}: sampled frames appear frozen")
    if (
        not (root / "meta/stats.json").exists()
        and not (root / "meta/episodes_stats.jsonl").exists()
    ):
        add(
            "training",
            "warn",
            "Stored normalization statistics missing; compute before training",
        )
    for key, value in info.items():
        if (
            key.endswith("_path")
            and isinstance(value, str)
            and (value.startswith("/") or ".." in Path(value).parts)
        ):
            add("portability", "fail", f"{key}: non-portable path")
    complete = len(episodes) == total_episodes
    if complete and frames != total_frames:
        add(
            "metadata",
            "fail",
            f"Frame total: read {frames}, metadata {total_frames}",
        )
    if not complete and (not episode_limit or episode_limit >= total_episodes):
        add(
            "metadata",
            "fail",
            f"Episode total: read {len(episodes)}, metadata {total_episodes}",
        )
    if not complete:
        add(
            "episodes",
            "skip",
            f"Sampled {len(episodes)} of {total_episodes} episodes",
        )
    for check in chosen:
        if not any(r["check"] == check for r in results):
            add(check, "pass", "No issues detected in inspected data")
    counts = {
        s: sum(r["status"] == s for r in results)
        for s in ("pass", "warn", "fail", "skip")
    }
    # results is a JSON report field, not an output directory.
    return {
        "version": 1,
        "dataset_version": version or display_version,
        "episodes": len(episodes),
        "frames": frames,
        "fps": fps,
        "scope": "all" if complete else "sample",
        "counts": counts,
        "status": "fail" if counts["fail"] else "warn" if counts["warn"] else "pass",
        "results": results,
        "flagged_episodes": sorted(flagged),
        "method": "LEVI native checks; video checks sample first/middle/last frames; thresholds are heuristics",
    }
