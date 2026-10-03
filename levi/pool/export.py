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
episode's source path, fingerprint, outcome and why it was picked (stratum,
quality score, reasons), per task the requested and achieved counts, every
exclusion and its reason, the LEVI commit and the format parameters. Held-out episodes are refused.
"""

import hashlib
import json
import os
import re
import shutil
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Literal

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
from . import embodiment, heldout, index, joblog, scanner, settings
from . import journal as journal_mod
from . import timing as timing_mod
from .recipe import NAME, Recipe, find_warnings, select_detailed

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
    # How the frames meet ``fps``: "resample" only drops frames, "retime" keeps
    # every frame and declares it at ``fps`` (levi/pool/timing.py). None =
    # the format's default (lerobot: resample + static filter; recap: retime,
    # every step kept); resolved at validation, so plans and records hold the
    # mode used. A raw capture copy has no time axis: timing is ignored (None).
    timing: Literal["resample", "retime"] | None = None
    filter_static: bool | None = None
    robot_type: str | None = Field(None, min_length=1, max_length=100)
    # raw_capture: hard-link files instead of copying them (same filesystem;
    # the export then shares its bytes with the source — never edit it).
    hardlink: bool = False
    # recap_value: an episode of the human category without an outcome (a
    # demonstration) counts as a success, recorded as ``sft_demonstration``.
    human_as_success: bool = False
    # recap_value (rewards)
    failure_reward: float = Field(-300.0, le=0, ge=-1e6)
    gamma: float = Field(1.0, gt=0, le=1)
    workers: int | None = Field(None, ge=1, le=64)
    # Episodes that fail to convert are left out and listed; the export stops
    # (resumable) when more than this share of its episodes failed.
    on_error_max_fraction: float = Field(0.1, ge=0, le=1)

    @field_validator("cameras")
    @classmethod
    def _cameras(cls, value):
        if len(set(value.values())) != len(value):
            raise ValueError("cameras must map to different output keys")
        return value

    @field_validator("camera_map")
    @classmethod
    def _map(cls, value):
        if len(set(value.values())) != len(value):
            raise ValueError("camera_map must map to different output keys")
        for key, feature in value.items():
            if not re.fullmatch(r"observation\.images\.[A-Za-z0-9_.-]+", feature):
                raise ValueError("camera_map values must start observation.images.")
            if not key:
                raise ValueError("camera_map keys must be nonempty")
        return value

    @model_validator(mode="after")
    def _timing(self):
        self.timing = (
            (self.timing or timing_mod.default_timing(self.format))
            if timing_mod.has_timing(self.format)
            else None
        )
        return self

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
            timing=self.timing or timing_mod.default_timing(self.format) or "resample",
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
    settings.require_heldout()
    selection = select_detailed(
        recipe, target=options.format, human_as_success=options.human_as_success
    )
    chosen, excluded = selection.chosen, selection.excluded
    warnings = find_warnings(recipe, chosen)
    blocking = [w for w in warnings if w["blocking"]]
    if blocking:
        raise ValueError(
            "Refusing to plan this export:\n- "
            + "\n- ".join(w["message"] for w in blocking)
        )
    if not chosen:
        raise ValueError(
            "The recipe selects no exportable episode"
            + (
                f" ({dict(Counter(e['reason'] for e in excluded))} excluded)"
                if excluded
                else ""
            )
        )
    texts = recipe.task_text
    shown: dict[str, str] = {}
    for row in chosen:
        text = texts.get(row["task"], row["task"])
        if shown.setdefault(text, row["task"]) != row["task"]:
            raise ValueError(
                f"Tasks {shown[text]!r} and {row['task']!r} would both be written "
                f"as {text!r}; give them different task_text"
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
        "measured_fps",
        "video_bytes",
        "state_dim",
        "action_dim",
        "category",
        "robot_flag",
        "human_label",
        "outcome",
        "outcome_source",
        "policy",
        "policy_model",
        "policy_checkpoint",
        "policy_method",
        "policy_phase",
        "policy_label",
        *embodiment.FIELDS,
        "fingerprint",
        "group",
        "stat_sig",
        "sel_stratum",
        "quality_score",
        "selection_reason",
    )
    episodes = [{k: row.get(k) for k in keep} for row in chosen]
    # The index's view of copies, checked at plan time too so a dry run
    # cannot pass what the export would refuse.
    refuse_heldout_groups(episodes)
    check_space({"episodes": episodes, "target": str(target)})
    return {
        "schema": SCHEMA,
        "recipe": recipe.model_dump(),
        "options": options.model_dump(),
        "target": str(target),
        "sources": sources,
        "episodes": episodes,
        "selection": list(selection.tasks.values()),
        "excluded": excluded,
        "index": index.summary().get("scanned_at"),
        "pool_roots": [str(p) for p in settings.pool_roots()],
        "heldout_lists": [str(p) for p in settings.heldout_files()],
        "heldout_disabled": settings.heldout_disabled(),
        "embodiment_rules": index.summary().get("embodiment_rules"),
        "warnings": [w for w in warnings if not w["blocking"]]
        + timing_mod.warnings(chosen, options.fps, options.timing),
    }


def _selection_fields(ep: dict) -> dict:
    """Why this episode is in: its stratum, quality score and reasons."""
    return {
        "selection_stratum": ep.get("sel_stratum"),
        "quality_score": ep.get("quality_score"),
        "selection_reason": ep.get("selection_reason"),
    }


def _selection_record(job: dict, episodes: list[dict]) -> list[dict]:
    """Per task: what was asked for, available and planned, and how many
    episodes the finished export holds."""
    exported = Counter(e["task"] for e in episodes)
    return [{**r, "exported": exported[r["task"]]} for r in job.get("selection") or []]


def _policy_fields(ep: dict) -> dict:
    return {
        k: ep.get(k)
        for k in (
            "policy_model",
            "policy_checkpoint",
            "policy_method",
            "policy_phase",
            "policy_label",
        )
    }


def _embodiment_fields(ep: dict) -> dict:
    return {f: ep.get(f) or embodiment.UNKNOWN for f in embodiment.FIELDS}


# ------------------------------------------------------------------ guards


def refuse_mixed_gripper(episodes: list[dict], recipe: dict):
    """Independent of the preview: the planned episodes may hold one known
    gripper (or only unknown ones: older data), unless the recipe allows a
    mix (``allow_mixed_gripper``) or names ``unknown`` on purpose. Refuses the
    whole export. A plan from an LEVI without these fields counts as unknown."""
    mix = embodiment.gripper_mix(
        (e.get("gripper") for e in episodes),
        recipe.get("grippers"),
        bool(recipe.get("allow_mixed_gripper")),
    )
    if mix["problem"]:
        raise ValueError(
            "Refusing to export: "
            + embodiment.mix_message(mix, embodiment.sources_by_gripper(episodes))
        )


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
    rules = scanner.rules_mod.load(settings.pool_dir())
    for ep in episodes:
        if ep["format"] == "robot_capture":
            demo = Path(ep["key"])
            # A plan or an interrupted export made before the embodiment
            # rules joined the signature carries the older one.
            if ep["stat_sig"] not in (
                scanner.raw_sig(demo, rules),
                scanner.legacy_raw_sig(demo),
            ):
                changed.append(ep["key"])
        elif ep["format"] == "lerobot":
            root = Path(ep["source_path"])
            markers = rules["levi_markers"]
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


def _below_message(slowest: float, fps: float) -> str:
    return (
        f"Raw captures converted at {slowest:g} fps, not {fps:g} (their cameras "
        f"are slower); lower the export fps to {max(1, int(slowest + 1e-9))} "
        "or use timing retime"
    )


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
        # Slowest measured camera rate per kept episode (id -> fps).
        self.measured: dict[str, float] = {}

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
            built = list(pool.map(build, todo))
        self.measured = {
            e.source_id: min(e.camera_fps.values()) for e in built if e.camera_fps
        }
        return built


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
        source_names = features.get("observation.state", {}).get("names")
        if (
            expected["names"] is not None
            and source_names is not None
            and list(source_names) != list(expected["names"])
        ):
            problems.append(
                f"{path}: state names {list(source_names)} differ from "
                f"{list(expected['names'])}"
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


def _inside(root: Path, path: Path) -> Path:
    """Every path an export writes lies in its staging folder."""
    resolved = Path(os.path.abspath(path))
    if not resolved.is_relative_to(Path(os.path.abspath(root))):
        raise PermissionError(f"Refusing to write outside the export: {path}")
    return path


def _link_or_copy(src: Path, dst: Path, hardlink: bool) -> bool:
    """Copy a file, or hard-link it when ``hardlink`` and it is a video:
    small files (metadata, CSVs) are always copied, because something may
    later rewrite them in place. True when linked."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if hardlink and src.suffix.lower() in scanner.VIDEO_SUFFIXES:
        try:
            os.link(src, dst)
            return True
        except OSError:
            pass
    shutil.copy2(src, dst)
    return False


