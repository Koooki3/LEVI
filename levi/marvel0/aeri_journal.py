"""What terminal D keeps on disk, read without writing (``aeri_state.derive``
decides from it; this module only reads).

* ``JournalReader``: D's state journal (``<rollout>/.aeri/runs/<name>/
  state_journal.jsonl``, one ``levi.aeri.run_event.v1`` line per event). Read
  incrementally from where the last look ended, the last half line left for
  the next look. **The hash chain is not verified** (D does that when it opens
  the journal; an account that can edit the file can also forge the page's
  requests). A line that does not parse is counted, never trusted.
* ``read_runner``: ``runner.json``, D's heartbeat (rewritten about every two
  seconds by its heartbeat thread). It proves the process is alive and answers,
  **not that the run is making progress**: a main thread stuck in a robot call
  does not stop the heartbeat thread.
* ``StepsReader``: ``.aeri/units/<name>-steps.jsonl``, the service steps
  1/7-6/7 D writes before its journal exists.
* ``queue_state``: the VLM requests of an external-mode run that no agent has
  answered yet.

**The page never keeps a judgement.** Of the journal's notes only ``CODES`` are
ever kept, and of those only what the page shows or decides on: the VLM's
verdicts on a subtask (``subtask_check``), its plans and retries stay in the
file (AERI.md: a person who sees them before labelling a round is no longer
blind; ``L2-AERI-SUBTASK-CHECK-BLIND``).
"""

import hashlib
import json
import os
import re
import stat
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

JOURNAL = "state_journal.jsonl"
RUNNER = "runner.json"
READ_CAP = 4 << 20  # bytes of a file read per look, at most
EVENTS_KEPT = 4000
SIDECAR_MAX = 1 << 20
SIDECAR_REL = re.compile(r"subtasks/[A-Za-z0-9_.-]+\.json")
FOLDED = re.compile(r"^\d+x, first: ")  # D folds repeats of a note into one line
STARTUP_HOME_TEXT = "explicit startup home confirmation required"

# The notes the page keeps (everything else in the journal is not read).
CODES = frozenset(
    {
        "subtask_service_step",
        "policy_warmup",
        "start_pose_checked",
        "subtask_inventory",
        "subtask_inventory_confirmed",
        "subtask_inventory_rejected",
        "subtask_round_released",
        "operator_verdict",
        "operator_verdict_dropped",
        "operator_interrupt",
        "operator_reset_confirmed",
        "startup_home_completed",
    }
)
DETAIL_CHARS = 300


@dataclass(frozen=True)
class Event:
    seq: int
    record: str
    from_state: str | None = None
    to_state: str | None = None
    reason: str | None = None
    code: str | None = None  # a kept note's code
    detail: str | None = None


@dataclass(frozen=True)
class JView:
    """What the journal says, folded."""

    present: bool = False
    bad_lines: int = 0  # whole lines that are not what D writes
    seq: int = -1  # the last sequence_no
    lines: int = 0
    header: bool = False
    commit_count: int = 0
    reverse_done: int = 0  # rounds whose reverse loop finished (subtask_reverse_done)
    last_commit: Event | None = None
    open_transaction: Event | None = None  # a prepared line not yet committed/aborted
    notes: dict = field(default_factory=dict)  # code -> its last Event
    inventory_ids: tuple | None = None  # the ids the VLM listed (latest list)
    inventory_seq: int = -1
    confirmed_seq: int = -1
    rejected_seq: int = -1
    released: tuple = ()  # (seq, round, round_id), oldest first
    verdicts: tuple = ()  # (seq, label)
    interrupt_seq: int = -1
    service_steps: int = 0
    startup_prompt_seq: int = -1  # the pose check that asks for o, p, h
    startup_requested_seq: int = -1  # a prepared startup_home_requested
    pose_refused_seq: int = -1  # the last pose check that refused

    @property
    def to_state(self) -> str | None:
        return self.last_commit.to_state if self.last_commit else None

    @property
    def reason(self) -> str | None:
        return self.last_commit.reason if self.last_commit else None

    @property
    def discarded(self) -> int:
        return sum(1 for _, label in self.verdicts if label == "discard")

    def released_after(self, seq: int) -> bool:
        return any(s > seq for s, _, _ in self.released)

    @property
    def round_released(self) -> int:
        """The number of the last round the person released (0: none)."""
        return self.released[-1][1] if self.released else 0

    @property
    def inventory_confirmed(self) -> bool:
        return (
            self.confirmed_seq > max(self.rejected_seq, -1) and self.confirmed_seq >= 0
        )

    @property
    def inventory_pending(self) -> bool:
        """A list was made and neither confirmed nor rejected since."""
        return self.inventory_seq >= 0 and self.inventory_seq > max(
            self.confirmed_seq, self.rejected_seq
        )


