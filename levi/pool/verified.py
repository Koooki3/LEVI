"""Verified outcomes: loaded from ``LEVI_POOL_OUTCOMES`` lists.

An agent (or a person) checked some rollout episodes independently of the
operator's key press and recorded the result. A list is JSON with
``episodes``: ``[{"path", "outcome": "success"|"failure", "basis"?,
"evidence"?}, ...]`` (``path`` absolute or relative to a pool root), an
optional ``name`` (default: the file's stem) and a ``source`` such as
``agent-verified``. An entry whose ``outcome`` is anything else (``unknown``,
a missing value) says nothing: it is skipped and counted, not an error.

In the index the verified outcome ranks below a human label and above the
robot's key press (``outcome_source`` is ``verified``); it applies to the whole
group of copies of a listed episode.
"""

from collections import Counter
from pathlib import Path

from . import manifest

OUTCOMES = ("success", "failure")


def load(files: list[Path]) -> tuple[list[dict], dict]:
    """``(entries, skipped)``: the usable entries, and how many entries were
    skipped per value (``{"unknown": 3}``; a missing value counts as
    ``"missing"``)."""
    entries: list[dict] = []
    skipped: Counter = Counter()
    for file in files:
        name, items = manifest.read(file, "verified-outcome")
        for i, item in enumerate(items):
            value = item.get("outcome")
            outcome = value.strip().lower() if isinstance(value, str) else None
            if outcome not in OUTCOMES:
                skipped[outcome or "missing"] += 1
                continue
            entries.append(
                {
                    "set": name,
                    "id": f"{name}:{i}",
                    "path": item["path"].strip(),
                    "outcome": outcome,
                    "basis": item.get("basis")
                    if isinstance(item.get("basis"), str)
                    else None,
                }
            )
    return entries, dict(skipped)
