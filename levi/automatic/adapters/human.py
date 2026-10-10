"""A person as the scene provider (T-CL-03; design X2 §1.2,
``reset.scene_check: operator_attested``).

In the human-assisted ("policy evaluation only") mode a person puts the
scene back. Whether it is ready is still decided the same way as with a
machine provider: a ``levi.aeri.scene.v1`` assessment, read by
``scene_assessment.arbitrate`` against the Initial State Contract, and only
``ready`` starts a forward episode. ``HumanSceneProvider`` makes the person
that provider:

1. ``submit`` captures the current camera frames first (``capture``), then
   publishes one question through a ``transport``: the request id (usable
   once), the contract, each predicate with readable text, and the frames;
2. the person answers each required predicate ``true``, ``false`` or
   ``null`` (cannot tell) and nothing else: an answer is
   ``{"request_id", "predicates": {name: true|false|null}}``. There is no
   way to say "the scene is ready": the decision follows from the answers
   (a false required predicate: ``reset_required``; one not read:
   ``unknown``, never a pass; all true: ``ready``, which the arbitration
   still checks against the contract, the evidence rule included);
3. ``collect`` turns the answer into the assessment, its evidence the
   frames just captured (``provider: human``). An answer to a request that
   is not open (never asked, answered already, withdrawn after a timeout or
   a stop) is dropped as ``E_UNSOLICITED`` and noted; a malformed answer is
   refused and noted, and the question stays open.

A check nobody answers within ``reset.human_scene_timeout_s`` is withdrawn
by the orchestrator: the scene counts as unavailable and the run waits for
a person again (``WAIT_HUMAN``).

Transports here are fakes and the file protocol only (``QueueTransport``,
``ScriptedTransport``, ``FileTransport``); the page that shows the question
is a later task. Nothing here moves anything or opens a connection.
"""

import hashlib
import json
import os
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from levi.domain import aeri

from ..journal import fsync_dir
from .events import Ticket

PROVIDER = "human"
# The longest an answer may stay valid (aeri.MAX_RESULT_VALIDITY_MS caps it).
VALIDITY_MS = 10_000
# How often a waiting collect looks for an answer (real clocks).
POLL_NS = 100_000_000
MAX_ANSWER_BYTES = 64 * 1024
UNSOLICITED = "E_UNSOLICITED"


def readable(name: str) -> str:
    """``object_at_source`` -> ``object at source`` (the contract format has
    no text field yet, HA-23)."""
    return name.replace("_", " ")


def _frame_ref(view: str, data: bytes) -> tuple[str, str]:
    digest = hashlib.sha256(data).hexdigest()
    return f"{view}:{digest[:16]}", digest


# --- transports ---------------------------------------------------------------------------------


class Transport(Protocol):
    def reachable(self) -> bool | str: ...
    def ask(self, question: dict) -> None: ...
    def withdraw(self, request_id: str, reason: str) -> None: ...
    def take_answers(self) -> list:
        """Answers that arrived since the last call (each a decoded
        mapping, or ``{"_refused": detail}`` for one that could not be
        read)."""
        ...


class QueueTransport:
    """In memory, thread safe: ``answer`` may be called from any thread."""

    def __init__(self):
        self._answers: queue.SimpleQueue = queue.SimpleQueue()
        self._lock = threading.Lock()
        self.questions: dict[str, dict] = {}
        self.withdrawn: list = []

    def reachable(self):
        return True

    def ask(self, question: dict) -> None:
        with self._lock:
            self.questions[question["request_id"]] = question

    def withdraw(self, request_id: str, reason: str) -> None:
        with self._lock:
            if self.questions.pop(request_id, None) is not None:
                self.withdrawn.append((request_id, reason))

    def answer(self, request_id, predicates, **extra) -> None:
        self._answers.put({"request_id": request_id, "predicates": predicates, **extra})

    def take_answers(self) -> list:
        out = []
        while True:
            try:
                out.append(self._answers.get_nowait())
            except queue.Empty:
                return out


class ScriptedTransport(QueueTransport):
    """A scripted person (dry runs and tests): every question is answered
    when it is asked, from ``script`` (a list of ``ready``,
    ``reset_required``, ``unknown``, or ``{name: value}`` mappings; then
    ``default``). ``ready``: every predicate true; ``reset_required``: the
    first required one false; ``unknown``: the first required one null."""

    def __init__(self, script=(), default="ready"):
        super().__init__()
        self.script = list(script)
        self.default = default
        self.asked = 0

    def ask(self, question: dict) -> None:
        super().ask(question)
        spec = (
            self.script[self.asked] if self.asked < len(self.script) else self.default
        )
        self.asked += 1
        if spec is None:
            return  # nobody answers this one
        names = [p["name"] for p in question["predicates"]]
        required = [p["name"] for p in question["predicates"] if p["required"]]
        if isinstance(spec, dict):
            values = dict(spec)
        else:
            values = {name: True for name in names}
            first = {"ready": True, "reset_required": False, "unknown": None}[spec]
            values[required[0]] = first
        self.answer(question["request_id"], values)


