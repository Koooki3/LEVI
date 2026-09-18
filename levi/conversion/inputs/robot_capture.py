"""Robot capture trees: ``task/demo_NNNN/`` with pose/gripper CSVs + video.

Covers both collectors in use: teleoperation (``data_collection_robotiq``)
and policy-rollout / eval captures (``online_rollout_data``), told apart by
``metadata.json["data_source"]``.
"""

import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from .. import media, raw
from ..episodes import SourceEpisode
from ..options import Options
from ..report import EpisodeFinding, InputReport, Requirement
from .base import InputFormat

# (id, label, fix) in display order. Labels are English; the UI translates
# them through the i18n catalogs.
REQUIREMENTS = [
    (
        "layout",
        "Capture layout (task folders with demo_* episodes)",
        "Point LEVI at a folder containing task/demo_NNNN capture directories.",
    ),
    (
        "task_text",
        "Task description per task folder",
        "Add task_description.txt next to the demo folders, or map folder names with task_map.",
    ),
    (
        "csv_schema",
        "Pose and gripper CSVs with the documented columns",
        "Each demo needs end_effector_pose.csv and gripper_state.csv with the documented columns.",
    ),
    (
        "frame_ids",
        "Frame ids finite, integer, unique and increasing",
        "Re-export the capture; LEVI never silently re-numbers frames.",
    ),
    (
        "timestamps",
        "Capture timestamps finite and non-decreasing",
        "Check the collector clock; exclude the affected demos.",
    ),
    (
        "alignment",
        "Pose, gripper and extra CSVs aligned frame for frame",
        "All CSVs with frame_index must share the same rows; exclude the affected demos.",
    ),
    (
        "success_flag",
        "success_flag consistent within each demo",
        "A demo mixing 0 and 1 was likely merged from two recordings; exclude it.",
    ),
    (
        "pose_values",
        "Finite positions and non-zero quaternions",
        "Exclude the affected demos.",
    ),
    (
        "gripper_commands",
        "Gripper commands are open/close",
        "Only 'open' and 'close' are understood; exclude the affected demos.",
    ),
    (
        "cameras",
        "Every configured camera present and readable",
        "Provide <camera>.mp4 (or <camera>_raw.avi) for each camera in the options, or change the camera mapping.",
    ),
    (
        "camera_codec",
        "Browser-playable video (H.264, yuv420p)",
        "Other codecs still convert (they are re-encoded) but cannot be retimed losslessly.",
    ),
    (
        "frame_count",
        "Video frame count matches CSV rows",
        "Exclude demos whose video and CSV disagree; LEVI never pads or drops to force a match.",
    ),
    (
        "fps",
        "Camera frame rates consistent",
        "All cameras of a demo must share one rate.",
    ),
    (
        "stale_state",
        "Robot state keeps updating (no stale runs)",
        "Raise stale_run only if repeated state timestamps are expected; otherwise exclude the demos.",
    ),
    (
        "stall_markers",
        "No camera stall recorded by the collector",
        "Exclude demos whose collector flagged a camera stall (frozen frames were recorded).",
    ),
    (
        "metadata_frames",
        "metadata.json frame_count matches the CSVs",
        "Exclude the affected demos.",
    ),
    (
        "completion",
        "Capture completion markers (stopped_at, stop_demo/episode_end)",
        "Only required when require_complete is on.",
    ),
    (
        "outcomes",
        "Per-episode success/failure labels",
        "Needed for RECAP value export: label outcomes in LEVI, or export as demonstrations (SFT).",
    ),
    (
        "video_decode",
        "Full video decode: frozen cameras, decoded frame counts, duplicate first frames",
        "Verified while converting; a failure stops the job before anything is published.",
    ),
    (
        "symlinks",
        "No symbolic links in the source tree",
        "Use a physical copy of the capture.",
    ),
]
LABELS = {rid: (label, fix) for rid, label, fix in REQUIREMENTS}


def demo_dirs(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("demo_*") if p.is_dir())


def _examples(items: list[str], limit: int = 3) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f" (+{len(items) - limit} more)" if len(items) > limit else "")


