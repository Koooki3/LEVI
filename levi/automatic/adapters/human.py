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
   ``{"request_id", "nonce", "frames_sha256", "predicates": {name:
   true|false|null}}`` (``answer_for``). ``nonce`` is drawn with
   ``secrets`` for each question and ``frames_sha256`` binds the answer to
   the frames the question showed, so an answer written before the
   question (its request id is predictable) or for other frames is never
   taken. There is no
   way to say "the scene is ready": the decision follows from the answers
   (a false required predicate: ``reset_required``; one not read:
   ``unknown``, never a pass; all true: ``ready``, which the arbitration
   still checks against the contract, the evidence rule included);
3. ``collect`` turns the answer into the assessment, its evidence the
   frames just captured (``provider: human``). The frames are kept in the
   run's evidence store before the question is asked, and the question
   names their files (``evidence/frames/<sha256>.<ext>``): the person
   answers on what the system captured. A frame that could not be kept is
   no evidence, and an assessment with such a frame is never ``ready``.
   An answer to a request that is not open (never asked, answered already,
   withdrawn after a timeout or a stop), with another nonce or other
   frames, or that was there before the question, is dropped as
   ``E_UNSOLICITED`` and noted; a malformed answer is refused and noted,
   and the question stays open. A request id is asked once.

A check nobody answers within ``reset.human_scene_timeout_s`` is withdrawn
by the orchestrator: the scene counts as unavailable and the run waits for
a person again (``WAIT_HUMAN``).