class FileTransport:
    """Questions and answers as files under ``root`` (owner-only):

    - ``questions/<request_id>.json``: written whole (temporary file,
      fsync, rename, fsync of the folder); removed when withdrawn or
      answered;
    - ``answers/*.json``: one answer per file, written whole the same way
      by the answering side (``write_answer``); taken answers move to
      ``answers/taken/``, unreadable ones to ``answers/refused/``.

    A file that is not whole (a writer killed half-way leaves only its
    hidden temporary file) is never read."""

    def __init__(self, root):
        self.root = Path(root)

    @property
    def questions(self) -> Path:
        return self.root / "questions"

    @property
    def answers(self) -> Path:
        return self.root / "answers"

    def _folders(self) -> None:
        for folder in (self.root, self.questions, self.answers):
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)

    def reachable(self):
        try:
            self._folders()
        except OSError as exc:
            return f"{self.root}: {exc.strerror or exc}"
        if not os.access(self.root, os.W_OK | os.X_OK):
            return f"{self.root}: not writable"
        return True

    def clear(self) -> None:
        """Withdraw every question left by an earlier process (none of its
        requests is open any more)."""
        if self.questions.is_dir():
            for path in self.questions.glob("*.json"):
                path.unlink(missing_ok=True)

    def ask(self, question: dict) -> None:
        self._folders()
        write_whole(
            self.questions / f"{question['request_id']}.json",
            json.dumps(question, sort_keys=True).encode(),
        )

    def withdraw(self, request_id: str, reason: str) -> None:
        (self.questions / f"{request_id}.json").unlink(missing_ok=True)

    def _move(self, path: Path, folder: str) -> None:
        target = self.answers / folder
        target.mkdir(mode=0o700, exist_ok=True)
        os.replace(path, target / path.name)
        fsync_dir(target)

    def take_answers(self) -> list:
        if not self.answers.is_dir():
            return []
        out = []
        for path in sorted(self.answers.glob("*.json")):
            if path.name.startswith("."):
                continue
            try:
                raw = path.read_bytes()
                if len(raw) > MAX_ANSWER_BYTES:
                    raise ValueError(f"over {MAX_ANSWER_BYTES} bytes")
                found = json.loads(raw, object_pairs_hook=_no_duplicates)
            except (OSError, ValueError) as exc:
                out.append({"_refused": f"{path.name}: {exc}"[:300]})
                self._move(path, "refused")
                continue
            self._move(path, "taken")
            out.append(found)
        return out


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"the key {key!r} appears twice")
        out[key] = value
    return out


