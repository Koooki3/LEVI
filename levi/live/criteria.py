"""When is a rollout directory finished? (interface C1 of the live plan.)

The evaluation client writes ``<root>/<group>/<task_folder>/demo_NNNN/`` and,
as the very last step of ``close()``, creates the empty file ``.complete``.
Until then the directory is being written: CSVs are back-filled, the videos
are muxed and renamed, ``metadata.json`` is rewritten. Linking such a
directory would hand LEVI half a demo (and, for a file the client later
replaces in place, a stale inode).

A demo is **complete** when

1. ``.complete`` exists -- or, for a rollout written before the marker
   existed, nothing in it changed for ``legacy_quiet_s`` seconds;
2. ``metadata.json`` has a non-empty ``stopped_at``;
3. ``events.csv`` has an ``episode_end`` row;
4. no ``*_raw.avi`` is left (the temporary capture before muxing);
5. ``media_storage.video_frames_match_csv`` is true;
6. ``cameras.stall_detection.stalled`` is empty.

The first four say "finished writing" (``pending`` until they hold); the last
two say "finished but unusable" (``rejected``, never retried).

Only ``stat`` calls and the two small files are read; videos are not opened.
Standard library only.
"""

import csv
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

DEMO = re.compile(r"^demo_(\d+)$")
INCOMPLETE = re.compile(r"^incomplete_(\d+)")
DISCARDED = re.compile(r"^discarded_(\d+)")
MARKER = ".complete"


@dataclass
class Completion:
    state: str  # complete | pending | rejected
    reason: str = ""
    completed_at: float | None = None
    marker: bool = False
    outcome: str | None = None  # eval.outcome as written (unlabeled, success...)

    @property
    def ok(self) -> bool:
        return self.state == "complete"


def kind_of(name: str) -> str | None:
    """demo | incomplete | discarded for a directory name, else None."""
    if DEMO.match(name):
        return "demo"
    if INCOMPLETE.match(name):
        return "incomplete"
    if DISCARDED.match(name):
        return "discarded"
    return None


def demo_number(name: str) -> int | None:
    m = DEMO.match(name)
    return int(m.group(1)) if m else None


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _has_episode_end(path: Path) -> bool:
    try:
        with path.open(newline="") as handle:
            return any(
                (row.get("event") or "").strip() == "episode_end"
                for row in csv.DictReader(handle)
            )
    except (OSError, csv.Error, UnicodeDecodeError):
        return False


def newest_mtime(demo: Path) -> float:
    """The latest modification time of the demo directory and its files."""
    newest = 0.0
    try:
        newest = demo.stat().st_mtime
        with os.scandir(demo) as entries:
            for entry in entries:
                try:
                    newest = max(newest, entry.stat().st_mtime)
                except OSError:
                    continue
    except OSError:
        pass
    return newest


def check(demo, *, now: float, legacy_quiet_s: float = 60.0, settle_s: float = 0.0):
    """The completion state of one ``demo_NNNN`` directory."""
    demo = Path(demo)
    marker = demo / MARKER
    has_marker = marker.exists()
    if has_marker:
        try:
            completed_at = marker.stat().st_mtime
        except OSError:
            return Completion("pending", "marker unreadable")
        if settle_s > 0 and now - completed_at < settle_s:
            return Completion("pending", "completion marker is very new", marker=True)
    else:
        completed_at = newest_mtime(demo)
        if not completed_at or (
            legacy_quiet_s > 0 and now - completed_at < legacy_quiet_s
        ):
            return Completion("pending", "no completion marker yet")
    meta = _read_json(demo / "metadata.json")
    if not isinstance(meta, dict):
        return Completion(
            "pending", "metadata.json missing or unreadable", marker=has_marker
        )
    if not meta.get("stopped_at"):
        return Completion(
            "pending", "metadata.json has no stopped_at", marker=has_marker
        )
    if not _has_episode_end(demo / "events.csv"):
        return Completion("pending", "events.csv has no episode_end", marker=has_marker)
    try:
        raw = [p.name for p in demo.glob("*_raw.avi")]
    except OSError:
        raw = []
    if raw:
        return Completion(
            "pending"
            if has_marker or now - completed_at < 10 * legacy_quiet_s
            else "rejected",
            f"unmuxed capture {raw[0]}",
            marker=has_marker,
        )
    storage = meta.get("media_storage") or {}
    outcome = (meta.get("eval") or {}).get("outcome")
    if storage.get("video_frames_match_csv") is not True:
        return Completion(
            "rejected",
            "video frame count does not match the CSV rows",
            completed_at,
            has_marker,
            outcome,
        )
    stalled = ((meta.get("cameras") or {}).get("stall_detection") or {}).get("stalled")
    if stalled:
        return Completion(
            "rejected",
            "camera stalled: " + ", ".join(map(str, stalled)),
            completed_at,
            has_marker,
            outcome,
        )
    return Completion("complete", "", completed_at, has_marker, outcome)


def abort_reason(directory) -> str | None:
    """``eval.abort_reason`` of an ``incomplete_NNNN`` directory, if written."""
    meta = _read_json(Path(directory) / "metadata.json")
    if isinstance(meta, dict):
        return (meta.get("eval") or {}).get("abort_reason")
    return None