Transports here are fakes and the file protocol only (``QueueTransport``,
``ScriptedTransport``, ``FileTransport``); the page that shows the question
is a later task. Nothing here moves anything or opens a connection.
"""

import hashlib
import hmac
import json
import os
import queue
import secrets
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from levi.domain import aeri

from .. import console_text
from .. import scene_assessment as sa
from ..journal import fsync_dir
from .events import Ticket

PROVIDER = "human"
# The longest an answer may stay valid (aeri.MAX_RESULT_VALIDITY_MS caps it).
VALIDITY_MS = 10_000
# How often a waiting collect looks for an answer (real clocks).
POLL_NS = 100_000_000
MAX_ANSWER_BYTES = 64 * 1024
UNSOLICITED = "E_UNSOLICITED"


readable = sa.predicate_text


def _frame_ref(view: str, data: bytes) -> tuple[str, str]:
    digest = hashlib.sha256(data).hexdigest()
    return f"{view}:{digest[:16]}", digest


def frames_digest(frames) -> str:
    """sha256 over the frames of a question (their sha256, in order)."""
    text = "\n".join(f"{f['ref']} {f['sha256']}" for f in frames)
    return hashlib.sha256(text.encode()).hexdigest()


def answer_for(question: dict, predicates: dict) -> dict:
    """The answer to ``question`` (what the answering side sends): its
    request id, nonce and frame digest echoed, and the predicates."""
    return {
        "request_id": question["request_id"],
        "nonce": question["nonce"],
        "frames_sha256": question["frames_sha256"],
        "predicates": predicates,
    }


def _request_id(value) -> str:
    if not isinstance(value, str) or not sa.LABEL.match(value):
        # A request id names a file in the file protocol: never a path.
        raise ValueError(f"not a request id: {str(value)[:60]!r}")
    return value


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
        """Answer the question ``request_id`` as a page would: echoing the
        nonce and frame digest of the question it shows. With no such
        question (never asked, or withdrawn) the answer has none."""
        with self._lock:
            question = self.questions.get(request_id)
        echo = (
            {"nonce": question["nonce"], "frames_sha256": question["frames_sha256"]}
            if question
            else {}
        )
        self.put({"request_id": request_id, **echo, "predicates": predicates, **extra})

    def put(self, answer: dict) -> None:
        """Any answer as it arrives (tests: forged ones too)."""
        self._answers.put(answer)

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
        name = _request_id(question.get("request_id"))
        self._folders()
        write_whole(
            self.questions / f"{name}.json",
            json.dumps(question, sort_keys=True).encode(),
        )

    def withdraw(self, request_id: str, reason: str) -> None:
        (self.questions / f"{_request_id(request_id)}.json").unlink(missing_ok=True)

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
                raw = _read_regular(path)
                found = json.loads(raw, object_pairs_hook=_no_duplicates)
            except (OSError, ValueError) as exc:
                out.append({"_refused": f"{path.name}: {exc}"[:300]})
                self._move(path, "refused")
                continue
            self._move(path, "taken")
            out.append(found)
        return out


IGNORED = "_ignored"


class AskOnlyTransport:
    """A channel that shows a question but whose answers never count (the
    page's file protocol on a real-robot run, decision UI-2: answering on the
    page is not a release; only the operator terminal releases motion).

    ``ask``, ``withdraw`` and ``clear`` go to the channel unchanged, so the
    page still shows the question and its frames. ``take_answers`` still
    takes what arrived (the files are moved away, never left to count
    later) and returns only markers ``{"_ignored": source, "request_id":
    ...}``: the provider notes them (``scene_answer_page_ignored``) and
    never reads their predicates. ``on_ignored(request_id)`` is called for
    each, for example to tell the terminal."""

    def __init__(self, channel, *, source="page", on_ignored=None):
        self.channel = channel
        self.source = source
        self.on_ignored = on_ignored

    def reachable(self):
        return self.channel.reachable()

    def clear(self) -> None:
        clear = getattr(self.channel, "clear", None)
        if clear is not None:
            clear()

    def ask(self, question: dict) -> None:
        self.channel.ask(question)

    def withdraw(self, request_id: str, reason: str) -> None:
        self.channel.withdraw(request_id, reason)

    def take_answers(self) -> list:
        out = []
        for answer in self.channel.take_answers():
            request_id = answer.get("request_id") if isinstance(answer, dict) else None
            request_id = str(request_id)[:120] if request_id is not None else None
            out.append({IGNORED: self.source, "request_id": request_id})
            if self.on_ignored is not None:
                self.on_ignored(request_id)
        return out


class BothTransport:
    """Several channels for one question (the page's file protocol and the
    terminal): every question goes to all of them, answers are taken from all
    of them. Which answer counts is decided by ``HumanSceneProvider`` (first
    to arrive; the later one is dropped and noted), not here. On a real-robot
    run the page's channel is wrapped in ``AskOnlyTransport``, so only the
    terminal's answers reach the provider."""

    def __init__(self, *channels):
        self.channels = channels

    def reachable(self):
        for channel in self.channels:
            found = channel.reachable()
            if found is not True:
                return found
        return True

    def clear(self) -> None:
        for channel in self.channels:
            clear = getattr(channel, "clear", None)
            if clear is not None:
                clear()

    def ask(self, question: dict) -> None:
        for channel in self.channels:
            channel.ask(question)

    def withdraw(self, request_id: str, reason: str) -> None:
        for channel in self.channels:
            channel.withdraw(request_id, reason)

    def take_answers(self) -> list:
        out = []
        for channel in self.channels:
            out.extend(channel.take_answers())
        return out


ANSWER_KEYS = {"y": True, "n": False, "u": None}