def write_whole(path: Path, data: bytes) -> None:
    """A file written whole or not at all (temporary file, fsync, rename,
    fsync of the folder)."""
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_dir(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def write_answer(root, request_id: str, predicates: dict) -> Path:
    """The answering side of ``FileTransport`` (what the page or a test
    writes): one answer, written whole."""
    folder = Path(root) / "answers"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = folder / f"{request_id}.{time.time_ns()}.json"
    write_whole(
        path,
        json.dumps({"request_id": request_id, "predicates": predicates}).encode(),
    )
    return path


# --- the provider ---------------------------------------------------------------------------------


class AnswerRefused(ValueError):
    pass


class HumanSceneProvider:
    """``submit`` / ``collect`` / ``cancel`` as every scene provider
    (``adapters.events.SceneAssessor``), with a person answering.

    ``capture()`` returns the current frames, ``{view: bytes}``; it is
    called in ``submit``, so the evidence is never older than the request
    (and, after a resume, never older than the resume). ``clock`` is the
    orchestrator's (``now()`` and ``domain``); ``wait(ns)`` sleeps while a
    collect waits (default: real sleep; a fake clock's ``advance`` in
    tests)."""

    provider = PROVIDER

    def __init__(
        self,
        contract,
        capture: Callable[[], dict],
        transport: Transport,
        clock,
        run_id: str,
        *,
        wait: Callable[[int], object] | None = None,
        validity_ms: int = VALIDITY_MS,
        max_frame_bytes: int = 8 * 1024 * 1024,
    ):
        if contract is None:
            raise ValueError("a person answers the predicates of a contract")
        if not 0 < validity_ms <= aeri.MAX_RESULT_VALIDITY_MS:
            raise ValueError("validity_ms is within the contract's limit")
        self.contract = contract
        self.capture = capture
        self.transport = transport
        self.clock = clock
        self.run_id = run_id
        self.wait = wait or (lambda ns: time.sleep(ns / 1e9))
        self.validity_ms = validity_ms
        self.max_frame_bytes = max_frame_bytes
        self._lock = threading.Lock()
        self._open: dict[str, Ticket] = {}
        self._answered: dict[str, tuple] = {}  # request id -> (answer, received)
        self._frames: dict[str, dict] = {}  # request id -> {ref: bytes}
        self._notes: list = []
        self._asked = 0
        clear = getattr(transport, "clear", None)
        if clear is not None:
            clear()

    # ------------------------------------------------------------ provider calls

    def reachable(self):
        return self.transport.reachable()

    def submit(self, request: dict) -> Ticket | bytes:
        """The question, asked once the frames are captured. A capture or a
        transport that fails gives ``unavailable`` (``provider_error``):
        never a pass, and never a fault of the run."""
        try:
            return self._submit(request)
        except Exception as exc:  # noqa: BLE001 - reported as unavailable
            with self._lock:
                self._open.pop(request["request_id"], None)
                self._frames.pop(request["request_id"], None)
            self._note("scene_check_failed", f"{type(exc).__name__}: {exc}")
            return self._unavailable(request, "provider_error", str(exc))

    def _unavailable(self, request: dict, code: str, detail: str) -> bytes:
        message = {
            "schema": aeri.SCHEMAS["scene"],
            "minor": aeri.MINOR,
            "run_id": self.run_id,
            "emitted_wall_ns": time.time_ns(),
            "kind": "unavailable",
            "request_id": request["request_id"],
            "code": code,
            "retryable": False,
            "retry_after_ms": None,
            "detail": detail[:300],
            "produced_ns": self.clock.now(),
            "clock_domain": self.clock.domain,
        }
        return aeri.dump(aeri.validate(message, "scene"))

    def _submit(self, request: dict) -> Ticket:
        frames = self.capture()
        if not isinstance(frames, dict) or not frames:
            raise ValueError("capture() returned no frames")
        observed = self.clock.now()
        refs, kept = [], {}
        for view, data in sorted(frames.items()):
            data = data if isinstance(data, bytes) else str(data).encode()
            ref, digest = _frame_ref(str(view), data)
            refs.append(
                {
                    "kind": "frame",
                    "ref": ref,
                    "sha256": digest,
                    "step": None,
                    "bytes": len(data),
                }
            )
            if len(data) <= self.max_frame_bytes:
                kept[ref] = data
        names = [(n, True) for n in self.contract.required] + [
            (n, False) for n in self.contract.optional
        ]
        question = {
            "request_id": request["request_id"],
            "run_id": self.run_id,
            "episode_id": request["episode_id"],
            "target": request["target"],
            "contract": {
                "id": self.contract.contract_id,
                "version": self.contract.contract_version,
                "status": self.contract.status,
            },
            "predicates": [
                {"name": n, "text": readable(n), "required": r} for n, r in names
            ],
            "frames": refs,
            "asked_ns": observed,
            "answers": ["true", "false", "null (cannot tell)"],
        }
        self._asked += 1
        ticket = Ticket(
            request_id=request["request_id"],
            number=self._asked,
            submitted_ns=observed,
            ready_ns=observed,
            request=dict(request),
            response={"observed_ns": observed, "refs": refs},
        )
        with self._lock:
            self._open[ticket.request_id] = ticket
            self._frames[ticket.request_id] = kept
        self.transport.ask(question)
        return ticket

    def cancel(self, ticket: Ticket, reason: str) -> None:
        with self._lock:
            self._open.pop(ticket.request_id, None)
            self._answered.pop(ticket.request_id, None)
        self.transport.withdraw(ticket.request_id, reason)

    def collect(self, ticket: Ticket, *, timeout_ns: int) -> bytes | None:
        end = self.clock.now() + max(0, timeout_ns)
        while True:
            self._take()
            with self._lock:
                found = self._answered.pop(ticket.request_id, None)
                if found is not None:
                    # One answer per request: from now on it is closed.
                    self._open.pop(ticket.request_id, None)
            if found is not None:
                self.transport.withdraw(ticket.request_id, "answered")
                return self._assessment(ticket, *found)
            now = self.clock.now()
            with self._lock:
                closed = ticket.request_id not in self._open
            if now >= end or closed:
                return None
            self.wait(min(POLL_NS, end - now))

    # ------------------------------------------------------------ answers

    def _take(self) -> None:
        for answer in self.transport.take_answers():
            if not isinstance(answer, dict) or "_refused" in answer:
                detail = answer.get("_refused") if isinstance(answer, dict) else ""
                self._note("scene_answer_refused", str(detail))
                continue
            request_id = answer.get("request_id")
            with self._lock:
                ticket = (
                    self._open.get(request_id) if isinstance(request_id, str) else None
                )
                taken = request_id in self._answered
            if ticket is None or taken:
                # Never asked, answered already, or withdrawn: dropped.
                self._note(
                    "scene_answer_unsolicited",
                    f"{UNSOLICITED}: {str(request_id)[:120]}",
                )
                continue
            try:
                values = self._check(answer)
            except AnswerRefused as exc:
                self._note("scene_answer_refused", f"{request_id}: {exc}")
                continue
            with self._lock:
                if request_id in self._open and request_id not in self._answered:
                    self._answered[request_id] = (values, self.clock.now())
                else:
                    self._note(
                        "scene_answer_unsolicited", f"{UNSOLICITED}: {request_id}"
                    )

    def _check(self, answer: dict) -> dict:
        """The person's values, or ``AnswerRefused``: only the predicates
        (every required one answered, no other name), only true, false or
        null. No decision, no "ready": a person answers predicates."""
        extra = sorted(set(answer) - {"request_id", "predicates"})
        if extra:
            raise AnswerRefused(f"only predicates are answered, not {extra[:4]}")
        values = answer.get("predicates")
        if not isinstance(values, dict):
            raise AnswerRefused("predicates is a mapping of name to true/false/null")
        known = set(self.contract.required) | set(self.contract.optional)
        unknown = sorted(set(values) - known)
        if unknown:
            raise AnswerRefused(f"no such predicate {unknown[:4]}")
        missing = [n for n in self.contract.required if n not in values]
        if missing:
            raise AnswerRefused(f"every required predicate is answered: {missing[:4]}")
        bad = [n for n, v in values.items() if not (v is None or type(v) is bool)]
        if bad:
            raise AnswerRefused(f"true, false or null only: {bad[:4]}")
        return dict(values)

    def _assessment(self, ticket: Ticket, values: dict, received: int) -> bytes:
        request = ticket.request
        refs = [
            {key: r[key] for key in ("kind", "ref", "sha256", "step")}
            for r in ticket.response["refs"]
        ]
        results = [
            {
                "name": name,
                "value": values.get(name),
                "required": name in self.contract.required,
                "evidence_refs": [],
            }
            for name in (*self.contract.required, *self.contract.optional)
            if name in values or name in self.contract.required
        ]
        required = [r for r in results if r["required"]]
        failed = [r["name"] for r in required if r["value"] is False]
        unread = [r["name"] for r in required if r["value"] is None]
        if failed:
            decision = "reset_required"
        elif unread:
            decision = "unknown"
        else:
            decision = "ready"
        produced = max(received, ticket.response["observed_ns"])
        message = {
            "schema": aeri.SCHEMAS["scene"],
            "minor": aeri.MINOR,
            "run_id": self.run_id,
            "emitted_wall_ns": time.time_ns(),
            "kind": "scene",
            "assessment_id": f"h-{ticket.request_id}",
            "request_id": ticket.request_id,
            "episode_id": request["episode_id"],
            "target": request["target"],
            "decision": decision,
            "contract_id": self.contract.contract_id,
            "contract_version": self.contract.contract_version,
            "predicate_results": results,
            "failed_predicates": failed,
            "unknown_predicates": unread,
            # "cannot tell" from the frames shown.
            "unknown_reason": "insufficient_evidence"
            if decision == "unknown"
            else None,
            "evidence_refs": refs[:32],
            "observed_ns": ticket.response["observed_ns"],
            "produced_ns": produced,
            "valid_until_ns": produced + self.validity_ms * 1_000_000,
            "clock_domain": self.clock.domain,
            "provider": PROVIDER,
            "model_name": None,
            "model_digest": None,
            "confidence_kind": "none",
            "calibrated_probability": None,
            "calibration_ref": None,
            "cost": None,
        }
        # Our own message must pass our own contract (never trusted blind:
        # the orchestrator parses it again).
        return aeri.dump(aeri.validate(message, "scene"))

    # ------------------------------------------------------------ for the orchestrator

    def frames(self, request_id: str) -> dict:
        """``{ref: bytes}`` captured for a request (the evidence store keeps
        them); forgotten once read."""
        with self._lock:
            return self._frames.pop(request_id, {})

    def _note(self, code: str, detail: str) -> None:
        with self._lock:
            self._notes.append((code, detail[:300]))
            del self._notes[:-1000]

    def drain_notes(self) -> list:
        """Notes since the last call (``(code, detail)``): answers dropped
        or refused. The orchestrator writes them to the journal."""
        self._take()
        with self._lock:
            notes, self._notes = self._notes, []
        return notes