def open_regular(path) -> int | None:
    """A read-only fd of a regular file of ours, or None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
    except OSError:
        os.close(fd)
        return None
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        return None
    return fd


class JournalReader:
    """Reads the journal of one run, and only what was appended since."""

    def __init__(self, path, run_dir=None):
        self.path = Path(path)
        self.run_dir = Path(run_dir) if run_dir is not None else self.path.parent
        self.offset = 0
        self._rest = b""
        self._events: deque = deque(maxlen=EVENTS_KEPT)
        self._state = JView()
        self._notes: dict = {}
        self._released: list = []
        self._verdicts: list = []
        self._open: Event | None = None

    def poll(self) -> JView:
        fd = open_regular(self.path)
        if fd is None:
            if self._state.present:
                # The file vanished (a run folder removed): start from nothing.
                self.__init__(self.path, self.run_dir)
            return self._state
        try:
            size = os.fstat(fd).st_size
            if size < self.offset:  # shorter than before: not the file we followed
                self.__init__(self.path, self.run_dir)
                self._state = JView(present=True)
            budget = READ_CAP
            while self.offset < size and budget > 0:
                chunk = os.pread(fd, min(1 << 16, size - self.offset), self.offset)
                if not chunk:
                    break
                self.offset += len(chunk)
                budget -= len(chunk)
                self._feed(chunk)
        except OSError:
            pass
        finally:
            os.close(fd)
        if not self._state.present:
            self._state = JView(present=True)
        return self._state

    def _feed(self, chunk: bytes) -> None:
        data = self._rest + chunk
        *whole, self._rest = data.split(b"\n")
        for raw in whole:
            if raw.strip():
                self._line(raw)

    def _line(self, raw: bytes) -> None:
        state = self._state
        try:
            row = json.loads(raw)
        except ValueError:
            self._state = _with(state, present=True, bad_lines=state.bad_lines + 1)
            return
        if not isinstance(row, dict) or not isinstance(row.get("sequence_no"), int):
            self._state = _with(state, present=True, bad_lines=state.bad_lines + 1)
            return
        seq, record = row["sequence_no"], row.get("record")
        note = row.get("note") if isinstance(row.get("note"), dict) else None
        code = note.get("code") if note else None
        event = Event(
            seq=seq,
            record=str(record),
            from_state=_text(row.get("from_state")),
            to_state=_text(row.get("to_state")),
            reason=_text(row.get("reason")),
            code=code if code in CODES else None,
            detail=_text(note.get("detail"))[:DETAIL_CHARS]
            if note and code in CODES and isinstance(note.get("detail"), str)
            else None,
        )
        changes: dict = {"present": True, "seq": seq, "lines": state.lines + 1}
        if record == "run_header":
            changes["header"] = True
        elif record == "prepared":
            self._open = event
            if event.reason == "startup_home_requested":
                changes["startup_requested_seq"] = seq
        elif record == "committed":
            self._open = None
            changes["commit_count"] = state.commit_count + 1
            changes["last_commit"] = event
            if event.reason == "subtask_reverse_done":
                changes["reverse_done"] = state.reverse_done + 1
        elif record == "aborted":
            self._open = None
        elif record == "note" and event.code is not None:
            changes.update(self._note(event, state))
        changes["open_transaction"] = self._open
        self._events.append(event)
        self._state = _with(state, **changes)

    def _note(self, event: Event, state: JView) -> dict:
        code, seq = event.code, event.seq
        self._notes[code] = event
        changes: dict = {"notes": dict(self._notes)}
        detail = event.detail or ""
        if code == "subtask_service_step":
            changes["service_steps"] = state.service_steps + 1
        elif code == "start_pose_checked":
            text = FOLDED.sub("", detail)
            if text.startswith(STARTUP_HOME_TEXT) and state.commit_count == 0:
                changes["startup_prompt_seq"] = seq
            elif text.startswith("refused"):
                changes["pose_refused_seq"] = seq
        elif code == "subtask_inventory":
            changes["inventory_seq"] = seq
            changes["inventory_ids"] = self._inventory_ids(detail)
        elif code == "subtask_inventory_confirmed":
            changes["confirmed_seq"] = seq
        elif code == "subtask_inventory_rejected":
            changes["rejected_seq"] = seq
        elif code == "subtask_round_released":
            fields = _json(detail)
            number = fields.get("round")
            number = (
                number
                if isinstance(number, int) and not isinstance(number, bool)
                else 0
            )
            self._released.append((seq, number, str(fields.get("round_id") or "")))
            changes["released"] = tuple(self._released[-200:])
        elif code in ("operator_verdict", "operator_verdict_dropped"):
            if code == "operator_verdict":
                label = _json(detail).get("label")
                self._verdicts.append((seq, label if isinstance(label, str) else "?"))
                changes["verdicts"] = tuple(self._verdicts[-200:])
        elif code == "operator_interrupt":
            changes["interrupt_seq"] = seq
        return changes

    def _inventory_ids(self, detail: str) -> tuple | None:
        """The ids of the list the VLM made: inline (``items``) or in the side
        file D moves a long detail to (``subtasks/<seq>-subtask_inventory.json``,
        its ``parsed`` rows). ``None`` when they cannot be read: the page then
        does not trust the object table (``aeri_state``)."""
        fields = _json(detail)
        items = fields.get("items")
        if isinstance(items, list) and all(isinstance(i, str) for i in items):
            return tuple(items)
        name = fields.get("file")
        if not isinstance(name, str) or not SIDECAR_REL.fullmatch(name):
            return None
        fd = open_regular(self.run_dir / name)
        if fd is None:
            return None
        try:
            raw = os.read(fd, SIDECAR_MAX + 1)
        except OSError:
            return None
        finally:
            os.close(fd)
        if len(raw) > SIDECAR_MAX:
            return None
        want = fields.get("file_sha256")
        if isinstance(want, str) and hashlib.sha256(raw).hexdigest() != want:
            return None  # not the file D wrote the note for
        try:
            body = json.loads(raw)
        except ValueError:
            return None
        rows = body.get("parsed") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            return None
        ids = [r.get("id") for r in rows if isinstance(r, dict)]
        return tuple(ids) if ids and all(isinstance(i, str) for i in ids) else None


def _with(view: JView, **changes) -> JView:
    return JView(**{**view.__dict__, **changes})


def _text(value) -> str | None:
    return value if isinstance(value, str) else None


def _json(text: str) -> dict:
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


# ---- the heartbeat -----------------------------------------------------------------------


@dataclass(frozen=True)
class RView:
    present: bool = False
    state: str | None = None  # running | exited
    pid: int | None = None
    identity: dict | None = None
    updated_wall_ns: int | None = None
    final_state: str | None = None


def read_runner(path) -> RView:
    fd = open_regular(path)
    if fd is None:
        return RView()
    try:
        raw = os.read(fd, 1 << 16)
    except OSError:
        return RView()
    finally:
        os.close(fd)
    try:
        body = json.loads(raw)
    except ValueError:
        return RView()
    if not isinstance(body, dict):
        return RView()
    pid = body.get("pid")
    stamp = body.get("updated_wall_ns")
    ident = body.get("identity")
    return RView(
        present=True,
        state=body.get("state") if isinstance(body.get("state"), str) else None,
        pid=pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
        identity=ident if isinstance(ident, dict) else None,
        updated_wall_ns=stamp
        if isinstance(stamp, int) and not isinstance(stamp, bool)
        else None,
        final_state=body.get("final_state")
        if isinstance(body.get("final_state"), str)
        else None,
    )


def runner_age_s(view: RView, now_wall: float) -> float | None:
    if view.updated_wall_ns is None:
        return None
    return max(0.0, now_wall - view.updated_wall_ns / 1e9)


# ---- the service steps ------------------------------------------------------------------


@dataclass(frozen=True)
class SView:
    steps: int = 0
    last_key: str | None = None
    last_step: int = 0
    failed: bool = False


class StepsReader:
    """``<name>-steps.jsonl`` (step, key, ok, wall_ns per line), incrementally."""

    def __init__(self, path):
        self.path = Path(path)
        self.offset = 0
        self._rest = b""
        self.view = SView()

    def poll(self) -> SView:
        fd = open_regular(self.path)
        if fd is None:
            return self.view
        try:
            size = os.fstat(fd).st_size
            if size < self.offset:
                self.offset, self._rest, self.view = 0, b"", SView()
            budget = READ_CAP
            while self.offset < size and budget > 0:
                chunk = os.pread(fd, min(1 << 16, size - self.offset), self.offset)
                if not chunk:
                    break
                self.offset += len(chunk)
                budget -= len(chunk)
                data = self._rest + chunk
                *whole, self._rest = data.split(b"\n")
                for raw in whole:
                    self._line(raw)
        except OSError:
            pass
        finally:
            os.close(fd)
        return self.view

    def _line(self, raw: bytes) -> None:
        try:
            row = json.loads(raw)
        except ValueError:
            return
        if not isinstance(row, dict):
            return
        step = row.get("step") if isinstance(row.get("step"), int) else 0
        self.view = SView(
            steps=self.view.steps + 1,
            last_key=row.get("key") if isinstance(row.get("key"), str) else None,
            last_step=step,
            failed=self.view.failed or row.get("ok") is False,
        )


# ---- the external VLM's queue ------------------------------------------------------------


def queue_state(queue_dir, now_wall: float) -> tuple:
    """``(pending, oldest_age_s)``: the request folders that have a prompt and
    no answer (what ``wait_request.sh`` looks for)."""
    pending, oldest = 0, None
    try:
        names = os.listdir(queue_dir)
    except OSError:
        return 0, None
    for name in names:
        folder = Path(queue_dir) / name
        try:
            if (folder / "answer.json").exists():
                continue
            stamp = os.lstat(folder / "prompt.txt").st_mtime
        except OSError:
            continue
        pending += 1
        age = max(0.0, now_wall - stamp)
        oldest = age if oldest is None else max(oldest, age)
    return pending, oldest
