"""Browsing views: raw captures made viewable and annotatable in place.

The viewer, annotation backend and SAM3 all need a LeRobot layout whose
timestamps are exactly ``frame_index / fps``. A raw capture has neither, so
registering one builds a *view* under ``outputs/LEVI/workbench/views/``:
every captured frame kept, videos stream-copied and retimed to one rate
(bit-identical pixels, no encode), parquet built from the CSVs. The catalog
entry keeps the raw root as its ``path`` and serves the view; annotations
are stored under the raw dataset's own name, and carried into any dataset
later converted from it (see ``levi/annotations/carryover.py``).

Views are for looking, not for training: exporting one is refused ("convert
first — annotations carry over"). Failing episodes are left out of the view
and listed in ``meta/levi_view.json`` instead of blocking the whole capture.
"""

import json
import shutil
import statistics
from pathlib import Path

from . import catalog, jobs
from .conversion import pipeline, registry
from .conversion.engine import fingerprint
from .conversion.options import Options
from .conversion.progress import Progress

VIEW_SCHEMA = "levi.view.v1"


def is_view(root: Path) -> bool:
    return (Path(root) / "meta/levi_view.json").is_file()


def view_fps(rates: list[float], options: Options) -> float:
    """One declared rate for the whole view: the median measured camera rate
    to 0.1 fps (image folders use ``source_fps``)."""
    if not rates:
        return options.source_fps
    return max(1.0, round(statistics.median(rates), 1))


def build(source: Path, target: Path, options: Options, progress_path=None) -> dict:
    """Engine stage ``view``: raw capture → browsing view at ``target``."""
    fmt = registry.detect(source)
    if fmt is None or not fmt.viewable:
        raise ValueError("Only raw captures need a browsing view")
    if fmt.id == "droid_raw":
        from .droid_view import build as build_droid

        return build_droid(source, target, options, progress_path)
    progress = Progress(progress_path, pipeline.STAGES)
    base = options.model_copy(
        update={"timing": "retime", "filter_static": False, "target": "lerobot_v21"}
    )
    report = fmt.inspect(source, base, progress)
    excluded = {e.source_id: e.errors for e in report.episodes if e.errors}
    if excluded:
        base = base.model_copy(
            update={"exclude_demos": sorted(set(base.exclude_demos) | set(excluded))}
        )
    rates = [r for e in fmt.episodes(source, base) for r in e.camera_fps.values()]
    base = base.model_copy(update={"fps": view_fps(rates, base)})
    result = pipeline.run(
        source,
        target,
        base,
        fmt,
        registry.output_format("lerobot_v21"),
        progress_path,
        strict=False,
    )
    view = {
        "schema": VIEW_SCHEMA,
        "source_root": str(source),
        "input_format": fmt.id,
        "variant": report.variant,
        "view_fps": base.fps,
        "source_fingerprint": fingerprint(source),
        "excluded": excluded,
        "skipped": result.get("skipped", {}),
    }
    catalog.atomic(target / "meta/levi_view.json", view)
    progress.finish()
    return {**result, "view": view}


def request(root: Path, options: dict | None = None) -> dict:
    """Register a raw capture: returns its catalog entry, starting a view
    build job when the view is missing or the capture changed since."""
    root = catalog.inside(root)
    fmt = registry.detect(root)
    if fmt is None or not fmt.viewable:
        raise ValueError(
            "Not a LeRobot dataset (meta/info.json) or a recognized raw capture"
        )
    # Serialized in-process so two registrations of one capture start one
    # build; each build still writes its own directory, so even concurrent
    # LEVI processes can only waste work, never corrupt a view.
    with catalog.locked():
        existing = catalog.name_for_path(root)
        entry = catalog.datasets().get(existing) if existing else None
        if entry and entry.get("view") and is_view(Path(entry["view"])):
            meta = json.loads((Path(entry["view"]) / "meta/levi_view.json").read_text())
            if meta.get("source_fingerprint") == fingerprint(root):
                return entry
        if entry and entry.get("view_status") == "building" and entry.get("view_job"):
            job = catalog.read(jobs.STATE / "jobs" / f"{entry['view_job']}.json", {})
            if job.get("status") in ("planned", "queued", "running"):
                return entry
        entry = catalog.add_entry(
            root, {"kind": "raw", "input_format": fmt.id, "view_status": "building"}
        )
        job = jobs.plan(
            "view",
            str(root),
            options=options or {},
            output=str(_staging(entry["name"])),
        )
        catalog.atomic(jobs.STATE / "jobs" / (job["id"] + ".json"), job)
        entry = catalog.add_entry(root, {"view_job": job["id"]})
    jobs.launch(job)
    return entry


def _staging(name: str) -> Path:
    """``views/.build-<dataset>-<YYYYmmdd-HHMM>`` (``-2``… on a clash).

    Readable while it exists -- it names the dataset and when the build began --
    and renamed to the bare dataset name when the build is published. The
    caller holds the catalog lock, so the existence check cannot race.
    """
    from .naming import _now

    folder = jobs.STATE / "views"
    base = f".build-{name}-{_now()}"
    candidate, attempt = folder / base, 1
    while candidate.exists():
        attempt += 1
        candidate = folder / f"{base}-{attempt}"
    return candidate


def publish(source: Path, output: Path) -> dict:
    """After a successful view job: point the catalog at the new view,
    re-key annotations if the episode set changed, drop the old view."""
    info = json.loads((output / "meta/info.json").read_text())
    with catalog.locked():
        return _publish(source, output, info)


def _publish(source: Path, output: Path, info: dict) -> dict:
    name = catalog.name_for_path(source)
    old = catalog.datasets().get(name, {}).get("view") if name else None
    final = output.parent / (name or output.name.lstrip("."))
    if old and Path(old) != output and Path(old).is_dir():
        from .annotations.carryover import rekey_view

        rekey_view(name, Path(old), output)
        shutil.rmtree(old)
    if final != output:
        if final.exists():
            shutil.rmtree(final)
        output.rename(final)
    return catalog.add_entry(
        source,
        {"view": str(final), "view_status": "ready", "info": info},
    )
