"""A dataset's subtask vocabulary: the labels a person picks from.

``<annotations>/vocabulary.json`` = ``{"subtasks": [{"id", "label",
"definition"}], "updated_at"}``. With it, a human subtask carries the same
``subtask_id`` an agent's does, so the two can be compared interval by
interval; without it, human subtasks stay free text as before.

When a dataset has no vocabulary of its own, the definitions of the last
agent plan on it are offered as a starting point (``suggested``).
"""

import json
import os
import re
import time
from pathlib import Path

ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
# Labels every temporal workflow accepts besides the defined ones.
SPECIAL = ("other", "unknown", "background")


def path_for(annotations_dir: Path) -> Path:
    return annotations_dir / "vocabulary.json"


def read(annotations_dir: Path) -> dict:
    try:
        value = json.loads(path_for(annotations_dir).read_text())
    except (OSError, ValueError):
        return {"subtasks": [], "updated_at": None}
    return {
        "subtasks": value.get("subtasks") or [],
        "updated_at": value.get("updated_at"),
    }


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
        clean.append(
            {
                "id": sid,
                "label": str(item.get("label") or sid).strip()[:80],
                "definition": str(item.get("definition") or "").strip()[:400],
            }
        )
    if len(clean) > 40:
        raise ValueError("At most 40 subtasks")
    annotations_dir.mkdir(parents=True, exist_ok=True)
    value = {"subtasks": clean, "updated_at": time.time()}
    target = path_for(annotations_dir)
    temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=1))
    os.replace(temp, target)
    return value


def ids(annotations_dir: Path) -> list[str]:
    return [s["id"] for s in read(annotations_dir)["subtasks"]]
