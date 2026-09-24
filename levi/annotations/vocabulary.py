"""A dataset's subtask vocabulary: the labels a person or an agent picks from.

``<annotations>/vocabulary.json`` = ``{"subtasks": [{"id", "label",
"definition", ...}], "updated_at"}``. A human subtask carries the same
``subtask_id`` an agent's does, so the two can be compared interval by
interval.

A dataset without a vocabulary of its own uses LEVI's built-in, task-agnostic
manipulation vocabulary (``levi/knowledge/subtasks.json``: object-transfer
phases and contact skills, each with its observable start, end and success
and the aliases other datasets use for it). An annotator may add a subtask
the vocabulary lacks while annotating -- with the same fields, and never a
synonym of an existing entry (:func:`check_new`); once a person commits the
work, the added entries join the dataset's own vocabulary with their origin.
"""

import json
import os
import re
import time
from pathlib import Path

ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
# Labels every temporal workflow accepts besides the defined ones.
SPECIAL = ("other", "unknown", "background")


BUILTIN = Path(__file__).resolve().parents[1] / "knowledge" / "subtasks.json"
LIMIT = 64
# The fields a plan's subtask definition needs (levi.agent.planning).
DEFINED = ("definition", "starts_when", "ends_when", "success_when")


def builtin() -> dict:
    """LEVI's built-in manipulation vocabulary (with its sources)."""
    return json.loads(BUILTIN.read_text())


def path_for(annotations_dir: Path) -> Path:
    return annotations_dir / "vocabulary.json"


def own(annotations_dir: Path) -> dict:
    """The dataset's own vocabulary file (empty when it has none)."""
    try:
        value = json.loads(path_for(annotations_dir).read_text())
    except (OSError, ValueError):
        return {"subtasks": [], "updated_at": None}
    return {
        "subtasks": value.get("subtasks") or [],
        "updated_at": value.get("updated_at"),
    }


def read(annotations_dir: Path) -> dict:
    """The vocabulary in force: the dataset's own, else the built-in one
    (``source`` says which)."""
    value = own(annotations_dir)
    if value["subtasks"]:
        return {**value, "source": "dataset"}
    return {"subtasks": builtin()["subtasks"], "updated_at": None, "source": "builtin"}


def _complete(entry: dict, fallback: dict | None) -> dict:
    """A plan definition from a vocabulary entry: missing fields come from
    the built-in entry of the same id, else from its definition text."""
    out = {"id": entry["id"], "label": entry.get("label") or entry["id"]}
    for field in DEFINED + ("confusions",):
        value = entry.get(field) or (fallback or {}).get(field) or ""
        if not value and field in DEFINED:
            value = entry.get("definition") or entry.get("label") or entry["id"]
        out[field] = value
    return out


def definitions(annotations_dir: Path) -> list[dict]:
    """Plan definitions for a temporal run on this dataset: its vocabulary in
    force, every entry with start, end and success."""
    base = {s["id"]: s for s in builtin()["subtasks"]}
    return [_complete(s, base.get(s["id"])) for s in read(annotations_dir)["subtasks"]]


def names(entries: list[dict]) -> dict[str, str]:
    """Every id and alias -> the id it names."""
    out = {}
    for s in entries:
        out[s["id"]] = s["id"]
        for alias in s.get("aliases") or []:
            out.setdefault(alias, s["id"])
    for special in SPECIAL:
        out.setdefault(special, special)
    return out


def check_new(entry: dict, existing: list[dict]) -> str | None:
    """Why an annotator's new subtask cannot join (None: it can)."""
    sid = str(entry.get("id", ""))
    if not ID.match(sid):
        return f"Subtask id {sid!r}: use lowercase letters, digits, - or _"
    known = names(existing + builtin()["subtasks"])
    if sid in known:
        target = known[sid]
        if target != sid:
            return (
                f"{sid!r} is an alias of {target!r} in the vocabulary; use {target!r}"
            )
        return f"{sid!r} is already in the vocabulary"
    missing = [f for f in DEFINED if not str(entry.get(f) or "").strip()]
    if missing:
        return f"New subtask {sid!r} needs {', '.join(missing)}"
    return None


KEPT = DEFINED + ("confusions", "source", "added_by", "added_at")


def write(annotations_dir: Path, subtasks: list[dict]) -> dict:
    clean, seen = [], set()
    for item in subtasks:
        sid = str(item.get("id", "")).strip().lower()
        if not ID.match(sid):
            raise ValueError(
                f"Subtask id {sid!r}: use lowercase letters, digits, - or _"
            )
        if sid in seen:
            raise ValueError(f"Subtask id {sid!r} appears twice")
        seen.add(sid)
        row = {
            "id": sid,
            "label": str(item.get("label") or sid).strip()[:80],
            "definition": str(item.get("definition") or "").strip()[:400],
        }
        for field in KEPT[1:]:
            if item.get(field) not in (None, ""):
                row[field] = (
                    item[field] if field == "added_at" else str(item[field])[:400]
                )
        if item.get("aliases"):
            row["aliases"] = [str(a) for a in item["aliases"]][:12]
        clean.append(row)
    if len(clean) > LIMIT:
        raise ValueError(f"At most {LIMIT} subtasks")
    annotations_dir.mkdir(parents=True, exist_ok=True)
    value = {"subtasks": clean, "updated_at": time.time()}
    target = path_for(annotations_dir)
    temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=1))
    os.replace(temp, target)
    return value


def ids(annotations_dir: Path) -> list[str]:
    return [s["id"] for s in read(annotations_dir)["subtasks"]]


def extend(annotations_dir: Path, entries: list[dict], origin: dict) -> dict:
    """Add an annotator's new subtasks (already checked) to the dataset's
    vocabulary, which starts from the one in force."""
    current = read(annotations_dir)["subtasks"]
    have = {s["id"] for s in current}
    added = [
        {
            **e,
            "source": "agent",
            "added_by": origin.get("run_id", ""),
            "added_at": time.time(),
        }
        for e in entries
        if e["id"] not in have
    ]
    if not added:
        return own(annotations_dir)
    return write(annotations_dir, current + added)