class TerminalTransport:
    """A person answering at the operator terminal, one key per predicate.

    ``keys`` is a key source (``poll(timeout_s) -> str | None``, ``flush()``,
    ``cbreak()`` as a context manager); ``say(text)`` prints a line.
    ``ask`` prints the question (text, each required predicate with its
    meaning, the evidence frame files) and ``take_answers`` reads the keys
    that arrived without ever blocking (the orchestrator's collect loop
    polls it): ``y`` the predicate holds, ``n`` it does not, ``u`` cannot
    tell; ``b`` goes back one predicate. **Enter never answers**: a question
    that releases motion is answered by an explicit key. The answer sent is
    the same mapping the page sends (``answer_for``), so the provider's
    rules (first answer wins, later ones are dropped and noted) apply
    unchanged and nothing here decides anything.

    ``stopped()`` ends the reading at once (a stop or signal); ``status()``
    may return a warning line (for example a red health monitor), printed
    when it changes. ``run_dir`` turns the frames' run-relative files into
    paths the person can open. ``text`` is the sentence catalogue
    (``console_text.Texts``, Chinese by default); ``tag()`` returns the
    "round n of N" prefix of a question (it is given the question's target)."""

    def __init__(
        self, keys, say, *, run_dir=None, stopped=None, status=None, text=None, tag=None
    ):
        self.keys = keys
        self.say = say
        self.t = text or console_text.Texts()
        self.tag = tag or (lambda target=None: "")
        self.run_dir = None if run_dir is None else Path(run_dir)
        self.stopped = stopped or (lambda: False)
        self.status = status or (lambda: None)
        self._lock = threading.Lock()
        self._pending: dict | None = None
        self._cbreak = None
        self._ready: list = []
        self._delivered: set = set()
        self._last_status = None

    def reachable(self):
        return True

    def waiting(self) -> bool:
        """Whether a question is waiting for this terminal's answer."""
        return self._pending is not None

    # -- the question

    def ask(self, question: dict) -> None:
        required = [p for p in question["predicates"] if p["required"]]
        with self._lock:
            self._pending = {
                "question": question,
                "required": required,
                "values": {},
                "index": 0,
            }
        # Old keystrokes are not answers to this question.
        self.keys.flush()
        self._enter_raw()
        self.say(self._screen(question, required))
        self._prompt()

    def _screen(self, question: dict, required: list) -> str:
        t = self.t
        head = t(
            "scene_head",
            question=t("target_" + question["target"])
            if "target_" + question["target"] in console_text.TEXT["zh"]
            else question["target"],
        )
        lines = [
            "",
            (self.tag(question["target"]) + " " + head).strip(),
            t("scene_frames"),
        ]
        for frame in question["frames"]:
            where = frame.get("file")
            if where and self.run_dir is not None:
                where = str(self.run_dir / where)
            lines.append(
                t(
                    "scene_frame",
                    view=frame["view"],
                    where=where or t("scene_frame_missing"),
                )
            )
        lines.append(t("scene_keys"))
        return "\n".join(lines)

    def _prompt(self) -> None:
        pending = self._pending
        if pending is None or pending["index"] >= len(pending["required"]):
            return
        predicate = pending["required"][pending["index"]]
        self.say(
            self.t(
                "scene_ask",
                number=pending["index"] + 1,
                total=len(pending["required"]),
                name=predicate["name"],
                text=predicate["text"],
            )
        )

    def withdraw(self, request_id: str, reason: str) -> None:
        with self._lock:
            pending = self._pending
            if pending is None or pending["question"]["request_id"] != request_id:
                return
            self._pending = None
            ours = request_id in self._delivered
        self._leave_raw()
        if ours:
            return
        if reason in ("answered", "timeout", "stopped"):
            self.say(self.t("scene_withdrawn_" + reason))
        else:
            self.say(self.t("scene_withdrawn_other", reason=reason))

    def take_answers(self) -> list:
        if self._pending is not None and not self.stopped():
            self._poll()
        warning = self.status()
        if warning != self._last_status:
            self._last_status = warning
            if warning:
                self.say(warning)
        with self._lock:
            out, self._ready = self._ready, []
        return out

    def _poll(self) -> None:
        while self._pending is not None:
            key = self.keys.poll(0)
            if key is None:
                return
            self._handle(key)

    def _handle(self, key: str) -> None:
        pending = self._pending
        names = [p["name"] for p in pending["required"]]
        low = key.lower()
        if low in ANSWER_KEYS:
            name = names[pending["index"]]
            pending["values"][name] = ANSWER_KEYS[low]
            word = self.t(
                {
                    True: "scene_word_true",
                    False: "scene_word_false",
                    None: "scene_word_unknown",
                }[ANSWER_KEYS[low]]
            )
            self.say(self.t("scene_answered", name=name, word=word))
            pending["index"] += 1
            if pending["index"] < len(names):
                self._prompt()
                return
            self._finish(pending)
        elif low == "b" and pending["index"] > 0:
            pending["index"] -= 1
            pending["values"].pop(names[pending["index"]], None)
            self._prompt()
        elif key in ("\n", "\r"):
            self.say(self.t("scene_enter_ignored"))

    def _finish(self, pending: dict) -> None:
        question = pending["question"]
        answer = answer_for(question, dict(pending["values"]))
        with self._lock:
            self._pending = None
            self._delivered.add(question["request_id"])
            self._ready.append(answer)
        self._leave_raw()
        self.say(self.t("scene_recorded"))

    # -- terminal mode

    def _enter_raw(self) -> None:
        if self._cbreak is None:
            self._cbreak = self.keys.cbreak()
            self._cbreak.__enter__()

    def _leave_raw(self) -> None:
        cbreak, self._cbreak = self._cbreak, None
        if cbreak is not None:
            cbreak.__exit__(None, None, None)

    def close(self) -> None:
        self._leave_raw()


