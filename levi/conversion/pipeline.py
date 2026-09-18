"""Raw capture → dataset in one pass per camera.

Flow (all writes go to a hidden sibling directory, renamed into place only
when every episode passed):

1. ``inspect`` (CSV + ffprobe, no decode) must have no failed requirement.
2. The parent plans every episode from its CSVs once: retained source rows,
   state/action, task, outcome, provenance — and writes the parquet files.
3. Worker processes handle one (episode, camera) each:
   - ``encode``: decode the source once — scanning every frame for the
     preflight (decoded count, frozen span, first-frame hash) while streaming
     the retained frames into the single H.264 encode — then decode the
     output once for its pixel statistics;
   - ``remux`` (every frame kept, H.264 source): stream-copy with rescaled
     timestamps (bit-identical pixels, no encode), then one decode of the
     output provides both the preflight and the statistics.
4. Preflight gate (all episodes), metadata, validation (reusing the measured
   decodes, no third pass), then publish.

Legacy cost was 9 decodes + 3 encodes per camera; this is 2 + 1 (encode) or
1 + 0 (remux).
"""

import json
import multiprocessing
import os
import shutil
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from ..catalog import atomic
from . import dataset, media, raw
from .episodes import SourceEpisode
from .inputs.robot_capture import output_fps, plan_positions
from .options import Options
from .progress import Progress

STAGES = ["Inspect", "Plan", "Convert episodes", "Validate", "Publish"]


