"""Exports of a recipe's episodes into a new dataset (docs/TRAINING_POOL.md).

Formats:

- ``lerobot_v21``: one merged LeRobot v2.1 dataset for openpi / pi0.5.
  Raw captures go through LEVI's conversion pipeline (one pass, the same
  options as ``levi convert``); LeRobot v2.x episodes are copied with new
  indices. Episodes follow the recipe's task order, then its source order.
  Camera keys become ``observation.images.hand`` / ``observation.images.view1``
  (``cameras`` for raw captures, ``camera_map`` for LeRobot sources); every
  source must share the output fps, the state and action dimensions and the
  video resolution — otherwise the export is refused with the differences.
- ``recap_value``: the same, plus RECAP rewards, returns and labels
  (``outputs/recap_value.py``); the outcome is the human label, else the
  robot's flag; episodes with neither are left out and listed.
- ``raw_capture``: raw capture folders copied (or hard-linked) into
  ``<task>/demo_NNNN`` renumbered per task, with ``task_description.txt``.

Everything is written to ``.<name>.partial`` next to the target and renamed
when complete. ``pool_export.json`` records the recipe, the task order, every
episode's source path and fingerprint, every exclusion and its reason, the
LEVI commit and the format parameters. Held-out episodes are refused.
"""

import json
import os
import re
import shutil
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..catalog import atomic
from ..conversion import dataset, media, pipeline
from ..conversion.episodes import SourceEpisode
from ..conversion.inputs.base import InputFormat
from ..conversion.inputs.robot_capture import RobotCapture, camera_info
from ..conversion.options import Options
from ..conversion.outputs.lerobot_v21 import LeRobotV21
from ..conversion.outputs.recap_value import RECAP_COLUMNS, RecapOptions, RecapValue
from ..conversion.progress import Progress
from ..conversion.report import InputReport, Requirement
from . import heldout, index, scanner, settings
from .recipe import NAME, Recipe, select

SCHEMA = "levi.pool.export.v1"
FORMATS = ("lerobot_v21", "recap_value", "raw_capture")
STAGES = ["Check", "Convert raw captures", "Merge", "Validate", "Publish"]
BASE_COLUMNS = (
    "timestamp",
    "frame_index",
    "episode_index",
    "index",
    "task_index",
    "observation.state",
    "action",
)
DATA_PATH = "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet"
VIDEO_PATH = (
    "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4"
)


class ExportOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    format: Literal[FORMATS]  # type: ignore[valid-type]
    name: str = Field(pattern=NAME)
    # Parent folder of the export (default <workspace>/exports/pool); must
    # lie inside LEVI_EXPORT_ROOTS.
    output_dir: str | None = None
    fps: float = Field(10, ge=1, le=240)
    # Raw capture camera -> output key (the pipeline's cameras option).
    cameras: dict[str, str] = Field(default_factory=lambda: dict(Options().cameras))
    # LeRobot source video key -> output key; keys already named like the
    # output need no entry.
    camera_map: dict[str, str] = Field(default_factory=dict)
    # Conversion of raw captures; None = the format's default (lerobot:
    # resample + static filter; recap: retime, every step kept).
    timing: Literal["resample", "retime"] | None = None
    filter_static: bool | None = None
    robot_type: str | None = Field(None, min_length=1, max_length=100)
    # raw_capture: hard-link files instead of copying them (same filesystem;
    # the export then shares its bytes with the source — never edit it).
    hardlink: bool = False
    # recap_value
    failure_reward: float = Field(-300.0, le=0, ge=-1e6)
    gamma: float = Field(1.0, gt=0, le=1)
    workers: int | None = Field(None, ge=1, le=64)

    @field_validator("camera_map")
    @classmethod
    def _map(cls, value):
        for key, feature in value.items():
            if not re.fullmatch(r"observation\.images\.[A-Za-z0-9_.-]+", feature):
                raise ValueError("camera_map values must start observation.images.")
            if not key:
                raise ValueError("camera_map keys must be nonempty")
        return value

    def target(self) -> Path:
        parent = (
            Path(self.output_dir).expanduser()
            if self.output_dir
            else settings.default_export_parent()
        )
        return parent / self.name

    def conversion(self) -> Options:
        recap = self.format == "recap_value"
        return Options(
            fps=self.fps,
            cameras=self.cameras,
            timing=self.timing or ("retime" if recap else "resample"),
            filter_static=(not recap)
            if self.filter_static is None
            else self.filter_static,
            robot_type=self.robot_type or "generic_arm",
            workers=self.workers,
            target="lerobot_v21",
        )


