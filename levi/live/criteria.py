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
    demo = Path(demo)
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


# Who decided ``eval.outcome`` by a key press in the evaluation client: the
# operator (``key``) or the operator after a timeout (``timeout-adjudicated``).
OPERATOR_KEYS = ("key", "timeout-adjudicated")


def operator_label(meta) -> dict | None:
    """The operator's own label of a rollout, read from its ``metadata.json``
    (``eval.*``), or None when the operator gave none.

    The operator label (ground truth) is kept apart from everything LEVI
    decides: it is never an outcome label of LEVI's (``annotations/outcomes``),
    never part of the automatic verdict and never shown to the model.

    1. ``eval.operator_outcome`` (a dual-label run: written once, at the
       operator's key press, and never changed afterwards, even when the
       client later discards an invalid episode);
    2. else ``eval.outcome`` success/failure decided by a key press
       (``eval.verdict_by`` ``key`` or ``timeout-adjudicated``);
    3. else ``eval.outcome`` unlabeled or discarded, as written (no label, but
       a record that the operator gave none or threw the episode away).

    Anything else (``aborted``, no ``eval`` block, values of another shape) is
    None. ``success_flag_final`` is never read: it is a placeholder in a
    rollout nobody labelled."""
    if not isinstance(meta, dict):
        return None
    ev = meta.get("eval")
    if not isinstance(ev, dict):
        return None
    by = ev.get("verdict_by") if isinstance(ev.get("verdict_by"), str) else None
    source = "capture-metadata"
    operator = ev.get("operator_outcome")
    if isinstance(operator, str) and operator in ("success", "failure", "discarded"):
        return {"outcome": operator, "by": "operator", "source": source}
    outcome = ev.get("outcome")
    if not isinstance(outcome, str):
        return None
    if outcome in ("success", "failure") and by in OPERATOR_KEYS:
        return {"outcome": outcome, "by": by, "source": source}
    if outcome in ("unlabeled", "discarded"):
        return {"outcome": outcome, "by": by, "source": source}
    return None