def _list_f32(values) -> pa.Array:
    return pa.array(
        [np.asarray(v, dtype=np.float32).tolist() for v in values],
        type=pa.list_(pa.float32()),
    )


def _levi_commit():
    from ..recap.jobs import _levi_commit as commit

    return commit()


# ------------------------------------------------------------------ run

BATCH_EPISODES = 8  # raw episodes per conversion part (one journal line each)
PARTS = ".parts"
SIZE_FACTOR = 1.3  # planned source video bytes -> bytes the export may write


class FatalExport(RuntimeError):
    """A failure of the run itself (disk, memory, too many bad episodes), not
    of one episode: the export stops and can be resumed."""


def _is_fatal(exc: BaseException) -> bool:
    """Whether an error stops the export (True) or only costs the episode
    that raised it (False)."""
    if joblog.TERMINATING.is_set() or isinstance(
        exc,
        (SystemExit, KeyboardInterrupt, FatalExport, MemoryError, BrokenProcessPool),
    ):
        return True
    if isinstance(exc, FileNotFoundError):
        return False
    return isinstance(exc, OSError)


def _volume(path: Path) -> Path:
    path = Path(path)
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def _tree_bytes(path: Path) -> int:
    total = 0
    for item in Path(path).rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def estimate_bytes(episodes: list[dict]) -> int:
    """What the export will write, roughly: the videos of its episodes (the
    index's ``video_bytes``; a LeRobot source's share of its video folder)
    times ``SIZE_FACTOR`` (re-encoding and merged metadata)."""
    total = 0
    lerobot: dict[str, int] = Counter()
    for ep in episodes:
        if ep.get("video_bytes"):
            total += int(ep["video_bytes"])
        elif ep["format"] == "lerobot":
            lerobot[ep["source_path"]] += 1
    for path, count in lerobot.items():
        root = Path(path)
        try:
            all_eps = int(_info(root).get("total_episodes") or 0) or count
            total += _tree_bytes(root / "videos") * count // max(1, all_eps)
        except (OSError, ValueError):
            continue
    return int(total * SIZE_FACTOR)


