"""RECAP advantage labels as a tie-breaking signal for episode selection.

A LEVI workspace the scan found may hold RECAP results per registered
LeRobot dataset (``summary.json``: per episode the fraction of frames labelled
positive) in either layout of ``levi/recap/store.py``: ``current.json`` names a
value model (``models/<model>/head.json`` -> ``v/<version>/``) or an
original-layout revision (``revisions/<id>/``). ``load`` maps pool episode
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
        summary = _current_summary(base / name)
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


def _safe(part) -> bool:
    return isinstance(part, str) and bool(part) and "/" not in part and ".." not in part


def _current_summary(folder: Path):
    """The current result's summary, whichever layout wrote it (a values-
    only result has null fractions, which ``_workspace`` skips)."""
    current = _read(folder / "current.json") or {}
    model, rid = current.get("model"), current.get("revision_id")
    if _safe(model):
        slot = folder / "models" / model
        version = (_read(slot / "head.json") or {}).get("version")
        if _safe(version):
            return _read(slot / "v" / version / "summary.json")
        return None
    if _safe(rid):
        return _read(folder / "revisions" / rid / "summary.json")
    return None


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