# ------------------------------------------------------------------ plan


def _all_source_paths() -> list[str]:
    try:
        return [s["path"] for s in index.sources(show_archive=True)]
    except ValueError:
        return []


def plan(recipe: Recipe, options: ExportOptions) -> dict:
    """Freeze the selection now: the export is reproducible from its plan."""
    chosen, excluded = select(recipe, target=options.format)
    if not chosen:
        raise ValueError(
            "The recipe selects no exportable episode"
            + (
                f" ({dict(Counter(e['reason'] for e in excluded))} excluded)"
                if excluded
                else ""
            )
        )
    sources = sorted({r["source_path"] for r in chosen} | set(_all_source_paths()))
    target = settings.check_export_target(options.target(), sources)
    keep = (
        "key",
        "source",
        "source_path",
        "format",
        "episode",
        "episode_index",
        "task",
        "task_raw",
        "frames",
        "fps",
        "state_dim",
        "action_dim",
        "category",
        "robot_flag",
        "human_label",
        "outcome",
        "outcome_source",
        "fingerprint",
        "group",
        "stat_sig",
    )
    episodes = [{k: row.get(k) for k in keep} for row in chosen]
    return {
        "schema": SCHEMA,
        "recipe": recipe.model_dump(),
        "options": options.model_dump(),
        "target": str(target),
        "sources": sources,
        "episodes": episodes,
        "excluded": excluded,
        "index": index.summary().get("scanned_at"),
        "pool_roots": [str(p) for p in settings.pool_roots()],
        "heldout_lists": [str(p) for p in settings.heldout_files()],
    }


# ------------------------------------------------------------------ guards


def refuse_heldout(episodes: list[dict], roots: list[Path], lists: list[Path]):
    """Independent of the index: no planned episode may be on a held-out
    list, by path or by a video's sha256. Refuses the whole export."""
    entries = heldout.load(lists)
    if not entries:
        return
    paths = set()
    for entry in entries:
        p = Path(entry["path"])
        for candidate in [p] if p.is_absolute() else [r / p for r in roots]:
            paths.add(str(candidate))
    by_frames = defaultdict(list)
    for entry in entries:
        if entry["sha256"]:
            by_frames[entry["frame_count"]].append(entry)
    hits = []
    cache: dict[Path, str] = {}

    def digest(video: Path) -> str:
        if video not in cache:
            cache[video] = heldout.sha256(video)
        return cache[video]

    for ep in episodes:
        if ep["key"] in paths:
            hits.append(ep["key"])
            continue
        if ep["format"] != "robot_capture":
            continue
        for entry in by_frames.get(ep["frames"], []) + by_frames.get(None, []):
            name, want = next(iter(entry["sha256"].items()))
            video = Path(ep["key"]) / name
            if video.is_file() and digest(video) == want:
                hits.append(f"{ep['key']} ({entry['id']})")
                break
    if hits:
        raise PermissionError(
            f"Refusing to export {len(hits)} held-out episode(s): "
            + ", ".join(hits[:10])
            + (" …" if len(hits) > 10 else "")
        )


def refuse_heldout_groups(episodes: list[dict]):
    """The current index's view: no planned episode may share a group (a
    copy, a filtered variant or a conversion) with a held-out episode."""
    try:
        df = index.frame()
    except ValueError:
        return
    held = set(df.loc[df.heldout.astype(bool), "group"])
    groups = dict(zip(df.key, df.group, strict=True))
    hits = [
        e["key"]
        for e in episodes
        if groups.get(e["key"], e.get("group")) in held or e.get("group") in held
    ]
    if hits:
        raise PermissionError(
            f"Refusing to export {len(hits)} held-out episode(s) (copies of a "
            "frozen test episode): " + ", ".join(hits[:10])
        )