def check_space(job: dict, staging: Path | None = None) -> dict:
    """Refuse early, with numbers, when the volume cannot hold the export
    (what an unfinished ``.partial`` already holds counts as written)."""
    wanted = estimate_bytes(job["episodes"])
    have = _tree_bytes(staging) if staging is not None and Path(staging).exists() else 0
    volume = _volume(Path(job["target"]).parent)
    free = shutil.disk_usage(volume).free
    margin = settings.free_margin_bytes()
    need = max(0, wanted - have)
    if need + margin > free:

        def gib(n):
            return (
                f"{n / 1024**3:.1f} GiB" if n >= 1024**3 else f"{n / 1024**2:.0f} MiB"
            )

        raise ValueError(
            f"Not enough free space for this export on {volume}: it needs about "
            f"{gib(need)} (the planned sources' videos times {SIZE_FACTOR:g}, "
            f"{gib(wanted)}, minus {gib(have)} already written) and keeps "
            f"{gib(margin)} in reserve; {gib(free)} is free. Free space, or choose "
            "another output folder"
        )
    return {"need": need, "free": free, "margin": margin}


def _probe_rates(raw_eps: list[dict], conv: Options) -> dict[str, float]:
    """The slowest measured camera rate of every raw episode (ffprobe)."""

    def one(ep):
        rates = [camera_info(Path(ep["key"]), c, conv)["fps"] for c in conv.cameras]
        return ep["key"], min(rates)

    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(pool.map(one, raw_eps))


def check_fps(job: dict, options: ExportOptions) -> None:
    """``resample`` never adds frames: a raw episode measured below the
    export fps refuses the export before anything is converted."""
    if options.format == "raw_capture" or options.timing != "resample":
        return
    raw_eps = [e for e in job["episodes"] if e["format"] == "robot_capture"]
    if not raw_eps:
        return
    try:
        rates = _probe_rates(raw_eps, options.conversion())
    except (OSError, ValueError, RuntimeError):
        return  # unreadable sources are reported by the capture checks
    slowest = min(rates.values())
    if options.fps > slowest + timing_mod.TOLERANCE:
        raise ValueError(_below_message(slowest, options.fps))


def _cancel_requested(job_path: Path | None) -> bool:
    return bool(job_path) and joblog.paths(job_path)["cancel"].exists()


def _stopping_reason(job_path: Path | None) -> str:
    if job_path and joblog.paths(job_path)["stopping"].exists():
        return "service stopped"
    return "stopped by a signal"


