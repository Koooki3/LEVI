"""The subtask-loop notes of a run, for the read-only run window.

A ``subtask_loop`` run writes its progress as journal notes (the codes in
``CODES``) whose ``detail`` is a JSON object. A detail longer than a note
allows (300 characters, ``aeri.Detail``) keeps its short fields and names a
sidecar under the run folder (``subtasks/<seq>-<code>-detail.json``, with
``file`` and ``file_sha256``) that holds the whole detail; each loop's plan
is a sidecar named by its ``subtask_plan`` note (``plan_file``, ``sha256``).

This module only reads. It returns the notes with their detail parsed (the
whole detail from its sidecar when the sidecar is below the run folder and
matches its digest), each plan file checked against its digest, and every
frame reference as ``{ref, sha256, stored}`` (``stored``: the frame is in
the run's evidence folder, so the page can show it). It decides nothing
about the run: a check's ``value`` is the local model's judgement, never a
label. A detail that does not parse is returned as ``detail: null`` with
``unparsed: true``, never guessed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath

from . import request_evidence_view
from .subtask_loop import NOTES

# The mode's journal notes (``subtask_loop.NOTES``) and the service steps
# terminal D journals at start (``fr3_run``).
CODES = frozenset((*NOTES, "subtask_service_step"))
MAX_NOTES = 4000
MAX_PLANS = 64
MAX_SIDECARS = 2000
MAX_SUBTASKS = 200
MAX_FRAMES = 32
DETAIL_SUFFIX = "-detail.json"
_SHA256_CHARS = frozenset("0123456789abcdef")

Read = Callable[[tuple[str, ...]], bytes]
Stored = Callable[[str], bool]


def _ms(ns) -> int | None:
    return ns // 1_000_000 if isinstance(ns, int) else None


def _is_digest(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _SHA256_CHARS


def _json_dict(text) -> dict | None:
    if not isinstance(text, (str, bytes)) or not text:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def plan_parts(run_dir: Path, name) -> tuple[str, ...] | None:
    """The path components of a sidecar below ``run_dir``, or ``None`` for a
    name that would lead outside it. An absolute name is accepted only when
    it lies inside ``run_dir``."""
    if not isinstance(name, str) or not name or len(name) > 512 or "\x00" in name:
        return None
    path = PurePosixPath(name)
    if path.is_absolute():
        try:
            path = path.relative_to(PurePosixPath(Path(run_dir).as_posix()))
        except ValueError:
            return None
    parts = path.parts
    if not parts or any(p in ("", ".", "..") for p in parts):
        return None
    return tuple(parts)


def _verified(
    read: Read, run_dir: Path, name, digest
) -> tuple[bytes | None, str | None]:
    """``(data, None)`` for a file below the run folder whose SHA-256 is
    ``digest``; otherwise ``(None, problem)``."""
    parts = plan_parts(run_dir, name)
    if parts is None:
        return None, "bad_path"
    try:
        data = read(parts)
    except OSError:
        return None, "missing"
    if hashlib.sha256(data).hexdigest() != digest:
        return None, "sha256_mismatch"
    return data, None


def frames_of(value, stored: Stored) -> list[dict]:
    """Frame references (a digest, or ``{ref, sha256}``) as ``{ref, sha256,
    stored}``; anything else is dropped."""
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:MAX_FRAMES]:
        if isinstance(item, str):
            ref, sha = None, item
        elif isinstance(item, dict):
            ref, sha = item.get("ref"), item.get("sha256")
        else:
            continue
        if not isinstance(sha, str) or not sha:
            continue
        out.append(
            {
                "ref": ref if isinstance(ref, str) else None,
                "sha256": sha,
                "stored": _is_digest(sha) and stored(sha),
            }
        )
    return out


def facts_text(value) -> str | None:
    """A ``done_when``: a text as it is, or facts ``[[fact, subject,
    target], ...]`` as ``subject fact target; ...``."""
    if isinstance(value, str):
        return value or None
    if not isinstance(value, list):
        return None
    lines = []
    for fact in value:
        if (
            isinstance(fact, (list, tuple))
            and len(fact) == 3
            and all(isinstance(f, str) for f in fact)
        ):
            lines.append(f"{fact[1]} {fact[0]} {fact[2]}")
        else:
            return None
    return "; ".join(lines) or None


def _subtasks(value) -> list[dict] | None:
    items = value.get("subtasks") if isinstance(value, dict) else None
    if not isinstance(items, list) or len(items) > MAX_SUBTASKS:
        return None
    out = []
    for item in items:
        if not isinstance(item, dict) or type(item.get("index")) is not int:
            return None
        row = {"index": item["index"]}
        for key in ("pick", "place", "instruction"):
            text = item.get(key)
            row[key] = text if isinstance(text, str) else None
        row["done_when"] = facts_text(item.get("done_when"))
        out.append(row)
    return out


def plan_of(read: Read, run_dir: Path, detail) -> dict:
    """The plan a ``subtask_plan`` note names: ``ok`` only when the file is
    below the run folder, its SHA-256 is the note's and it holds
    ``{"subtasks": [{index, pick, place, done_when, instruction}]}``;
    otherwise ``problem`` says which check failed."""
    detail = detail if isinstance(detail, dict) else {}
    data, problem = _verified(
        read, run_dir, detail.get("plan_file"), detail.get("sha256")
    )
    if data is None:
        return {"ok": False, "problem": problem, "subtasks": []}
    try:
        subtasks = _subtasks(json.loads(data))
    except ValueError:
        subtasks = None
    if subtasks is None:
        return {"ok": False, "problem": "bad_shape", "subtasks": []}
    return {"ok": True, "problem": None, "subtasks": subtasks}


def notes_of(
    events: Iterable, run_dir: Path, read: Read, stored: Stored
) -> tuple[list[dict], bool]:
    """``(notes, truncated)``: every subtask/VLM note in journal order (the
    newest ``MAX_NOTES`` when there are more), each detail whole."""
    picked = []
    for event in events:
        note = getattr(event, "note", None)
        if getattr(event, "record", None) != "note" or note is None:
            continue
        if note.code in CODES:
            picked.append((event, note))
    truncated = len(picked) > MAX_NOTES
    rows, sidecars = [], 0
    for event, note in picked[-MAX_NOTES:]:
        detail = _json_dict(note.detail)
        row = {
            "seq": event.sequence_no,
            "at": _ms(event.emitted_wall_ns),
            "code": note.code,
            "detail": detail,
        }
        if detail is None:
            if note.detail:
                row["unparsed"] = True
            rows.append(row)
            continue
        name = detail.get("file")
        if (
            isinstance(name, str)
            and name.endswith(DETAIL_SUFFIX)
            and sidecars < MAX_SIDECARS
        ):
            sidecars += 1
            data, problem = _verified(read, run_dir, name, detail.get("file_sha256"))
            whole = _json_dict(data) if data is not None else None
            if whole is not None:
                detail = {**whole, **{k: v for k, v in detail.items() if k in whole}}
            else:
                row["sidecar_problem"] = problem or "bad_shape"
        if "frames" in detail:
            detail = {**detail, "frames": frames_of(detail["frames"], stored)}
        row["detail"] = detail
        rows.append(row)
    return rows, truncated


def view(
    events: Iterable,
    run_dir: Path,
    read: Read,
    *,
    reset_mode,
    stored: Stored = lambda sha: False,
) -> dict:
    """What the run window shows of a subtask loop: the notes and the plan
    files they name, keyed by the note's ``sha256``."""
    events = list(events)
    notes, truncated = notes_of(events, run_dir, read, stored)
    evidence, evidence_truncated = request_evidence_view.project(
        events, notes, run_dir, read, _verified
    )
    plans: dict[str, dict] = {}
    for row in notes:
        if row["code"] != "subtask_plan" or len(plans) >= MAX_PLANS:
            continue
        digest = (row["detail"] or {}).get("sha256")
        if not _is_digest(digest) or digest in plans:
            continue
        plans[digest] = plan_of(read, run_dir, row["detail"])
    return {
        "reset_mode": reset_mode,
        "notes": request_evidence_view.without_references(notes),
        "request_evidence": evidence,
        "request_evidence_truncated": evidence_truncated,
        "plans": plans,
        "truncated": truncated,
    }