def output_fps(measured: list[float], options: Options) -> tuple[float, str | None]:
    """The fps a conversion will declare, and a note when it differs from
    the request. ``resample`` can't upsample, so it lowers the target to the
    slowest measured camera; ``retime`` declares every frame at ``fps``."""
    if options.timing == "retime" or not measured:
        return options.fps, None
    slowest = min(measured)
    if options.fps > slowest + 0.01:
        return slowest, (
            f"Requested output FPS {options.fps:g} exceeds the measured capture "
            f"FPS ({slowest:g} min); automatically lowered to {slowest:g}."
        )
    return options.fps, None


def plan_positions(
    xyz, q, command, source_fps: float, out_fps: float, options: Options
):
    """Retained source rows: resample (or keep all, when retiming) then the
    optional static filter, evaluated in resampled index space exactly like
    the staged-then-filtered legacy pipeline."""
    n = len(xyz)
    if options.timing == "retime":
        sample = np.arange(n)
    else:
        sample = raw.sample_positions(n, source_fps, out_fps)
    if not options.filter_static:
        return sample
    keep = raw.keep_positions(xyz[sample], q[sample], command[sample], options)
    return sample[keep]


def camera_info(demo: Path, camera: str, options: Options) -> dict:
    """ffprobe metadata for a video camera, or the image count for a folder of
    frames (declared at ``options.source_fps``). No decoding."""
    folder = demo / camera
    if folder.is_dir():
        return {"images": len(raw.image_files(demo, camera)), "fps": options.source_fps}
    return media.probe_cached(raw.camera_path(demo, camera))