def _unchanged(episodes: list[dict]):
    changed = []
    for ep in episodes:
        if ep["format"] == "robot_capture":
            if (
                scanner._sig(
                    Path(ep["key"]), [Path(ep["key"]).parent / "task_description.txt"]
                )
                != ep["stat_sig"]
            ):
                changed.append(ep["key"])
        elif ep["format"] == "lerobot":
            root = Path(ep["source_path"])
            markers = scanner.rules_mod.load(settings.pool_dir())["levi_markers"]
            if (
                scanner._sig(
                    root / "meta", [root / m for m in markers if (root / m).is_file()]
                )
                != ep["stat_sig"]
            ):
                changed.append(ep["key"])
    if changed:
        raise ValueError(
            f"{len(changed)} source episode(s) changed since the pool was scanned "
            f"(e.g. {changed[0]}); run `levi pool scan` and plan again"
        )


# ------------------------------------------------------------------ inputs


class PoolCaptures(InputFormat):
    """The planned raw episodes as one conversion input, in export order.

    Each demo is inspected with the robot-capture checks; failing demos are
    left out (and listed) instead of failing the whole export."""

    id = "robot_capture"
    label = "Training pool raw captures"

    def __init__(self, episodes: list[tuple[str, dict]], texts: dict[str, str]):
        self.items = episodes
        self.texts = texts
        self.dropped: dict[str, list[str]] = {}

    def inspect(self, root, options, progress=None) -> InputReport:
        capture = RobotCapture()
        if progress:
            progress.stage("Inspect", len(self.items))

        def check(item):
            pid, ep = item
            demo = Path(ep["key"])
            return pid, capture._inspect_demo(demo.parent, demo, options)

        with ThreadPoolExecutor(max_workers=8) as pool:
            for pid, found in pool.map(check, self.items):
                if found["codes"]:
                    self.dropped[pid] = list(found["codes"].values())
                if progress:
                    progress.advance(pid)
        report = InputReport(
            source=str(root), format=self.id, label=self.label, variant="mixed"
        )
        kept = len(self.items) - len(self.dropped)
        report.requirements = [
            Requirement(
                id="layout",
                label="Planned raw episodes",
                status="pass" if kept else "fail",
                detail=f"{kept} of {len(self.items)} pass the capture checks",
            )
        ]
        report.summary = {"demos": kept, "dropped": len(self.dropped)}
        return report

    def episodes(self, root, options) -> list[SourceEpisode]:
        def build(item):
            pid, ep = item
            demo = Path(ep["key"])
            cameras, rates, probes = {}, {}, {}
            for camera in options.cameras:
                info = camera_info(demo, camera, options)
                from ..conversion import raw

                cameras[camera] = str(raw.camera_path(demo, camera))
                rates[camera] = info["fps"]
                probes[camera] = info
            episode = SourceEpisode(
                source_id=pid,
                path=str(demo),
                task=self.texts.get(ep["task"], ep["task"]),
                outcome=ep.get("outcome"),
                cameras=cameras,
                camera_fps=rates,
                metadata={"probe": probes},
            )
            if ep.get("outcome_source") == "human":
                episode.metadata["outcome_source"] = "human"
            return episode

        todo = [i for i in self.items if i[0] not in self.dropped]
        with ThreadPoolExecutor(max_workers=8) as pool:
            return list(pool.map(build, todo))


# ------------------------------------------------------------------ helpers


def _info(root: Path) -> dict:
    return json.loads((root / "meta/info.json").read_text())


def _video_keys(info: dict) -> list[str]:
    return [k for k, f in info["features"].items() if f.get("dtype") == "video"]


def _mapping(info: dict, camera_map: dict) -> dict[str, str]:
    return {k: camera_map.get(k, k) for k in _video_keys(info)}


