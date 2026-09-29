"""RECAP advantage labels as a tie-breaking signal for episode selection.

A LEVI workspace the scan found may hold RECAP results per registered
LeRobot dataset (``recap_values/<name>/revisions/<id>/summary.json``: per
episode the fraction of frames labelled positive). ``load`` maps pool episode
keys (``<dataset folder>#<episode index>``) to that fraction. Everything is
optional: no scan record of workspaces, no results, or unreadable files give
an empty map, and selection then ignores the signal.
"""

import json
from functools import lru_cache
from pathlib import Path

from . import index, labels


def _read(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _workspace(workspace: Path) -> dict[str, float]:
    out: dict[str, float] = {}
    base = workspace / labels.WORKBENCH / "recap_values"
    for entry in labels.catalog_entries(workspace):
        name, path = entry.get("name"), entry.get("path")
        if not name or not path or entry.get("kind") == "raw":
            continue
        current = _read(base / name / "current.json") or {}
        rid = current.get("revision_id")
        summary = _read(base / name / "revisions" / str(rid) / "summary.json")
        if not isinstance(summary, dict):
            continue
        root = str(Path(path).resolve())
        for ep, value in (summary.get("episodes") or {}).items():
            fraction = (
                value.get("positive_fraction") if isinstance(value, dict) else None
            )
            if isinstance(fraction, (int, float)):
                out[f"{root}#{ep}"] = float(fraction)
    return out


@lru_cache(maxsize=4)
def _load(scanned_at: str, workspaces: tuple[str, ...]) -> dict[str, float]:
    out: dict[str, float] = {}
    for workspace in workspaces:
        out.update(_workspace(Path(workspace)))
    return out


def load() -> dict[str, float]:
    """Pool episode key → positive fraction of its RECAP labels ({} if none)."""
    try:
        summary = index.summary() or {}
        workspaces = tuple(summary.get("workspaces") or ())
        return _load(str(summary.get("scanned_at")), workspaces) if workspaces else {}
    except (OSError, ValueError):
        return {}