def _dispose(staging: Path, journal, exc: BaseException, job_path, log) -> str:
    """What becomes of the ``.partial`` folder when a run ends early: a
    cancel removes it; a stop or crash keeps it; a failure keeps it while it
    holds finished work (a resume needs it) and removes it otherwise."""
    terminating = isinstance(exc, (SystemExit, KeyboardInterrupt)) or (
        joblog.TERMINATING.is_set()
    )
    if _cancel_requested(job_path):
        shutil.rmtree(staging, ignore_errors=True)
        log.info("cancelled; the partial folder was removed")
        return "removed"
    if journal is not None and journal.has_units():
        if terminating:
            journal.note_stopped("interrupted", _stopping_reason(job_path))
            log.warning(
                f"{_stopping_reason(job_path)}; the partial folder is kept: "
                f"{journal.new_units} unit(s) finished in this run"
            )
        else:
            journal.note_stopped("failed", f"{type(exc).__name__}: {exc}"[:500])
            log.warning("failed; the partial folder is kept for a resume")
        return "kept"
    shutil.rmtree(staging, ignore_errors=True)
    log.info("nothing finished yet; the partial folder was removed")
    return "removed"


class _NullLog:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class RunContext:
    """What one export run threads through its stages."""

    def __init__(self, job, options, staging, progress, journal, job_path, log):
        self.job = job
        self.options = options
        self.staging = staging
        self.progress = progress
        self.journal = journal
        self.job_path = job_path
        self.log = log
        self.failures: list[dict] = []
        self.convert_errors = 0

    def fail(self, ep: dict, exc: BaseException, stage: str, unit: str) -> dict:
        """One episode's failure: recorded, and the episode left out (the
        returned exclusion row). ``check_budget`` stops the run when too many."""
        info = joblog.failure(exc, episode=ep["key"], unit=unit, stage=stage)
        if self.job_path:
            joblog.append_failure(self.job_path, info)
        self.log.trace(exc, f"{stage} {ep['key']}")
        self.failures.append(
            {k: info[k] for k in ("episode", "unit", "stage", "type", "message")}
        )
        self.convert_errors += 1
        return {
            "key": ep["key"],
            "source": ep["source"],
            "task": ep["task"],
            "reason": "convert_error",
            "detail": [f"{type(exc).__name__}: {str(exc)[:300]}"],
        }

    def preflight_drop(self, ep: dict, reasons: list[str]) -> dict:
        """An episode that failed the capture checks: listed, left out."""
        info = {
            "episode": ep["key"],
            "unit": f"preflight|{ep['key']}",
            "stage": "Convert raw captures",
            "type": "CaptureCheck",
            "message": "; ".join(reasons)[:2000],
            "traceback": "",
        }
        if self.job_path:
            joblog.append_failure(self.job_path, info)
        self.log.warning(f"left out {ep['key']}: {info['message']}")
        self.failures.append(
            {k: info[k] for k in ("episode", "unit", "stage", "type", "message")}
        )
        return {
            "key": ep["key"],
            "source": ep["source"],
            "task": ep["task"],
            "reason": "conversion_preflight",
            "detail": reasons,
        }

    def check_budget(self) -> None:
        total = max(1, len(self.job["episodes"]))
        if self.convert_errors / total > self.options.on_error_max_fraction:
            raise FatalExport(
                f"{self.convert_errors} of {total} episodes failed to convert, more "
                f"than the allowed {self.options.on_error_max_fraction:.0%} "
                "(on_error_max_fraction); see the job's errors.jsonl. Finished work "
                "is kept: fix the cause and resume, or raise the limit and run again"
            )