def auto_workers() -> int:
    # Each worker runs one decoder plus a 2-thread encoder; leave most of a
    # shared machine free (robot sessions, other jobs).
    return max(1, min(4, (os.cpu_count() or 4) // 4))


def _init_worker():
    import cv2

    cv2.setNumThreads(1)


def process_camera(task: dict) -> dict:
    """Worker entry point: one camera of one episode. Returns measurements or
    ``{"error": str}`` — never raises across the process boundary."""
    try:
        destination = Path(task["destination"])
        positions = task["positions"]
        if task["mode"] == "remux":
            try:
                media.remux(
                    Path(task["source"]),
                    destination,
                    task["rate"],
                    task["fps"],
                    len(positions),
                )
                if not task.get("measure", True):
                    # Browsing view: the remux already verified the frame
                    # count and timing from packets; nothing is decoded.
                    out = {**media.probe(destination), "frames": len(positions)}
                    preflight = {
                        "frames": len(positions),
                        "span": None,
                        "first_hash": None,
                    }
                    return {"preflight": preflight, "output": out, "mode": "remux"}
                out = media.inspect(destination, pixels=True)
                preflight = {
                    "frames": out["frames"],
                    "span": out["span"],
                    "first_hash": out["first_hash"],
                }
                return {"preflight": preflight, "output": out, "mode": "remux"}
            except ValueError as exc:
                # Irregular source timing: fall back to a real encode.
                destination.unlink(missing_ok=True)
                fallback = str(exc)
        else:
            fallback = None
        scan = media.FrameScan()
        if task["images"]:
            files = [Path(p) for p in task["images"]]
            frames = raw.read_images(files, range(len(files)))
        else:
            frames = media.decode(Path(task["source"]))
        media.encode(
            media.scanned_selection(frames, positions, scan),
            destination,
            task["fps"],
            len(positions),
        )
        out = media.inspect(destination, pixels=True)
        result = {"preflight": scan.result(), "output": out, "mode": "encode"}
        if fallback:
            result["note"] = f"remux fallback: {fallback}"
        return result
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def _state(xyz, q, command, options):
    orientation = (
        raw.euler(q) if options.orientation == "euler" else raw.quaternion_continuous(q)
    )
    state = np.column_stack([xyz, orientation, command]).astype(np.float32)
    action = (
        np.vstack([state[1:], state[-1:]])
        if options.action_mode == "next_state"
        else state.copy()
    )
    return state, action


def _names(options):
    rotation = (
        ["rx", "ry", "rz"]
        if options.orientation == "euler"
        else ["qx", "qy", "qz", "qw"]
    )
    return ["x", "y", "z"] + rotation + ["gripper"]


def plan_episode(episode: SourceEpisode, options: Options, fps: float):
    demo = Path(episode.path)
    pose, _grip, xyz, q, command = raw.load(demo)
    rates = list(episode.camera_fps.values())
    source_fps = options.source_fps if episode.image_mode else min(rates)
    positions = plan_positions(xyz, q, command, source_fps, fps, options)
    if len(positions) < 2:
        raise ValueError(f"{episode.source_id}: fewer than two frames retained")
    if options.timing == "resample":
        # Stale-state runs are judged on the rows the dataset will contain.
        sampled = raw.sample_positions(len(pose), source_fps, fps)
        stamps = pose.source_stamp_sec.to_numpy()[sampled]
    else:
        stamps = pose.source_stamp_sec.to_numpy()
    longest = run = 0
    for same in np.diff(stamps) == 0:
        run = run + 1 if same else 0
        longest = max(longest, run)
    errors = []
    if longest >= options.stale_run:
        errors.append(f"Stale state source timestamp run: {longest}")
    state, action = _state(xyz[positions], q[positions], command[positions], options)
    keep_all = len(positions) == len(pose)
    return {
        "episode": episode,
        "rows": len(pose),
        "positions": positions,
        "source_fps": source_fps,
        "keep_all": keep_all,
        "state": state,
        "action": action,
        # A capture staged by an earlier LEVI run keeps the original ids and
        # times in source_* columns; provenance always points at the original.
        "frame_ids": pose.get("source_frame_index", pose.frame_index)
        .to_numpy()[positions]
        .tolist(),
        "capture_times": pose.get("source_timestamp_sec", pose.timestamp_sec)
        .to_numpy()[positions]
        .tolist(),
        "errors": errors,
    }


def run(
    source: Path,
    target: Path,
    options: Options,
    input_format,
    output_format,
    progress_path: Path | None = None,
    intermediate: Path | None = None,
    strict: bool = True,
):
    """``strict=False`` (browsing views): episodes whose CSVs cannot be
    planned are skipped and quality findings (stale state, frozen camera,
    duplicate first frames) are recorded instead of failing the run."""
    settings = output_format.target_options(options.target_options)
    progress = Progress(progress_path, STAGES)
    workers = options.workers or auto_workers()
    staging = target.parent / f".{target.name}.partial"
    if target.exists() or staging.exists():
        raise ValueError(f"Output directory already exists: {target}")

    # 1. Inspect: nothing is written unless every requirement passes.
    report = input_format.inspect(source, options, progress)
    if report.failed:
        failures = "; ".join(f"{r.label}: {r.detail}" for r in report.failed)
        raise ValueError(
            f"Capture preflight failed ({failures}). Source captures remain unchanged."
        )

    # 2. Plan every episode from its CSVs.
    episodes = input_format.episodes(source, options)
    for episode in episodes:
        label = options.outcome_labels.get(episode.source_id)
        if label:
            episode.outcome = label
            episode.metadata["outcome_source"] = "human"
    rates = [r for e in episodes for r in e.camera_fps.values()]
    fps, fps_note = output_fps(rates, options)
    if fps_note:
        progress.warn(fps_note)
        print(fps_note, flush=True)
    progress.stage("Plan", len(episodes))
    plans, errors, skipped = [], {}, {}
    for episode in episodes:
        try:
            planned = plan_episode(episode, options, fps)
            plans.append(planned)
            if planned["errors"]:
                errors[episode.source_id] = planned["errors"]
        except ValueError as exc:
            if strict:
                errors[episode.source_id] = [str(exc)]
            else:
                skipped[episode.source_id] = str(exc)
        progress.advance(episode.source_id)
    if not plans:
        raise ValueError("No episode could be planned: " + json.dumps(skipped))
    if errors and strict:
        raise ValueError(
            "Capture preflight failed: " + json.dumps(errors, ensure_ascii=False)
        )

    staging.mkdir(parents=True)
    try:
        result = _write(
            staging,
            source,
            options,
            output_format,
            settings,
            plans,
            fps,
            workers,
            progress,
            report,
            strict,
        )
        if intermediate is not None:
            _intermediates(source, intermediate, options, plans, fps)
        progress.stage("Publish", 1)
        if target.exists():
            raise ValueError(f"Output directory already exists: {target}")
        staging.rename(target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    progress.finish()
    return {
        "ok": True,
        "dataset_path": str(target),
        "validation": result["validation"],
        "excluded_demos": options.exclude_demos,
        "target": output_format.id,
        "fps": fps,
        "video_modes": result["video_modes"],
        **({"fps_note": fps_note} if fps_note else {}),
        **({"skipped": skipped} if skipped else {}),
        **({"quality_findings": errors} if errors else {}),
    }


def _write(
    root,
    source,
    options,
    output_format,
    settings,
    plans,
    fps,
    workers,
    progress,
    report,
    strict=True,
):
    tasks: dict[str, int] = {}
    for p in plans:
        tasks.setdefault(p["episode"].task, len(tasks))
    offset = 0
    jobs = []
    features = {}
    episodes_meta, epstats, provenance = [], [], []
    for ep, p in enumerate(plans):
        episode: SourceEpisode = p["episode"]
        n = len(p["positions"])
        cols = {
            "timestamp": np.arange(n, dtype=np.float32) / fps,
            "frame_index": np.arange(n, dtype=np.int64),
            "episode_index": np.full(n, ep, dtype=np.int64),
            "index": np.arange(offset, offset + n, dtype=np.int64),
            "task_index": np.full(n, tasks[episode.task], dtype=np.int64),
        }
        row = {
            "episode_index": ep,
            "tasks": [episode.task],
            "length": n,
            "source_demo": episode.source_id,
        }
        if episode.outcome is not None:
            row["levi_outcome"] = episode.outcome
        if episode.metadata.get("outcome_source"):
            row["levi_outcome_source"] = episode.metadata["outcome_source"]
        extra = output_format.episode_columns(n, row, settings)
        row.update(output_format.episode_fields(row, settings))
        table = {
            **cols,
            "action": pa.array(p["action"].tolist(), type=pa.list_(pa.float32())),
            "observation.state": pa.array(
                p["state"].tolist(), type=pa.list_(pa.float32())
            ),
        }
        for key, (values, _feature) in extra.items():
            table[key] = values
        part = root / f"data/chunk-{ep // 1000:03d}/episode_{ep:06d}.parquet"
        part.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table(table), part, compression="snappy")
        stats = {k: dataset.stats(v.reshape(-1, 1)) for k, v in cols.items()}
        stats.update(
            {
                "action": dataset.stats(p["action"]),
                "observation.state": dataset.stats(p["state"]),
            }
        )
        for key, (values, feature) in extra.items():
            stats[key] = dataset.stats(
                np.asarray(values, dtype=np.float64).reshape(n, -1)
            )
            features[key] = feature
        episodes_meta.append(row)
        epstats.append({"episode_index": ep, "stats": stats})
        provenance.append(
            {
                "episode_index": ep,
                "source_demo": episode.source_id,
                "source_positions": p["positions"].tolist(),
                "source_frame_ids": p["frame_ids"],
                "source_capture_timestamps": p["capture_times"],
            }
        )
        for camera, key in options.cameras.items():
            images = None
            if episode.image_mode:
                images = [str(f) for f in raw.image_files(Path(episode.path), camera)]
                if len(images) != p["rows"]:
                    raise ValueError(
                        f"{episode.source_id}: {camera} image and CSV counts differ"
                    )
            probe = episode.metadata.get("probe", {}).get(camera, {})
            mode = (
                "remux"
                if p["keep_all"] and not images and probe and media.remuxable(probe)
                else "encode"
            )
            jobs.append(
                {
                    "measure": strict,
                    "episode": ep,
                    "source_id": episode.source_id,
                    "camera": camera,
                    "key": key,
                    "mode": mode,
                    "rows": p["rows"],
                    "source": episode.cameras.get(camera),
                    "images": images,
                    "source_fps": p["source_fps"],
                    "rate": probe.get("rate", "0/1"),
                    "fps": fps,
                    "positions": p["positions"].tolist(),
                    "destination": str(
                        root
                        / f"videos/chunk-{ep // 1000:03d}/{key}/episode_{ep:06d}.mp4"
                    ),
                }
            )
        offset += n

    # Video work in worker processes; the parent alone writes progress.
    progress.stage("Convert episodes", len(jobs))
    results = {}
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(
        workers, mp_context=context, initializer=_init_worker
    ) as pool:
        pending = {pool.submit(process_camera, job): job for job in jobs}
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                job = pending.pop(future)
                results[(job["episode"], job["camera"])] = future.result()
                progress.advance(f"{job['source_id']} · {job['camera']}")

    # Preflight gate over the scan results, same rules as the legacy audit.
    preflight, first_frames, measured, modes = {}, {}, {}, {}
    for job in jobs:
        res = results[(job["episode"], job["camera"])]
        problems = preflight.setdefault(job["source_id"], [])
        if "error" in res:
            problems.append(f"{job['camera']}: {res['error']}")
            continue
        scan = res["preflight"]
        expected = job["rows"] if res["mode"] == "encode" else len(job["positions"])
        if scan["frames"] != expected:
            problems.append(
                f"{job['camera']}: {scan['frames']} video frames vs {job['rows']} CSV rows"
            )
        if scan["span"] is not None and scan["span"] < options.frozen_threshold:
            problems.append(f"{job['camera']}: frozen camera (span {scan['span']:.4f})")
        if scan["first_hash"] is not None:
            first_frames.setdefault((job["camera"], scan["first_hash"]), []).append(
                job["source_id"]
            )
        rel = Path(job["destination"]).relative_to(root).as_posix()
        measured[rel] = res["output"]
        modes[res["mode"]] = modes.get(res["mode"], 0) + 1
        if res.get("note"):
            progress.warn(f"{job['source_id']} {job['camera']}: {res['note']}")
    duplicates = [
        {"camera": camera, "demos": demos}
        for (camera, _), demos in first_frames.items()
        if len(demos) > 1
    ]
    failed = {k: v for k, v in preflight.items() if v}
    # Without a readable, complete video there is nothing to publish, even
    # for a browsing view; frozen cameras are only findings there.
    hard = {k: [p for p in v if "frozen camera" not in p] for k, v in failed.items()}
    hard = {k: v for k, v in hard.items() if v}
    preflight_report = {
        "ok": not failed,
        "input": report.model_dump(),
        "episodes": preflight,
        "duplicate_first_frames": duplicates,
        "excluded_demos": options.exclude_demos,
    }
    atomic(root / "meta/levi_preflight.json", preflight_report)
    if failed if strict else hard:
        raise ValueError(
            "Capture preflight failed: "
            + json.dumps(failed if strict else hard, ensure_ascii=False)
        )

    camera_shapes = {}
    for job in jobs:
        out = measured[Path(job["destination"]).relative_to(root).as_posix()]
        shape = [out["height"], out["width"], 3]
        if camera_shapes.setdefault(job["key"], shape) != shape:
            raise ValueError("Camera resolution changes between episodes")
        if "stats" in out:  # absent for browsing views (nothing decoded)
            epstats[job["episode"]]["stats"][job["key"]] = out["stats"]
        features[job["key"]] = {
            "dtype": "video",
            "shape": shape,
            "names": ["height", "width", "channels"],
            "info": {
                "video.height": out["height"],
                "video.width": out["width"],
                "video.codec": out["codec"],
                "video.pix_fmt": out["pixel_format"],
                "video.fps": fps,
                "video.channels": 3,
                "video.is_depth_map": False,
                "has_audio": False,
            },
        }
    for key in ["timestamp", "frame_index", "episode_index", "index", "task_index"]:
        features[key] = {
            "dtype": "float32" if key == "timestamp" else "int64",
            "shape": [1],
            "names": None,
        }
    names = _names(options)
    for key in ["action", "observation.state"]:
        features[key] = {"dtype": "float32", "shape": [len(names)], "names": names}

    total = offset
    info = {
        "codebase_version": "v2.1",
        "robot_type": options.robot_type,
        "fps": fps,
        "total_episodes": len(episodes_meta),
        "total_frames": total,
        "total_tasks": len(tasks),
        "total_videos": len(episodes_meta) * len(options.cameras),
        "total_chunks": (len(episodes_meta) + 999) // 1000,
        "chunks_size": 1000,
        "splits": {"train": f"0:{len(episodes_meta)}"},
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
        "features": features,
    }
    atomic(root / "meta/info.json", info)
    dataset.jsonl(root / "meta/episodes.jsonl", episodes_meta)
    dataset.jsonl(root / "meta/episodes_stats.jsonl", epstats)
    dataset.jsonl(
        root / "meta/tasks.jsonl",
        [{"task_index": i, "task": text} for text, i in tasks.items()],
    )
    atomic(root / "meta/stats.json", dataset.aggregate(epstats))
    dataset.jsonl(root / "meta/levi_provenance.jsonl", provenance)
    output_format.finalize(root, episodes_meta, info, settings)
    atomic(
        root / "meta/levi_conversion.json",
        {
            "schema": "levi.conversion.v2",
            "target": output_format.id,
            "input_format": report.format,
            "input_variant": report.variant,
            "source": str(source),
            "options": options.model_dump(),
            "target_options": settings.model_dump(),
            "fps": fps,
            "timing": options.timing,
            "video_modes": modes,
            "action_semantics": options.action_mode,
            "rotation_units": "radians"
            if options.orientation == "euler"
            else "unit quaternion xyzw",
            "position_units": "metres",
            "gripper": "command: open=1, close=0; not measured aperture",
            "image_statistics": "all decoded output pixels, RGB normalized to [0,1]",
        },
    )

    progress.stage("Validate", 1)
    validation = dataset.validate(root, measured=measured)
    atomic(root / "meta/levi_validation.json", validation)
    if not validation["ok"]:
        raise ValueError(
            "Converted dataset failed validation: "
            + "; ".join(validation["failures"][:10])
        )
    return {"validation": validation, "video_modes": modes}


def _intermediates(source, directory, options, plans, fps):
    """Opt-in audit copies of the legacy stages: every demo resampled
    (``staged``) and after the static filter (``filtered``)."""
    directory.mkdir(parents=True, exist_ok=True)
    staged_options = options.model_copy(update={"fps": fps})
    for p in plans:
        demo = Path(p["episode"].path)
        relative = p["episode"].source_id
        pose, *_ = raw.load(demo)
        if options.timing == "retime":
            sample = np.arange(len(pose))
        else:
            sample = raw.sample_positions(len(pose), p["source_fps"], fps)
        raw.transform_demo(
            demo,
            directory / "staged" / relative,
            sample,
            staged_options,
            p["episode"].image_mode,
        )
        raw.copy_task(demo, directory / "staged" / relative, options)
        if options.filter_static:
            raw.transform_demo(
                demo,
                directory / "filtered" / relative,
                p["positions"],
                staged_options,
                p["episode"].image_mode,
            )
            raw.copy_task(demo, directory / "filtered" / relative, options)
