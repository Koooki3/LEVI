"""A person's confirmation that an episode's subtask annotation is complete.

One file per episode -- ``<annotations>/status/episode_NNNNNN.json`` -- like
the outcome labels, so people working in several LEVI processes never
overwrite each other. Confirming is separate from saving: a save may be work
in progress, a confirmation says "this episode is done". The annotation
recorder counts the confirmations made while it runs.
"""

import json
import os
import re
import time
from pathlib import Path

_FILE = re.compile(r"episode_(\d{6,})\.json")


def status_dir(annotations_dir: Path) -> Path:
    return annotations_dir / "status"


def read_all(annotations_dir: Path) -> dict[int, dict]:
    """Episode index -> ``{"episode_index", "done", "by", "confirmed_at"}``."""
    folder = status_dir(annotations_dir)
    found = {}
    if not folder.is_dir():
        return found
    for path in folder.iterdir():
        match = _FILE.fullmatch(path.name)
        if not match:
            continue
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            continue  # being replaced by another process
        if value.get("done") is True:
            found[int(match.group(1))] = value
    return found


def confirm(
    annotations_dir: Path, episode: int, done: bool, by: str = "human"
) -> dict | None:
    """Mark (or with ``done=False`` unmark) one episode complete, atomically."""
    if episode < 0:
        raise ValueError("Episode index must be non-negative")
    folder = status_dir(annotations_dir)
    path = folder / f"episode_{episode:06d}.json"
    if not done:
        path.unlink(missing_ok=True)
        return None
    folder.mkdir(parents=True, exist_ok=True)
    value = {
        "episode_index": episode,
        "done": True,
        "by": by,
        "confirmed_at": time.time(),
    }
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(value))
    os.replace(temp, path)
    return value