def run(job: dict, progress_path: Path | None = None, *, resume: bool = False) -> dict:
    options = ExportOptions.model_validate(job["options"])
    target = Path(job["target"])
    sources = job["sources"]
    staging = target.parent / f".{target.name}.partial"
    settings.check_export_target(target, sources, allow_partial=resume)
    if not job.get("heldout_lists") and not job.get("heldout_disabled"):
        raise ValueError("The plan names no held-out list; refusing to export")
    settings.guard_write(staging, sources)
    job_path = settings.job_path(job["id"]) if job.get("id") else None
    log = joblog.JobLog(joblog.paths(job_path)["log"]) if job_path else _NullLog()
    progress = Progress(progress_path, STAGES)
    progress.stage("Check", len(job["episodes"]))
    episodes = job["episodes"]
    log.info(
        f"{'resuming' if resume else 'starting'} export {options.name} "
        f"({options.format}, {len(episodes)} episodes) -> {target}"
    )
    refuse_heldout(
        episodes,
        [Path(p) for p in job["pool_roots"]],
        [Path(p) for p in job["heldout_lists"]],
    )
    refuse_heldout_groups(episodes)
    refuse_mixed_gripper(episodes, job["recipe"])
    _unchanged(episodes)
    check_space(job, staging if resume else None)
    check_fps(job, options)
    if resume:
        journal = journal_mod.Journal.open(staging, job)
        log.info(
            f"journal verified: {len(journal.units)} finished unit(s), "
            f"{journal.interruptions} earlier interruption(s)"
        )
        # What a crash left half-written is not trusted; the units are.
        (staging / "pool_export.json").unlink(missing_ok=True)
        shutil.rmtree(staging / "meta", ignore_errors=True)
    else:
        staging.mkdir(parents=True)
        journal = journal_mod.Journal.create(staging, job)
    ctx = RunContext(job, options, staging, progress, journal, job_path, log)
    try:
        if options.format == "raw_capture":
            result = _raw_capture(ctx)
        else:
            result = _lerobot(ctx)
        record = {
            "schema": SCHEMA,
            "name": options.name,
            "job_id": job.get("id"),
            "format": options.format,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "levi_commit": _levi_commit(),
            "recipe": job["recipe"],
            "task_order": result["task_order"],
            "selection": _selection_record(job, result["episodes"]),
            "params": options.model_dump(),
            "index_scanned_at": job.get("index"),
            "pool_roots": job["pool_roots"],
            "heldout_lists": job["heldout_lists"],
            "heldout_disabled": bool(job.get("heldout_disabled")),
            "conversion": result.get("conversion"),
            "timing": result.get("timing"),
            "embodiment": embodiment.record(
                result["episodes"],
                job["recipe"],
                (job.get("embodiment_rules") or {}).get("version"),
            ),
            "counts": {
                "episodes": len(result["episodes"]),
                "frames": result["frames"],
                "excluded": dict(
                    Counter(e["reason"] for e in job["excluded"] + result["dropped"])
                ),
            },
            "episodes": result["episodes"],
            "excluded": job["excluded"] + result["dropped"],
            "warnings": job.get("warnings", []) + result.get("warnings", []),
            "errors": ctx.failures,
            "resumed": journal.resumes > 0,
            "resumes": journal.resumes,
            "interruptions": journal.interruptions,
        }
        atomic(staging / "pool_export.json", record)
        progress.stage("Publish", 1)
        journal.discard()
        shutil.rmtree(staging / PARTS, ignore_errors=True)
        if target.exists():
            raise ValueError(f"Export directory already exists: {target}")
        staging.rename(target)
    except BaseException as exc:
        if not isinstance(exc, (SystemExit, KeyboardInterrupt)):
            log.trace(exc, "export")
        _dispose(staging, journal, exc, job_path, log)
        raise
    progress.finish()
    size = _tree_bytes(target)
    log.info(
        f"finished: {record['counts']['episodes']} episodes, "
        f"{record['counts']['frames']} frames, {len(ctx.failures)} failure(s)"
    )
    return {
        "ok": True,
        "dataset_path": str(target),
        "format": options.format,
        "episodes": record["counts"]["episodes"],
        "frames": record["counts"]["frames"],
        "excluded": record["counts"]["excluded"],
        "warnings": record["warnings"],
        "errors": len(ctx.failures),
        "resumed": record["resumed"],
        "interruptions": journal.interruptions,
        "bytes": size,
    }


def _timing_record(options: ExportOptions, rows: list[dict]) -> dict:
    """The timing mode used and how far each source's time axis moved: per
    episode ``source_fps`` (measured) and ``time_scale`` (exported duration /
    recorded duration; ``source_fps / fps`` for retime, 1 for resample)."""
    rates = [r["source_fps"] for r in rows if r.get("source_fps")]
    scales = [r["time_scale"] for r in rows if r.get("time_scale")]
    off = [s for s in scales if abs(s - 1) > timing_mod.NOTE_FRACTION]
    return {
        "mode": options.timing,
        "export_fps": options.fps,
        "source_fps_min": min(rates, default=None),
        "source_fps_max": max(rates, default=None),
        "time_scale_min": min(scales, default=None),
        "time_scale_max": max(scales, default=None),
        "episodes_off_by_over_2_percent": len(off),
    }


def _texts(job) -> dict:
    return job["recipe"].get("task_text") or {}


def _task_order(episodes, recipe) -> list[str]:
    present = list(dict.fromkeys(e["task"] for e in episodes))
    listed = [
        t["task"] if isinstance(t, dict) else t for t in recipe.get("tasks") or []
    ]
    order = [t for t in listed if t in present]
    return order + [t for t in present if t not in order]