def _schema_check(plan_eps: list[dict], options: ExportOptions) -> dict:
    """The output schema, or a refusal listing how the sources differ."""
    conv = options.conversion()
    raw_eps = [e for e in plan_eps if e["format"] == "robot_capture"]
    lerobot = sorted({e["source_path"] for e in plan_eps if e["format"] == "lerobot"})
    names = pipeline._names(conv)
    expected = {
        "fps": options.fps,
        "state": len(names) if raw_eps else None,
        "action": len(names) if raw_eps else None,
        "cameras": sorted(conv.cameras.values()) if raw_eps else None,
        "names": names if raw_eps else None,
    }
    problems = []
    infos = {}
    shapes: dict[str, dict] = defaultdict(dict)
    for path in lerobot:
        info = _info(Path(path))
        infos[path] = info
        features = info["features"]
        state = (features.get("observation.state", {}).get("shape") or [None])[0]
        action = (features.get("action", {}).get("shape") or [None])[0]
        cameras = sorted(_mapping(info, options.camera_map).values())
        if expected["state"] is None:
            expected.update(
                state=state,
                action=action,
                cameras=cameras,
                names=features.get("observation.state", {}).get("names"),
            )
        if abs(float(info.get("fps") or 0) - options.fps) > 0.01:
            problems.append(
                f"{path}: fps {info.get('fps')} (export fps {options.fps:g})"
            )
        if state != expected["state"]:
            problems.append(
                f"{path}: observation.state has {state} dims, expected {expected['state']}"
            )
        if action != expected["action"]:
            problems.append(
                f"{path}: action has {action} dims, expected {expected['action']}"
            )
        if cameras != expected["cameras"]:
            problems.append(
                f"{path}: cameras {cameras} after camera_map, expected {expected['cameras']}"
            )
        for key, out in _mapping(info, options.camera_map).items():
            shapes[out][path] = features[key].get("shape")
    for out, per in shapes.items():
        if len({json.dumps(s) for s in per.values()}) > 1:
            problems.append(f"{out}: resolutions differ between sources {per}")
    if problems:
        raise ValueError(
            "The selected sources do not share one schema; nothing was written:\n- "
            + "\n- ".join(problems)
        )
    return {"expected": expected, "infos": infos}


def _link_or_copy(src: Path, dst: Path, hardlink: bool):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if hardlink:
        try:
            os.link(src, dst)
            return
        except OSError:
            pass
    shutil.copy2(src, dst)


def _list_f32(values) -> pa.Array:
    return pa.array(
        [np.asarray(v, dtype=np.float32).tolist() for v in values],
        type=pa.list_(pa.float32()),
    )


def _levi_commit():
    from ..recap.jobs import _levi_commit as commit

    return commit()


# ------------------------------------------------------------------ run


