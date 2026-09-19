"""Execute validated stages; all writes target fresh, independent directories."""

import json
import os
import shutil
from pathlib import Path

import numpy as np

from ..catalog import atomic
from ..paths import inside
from . import dataset, media, raw, registry
from .options import STAGES, Options
from .progress import Progress


def fingerprint(source: Path):
    """Capture size/mtime manifest, used before/after jobs to detect concurrent edits."""
    import hashlib

    rows = []
    for p in sorted(source.rglob("*")):
        rel = p.relative_to(source).as_posix()
        if p.is_symlink():
            # Links matter (conversion refuses them), whatever they point to.
            rows.append((rel, "link", os.readlink(p)))
        elif p.is_file():
            st = p.stat()
            rows.append((rel, st.st_size, st.st_mtime_ns))
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def execute(
    stage: str,
    source: Path,
    target: Path,
    options: Options,
    progress: Path | None = None,
    intermediate: Path | None = None,
):
    if stage not in STAGES:
        raise ValueError("Unknown pipeline stage")
    source = inside(source)
    target = inside(target)
    if not source.is_dir():
        raise ValueError("Input directory does not exist")
    if target.exists() or target.is_relative_to(source):
        raise ValueError("Use a new output directory outside the input tree")
    raw.check_tree(source)
    if stage == "validate":
        return dataset.validate(source)
    if stage == "inspect":
        report = registry.inspect(source, options, Progress(progress, ["Inspect"]))
        # The inspection itself succeeded even when requirements fail.
        return {"ok": True, "report": report.model_dump()}
    if stage == "view":
        from ..views import build

        return build(source, target, options, progress)
    if stage == "pipeline":
        # Any registered input → options.target (LeRobot v2.1 by default);
        # see pipeline.py for the single-pass flow and registry.py for the
        # supported combinations.
        return registry.export(
            source,
            target,
            options,
            progress,
            intermediate if options.keep_intermediates else None,
        )
    if stage in ("timestamps", "tasks", "tasks-preview"):
        return dataset.repair(source, target, options, stage)
    selected = raw.demos(source, options)
    if stage in (
        "summary",
        "frozen",
        "stage-preview",
        "static",
        "filter-preview",
        "fps-preview",
    ):
        records = []
        for demo in selected:
            if stage == "fps-preview" and all(
                (demo / c).is_dir() for c in options.cameras
            ):
                pose, *_ = raw.load(demo)
                rec = {
                    "frames": len(pose),
                    "image_counts": {
                        c: len(raw.image_files(demo, c)) for c in options.cameras
                    },
                    "source_fps": options.source_fps,
                    "target_fps": options.fps,
                    "ok": True,
                }
                rec["ok"] = all(n == len(pose) for n in rec["image_counts"].values())
            else:
                rec = raw.audit(demo, options)
            rec["demo"] = demo.relative_to(source).as_posix()
            records.append(rec)
        duplicates = {}
        for rec in records:
            for camera, v in rec.get("cameras", {}).items():
                duplicates.setdefault((camera, v["first_hash"]), []).append(rec["demo"])
        return {
            "ok": all(r["ok"] for r in records),
            "records": records,
            "duplicate_first_frames": [
                {"camera": k[0], "demos": v}
                for k, v in duplicates.items()
                if len(v) > 1
            ],
            "excluded_demos": options.exclude_demos,
        }
    if stage in ("images", "fps", "stage", "filter"):
        if stage in ("stage", "filter"):
            records = [raw.audit(d, options) for d in selected]
            if any(not r["ok"] for r in records):
                raise ValueError(
                    "Preflight failed: " + json.dumps(records, ensure_ascii=False)
                )
        target.mkdir(parents=True)
        for demo in selected:
            out = target / demo.relative_to(source)
            pose, _, xyz, q, grip = raw.load(demo)
            image_mode = stage == "images" or (
                stage == "fps" and all((demo / c).is_dir() for c in options.cameras)
            )
            if stage in ("images", "fps"):
                rate = (
                    options.source_fps
                    if image_mode
                    else media.probe(
                        raw.camera_path(demo, next(iter(options.cameras)))
                    )["fps"]
                )
                if not image_mode:
                    for camera in options.cameras:
                        v = media.inspect(raw.camera_path(demo, camera))
                        if v["frames"] != len(pose) or abs(v["fps"] - rate) > 0.01:
                            raise ValueError("Camera/CSV alignment mismatch")
                positions = raw.sample_positions(len(pose), rate, options.fps)
            elif stage == "filter":
                positions = raw.keep_positions(xyz, q, grip, options)
            else:
                positions = np.arange(len(pose))
            if stage == "stage" and all(
                (demo / (c + ".mp4")).is_file() for c in options.cameras
            ):
                shutil.copytree(demo, out)
            else:
                raw.transform_demo(demo, out, positions, options, image_mode)
            raw.copy_task(demo, out, options)
        return {"ok": True, "output": str(target), "demos": len(selected)}
    if stage == "convert":
        out = dataset.convert(source, target, options)
        report = dataset.validate(out)
        atomic(out / "meta/levi_validation.json", report)
        return {"ok": report["ok"], "dataset_path": str(out), "validation": report}
    raise ValueError("Unimplemented stage")