def _copy_hashed(src: Path, dst: Path) -> str:
    """``copy2`` that also returns the sha256 of what it wrote."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with Path(src).open("rb") as reader, dst.open("wb") as writer:
        for chunk in iter(lambda: reader.read(1 << 20), b""):
            digest.update(chunk)
            writer.write(chunk)
    shutil.copystat(src, dst)
    return digest.hexdigest()


def _raw_capture(ctx: RunContext) -> dict:
    job, options, staging, progress = ctx.job, ctx.options, ctx.staging, ctx.progress
    texts = _texts(job)
    counters: Counter = Counter()
    rows = []
    dropped = []
    frames = 0
    progress.stage("Merge", len(job["episodes"]))
    for ep in job["episodes"]:
        if ep["format"] != "robot_capture":
            raise ValueError(f"{ep['key']} is not a raw capture")
        text = texts.get(ep["task"], ep["task"])
        folder = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")
        if folder.strip(".") == "":  # "", ".", "..", "..."
            folder = "task"
        number = counters[folder]
        dst = _inside(staging, staging / folder / f"demo_{number:04d}")
        rel = dst.relative_to(staging).as_posix()
        src = Path(ep["key"])
        unit = f"copy|{ep['key']}"
        recorded = ctx.journal.done(unit)
        try:
            if (
                recorded
                and recorded.get("path") == rel
                and ctx.journal.verified(recorded["files"])
            ):
                linked = recorded["linked"]
            else:
                if dst.exists():
                    shutil.rmtree(dst)  # a copy the crash cut short
                linked, files = 0, {}
                for path in sorted(src.rglob("*")):
                    if path.is_dir() or path.is_symlink():
                        continue
                    out = _inside(staging, dst / path.relative_to(src))
                    linked += _link_or_copy(path, out, options.hardlink)
                    name = out.relative_to(staging).as_posix()
                    files[name] = journal_mod.file_record(staging, name)
                ctx.journal.record(unit, path=rel, linked=linked, files=files)
        except Exception as exc:
            if _is_fatal(exc):
                raise
            shutil.rmtree(dst, ignore_errors=True)
            dropped.append(ctx.fail(ep, exc, "Merge", unit))
            ctx.check_budget()
            progress.advance(ep["key"])
            continue
        counters[folder] += 1
        description = _inside(staging, staging / folder / "task_description.txt")
        if not description.exists():
            description.write_text(text + "\n", encoding="utf-8")
        frames += int(ep["frames"] or 0)
        rows.append(
            {
                "path": rel,
                "source_path": ep["key"],
                "source": ep["source"],
                "task": ep["task"],
                "group": ep.get("group"),
                "fingerprint": ep["fingerprint"],
                "hardlinked_files": linked,
                "outcome": ep["outcome"],
                "outcome_source": ep["outcome_source"],
                **_policy_fields(ep),
                **_embodiment_fields(ep),
                **_selection_fields(ep),
                "frames": ep["frames"],
            }
        )
        progress.advance(ep["key"])
    if not rows:
        raise ValueError("No episode could be copied")
    copied = {r["source_path"] for r in rows}
    return {
        "episodes": rows,
        "frames": frames,
        "dropped": dropped,
        "task_order": _task_order(
            [e for e in job["episodes"] if e["key"] in copied], job["recipe"]
        ),
    }


class Part:
    """One conversion part: a small dataset made from a few raw episodes."""

    def __init__(self, part_id: str, directory: Path, measured: dict[str, float]):
        self.id = part_id
        self.dir = directory
        self.measured = measured
        self.info = _info(directory)
        self.rows = {
            r["source_demo"]: r
            for r in dataset.read_jsonl(directory / "meta/episodes.jsonl")
        }
        self.stats = {
            r["episode_index"]: r["stats"]
            for r in dataset.read_jsonl(directory / "meta/episodes_stats.jsonl")
        }

    def files(self, staging: Path) -> tuple[dict, dict]:
        """Every file of the part with its size and sha256 (paths relative to
        the staging folder), and the files of each (episode, camera) unit."""
        chunk = int(self.info.get("chunks_size") or 1000)
        rels = ["meta/info.json", "meta/episodes.jsonl", "meta/episodes_stats.jsonl"]
        units: dict[str, list[str]] = {}
        for pid, row in self.rows.items():
            index_ = row["episode_index"]
            units[f"{pid}|data"] = [
                self.info["data_path"].format(
                    episode_chunk=index_ // chunk, episode_index=index_
                )
            ]
            for key in _video_keys(self.info):
                units[f"{pid}|{key}"] = [
                    self.info["video_path"].format(
                        episode_chunk=index_ // chunk,
                        episode_index=index_,
                        video_key=key,
                    )
                ]
        for paths in units.values():
            rels += paths
        rels = list(dict.fromkeys(rels))

        def name(rel: str) -> str:
            return (self.dir / rel).relative_to(staging).as_posix()

        files = {name(r): journal_mod.file_record(self.dir, r) for r in rels}
        return files, {u: [name(r) for r in paths] for u, paths in units.items()}


def _next_part(ctx: RunContext) -> str:
    seq = 1 + max(
        (int(r["part"][1:]) for r in ctx.journal.prefixed("part|")), default=-1
    )
    # Ids also skip the folders of parts that never got their journal line.
    parts_dir = ctx.staging / PARTS
    if parts_dir.is_dir():
        for child in parts_dir.iterdir():
            digits = re.sub(r"\D", "", child.name)
            if digits:
                seq = max(seq, int(digits) + 1)
    return f"p{seq:04d}"


def _run_part(ctx: RunContext, items: list[tuple[str, dict]], texts, conv):
    """Convert ``items`` with the conversion pipeline as one part and journal
    it. A part that fails is retried episode by episode, so one bad episode
    costs only itself. Returns (parts, excluded rows)."""
    part_id = _next_part(ctx)
    parts_dir = ctx.staging / PARTS
    parts_dir.mkdir(parents=True, exist_ok=True)
    directory = parts_dir / part_id
    by_pid = dict(items)
    source = PoolCaptures(items, texts)
    mark = len(ctx.failures)
    try:
        pipeline.run(
            parts_dir / f"in{part_id}",
            directory,
            conv,
            source,
            LeRobotV21(),
            None,
            strict=True,
        )
    except Exception as exc:
        shutil.rmtree(directory, ignore_errors=True)
        shutil.rmtree(parts_dir / f".{part_id}.partial", ignore_errors=True)
        if _is_fatal(exc):
            raise
        if source.dropped and len(source.dropped) == len(items):
            excluded = [
                ctx.preflight_drop(by_pid[p], r) for p, r in source.dropped.items()
            ]
        elif len(items) > 1:
            ctx.log.warning(
                f"a part of {len(items)} episodes failed ({type(exc).__name__}: "
                f"{str(exc)[:200]}); retrying them one by one"
            )
            parts, excluded = [], []
            for item in items:
                more_parts, more_excluded = _run_part(ctx, [item], texts, conv)
                parts += more_parts
                excluded += more_excluded
            return parts, excluded
        else:
            _, ep = items[0]
            excluded = [
                ctx.fail(ep, exc, "Convert raw captures", f"convert|{ep['key']}")
            ]
        ctx.journal.record(
            f"part|{part_id}",
            part=part_id,
            items=[p for p, _ in items],
            excluded=excluded,
            failures=ctx.failures[mark:],
            measured={},
            files={},
            units={},
        )
        ctx.check_budget()
        return [], excluded
    part = Part(part_id, directory, dict(source.measured))
    if abs(float(part.info["fps"]) - ctx.options.fps) > 0.01:
        raise FatalExport(_below_message(float(part.info["fps"]), ctx.options.fps))
    excluded = [ctx.preflight_drop(by_pid[p], r) for p, r in source.dropped.items()]
    files, units = part.files(ctx.staging)
    ctx.journal.record(
        f"part|{part_id}",
        part=part_id,
        items=[p for p, _ in items],
        excluded=excluded,
        failures=ctx.failures[mark:],
        measured=part.measured,
        files=files,
        units=units,
    )
    return [part], excluded


def _convert_raw(ctx: RunContext, raw_items, texts, conv):
    """The raw episodes as journaled conversion parts: (part by episode id,
    excluded rows). A resume keeps every part whose files verify."""
    progress = ctx.progress
    cameras = len(conv.cameras)
    progress.stage("Convert raw captures", len(raw_items) * cameras)
    parts: dict[str, Part] = {}
    excluded: list[dict] = []
    finished: set[str] = set()
    parts_dir = ctx.staging / PARTS
    for record in list(ctx.journal.prefixed("part|")):
        directory = parts_dir / record["part"]
        usable = (directory.is_dir() or not record["files"]) and ctx.journal.verified(
            record["files"]
        )
        if not usable:
            ctx.log.warning(
                f"part {record['part']} failed verification; its episodes are "
                "converted again"
            )
            del ctx.journal.units[f"part|{record['part']}"]
            continue
        if record["files"]:
            part = Part(record["part"], directory, record.get("measured") or {})
            for pid in record["items"]:
                if pid in part.rows:
                    parts[pid] = part
        excluded += record.get("excluded") or []
        failures = record.get("failures") or []
        ctx.failures += failures
        ctx.convert_errors += sum(1 for f in failures if f["type"] != "CaptureCheck")
        finished.update(record["items"])
    if parts_dir.is_dir():
        keep = {r["part"] for r in ctx.journal.prefixed("part|")}
        for child in parts_dir.iterdir():
            if child.name not in keep:
                shutil.rmtree(child, ignore_errors=True)
    if finished:
        ctx.log.info(f"{len(finished)} raw episode(s) already converted")
        progress.advance("resumed", step=len(finished) * cameras)
    remaining = [item for item in raw_items if item[0] not in finished]
    batch = settings.batch_episodes() or max(
        BATCH_EPISODES, 2 * (conv.workers or pipeline.auto_workers())
    )
    for start in range(0, len(remaining), batch):
        chunk = remaining[start : start + batch]
        made, dropped = _run_part(ctx, chunk, texts, conv)
        excluded += dropped
        for part in made:
            for pid in part.rows:
                parts[pid] = part
        progress.advance(chunk[-1][1]["key"], step=len(chunk) * cameras)
        ctx.log.info(
            f"converted {min(start + batch, len(remaining))}/{len(remaining)} episodes"
        )
    return parts, excluded


def _check_lerobot_episode(ep: dict, info: dict, mapping: dict) -> None:
    """Cheap checks that would make merging this episode fail, made before
    anything is written so a bad source episode costs only itself."""
    root = Path(ep["source_path"])
    old = int(ep["episode_index"])
    chunk = int(info.get("chunks_size") or 1000)
    path = root / info["data_path"].format(
        episode_chunk=old // chunk, episode_index=old
    )
    frame_index = pq.read_table(path, columns=["frame_index"])["frame_index"]
    n = len(frame_index)
    if not np.array_equal(np.asarray(frame_index.to_pylist()), np.arange(n)):
        raise ValueError(f"{ep['key']}: frame_index is not 0..{n - 1}")
    for key in mapping:
        video = root / info["video_path"].format(
            episode_chunk=old // chunk, episode_index=old, video_key=key
        )
        if not video.is_file():
            raise FileNotFoundError(f"{ep['key']}: missing video {video}")


def _place_video(src: Path, dst: Path, converted: bool) -> str | None:
    """Put ``src`` at ``dst``: a hard link for a converted part (the parts
    folder goes at the end), a hashed copy for a LeRobot source. Returns the
    sha256 when it was computed on the way."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.unlink(missing_ok=True)
    if converted:
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)
        return None
    return _copy_hashed(src, dst)