def _read_regular(path: Path) -> bytes:
    """A regular file's bytes, at most ``MAX_ANSWER_BYTES``: never through
    a symbolic link, never blocking on a FIFO."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(MAX_ANSWER_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_ANSWER_BYTES:
        raise ValueError(f"over {MAX_ANSWER_BYTES} bytes")
    return raw


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


def write_answer(root, question: dict, predicates: dict) -> Path:
    """The answering side of ``FileTransport`` (what the page or a test
    writes): the answer to ``question`` (as read from ``questions/``),
    written whole."""
    request_id = _request_id(question.get("request_id"))
    folder = Path(root) / "answers"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = folder / f"{request_id}.{time.time_ns()}.json"
    write_whole(path, json.dumps(answer_for(question, predicates)).encode())
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
        store=None,
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
        # The run's ``recorder.EvidenceStore`` (the orchestrator hands it
        # over, ``use_evidence``): frames are kept there before the question.
        self.store = store
        self._lock = threading.Lock()
        self._used: set = set()  # request ids asked once
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

    def use_evidence(self, store) -> None:
        self.store = store

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
        request_id = _request_id(request["request_id"])
        with self._lock:
            if request_id in self._used:
                raise ValueError(f"{request_id} was asked before (one question each)")
            self._used.add(request_id)
        # Whatever arrived before the question is not an answer to it.
        self._take()
        frames = self.capture()
        if not isinstance(frames, dict) or not frames:
            raise ValueError("capture() returned no frames")
        observed = self.clock.now()
        shown, kept, unsaved = [], {}, []
        for view, data in sorted(frames.items()):
            data = data if isinstance(data, bytes) else str(data).encode()
            ref, digest = _frame_ref(str(view), data)
            saved = self._keep(ref, data)
            shown.append(
                {
                    "kind": "frame",
                    "ref": ref,
                    "view": str(view),
                    "sha256": digest,
                    "bytes": len(data),
                    # Relative to the run directory; None when not kept.
                    "file": saved["file"],
                    "skipped": saved["skipped"],
                }
            )
            if saved["file"] is None:
                unsaved.append(ref)
            else:
                kept[ref] = data
        names = [(n, True) for n in self.contract.required] + [
            (n, False) for n in self.contract.optional
        ]
        question = {
            "request_id": request_id,
            "nonce": secrets.token_urlsafe(16),
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
            "frames": shown,
            "frames_sha256": frames_digest(shown),
            "asked_ns": observed,
            "answers": ["true", "false", "null (cannot tell)"],
        }
        self._asked += 1
        ticket = Ticket(
            request_id=request_id,
            number=self._asked,
            submitted_ns=observed,
            ready_ns=observed,
            request=dict(request),
            response={
                "observed_ns": observed,
                "refs": [f for f in shown if f["file"] is not None],
                "unsaved": unsaved,
                "nonce": question["nonce"],
                "frames_sha256": question["frames_sha256"],
            },
        )
        with self._lock:
            self._open[request_id] = ticket
            self._frames[request_id] = (kept, unsaved)
        self.transport.ask(question)
        return ticket

    def _keep(self, ref: str, data: bytes) -> dict:
        """The frame in the evidence store (``{"file", "skipped"}``); never
        raises: a frame that cannot be kept is no evidence."""
        if self.store is None:
            return {"file": None, "skipped": "no_evidence_store"}
        try:
            found = self.store.keep_frame(ref, data)
        except Exception as exc:  # noqa: BLE001 - the frame is no evidence
            self._note("evidence_write_failed", f"{ref}: {exc}")
            return {"file": None, "skipped": "write_failed"}
        if found["file"] is None:
            return {"file": None, "skipped": found["skipped"]}
        return {"file": f"{self.store.folder.name}/{found['file']}", "skipped": None}

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
        """Route the answers that arrived. Notes are written after the lock
        is released (the lock is never taken twice)."""
        for answer in self.transport.take_answers():
            if isinstance(answer, dict) and IGNORED in answer:
                # Shown on a channel whose answers never count (UI-2).
                self._note(
                    "scene_answer_page_ignored",
                    f"{answer[IGNORED]}: {answer.get('request_id')}",
                )
                continue
            if not isinstance(answer, dict) or "_refused" in answer:
                detail = answer.get("_refused") if isinstance(answer, dict) else ""
                self._note("scene_answer_refused", str(detail))
                continue
            request_id = answer.get("request_id")
            with self._lock:
                ticket = (
                    self._open.get(request_id) if isinstance(request_id, str) else None
                )
                taken = isinstance(request_id, str) and request_id in self._answered
            if ticket is None or taken or not self._echoes(ticket, answer):
                # Never asked, answered already, withdrawn, or not an
                # answer to this question (nonce, frames): dropped.
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
                fresh = request_id in self._open and request_id not in self._answered
                if fresh:
                    self._answered[request_id] = (values, self.clock.now())
            if not fresh:
                self._note("scene_answer_unsolicited", f"{UNSOLICITED}: {request_id}")

    @staticmethod
    def _echoes(ticket: Ticket, answer: dict) -> bool:
        nonce, digest = answer.get("nonce"), answer.get("frames_sha256")
        if not isinstance(nonce, str) or not isinstance(digest, str):
            return False
        return hmac.compare_digest(
            nonce, ticket.response["nonce"]
        ) and hmac.compare_digest(digest, ticket.response["frames_sha256"])

    def _check(self, answer: dict) -> dict:
        """The person's values, or ``AnswerRefused``: only the predicates
        (every required one answered, no other name), only true, false or
        null. No decision, no "ready": a person answers predicates."""
        extra = sorted(
            set(answer) - {"request_id", "nonce", "frames_sha256", "predicates"}
        )
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
        # Only frames that were kept are evidence.
        refs = [
            {"kind": "frame", "ref": r["ref"], "sha256": r["sha256"], "step": None}
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
        elif unread or ticket.response["unsaved"]:
            # A frame the person was asked about could not be kept: the
            # answer is not on evidence anyone can look at again.
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
        """``{ref: bytes}`` of the frames kept for a request; forgotten
        once read (``unsaved`` first)."""
        with self._lock:
            return self._frames.pop(request_id, ({}, []))[0]

    def unsaved(self, request_id: str) -> list:
        """Refs of the frames shown for a request that could not be kept."""
        with self._lock:
            return list(self._frames.get(request_id, ({}, []))[1])

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
