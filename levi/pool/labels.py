"""Human outcome labels from every LEVI workspace the pool can see, read-only.

A workspace is a folder holding ``outputs/LEVI/workbench/datasets.json``.
Labels live per catalog entry (``annotations/<name>/outcomes/``, or the
active revision's bundle when the agent store has one); a raw capture's
labels are keyed by episode index of its browsing view and mapped back to the
demo folder through the view's ``source_demo``. Namespaces keep experiment
labels of their own and are not read. The agent store is opened read-only
(``mode=ro``) so the pool never writes into another workspace.
"""

import json
import sqlite3
from pathlib import Path

from ..annotations.outcomes import read_labels

WORKBENCH = Path("outputs/LEVI/workbench")


def is_workspace(folder: Path) -> bool:
    return (folder / WORKBENCH / "datasets.json").is_file()


def _head_folder(state: Path, name: str) -> Path:
    database = state / "agent/workbench.sqlite3"
    if database.is_file():
        try:
            with sqlite3.connect(
                f"file:{database}?mode=ro", uri=True, timeout=5
            ) as conn:
                row = conn.execute(
                    "SELECT revision FROM heads WHERE dataset=?", (name,)
                ).fetchone()
        except sqlite3.Error:
            row = None
        if row and row[0] != "legacy":
            return state / "agent/datasets" / name / "revisions" / row[0] / "annotations"
    return state / "annotations" / name


def _read_jsonl(path: Path) -> list[dict]:
    try:
        return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    except (OSError, ValueError):
        return []


def catalog_entries(workspace: Path) -> list[dict]:
    try:
        items = json.loads((workspace / WORKBENCH / "datasets.json").read_text())
    except (OSError, ValueError):
        return []
    return [v for v in items.values() if isinstance(v, dict) and not v.get("base")]


def workspace_labels(workspace: Path) -> dict[str, dict]:
    """Pool episode key → {"outcome", "workspace", "dataset"}."""
    state = workspace / WORKBENCH
    found: dict[str, dict] = {}
    for entry in catalog_entries(workspace):
        name = entry.get("name")
        path = entry.get("path")
        if not name or not path:
            continue
        labels = read_labels(_head_folder(state, name))
        if not labels:
            continue
        if entry.get("kind") == "raw":
            view = entry.get("view")
            rows = _read_jsonl(Path(view) / "meta/episodes.jsonl") if view else []
            demos = {r.get("episode_index"): r.get("source_demo") for r in rows}
            for ep, value in labels.items():
                demo = demos.get(ep)
                if demo:
                    key = str((Path(path) / demo).resolve())
                    found[key] = {
                        "outcome": value["outcome"],
                        "workspace": str(workspace),
                        "dataset": name,
                    }
        else:
            root = str(Path(path).resolve())
            for ep, value in labels.items():
                found[f"{root}#{ep}"] = {
                    "outcome": value["outcome"],
                    "workspace": str(workspace),
                    "dataset": name,
                }
    return found


def registered_paths(workspaces: list[Path]) -> set[str]:
    """Folders a LEVI workspace registered as datasets (each one a source)."""
    out = set()
    for workspace in workspaces:
        for entry in catalog_entries(workspace):
            if entry.get("path"):
                out.add(str(Path(entry["path"]).resolve()))
    return out
