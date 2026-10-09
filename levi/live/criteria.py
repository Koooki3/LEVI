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
import math
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
# How a dual-label episode ended (``eval.ended_by``): ``operator_key`` (the
# operator's key press during the run ended it early) or ``budget`` (it ran the
# whole step budget, as an unattended run does). Anything else is unknown.
ENDED_BY = ("operator_key", "budget")


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

    A known ``eval.ended_by`` is kept in the label as ``ended_by``: an episode
    the operator's key ended early is shorter than an unattended one, so the
    agreement is read apart by it. The key is absent when the metadata has none.

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
    ended = ev.get("ended_by")
    extra = {"ended_by": ended} if ended in ENDED_BY else {}
    operator = ev.get("operator_outcome")
    if isinstance(operator, str) and operator in ("success", "failure", "discarded"):
        return {"outcome": operator, "by": "operator", "source": source, **extra}
    outcome = ev.get("outcome")
    if not isinstance(outcome, str):
        return None
    if outcome in ("success", "failure") and by in OPERATOR_KEYS:
        return {"outcome": outcome, "by": by, "source": source, **extra}
    if outcome in ("unlabeled", "discarded"):
        return {"outcome": outcome, "by": by, "source": source, **extra}
    return None


# What the evaluation client may write as ``eval.agent_label.status``: ``ok``
# (the online judgement answered) or why there is no agent label.
AGENT_STATUSES = ("ok", "unavailable", "error", "timeout", "skipped")
READINGS = ("supported", "contradicted", "unknown")
# What a final-state judgement with a start check (``generic-final.v2``) may
# say instead of a reading: the start frames, not the final ones, decided
# (``anchored.ALREADY_AT_START``, ``anchored.START_UNCLEAR``; a test holds the
# two lists together). Both are failures that are undecided.
START_READINGS = ("already_satisfied_at_start", "start_unclear")
START_CHECKS = ("skipped", "passed", "voided", "unclear")
REWORDINGS = ("task_text", "task_folder")


def _epoch(value):
    """Epoch seconds from a number or an ISO time string, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
    elif isinstance(value, str) and value.strip():
        from .sessions import parse_time

        seconds = parse_time(value)
    else:
        return None
    # "nan", Infinity and 1e400 would later fail the state file's strict JSON
    # write and stall the mirroring of the whole task.
    return seconds if isinstance(seconds, float) and math.isfinite(seconds) else None


def _usage(label) -> dict:
    """What the online judgement cost, as the client relayed it."""

    def whole(value):
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    elapsed = label.get("elapsed_s")
    frames = label.get("frames")
    model = label.get("model")
    return {
        "tokens": whole(label.get("tokens")),
        "prompt_tokens": whole(label.get("prompt_tokens")),
        "elapsed_s": float(elapsed)
        if isinstance(elapsed, (int, float))
        and not isinstance(elapsed, bool)
        and math.isfinite(elapsed)
        else None,
        "model": str(model)[:100] if isinstance(model, str) else None,
        "images": len(frames) if isinstance(frames, list) else None,
    }


def agent_label(meta) -> dict | None:
    """The online judgement the evaluation client relayed into the rollout's
    ``metadata.json`` (``eval.agent_label``, interface C5), or None when there
    is none.

    Returns ``{"source": "online", "status", "reason", "timing",
    "request_id", "verdict"}``. ``verdict`` is set only for ``status`` ``ok``
    with a well-formed result, in the shape the live worker gives its own
    verdicts (``outcome``, ``events``, ``valid_events``, ``undecided``,
    ``rule`` ``final_state``, ``basis.final_reading``, ``spec``,
    ``spec_version``, ``review: auto``, ``evaluated: false``) plus
    ``source: online``; any other status, or an ``ok`` whose fields do not
    hold together, gives no verdict (the episode has no automatic label,
    ``no_agent``). The operator label is never read here and never mixed in.
    """
    if not isinstance(meta, dict):
        return None
    ev = meta.get("eval")
    label = ev.get("agent_label") if isinstance(ev, dict) else None
    if not isinstance(label, dict) or label.get("source") != "online":
        return None
    status = label.get("status")
    reason = label.get("reason")
    reason = str(reason)[:300] if reason is not None else None
    request_id = label.get("request_id")
    out = {
        "source": "online",
        "status": status if status in AGENT_STATUSES else "error",
        "reason": reason
        if status in AGENT_STATUSES
        else f"unknown status {str(status)[:40]!r}",
        "timing": label.get("timing")
        if label.get("timing") in ("during_run", "after_budget")
        else None,
        "request_id": str(request_id)[:64] if request_id else None,
        "usage": _usage(label),
        "verdict": None,
    }
    if out["status"] != "ok":
        return out
    outcome = label.get("outcome")
    undecided = label.get("undecided")
    spec = label.get("spec")
    reading = label.get("reading")
    if (
        outcome not in ("success", "failure")
        or not isinstance(undecided, bool)
        or not isinstance(spec, dict)
        or not isinstance(spec.get("id"), str)
        or (outcome == "success" and undecided)
    ):
        out.update(status="error", reason="malformed agent_label (status ok)")
        return out
    version = spec.get("version")
    reading = reading if reading in READINGS else None
    # The rule's own reading (revision 2 of the interface) is the final
    # frames' reading, or the start check's decision over it.
    final = label.get("final_reading")
    if final in START_READINGS:
        if outcome != "failure" or not undecided:
            out.update(status="error", reason="malformed agent_label (status ok)")
            return out
        reading = final
    elif final in READINGS:
        reading = final
    valid = 1 if outcome == "success" else 0
    out["verdict"] = {
        "outcome": outcome,
        "events": 1,
        "valid_events": valid,
        "undecided": undecided,
        "basis": {
            "valid_events": valid,
            "min_valid": 1,
            "rule": "final_state",
            "final_reading": reading,
            **(
                {"start_check": label["start_check"]}
                if label.get("start_check") in START_CHECKS
                else {}
            ),
        },
        "rule": "final_state",
        "min_valid": 1,
        "run_id": None,
        "spec": spec["id"][:64],
        "spec_version": version
        if isinstance(version, int) and not isinstance(version, bool)
        else None,
        "at": _epoch(label.get("received_at")),
        "review": "auto",
        "evaluated": False,
        "source": "online",
        **(
            {"task_rewritten": label["task_rewritten"]}
            if label.get("task_rewritten") in REWORDINGS
            else {}
        ),
    }
    return out
