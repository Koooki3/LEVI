"""Held-out (frozen test) episodes: loaded from ``LEVI_POOL_HELDOUT`` lists.

A list is JSON with ``episodes``: ``[{"path", "sha256": {<video file>: hex},
"frame_count"?, "frozen_id"?}, ...]``; ``path`` is relative to a pool root
(or absolute). Only ``episodes`` are held out — other keys (development
pools, subsets) are ignored. An episode is held out when

1. its path matches an entry (the original), or
2. one of its videos has an entry's sha256 (a renamed or moved copy), or
3. it shares the content fingerprint of a held-out episode (byte copies,
   and LeRobot episodes converted from it).

Held-out episodes are never exported; this is a refusal, not a filter.
"""

import hashlib
import json
from pathlib import Path


def load(files: list[Path]) -> list[dict]:
    entries = []
    for file in files:
        try:
            data = json.loads(Path(file).read_text())
        except (OSError, ValueError) as exc:
            raise ValueError(f"Cannot read held-out list {file}: {exc}") from exc
        name = data.get("version") or Path(file).stem
        for i, item in enumerate(data.get("episodes") or []):
            if not isinstance(item, dict) or not item.get("path"):
                raise ValueError(f"{file}: episode {i} has no path")
            entries.append(
                {
                    "set": name,
                    "id": item.get("frozen_id") or f"{name}:{i}",
                    "path": item["path"],
                    "sha256": dict(item.get("sha256") or {}),
                    "frame_count": item.get("frame_count"),
                }
            )
    return entries


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()