class RobotCapture(InputFormat):
    id = "robot_capture"
    label = "Robot capture (pose/gripper CSV + video demos)"
    description = (
        "task/demo_NNNN folders with end_effector_pose.csv, gripper_state.csv and "
        "one video per camera — from teleoperation or policy rollouts."
    )
    evidence = "real-data"

    def detect(self, root: Path) -> float:
        for demo in demo_dirs(root)[:20]:
            if (demo / "end_effector_pose.csv").is_file() and (
                any(demo.glob("*.mp4")) or any(demo.glob("*_raw.avi"))
            ):
                return 1.0
        return 0.0

    def _inspect_demo(self, root: Path, demo: Path, options: Options) -> dict:
        found = {
            "id": demo.relative_to(root).as_posix(),
            "codes": {},
            "warnings": [],
            "variant": "teleop",
            "outcome": None,
            "frames": None,
            "cameras": {},
            "fps": [],
            "image_mode": False,
            # Requirement ids the warnings belong to, kept apart from the
            # free-text messages so aggregation never parses them.
            "codec": [],
            "incomplete": [],
        }

        def fail(code, message):
            found["codes"].setdefault(code, message)

        try:
            pose, *_ = raw.load(demo)
            found["frames"] = len(pose)
        except raw.CaptureError as exc:
            fail(exc.code, str(exc))
            pose = None
        except (OSError, ValueError, KeyError) as exc:
            fail("csv_schema", str(exc))
            pose = None
        for camera in options.cameras:
            try:
                info = camera_info(demo, camera, options)
            except (OSError, ValueError, RuntimeError) as exc:
                fail("cameras", f"{camera}: {exc}")
                continue
            found["cameras"][camera] = info
            found["fps"].append(info["fps"])
            if "images" in info:
                found["image_mode"] = True
            elif info.get("codec") != "h264" or info.get("pixel_format") != "yuv420p":
                found["codec"].append(camera)
                found["warnings"].append(
                    f"{camera}: {info.get('codec')}/{info.get('pixel_format')}"
                )
            count = info.get("images", info.get("declared_frames"))
            if pose is not None and count is not None and count != len(pose):
                fail("frame_count", f"{camera}: {count} frames vs {len(pose)} CSV rows")
        rates = [v["fps"] for v in found["cameras"].values()]
        if rates and max(rates) - min(rates) > 0.01:
            fail(
                "fps",
                "Camera frame rates differ: " + ", ".join(f"{r:g}" for r in rates),
            )
        if pose is not None and rates:
            out_fps, _ = output_fps([min(rates)], options)
            try:
                keep = (
                    raw.sample_positions(len(pose), min(rates), out_fps)
                    if options.timing == "resample"
                    else np.arange(len(pose))
                )
                stamps = pose.source_stamp_sec.to_numpy()[keep]
                longest = run = 0
                for same in np.diff(stamps) == 0:
                    run = run + 1 if same else 0
                    longest = max(longest, run)
                if longest >= options.stale_run:
                    fail("stale_state", f"Stale state source timestamp run: {longest}")
            except ValueError as exc:
                fail("fps", str(exc))
        metadata = demo / "metadata.json"
        if metadata.exists():
            try:
                meta = json.loads(metadata.read_text())
            except ValueError as exc:
                fail("metadata_frames", f"Unreadable metadata.json: {exc}")
                meta = {}
            if meta.get("data_source") == "policy_rollout":
                found["variant"] = "policy_rollout"
            if meta.get("camera_stalled") or meta.get("cameras", {}).get(
                "stall_detection", {}
            ).get("stalled"):
                fail("stall_markers", "Capture metadata records camera stall")
            if pose is not None and meta.get("frame_count", len(pose)) != len(pose):
                fail("metadata_frames", "metadata.json frame_count mismatch")
            if not meta.get("stopped_at"):
                found["warnings"].append("No stopped_at marker")
                found["incomplete"].append("No stopped_at marker")
        else:
            found["warnings"].append("No metadata.json")
            found["incomplete"].append("No metadata.json")
        events = demo / "events.csv"
        if events.exists():
            try:
                names = set(pd.read_csv(events).get("event", []))
            except (OSError, ValueError):
                names = set()
            if "camera_stalled" in names:
                fail("stall_markers", "Capture contains camera_stalled event")
            if not names & {"stop_demo", "episode_end"}:
                found["warnings"].append("No stop_demo/episode_end event")
                found["incomplete"].append("No stop_demo/episode_end event")
        else:
            found["warnings"].append("No events.csv")
            found["incomplete"].append("No events.csv")
        found["outcome"] = raw.demo_outcome(demo)
        return found

    def inspect(self, root: Path, options: Options, progress=None) -> InputReport:
        root = Path(root)
        report = InputReport(source=str(root), format=self.id, label=self.label)
        reqs: dict[str, Requirement] = {}

        def put(rid, status, detail="", verified="now"):
            label, fix = LABELS[rid]
            reqs[rid] = Requirement(
                id=rid,
                label=label,
                status=status,
                detail=detail,
                verified=verified,
                fix=fix
                if status in ("fail", "warn") or verified == "during_scan"
                else "",
            )

        links = [p for p in [root, *root.rglob("*")] if p.is_symlink()]
        put(
            "symlinks",
            "fail" if links else "pass",
            f"{len(links)} symbolic link(s), e.g. {links[0].relative_to(root)}"
            if links
            else "",
        )
        demos = demo_dirs(root)
        excluded = set(options.exclude_demos)
        known = {d.relative_to(root).as_posix() for d in demos}
        unknown = sorted(excluded - known)
        selected = [d for d in demos if d.relative_to(root).as_posix() not in excluded]
        discarded = [p for p in root.rglob("discarded_*") if p.is_dir()]
        if unknown or not selected:
            put(
                "layout",
                "fail",
                f"Unknown excluded demo paths: {unknown}"
                if unknown
                else "No demo_* captures selected",
            )
            report.requirements = [reqs[r] for r, _, _ in REQUIREMENTS if r in reqs]
            return report
        put(
            "layout",
            "pass",
            f"{len(selected)} demos in {len({d.parent for d in selected})} task folder(s)"
            + (f"; {len(excluded)} excluded" if excluded else "")
            + (
                f"; {len(discarded)} discarded_* folder(s) ignored" if discarded else ""
            ),
        )
        task_dirs = sorted({d.parent for d in selected})
        missing_text = [
            t.relative_to(root).as_posix() or "."
            for t in task_dirs
            if not (t / "task_description.txt").is_file()
        ]
        tasks = Counter(raw.task_text(d, options) for d in selected)
        put(
            "task_text",
            "warn" if missing_text else "pass",
            (
                f"Folder name used for: {_examples(missing_text)}; "
                if missing_text
                else ""
            )
            + f"{len(tasks)} task(s)",
        )

        if progress:
            progress.stage("Inspect", len(selected))
        results = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            for found in pool.map(
                lambda d: self._inspect_demo(root, d, options), selected
            ):
                results.append(found)
                if progress:
                    progress.advance(found["id"])

        for code in (
            "csv_schema",
            "frame_ids",
            "timestamps",
            "alignment",
            "success_flag",
            "pose_values",
            "gripper_commands",
            "cameras",
            "frame_count",
            "fps",
            "stale_state",
            "stall_markers",
            "metadata_frames",
        ):
            bad = [r["id"] for r in results if code in r["codes"]]
            if bad:
                first = next(r["codes"][code] for r in results if code in r["codes"])
                put(
                    code, "fail", f"{len(bad)} demo(s): {_examples(bad)} — e.g. {first}"
                )
            else:
                put(code, "pass")
        codec_warn = [r["id"] for r in results if r["codec"]]
        put(
            "camera_codec",
            "warn" if codec_warn else "pass",
            f"{len(codec_warn)} demo(s) not H.264/yuv420p: {_examples(codec_warn)}"
            if codec_warn
            else "",
        )
        incomplete = [r["id"] for r in results if r["incomplete"]]
        put(
            "completion",
            ("fail" if options.require_complete else "warn") if incomplete else "pass",
            f"{len(incomplete)} demo(s) missing markers: {_examples(incomplete)}"
            if incomplete
            else "",
        )

        rates = [f for r in results for f in r["fps"]]
        variants = Counter(r["variant"] for r in results)
        variant = variants.most_common(1)[0][0]
        outcomes = Counter(r["outcome"] or "unlabeled" for r in results)
        if variant == "policy_rollout":
            unlabeled = outcomes.get("unlabeled", 0)
            put(
                "outcomes",
                "warn" if unlabeled else "pass",
                f"{outcomes.get('success', 0)} success / {outcomes.get('failure', 0)} failure"
                + (f" / {unlabeled} unlabeled" if unlabeled else ""),
            )
        else:
            put(
                "outcomes",
                "info",
                "Teleoperation capture: no per-episode outcome labels "
                "(success_flag is an operator toggle, not a task result)",
            )
        put("video_decode", "info", "Checked during conversion", verified="during_scan")

        out_fps, fps_note = output_fps(rates, options)
        report.variant = variant
        report.summary = {
            "demos": len(selected),
            "excluded": sorted(excluded),
            "discarded": len(discarded),
            "tasks": dict(tasks),
            "frames": int(sum(r["frames"] or 0 for r in results)),
            "outcomes": dict(outcomes),
            "cameras": list(options.cameras),
            "fps_min": min(rates) if rates else None,
            "fps_max": max(rates) if rates else None,
            "output_fps": out_fps,
            "fps_note": fps_note,
            "timing": options.timing,
        }
        report.episodes = [
            EpisodeFinding(
                source_id=r["id"],
                frames=r["frames"],
                outcome=r["outcome"],
                errors=list(r["codes"].values()),
                warnings=r["warnings"],
            )
            for r in results
        ]
        report.requirements = [reqs[r] for r, _, _ in REQUIREMENTS if r in reqs]
        return report

    def episodes(self, root: Path, options: Options) -> list[SourceEpisode]:
        root = Path(root)

        def build(demo):
            cameras, rates, probes = {}, {}, {}
            image_mode = all((demo / c).is_dir() for c in options.cameras)
            for camera in options.cameras:
                info = camera_info(demo, camera, options)
                if image_mode:
                    cameras[camera] = str(demo / camera)
                else:
                    cameras[camera] = str(raw.camera_path(demo, camera))
                    rates[camera] = info["fps"]
                    probes[camera] = info
            return SourceEpisode(
                source_id=demo.relative_to(root).as_posix(),
                path=str(demo),
                task=raw.task_text(demo, options),
                outcome=raw.demo_outcome(demo),
                cameras=cameras,
                camera_fps=rates,
                image_mode=image_mode,
                metadata={"probe": probes},
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            return list(pool.map(build, raw.demos(root, options)))
