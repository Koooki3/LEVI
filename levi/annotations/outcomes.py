"""Human episode outcome labels (success / failure).

One file per episode — ``annotations/<dataset name>/outcomes/
episode_NNNNNN.json`` — like the language sidecars, so collaborators on
several LEVI processes never overwrite each other's labels. A human label
overrides the dataset's own ``levi_outcome`` metadata; the RECAP export
snapshots them into its plan (``Options.outcome_labels``).
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Literal

Outcome = Literal["success", "failure"]
_FILE = re.compile(r"episode_(\d{6,})\.json")


def outcome_dir(annotations_dir: Path) -> Path:
    return annotations_dir / "outcomes"


def read_labels(annotations_dir: Path) -> dict[int, dict]:
    """Episode index → ``{"outcome", "source": "human", "updated_at"}``."""
    folder = outcome_dir(annotations_dir)
    labels = {}
    if not folder.is_dir():
        return labels
    for path in folder.iterdir():
        match = _FILE.fullmatch(path.name)
        if not match:
            continue
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            continue  # being replaced by another process
        if value.get("outcome") in ("success", "failure"):
            labels[int(match.group(1))] = value
    return labels


def write_label(
    annotations_dir: Path, episode: int, outcome: Outcome | None
) -> dict | None:
    """Set (or with ``None`` clear) one episode's label, atomically."""
    if episode < 0:
        raise ValueError("Episode index must be non-negative")
    folder = outcome_dir(annotations_dir)
    path = folder / f"episode_{episode:06d}.json"
    if outcome is None:
        path.unlink(missing_ok=True)
        return None
    if outcome not in ("success", "failure"):
        raise ValueError("Outcome must be success or failure")
    folder.mkdir(parents=True, exist_ok=True)
    value = {
        "episode_index": episode,
        "outcome": outcome,
        "source": "human",
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(value))
    os.replace(temp, path)
    return value


def labels_for_source(source: Path) -> dict[str, str]:
    """Human labels for a conversion source, keyed the way its input format
    names episodes: ``str(episode_index)`` for a dataset, the demo path
    (``source_demo``) for a raw capture browsed through its view."""
    from .. import catalog

    name = catalog.name_for_path(source)
    if name is None:
        return {}
    labels = read_labels(catalog.STATE / "annotations" / name)
    if not labels:
        return {}
    view = catalog.datasets().get(name, {}).get("view")
    if not view:
        return {str(ep): v["outcome"] for ep, v in labels.items()}
    rows = [
        json.loads(line)
        for line in (Path(view) / "meta/episodes.jsonl").read_text().splitlines()
        if line.strip()
    ]
    demos = {r["episode_index"]: r.get("source_demo") for r in rows}
    return {demos[ep]: v["outcome"] for ep, v in labels.items() if demos.get(ep)}