def _lerobot(ctx: RunContext) -> dict:
    job, options, staging, progress = ctx.job, ctx.options, ctx.staging, ctx.progress
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
    dropped: list[dict] = []
    owner: dict[str, Part] = {}
    if raw_items:
        owner, dropped = _convert_raw(ctx, raw_items, texts, conv)
    lerobot_bad: set[int] = set()
    for e in planned:
        if e["format"] != "lerobot":
            continue
        info = schema["infos"][e["source_path"]]
        try:
            _check_lerobot_episode(e, info, _mapping(info, options.camera_map))
        except Exception as exc:
            if _is_fatal(exc):
                raise
            dropped.append(ctx.fail(e, exc, "Merge", f"merge|{e['key']}"))
            lerobot_bad.add(id(e))
    ctx.check_budget()
    kept = [
        e
        for e in planned
        if id(e) not in lerobot_bad
        and (e["format"] != "robot_capture" or pids[id(e)] in owner)
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
    part_info = next(iter(owner.values())).info if owner else None
    offset = 0
    progress.stage("Merge", len(kept))
    for new, ep in enumerate(kept):
        if ep["format"] == "robot_capture":
            pid = pids[id(ep)]
            part = owner[pid]
            root, info = part.dir, part.info
            source_fps = part.measured.get(pid)
            old = part.rows[pid]["episode_index"]
            mapping = {k: k for k in _video_keys(info)}
            stats = dict(part.stats.get(old, {}))
            converted = True
        else:
            root = Path(ep["source_path"])
            info = schema["infos"][ep["source_path"]]
            source_fps = float(info.get("fps") or 0) or None
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
            converted = False
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
            unit = f"merge|{ep['key']}|{out_key}"
            recorded = ctx.journal.done(unit)
            if (
                recorded
                and set(recorded["files"]) == {rel}
                and ctx.journal.verified(recorded["files"])
            ):
                probe = recorded["probe"]
            else:
                sha = _place_video(src, dst, converted)
                probe = media.probe(dst)
                ctx.journal.record(
                    unit,
                    files={rel: journal_mod.file_record(staging, rel, sha)},
                    probe=probe,
                )
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
                "group": ep.get("group"),
                "fingerprint": ep["fingerprint"],
                "outcome": ep["outcome"],
                "outcome_source": ep["outcome_source"],
                **_policy_fields(ep),
                **_embodiment_fields(ep),
                **_selection_fields(ep),
                "frames": n,
                "source_fps": timing_mod.rounded(source_fps),
                "time_scale": timing_mod.rounded(
                    timing_mod.time_scale(source_fps, options.fps, options.timing)
                ),
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
        "fps": int(options.fps) if float(options.fps).is_integer() else options.fps,
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
        "timing": _timing_record(options, record_rows),
    }