def run(job: dict, progress_path: Path | None = None) -> dict:
    options = ExportOptions.model_validate(job["options"])
    target = Path(job["target"])
    sources = job["sources"]
    settings.check_export_target(target, sources)
    staging = target.parent / f".{target.name}.partial"
    settings.guard_write(staging, sources)
    progress = Progress(progress_path, STAGES)
    progress.stage("Check", len(job["episodes"]))
    episodes = job["episodes"]
    refuse_heldout(
        episodes,
        [Path(p) for p in job["pool_roots"]],
        [Path(p) for p in job["heldout_lists"]],
    )
    refuse_heldout_groups(episodes)
    _unchanged(episodes)
    staging.mkdir(parents=True)
    try:
        if options.format == "raw_capture":
            result = _raw_capture(job, options, staging, progress)
        else:
            result = _lerobot(job, options, staging, progress, progress_path)
        record = {
            "schema": SCHEMA,
            "name": options.name,
            "format": options.format,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "levi_commit": _levi_commit(),
            "recipe": job["recipe"],
            "task_order": result["task_order"],
            "params": options.model_dump(),
            "index_scanned_at": job.get("index"),
            "pool_roots": job["pool_roots"],
            "heldout_lists": job["heldout_lists"],
            "counts": {
                "episodes": len(result["episodes"]),
                "frames": result["frames"],
                "excluded": dict(
                    Counter(e["reason"] for e in job["excluded"] + result["dropped"])
                ),
            },
            "episodes": result["episodes"],
            "excluded": job["excluded"] + result["dropped"],
            "warnings": result.get("warnings", []),
        }
        atomic(staging / "pool_export.json", record)
        progress.stage("Publish", 1)
        if target.exists():
            raise ValueError(f"Export directory already exists: {target}")
        staging.rename(target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    progress.finish()
    return {
        "ok": True,
        "dataset_path": str(target),
        "format": options.format,
        "episodes": record["counts"]["episodes"],
        "frames": record["counts"]["frames"],
        "excluded": record["counts"]["excluded"],
        "warnings": record["warnings"],
    }


def _texts(job) -> dict:
    return job["recipe"].get("task_text") or {}


def _task_order(episodes, recipe) -> list[str]:
    present = list(dict.fromkeys(e["task"] for e in episodes))
    order = [t for t in recipe.get("tasks") or [] if t in present]
    return order + [t for t in present if t not in order]


def _raw_capture(job, options, staging, progress) -> dict:
    texts = _texts(job)
    counters: Counter = Counter()
    rows = []
    frames = 0
    progress.stage("Merge", len(job["episodes"]))
    for ep in job["episodes"]:
        if ep["format"] != "robot_capture":
            raise ValueError(f"{ep['key']} is not a raw capture")
        text = texts.get(ep["task"], ep["task"])
        folder = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "task"
        number = counters[folder]
        counters[folder] += 1
        dst = staging / folder / f"demo_{number:04d}"
        src = Path(ep["key"])
        for path in sorted(src.rglob("*")):
            if path.is_dir() or path.is_symlink():
                continue
            _link_or_copy(path, dst / path.relative_to(src), options.hardlink)
        description = staging / folder / "task_description.txt"
        if not description.exists():
            description.write_text(text + "\n", encoding="utf-8")
        frames += int(ep["frames"] or 0)
        rows.append(
            {
                "path": dst.relative_to(staging).as_posix(),
                "source_path": ep["key"],
                "source": ep["source"],
                "task": ep["task"],
                "fingerprint": ep["fingerprint"],
                "outcome": ep["outcome"],
                "outcome_source": ep["outcome_source"],
                "frames": ep["frames"],
            }
        )
        progress.advance(ep["key"])
    return {
        "episodes": rows,
        "frames": frames,
        "dropped": [],
        "task_order": _task_order(job["episodes"], job["recipe"]),
    }


def _lerobot(job, options, staging, progress, progress_path) -> dict:
    recap = options.format == "recap_value"
    texts = _texts(job)
    planned = job["episodes"]
    schema = _schema_check(planned, options)
    conv = options.conversion()
    warnings = []
    if recap and any(e["format"] == "lerobot" for e in planned):
        warnings.append(
            "LeRobot sources keep their own frame selection: their rewards are "
            "per stored frame, not necessarily per executed step"
        )
    pids = {id(e): f"p{i:06d}" for i, e in enumerate(planned)}
    raw_items = [(pids[id(e)], e) for e in planned if e["format"] == "robot_capture"]
    dropped = []
    part = staging / ".parts" / "raw"
    part_rows: dict[str, dict] = {}
    part_info = None
    part_stats: dict[int, dict] = {}
    if raw_items:
        progress.stage("Convert raw captures", len(raw_items))
        source = PoolCaptures(raw_items, texts)
        pipeline.run(
            staging / ".parts" / "input",
            part,
            conv,
            source,
            LeRobotV21(),
            progress_path,
            strict=True,
        )
        for pid, reasons in source.dropped.items():
            ep = next(e for p, e in raw_items if p == pid)
            dropped.append(
                {
                    "key": ep["key"],
                    "source": ep["source"],
                    "task": ep["task"],
                    "reason": "conversion_preflight",
                    "detail": reasons,
                }
            )
        part_info = _info(part)
        for row in dataset.read_jsonl(part / "meta/episodes.jsonl"):
            part_rows[row["source_demo"]] = row
        for row in dataset.read_jsonl(part / "meta/episodes_stats.jsonl"):
            part_stats[row["episode_index"]] = row["stats"]
        conv_fps = float(part_info["fps"])
        if abs(conv_fps - options.fps) > 0.01:
            raise ValueError(
                f"Raw captures converted at {conv_fps:g} fps, not {options.fps:g} "
                "(their cameras are slower); lower the export fps"
            )
    kept = [
        e for e in planned if e["format"] != "robot_capture" or pids[id(e)] in part_rows
    ]
    if not kept:
        raise ValueError("No episode passed the capture checks")
    task_order = _task_order(kept, job["recipe"])
    task_index = {t: i for i, t in enumerate(task_order)}
    recap_value = RecapValue()
    recap_settings = RecapOptions(
        dataset_type="rollout",
        failure_reward=options.failure_reward,
        gamma=options.gamma,
    )
    ds_stats: dict[str, dict] = {}
    features: dict = {}
    names = None
    episodes_meta, epstats, provenance, measured, record_rows = [], [], [], {}, []
    offset = 0
    progress.stage("Merge", len(kept))
    for new, ep in enumerate(kept):
        if ep["format"] == "robot_capture":
            root, info = part, part_info
            old = part_rows[pids[id(ep)]]["episode_index"]
            mapping = {k: k for k in _video_keys(info)}
            stats = dict(part_stats.get(old, {}))
            move = True
        else:
            root = Path(ep["source_path"])
            info = schema["infos"][ep["source_path"]]
            old = int(ep["episode_index"])
            mapping = _mapping(info, options.camera_map)
            if ep["source_path"] not in ds_stats:
                path = root / "meta/episodes_stats.jsonl"
                ds_stats[ep["source_path"]] = (
                    {r["episode_index"]: r["stats"] for r in dataset.read_jsonl(path)}
                    if path.is_file()
                    else {}
                )
            stats = {
                mapping.get(k, k): v
                for k, v in ds_stats[ep["source_path"]].get(old, {}).items()
            }
            move = False
        chunk = int(info.get("chunks_size") or 1000)
        table = pq.read_table(
            root
            / info["data_path"].format(episode_chunk=old // chunk, episode_index=old)
        )
        n = table.num_rows
        if not np.array_equal(
            np.asarray(table["frame_index"].to_pylist()), np.arange(n)
        ):
            raise ValueError(f"{ep['key']}: frame_index is not 0..{n - 1}")
        if names is None:
            names = info["features"]["observation.state"].get("names")
        text = texts.get(ep["task"], ep["task"])
        columns = {
            "timestamp": pa.array(np.arange(n, dtype=np.float32) / options.fps),
            "frame_index": pa.array(np.arange(n, dtype=np.int64)),
            "episode_index": pa.array(np.full(n, new, dtype=np.int64)),
            "index": pa.array(np.arange(offset, offset + n, dtype=np.int64)),
            "task_index": pa.array(np.full(n, task_index[ep["task"]], dtype=np.int64)),
            "observation.state": _list_f32(table["observation.state"].to_pylist()),
            "action": _list_f32(table["action"].to_pylist()),
        }
        row = {
            "episode_index": new,
            "tasks": [text],
            "length": n,
            "pool_key": ep["key"],
            "pool_source": ep["source"],
            "pool_fingerprint": ep["fingerprint"],
        }
        if ep["format"] == "robot_capture":
            row["source_demo"] = ep["key"]
        if ep.get("outcome") in ("success", "failure"):
            row["levi_outcome"] = ep["outcome"]
            row["levi_outcome_source"] = ep.get("outcome_source")
        stats = {k: v for k, v in stats.items() if k not in RECAP_COLUMNS}
        for key in ("timestamp", "frame_index", "episode_index", "index", "task_index"):
            stats[key] = dataset.stats(
                np.asarray(columns[key].to_numpy(), dtype=np.float64).reshape(-1, 1)
            )
        for key in ("observation.state", "action"):
            if key not in stats:
                stats[key] = dataset.stats(
                    np.asarray(columns[key].to_pylist(), dtype=np.float64)
                )
        if recap:
            for key, (values, feature) in recap_value.episode_columns(
                n, row, recap_settings
            ).items():
                columns[key] = pa.array(values)
                features[key] = feature
                stats[key] = dataset.stats(
                    np.asarray(values, dtype=np.float64).reshape(n, -1)
                )
        out = staging / DATA_PATH.format(episode_chunk=new // 1000, episode_index=new)
        out.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table(columns), out, compression="snappy")
        for src_key, out_key in mapping.items():
            src = root / info["video_path"].format(
                episode_chunk=old // chunk, episode_index=old, video_key=src_key
            )
            rel = VIDEO_PATH.format(
                episode_chunk=new // 1000, episode_index=new, video_key=out_key
            )
            dst = staging / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if move:
                os.replace(src, dst)
            else:
                shutil.copy2(src, dst)
            probe = media.probe(dst)
            measured[rel] = {**probe, "frames": probe["declared_frames"]}
            feature = dict(info["features"][src_key])
            feature["info"] = {**feature.get("info", {}), "video.fps": options.fps}
            shape = [probe["height"], probe["width"], 3]
            if out_key in features and features[out_key]["shape"] != shape:
                raise ValueError(
                    f"{out_key}: resolution {shape} of {ep['key']} differs from "
                    f"{features[out_key]['shape']}; the sources do not share one schema"
                )
            feature["shape"] = shape
            features.setdefault(out_key, feature)
        stats = {
            k: v
            for k, v in stats.items()
            if k in BASE_COLUMNS or k in mapping.values() or k in RECAP_COLUMNS
        }
        episodes_meta.append(row)
        epstats.append({"episode_index": new, "stats": stats})
        provenance.append(
            {"episode_index": new, "pool_key": ep["key"], "source_episode": old}
        )
        record_rows.append(
            {
                "episode_index": new,
                "source_path": ep["key"],
                "source": ep["source"],
                "format": ep["format"],
                "task": ep["task"],
                "fingerprint": ep["fingerprint"],
                "outcome": ep["outcome"],
                "outcome_source": ep["outcome_source"],
                "frames": n,
            }
        )
        offset += n
        progress.advance(ep["key"])
    for key in ("timestamp", "frame_index", "episode_index", "index", "task_index"):
        features[key] = {
            "dtype": "float32" if key == "timestamp" else "int64",
            "shape": [1],
            "names": None,
        }
    dims = schema["expected"]["state"]
    for key in ("observation.state", "action"):
        features[key] = {"dtype": "float32", "shape": [dims], "names": names}
    robot_type = options.robot_type or (
        (part_info or {}).get("robot_type")
        or next(iter(schema["infos"].values()), {}).get("robot_type")
        or "generic_arm"
    )
    cameras = [k for k, f in features.items() if f.get("dtype") == "video"]
    info = {
        "codebase_version": "v2.1",
        "robot_type": robot_type,
        "fps": options.fps,
        "total_episodes": len(episodes_meta),
        "total_frames": offset,
        "total_tasks": len(task_order),
        "total_videos": len(episodes_meta) * len(cameras),
        "total_chunks": (len(episodes_meta) + 999) // 1000,
        "chunks_size": 1000,
        "splits": {"train": f"0:{len(episodes_meta)}"},
        "data_path": DATA_PATH,
        "video_path": VIDEO_PATH,
        "features": features,
    }
    shutil.rmtree(staging / ".parts", ignore_errors=True)
    atomic(staging / "meta/info.json", info)
    dataset.jsonl(staging / "meta/episodes.jsonl", episodes_meta)
    dataset.jsonl(staging / "meta/episodes_stats.jsonl", epstats)
    dataset.jsonl(
        staging / "meta/tasks.jsonl",
        [{"task_index": i, "task": texts.get(t, t)} for i, t in enumerate(task_order)],
    )
    atomic(staging / "meta/stats.json", dataset.aggregate(epstats))
    dataset.jsonl(staging / "meta/levi_provenance.jsonl", provenance)
    if recap:
        recap_value.finalize(staging, episodes_meta, info, recap_settings)
    progress.stage("Validate", 1)
    validation = dataset.validate(staging, measured=measured)
    atomic(staging / "meta/levi_validation.json", validation)
    if not validation["ok"]:
        raise ValueError(
            "Merged dataset failed validation: "
            + "; ".join(validation["failures"][:10])
        )
    return {
        "episodes": record_rows,
        "frames": offset,
        "dropped": dropped,
        "task_order": task_order,
        "warnings": warnings,
        "conversion": conv.model_dump() if raw_items else None,
    }
