"""What a catalog entry *is*: format, version, origin and what LEVI can do
with it — shown in the Workbench's dataset list and used by the viewer to
adapt (a raw capture is browsed through a view and cannot be exported).

Read from small metadata files only (``meta/info.json`` and LEVI's own
``meta/levi_*.json`` / ``.levi-export.json`` records); never parquet or video.
"""

import json
from pathlib import Path
from typing import Any

EXPORT_MARKER = ".levi-export.json"


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def describe(entry: dict) -> dict[str, Any]:
    raw = entry.get("kind") == "raw"
    root = Path(entry.get("view") or entry["path"]) if raw else Path(entry["path"])
    # Read now: the dataset may have changed since it was registered.
    info = _read(root / "meta/info.json") or entry.get("info") or {}
    result: dict[str, Any] = {
        "kind": "raw" if raw else "lerobot",
        "version": info.get("codebase_version"),
        "fps": info.get("fps"),
        "episodes": info.get("total_episodes"),
        "frames": info.get("total_frames"),
        "robot_type": info.get("robot_type"),
    }
    if raw:
        view = _read(root / "meta/levi_view.json")
        result.update(
            origin="raw_capture",
            input_format=entry.get("input_format") or view.get("input_format"),
            variant=view.get("variant"),
            view_status=entry.get("view_status"),
            view_fps=view.get("view_fps"),
            excluded=len(view.get("excluded") or {}),
            source_time_error_max_seconds=view.get("source_time_error_max_seconds"),
            capabilities={
                "browse": entry.get("view_status") == "ready",
                "annotate": entry.get("view_status") == "ready",
                "outcome_labels": True,
                "sam3": True,
                "doctor": "view",
                "export_annotated": False,
                "push_to_hub": False,
                "convert": (entry.get("input_format") or view.get("input_format"))
                != "droid_raw",
            },
        )
        return result
    conversion = _read(root / "meta/levi_conversion.json")
    recap = _read(root / "meta/levi_recap.json")
    export = _read(root / EXPORT_MARKER)
    if export:
        origin = "levi_export"
        result["source"] = export.get("source_root")
    elif recap or conversion.get("target") == "recap_value":
        origin = "levi_recap"
        result["recap"] = {
            k: recap.get(k)
            for k in ("dataset_type", "failure_reward", "gamma", "outcomes")
        }
    elif conversion:
        origin = "levi_conversion"
    else:
        origin = "external"
    if conversion:
        options = conversion.get("options", {})
        result.update(
            source=conversion.get("derived_from") or conversion.get("source"),
            input_format=conversion.get("input_format"),
            variant=conversion.get("input_variant"),
            timing=conversion.get("timing") or options.get("timing", "resample"),
            filter_static=options.get("filter_static"),
        )
    if conversion and not result.get("input_format"):
        # levi.conversion.v1 records predate these fields; v1 only ever
        # converted robot captures, and rollouts carry levi_outcome.
        result["input_format"] = "robot_capture"
        try:
            with (root / "meta/episodes.jsonl").open() as handle:
                first = json.loads(handle.readline() or "{}")
        except (OSError, ValueError):
            first = {}
        result["variant"] = (
            "policy_rollout" if "levi_outcome" in first else result.get("variant")
        )
    result["origin"] = origin
    result["capabilities"] = {
        "browse": True,
        "annotate": True,
        "outcome_labels": True,
        "sam3": True,
        "doctor": True,
        "export_annotated": True,
        "push_to_hub": True,
        "convert": result["version"] is not None
        and str(result["version"]).startswith("v2"),
    }
    return result
