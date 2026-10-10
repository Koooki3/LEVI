"""The durable state journal of one AERI run: its single source of truth.

Files in the run's directory (``<rollout_root>/.aeri/runs/<run_id>/``):

- ``state_journal.jsonl``: append-only ``levi.aeri.run_event.v1`` lines.
  Each line is one ``write`` followed by ``fsync``; the directory is synced
  when the file is created. Line 0 is the run header (plan hash, contract
  versions, LEVI commit); every line carries the sha256 of the previous
  line's bytes (``prev_sha256``), so a torn or edited line is found.
- ``journal.lock``: who writes. The writer holds an ``flock`` on the run
  directory itself; a second writer (another process, or a second open in
  this one) is refused (``JournalBusy``), even when ``journal.lock`` was
  removed; the kernel drops the lock when the holder dies.
- ``state.json``: a snapshot derived from the journal after each commit
  (temporary file, fsync, replace, fsync directory). It is never read back
  to decide anything; losing it loses nothing.
- ``torn/``: the bytes of a torn last line, kept for inspection before the
  file is cut back to its last whole line.
- ``plan.json``: the normalised plan the run was started with and its
  ``plan_sha256`` (the header's), when the caller gave it; written once
  (temporary file, fsync, replace, fsync directory) before the header, and
  checked whenever the journal is created or reopened.

Transactions: ``prepared`` (synced before any side effect) -> the caller
acts -> ``acknowledged`` (what the controller says happened: yes, no or
unknown; not that the goal was reached) -> ``committed`` (only now does the
state change) or ``aborted``. A ``none`` action needs no acknowledgement; a
real action is committed only after ``executed: yes``. One transaction is
open at a time.

Reading back: only a last line that has no newline, or that cannot be
decoded as JSON, is torn (a crash): it is ignored. A whole line that decodes
but does not chain to the line before (an edit: a crash never leaves one),
or a whole, chained line that fails the contract (a newer minor, an unknown
field, a broken rule) is kept and makes the journal corrupt, like any bad
line before the last; a corrupt journal is never written to or cut again,
and its ``effective_state`` is FAULT_LOCKED.

Recovery after a crash never replays anything: unless the run had reached
``COMPLETED``, it aborts the dangling transaction (with a
``crash_before_commit`` note) and commits a move to ``FAULT_LOCKED``
(``recovery_ambiguous``) under a new control epoch, so no token from before
the crash is valid. A corrupt journal reports ``FAULT_LOCKED``
(``journal_corrupt``) and writes nothing. Leaving ``FAULT_LOCKED`` is an
operator's command, not this module's.

Idempotency: transaction ids are unique; a physical action (home, policy
steps) is non-idempotent and carries a ``step`` the orchestrator assigns,
increasing per (episode, kind); its ``idempotency_key`` (run, episode,
kind, step: no control epoch) is refused once prepared, in every later epoch
and after any recovery (the FR3 server cannot deduplicate, so it is never
sent twice). A retry after ``executed: no`` is a new logical command: a new
step and ``retry_of`` naming the closed transaction that did not run; ``by_command`` finds
what an operator command already did; ``expected_seq`` is a compare-and-set
on the next line number.

Leaving FAULT_LOCKED: only an operator's command (``command_id`` set) to
PREFLIGHT, or a recovery to FAULT_LOCKED, with a ``none`` action. A commit
keeps the reason, episode and policy epoch it prepared.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from levi.domain import aeri

JOURNAL = "state_journal.jsonl"
LOCK = "journal.lock"
SNAPSHOT = "state.json"
TORN = "torn"
PLAN_FILE = "plan.json"
INITIAL_STATE = "PREFLIGHT"
ZEROS = "0" * 64
SCHEMA = aeri.SCHEMAS["run_event"]
# Fields only the journal writes.
RESERVED = frozenset(
    {
        "schema",
        "minor",
        "run_id",
        "emitted_wall_ns",
        "sequence_no",
        "record",
        "mono_ns",
        "clock_domain",
        "prev_sha256",
    }
)


class JournalError(Exception):
    """``code``: E_BUSY, E_EXISTS, E_EMPTY, E_PLAN, E_CORRUPT, E_BROKEN,
    E_SEQ, E_PROTOCOL, E_CONTRACT, E_REPEAT."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class JournalBusy(JournalError):
    def __init__(self, holder: dict | None):
        super().__init__("E_BUSY", f"another writer holds the journal: {holder}")
        self.holder = holder


class JournalRefused(JournalError):
    """The append (or open) would break the journal's rules."""


def line_sha(line: bytes) -> str:
    """The chain value of a line (its bytes without the newline)."""
    return hashlib.sha256(line).hexdigest()


# sha256(run, episode, kind, step); the contract checks every prepared line.
action_key = aeri.action_key


def params_sha(params) -> str:
    return hashlib.sha256(
        json.dumps(params, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def process_identity(pid: int | None = None) -> dict:
    """``{pid, start_ticks, boot_id}`` of a live process (this one by
    default), as the run event's ``authority.process`` wants it."""
    from levi.children import identity

    pid = os.getpid() if pid is None else pid
    found = identity(pid)
    if found is None:
        raise JournalError("E_PROTOCOL", f"process {pid} is not running")
    return {
        "pid": pid,
        "start_ticks": int(found["start_ticks"]),
        "boot_id": found["boot"],
    }


def fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_durable(path: Path, data: bytes) -> None:
    """Replace ``path`` so that a power cut leaves the old or the new bytes."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    fsync_dir(path.parent)


# --- the plan in the run folder ------------------------------------------------------


def plan_digest(plan) -> str:
    """``plan_sha256`` of a normalised plan: the rule of the job loader
    (``cli.load_job``); a value JSON cannot hold counts as its text."""
    return hashlib.sha256(
        json.dumps(plan, sort_keys=True, default=str).encode()
    ).hexdigest()


def read_plan(directory) -> dict | None:
    """``{"plan_sha256", "plan"}`` from the run folder's ``plan.json``;
    ``None`` when there is none. One that cannot be read, or whose plan does
    not hash to its ``plan_sha256``, is refused (``E_PLAN``)."""
    path = Path(directory) / PLAN_FILE
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise JournalRefused("E_PLAN", f"{path}: {exc}") from None
    try:
        kept = json.loads(data)
    except (ValueError, RecursionError):
        raise JournalRefused("E_PLAN", f"{path} is not JSON") from None
    if (
        not isinstance(kept, dict)
        or not isinstance(kept.get("plan_sha256"), str)
        or not isinstance(kept.get("plan"), dict)
        or plan_digest(kept["plan"]) != kept["plan_sha256"]
    ):
        raise JournalRefused("E_PLAN", f"{path} does not hold a plan and its sha256")
    return kept


def keep_plan(directory, plan: dict, plan_sha256: str) -> bool:
    """Write ``plan.json`` once (durably); an existing one must hold the
    same plan sha256 (``E_PLAN`` otherwise, never overwritten). ``True``
    when this call wrote it. Called under the journal's lock."""
    if not isinstance(plan, dict) or plan_digest(plan) != plan_sha256:
        raise JournalRefused("E_PLAN", "the plan does not hash to plan_sha256")
    kept = read_plan(directory)
    if kept is not None:
        if kept["plan_sha256"] != plan_sha256:
            raise JournalRefused(
                "E_PLAN", f"{PLAN_FILE} holds another plan ({kept['plan_sha256']})"
            )
        return False
    value = {"plan_sha256": plan_sha256, "plan": plan}
    text = json.dumps(value, sort_keys=True, indent=1, default=str) + "\n"
    write_durable(Path(directory) / PLAN_FILE, text.encode())
    return True


def _check_kept_plan(directory, plan_sha256: str) -> None:
    """A ``plan.json`` already in the folder belongs to this plan."""
    kept = read_plan(directory)
    if kept is not None and kept["plan_sha256"] != plan_sha256:
        raise JournalRefused(
            "E_PLAN",
            f"{PLAN_FILE} holds plan {kept['plan_sha256']}, the run {plan_sha256}",
        )


# --- replay: the rules every line must keep ------------------------------------------


def refuse(text: str):
    raise JournalRefused("E_PROTOCOL", text)


@dataclass
class Replay:
    """The run as the journal tells it, rebuilt line by line."""

    run_id: str | None = None
    header: aeri.RunEvent | None = None
    state: str = INITIAL_STATE
    open_tx: aeri.RunEvent | None = None
    open_ack: aeri.RunEvent | None = None
    control_epoch: int = 0
    transactions: set = field(default_factory=set)
    attempted: dict = field(default_factory=dict)  # idempotency key -> tx id
    commands: dict = field(default_factory=dict)  # command id -> [sequence_no]
    # (episode_id, kind) -> highest step of a non-idempotent action prepared
    last_step: dict = field(default_factory=dict)
    # tx id -> the physical action's prepared event and its acknowledgement
    physical: dict = field(default_factory=dict)
    closed: set = field(default_factory=set)  # tx ids committed or aborted
    last_mono: dict = field(default_factory=dict)  # clock domain -> mono_ns

    @property
    def completed(self) -> bool:
        return self.state == "COMPLETED" and self.open_tx is None

    def check(self, event: aeri.RunEvent) -> None:
        """Raise ``JournalRefused`` when ``event`` cannot follow."""
        if event.record == "run_header":
            if self.header is not None:
                refuse("a second run header")
            return
        if self.header is None:
            refuse("the first line is the run header")
        if event.run_id != self.run_id:
            refuse(f"run {event.run_id} in the journal of run {self.run_id}")
        if event.control_epoch < self.control_epoch:
            refuse("control_epoch went back")
        last = self.last_mono.get(event.clock_domain)
        if last is not None and event.mono_ns < last:
            refuse("mono_ns went back within one clock domain")
        record, tx = event.record, event.transaction_id
        if record == "prepared":
            if self.open_tx is not None:
                refuse(f"{self.open_tx.transaction_id} is still open")
            if self.state == "COMPLETED":
                refuse("the run is completed")
            if tx in self.transactions:
                refuse(f"{tx} was used before")
            if event.from_state != self.state:
                refuse(f"from_state {event.from_state} but the state is {self.state}")
            if self.state == "FAULT_LOCKED":
                self._check_leaving_fault(event)
            action = event.action
            if action.non_idempotent and action.idempotency_key in self.attempted:
                refuse(
                    "a non-idempotent action is never repeated: tried in "
                    f"{self.attempted[action.idempotency_key]}"
                )
            if action.non_idempotent:
                last = self.last_step.get((event.episode_id, action.kind))
                if last is not None and action.step <= last:
                    refuse(
                        f"step {action.step} of {action.kind}: steps only go up "
                        f"(last {last})"
                    )
            if action.retry_of is not None:
                self._check_retry(event)
            return
        if record == "note":
            return
        if self.open_tx is None or tx != self.open_tx.transaction_id:
            refuse(f"{record} of {tx}, which is not the open transaction")
        if record == "acknowledged":
            if self.open_ack is not None:
                refuse(f"{tx} is already acknowledged")
            return
        if record == "committed":
            prepared = self.open_tx
            if (event.from_state, event.to_state) != (
                prepared.from_state,
                prepared.to_state,
            ):
                refuse("a commit moves between the states it prepared")
            for name in ("reason", "episode_id", "episode_role", "policy_epoch"):
                if getattr(event, name) != getattr(prepared, name):
                    refuse(f"a commit keeps the {name} it prepared")
            if prepared.action.kind != "none":
                if self.open_ack is None:
                    refuse(f"{tx} acts and is not acknowledged")
                if self.open_ack.ack.executed != "yes":
                    refuse(
                        f"{tx} was not executed ({self.open_ack.ack.executed}): abort it"
                    )

    def _check_retry(self, event: aeri.RunEvent) -> None:
        """A retry names a closed transaction of the same action and episode
        that the controller reported as not executed."""
        before = self.physical.get(event.action.retry_of)
        if before is None:
            refuse(f"retry_of {event.action.retry_of}: no such physical action")
        prepared, ack = before
        if (prepared.episode_id, prepared.action.kind) != (
            event.episode_id,
            event.action.kind,
        ):
            refuse("a retry repeats the same action of the same episode")
        if prepared.transaction_id not in self.closed:
            refuse("a retry follows a closed transaction")
        if ack is None or ack.ack.executed != "no":
            refuse("only an action reported as not executed (executed: no) is retried")

    @staticmethod
    def _check_leaving_fault(event: aeri.RunEvent) -> None:
        """Out of FAULT_LOCKED only by an operator's command, and only to
        PREFLIGHT (or a recovery's FAULT_LOCKED -> FAULT_LOCKED); no action."""
        who = event.authority
        if who.principal_kind == "recovery":
            allowed = ("FAULT_LOCKED",)
        elif who.principal_kind == "operator" and who.command_id is not None:
            allowed = ("PREFLIGHT", "FAULT_LOCKED")
        else:
            refuse("only an operator's command or a recovery leaves FAULT_LOCKED")
        if event.to_state not in allowed:
            refuse(f"FAULT_LOCKED leads only to {', '.join(allowed)}")
        if event.action.kind != "none":
            refuse("nothing acts on the way out of FAULT_LOCKED")

    def apply(self, event: aeri.RunEvent) -> None:
        self.check(event)
        record = event.record
        if record == "run_header":
            self.header, self.run_id = event, event.run_id
        elif record == "prepared":
            self.open_tx, self.open_ack = event, None
            self.transactions.add(event.transaction_id)
            action = event.action
            if action.non_idempotent:
                self.last_step[(event.episode_id, action.kind)] = action.step
                self.physical[event.transaction_id] = (event, None)
            self.attempted.setdefault(
                event.action.idempotency_key, event.transaction_id
            )
        elif record == "acknowledged":
            self.open_ack = event
            tx = event.transaction_id
            if tx in self.physical:
                self.physical[tx] = (self.physical[tx][0], event)
        elif record == "committed":
            self.state = event.to_state
            self.open_tx = self.open_ack = None
            self.closed.add(event.transaction_id)
        elif record == "aborted":
            self.open_tx = self.open_ack = None
            self.closed.add(event.transaction_id)
        self.control_epoch = max(self.control_epoch, event.control_epoch)
        self.last_mono[event.clock_domain] = event.mono_ns
        command = event.authority.command_id
        if command is not None:
            self.commands.setdefault(command, []).append(event.sequence_no)


# --- scanning the file -----------------------------------------------------------------


@dataclass
class Scan:
    """What a journal file holds: whole valid lines, then a torn tail."""

    events: list = field(default_factory=list)
    lines: list = field(default_factory=list)  # raw bytes of each event's line
    good_bytes: int = 0
    torn: bytes = b""
    corrupt: str | None = None
    corrupt_code: str | None = None
    replay: Replay = field(default_factory=Replay)

    @property
    def effective_state(self) -> str:
        """The state to act on: a corrupt journal is FAULT_LOCKED, whatever
        its readable lines say."""
        return "FAULT_LOCKED" if self.corrupt else self.replay.state


def scan(path: Path) -> Scan:
    """Read a journal file without changing it."""
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        return Scan()
    parts = data.split(b"\n")
    whole, tail = parts[:-1], parts[-1]
    result = Scan()
    previous = ZEROS
    offset = 0
    for index, line in enumerate(whole):
        last = index == len(whole) - 1 and not tail
        # Torn means a crash: a last line without its newline (the tail), or
        # a last line that cannot be decoded. A whole line that decodes but
        # does not chain is an edit, never a crash artefact: corrupt.
        try:
            raw = json.loads(line)
        except (ValueError, RecursionError):
            if last:
                result.torn = data[offset:]
            else:
                result.corrupt, result.corrupt_code = (
                    f"line {index}: not JSON",
                    "E_JSON",
                )
            return result
        if not isinstance(raw, dict) or raw.get("prev_sha256") != previous:
            result.corrupt = f"line {index}: the hash chain is broken"
            result.corrupt_code = "E_CHAIN"
            return result
        # A whole, chained line that fails the contract or the rules was
        # written that way (a newer writer, an edit): it is kept, and the
        # journal is corrupt, never cut back.
        try:
            event = aeri.parse(line, "run_event")
        except aeri.AeriError as exc:
            result.corrupt = f"line {index}: {exc}"
            result.corrupt_code = exc.code
            return result
        problem = None
        if event.sequence_no != index:
            problem = f"line {index} says sequence_no {event.sequence_no}"
        else:
            try:
                result.replay.apply(event)
            except JournalRefused as exc:
                problem = f"line {index}: {exc.detail}"
        if problem is not None:
            result.corrupt, result.corrupt_code = problem, "E_PROTOCOL"
            return result
        result.events.append(event)
        result.lines.append(line)
        previous = line_sha(line)
        offset += len(line) + 1
        result.good_bytes = offset
    result.torn = tail
    return result


# --- the writer -----------------------------------------------------------------------


@dataclass
class Recovery:
    state: str
    reason: str | None
    previous_state: str | None
    dangling: str | None = None
    written: list = field(default_factory=list)
    detail: str = ""


class _Lock:
    """The single-writer lock: ``flock`` on the run directory itself (a
    directory cannot be replaced while it holds the journal, so removing
    ``journal.lock`` does not let a second writer in). ``journal.lock`` only
    says who holds it."""

    def __init__(self, directory: Path):
        self._fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._fd)
            try:
                holder = json.loads((directory / LOCK).read_text() or "null")
            except (OSError, ValueError):
                holder = None
            raise JournalBusy(holder) from None
        try:
            me = {"pid": os.getpid(), "at": time.time()}
            with contextlib.suppress(JournalError):
                me.update(process_identity())
            write_durable(directory / LOCK, json.dumps(me).encode())
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


class Journal:
    """The one writer of a run's journal. Use ``create`` or ``open``."""

    def __init__(
        self, directory: Path, lock_handle, result: Scan, clock, domain, run_id=None
    ):
        self.directory = Path(directory)
        self._run_id = run_id
        self.path = self.directory / JOURNAL
        self._lock = lock_handle
        self._events = result.events
        self._replay = result.replay
        self.corrupt = result.corrupt
        self._last_sha = line_sha(result.lines[-1]) if result.lines else ZEROS
        # Every line of one journal has the minor of its header: a new run
        # writes this code's run_event minor, an older run keeps its own.
        self.minor = (
            result.events[0].minor if result.events else aeri.MINORS["run_event"]
        )
        self._clock = clock
        self.clock_domain = domain
        self._mutex = threading.Lock()
        self._broken: str | None = None
        self.snapshot_error: str | None = None
        self._fd = None
        if self.corrupt is None:
            self._fd = os.open(self.path, os.O_WRONLY | os.O_APPEND)

    # ------------------------------------------------------------ open

    @staticmethod
    def _take_lock(directory: Path) -> "_Lock":
        return _Lock(directory)

    @staticmethod
    def _cut_torn(directory: Path, result: Scan) -> None:
        """Keep the torn bytes aside, then cut the file to whole lines."""
        if not result.torn:
            return
        folder = directory / TORN
        if not folder.exists():
            folder.mkdir()
            fsync_dir(directory)
        write_durable(
            folder / f"{len(result.events):06d}-{time.time_ns()}.bin", result.torn
        )
        with (directory / JOURNAL).open("r+b") as handle:
            handle.truncate(result.good_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        result.torn = b""

    @classmethod
    def create(
        cls,
        directory: Path,
        *,
        run_id: str,
        plan_sha256: str,
        authority: dict,
        levi_commit: str | None = None,
        contracts: dict | None = None,
        clock: Callable[[], int] = time.monotonic_ns,
        clock_domain: str | None = None,
        reset_mode: str | None = None,
        scene_check: str | None = None,
        plan: dict | None = None,
    ) -> "Journal":
        """Start the journal of a new run (refused when one exists).
        ``reset_mode`` and ``scene_check`` (code names) go into the header
        only when given; a caller that gives neither writes the header an
        older caller wrote, at this code's minor. ``plan``: the normalised
        plan, kept as ``plan.json`` before the header (it must hash to
        ``plan_sha256``); a ``plan.json`` already there must be this plan's."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        fsync_dir(directory.parent)
        lock = cls._take_lock(directory)
        journal = None
        try:
            result = scan(directory / JOURNAL)
            if result.events or result.corrupt:
                raise JournalRefused("E_EXISTS", f"{directory} already has a journal")
            if plan is not None:
                keep_plan(directory, plan, plan_sha256)
            else:
                _check_kept_plan(directory, plan_sha256)
            path = directory / JOURNAL
            if path.exists():
                cls._cut_torn(directory, result)  # a header torn by a crash
            else:
                os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644))
                fsync_dir(directory)
            journal = cls(
                directory,
                lock,
                result,
                clock,
                clock_domain or aeri.host_clock_domain(),
                run_id=run_id,
            )
            versions = contracts or {
                schema: aeri.MINORS[name] for name, schema in aeri.SCHEMAS.items()
            }
            header = {
                "plan_sha256": plan_sha256,
                "contracts": [
                    {"schema": schema, "minor": minor}
                    for schema, minor in sorted(versions.items())
                ],
                "levi_commit": levi_commit,
            }
            for name, value in (
                ("reset_mode", reset_mode),
                ("scene_check", scene_check),
            ):
                if value is not None:
                    header[name] = value
            journal.append(
                "run_header", authority=authority, control_epoch=0, header=header
            )
            return journal
        except BaseException:
            if journal is not None:
                journal.close()
            lock.close()
            raise

    @classmethod
    def open(
        cls,
        directory: Path,
        *,
        plan_sha256: str | None,
        clock: Callable[[], int] = time.monotonic_ns,
        clock_domain: str | None = None,
        reset_mode: str | None = None,
        scene_check: str | None = None,
        authority: dict | None = None,
        plan: dict | None = None,
    ) -> "Journal":
        """Take over an existing journal as its writer: a torn last line is
        set aside, the plan hash must match (``None`` skips that check, for
        recovery tools). A corrupt journal opens read-only (``corrupt``).

        ``reset_mode``/``scene_check``: the configuration's; one the header
        names differently refuses the open (``E_PLAN``; the header is never
        rewritten) and, with an ``authority``, leaves a
        ``run_header_mismatch`` note. A header without them (run_event
        minor 0) or a caller that gives none is not checked.

        A ``plan.json`` in the folder must be the header's plan (``E_PLAN``);
        ``plan`` given (it must hash to the header's ``plan_sha256``) is kept
        as ``plan.json`` when the run has none (a run started before it)."""
        directory = Path(directory)
        if not (directory / JOURNAL).is_file():
            raise JournalRefused("E_EMPTY", f"{directory} has no journal")
        lock = cls._take_lock(directory)
        try:
            result = scan(directory / JOURNAL)
            if result.corrupt is None:
                if not result.events:
                    raise JournalRefused("E_EMPTY", f"{directory} has no run header")
                header = result.events[0].header
                if plan_sha256 is not None and header.plan_sha256 != plan_sha256:
                    raise JournalRefused(
                        "E_PLAN", "the plan changed since the run started"
                    )
                _check_kept_plan(directory, header.plan_sha256)
                cls._cut_torn(directory, result)
            journal = cls(
                directory, lock, result, clock, clock_domain or aeri.host_clock_domain()
            )
        except BaseException:
            lock.close()
            raise
        try:
            if journal.corrupt is None:
                journal._check_modes(
                    {"reset_mode": reset_mode, "scene_check": scene_check}, authority
                )
                if plan is not None:
                    keep_plan(directory, plan, journal._events[0].header.plan_sha256)
            return journal
        except BaseException:
            journal.close()
            raise

    def _check_modes(self, given: dict, authority: dict | None) -> None:
        """Refuse a configuration whose modes differ from the header's."""
        header = self._events[0].header
        problems = []
        for name, value in given.items():
            said = getattr(header, name, None)
            if value is not None and said is not None and value != said:
                problems.append(f"{name}: the run header says {said}, not {value}")
        if not problems:
            return
        detail = "; ".join(problems)
        if authority is not None:
            # On record for whoever looks at the run; a refused note (a
            # broken writer, a clock that went back) does not hide the refusal.
            with contextlib.suppress(JournalError):
                self.note("run_header_mismatch", detail[:300], authority=authority)
        raise JournalRefused(
            "E_PLAN", f"the configuration does not match the run header: {detail}"
        )

    @staticmethod
    def read(directory: Path) -> Scan:
        """The journal as it stands, without the lock and without writing."""
        return scan(Path(directory) / JOURNAL)

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        if self._lock is not None:
            self._lock.close()
            self._lock = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ view

    @property
    def run_id(self) -> str | None:
        return self._replay.run_id or self._run_id

    @property
    def state(self) -> str:
        """FAULT_LOCKED for a corrupt journal, whatever its readable lines
        say; otherwise the state its last commit set."""
        return "FAULT_LOCKED" if self.corrupt else self._replay.state

    @property
    def completed(self) -> bool:
        return self.corrupt is None and self._replay.completed

    @property
    def control_epoch(self) -> int:
        return self._replay.control_epoch

    @property
    def open_transaction(self) -> aeri.RunEvent | None:
        return self._replay.open_tx

    @property
    def next_seq(self) -> int:
        return len(self._events)

    @property
    def events(self) -> list:
        return list(self._events)

    def attempted(self, idempotency_key: str) -> str | None:
        """The transaction that first prepared this action, if any."""
        return self._replay.attempted.get(idempotency_key)

    def by_command(self, command_id: str) -> list:
        return [self._events[n] for n in self._replay.commands.get(command_id, [])]

    def transaction(self, transaction_id: str) -> list:
        return [e for e in self._events if e.transaction_id == transaction_id]

    # ----------------------------------------------------------- write

    def append(self, record: str, *, expected_seq: int | None = None, **fields):
        """Validate, check the rules and durably append one line."""
        reserved = set(fields) & RESERVED
        if reserved:
            raise JournalRefused(
                "E_CONTRACT", f"{sorted(reserved)} are set by the journal"
            )
        with self._mutex:
            if self.corrupt is not None:
                raise JournalRefused("E_CORRUPT", self.corrupt)
            if self._broken is not None:
                raise JournalRefused("E_BROKEN", self._broken)
            if self._fd is None:
                raise JournalRefused("E_BROKEN", "the journal is closed")
            sequence = len(self._events)
            if expected_seq is not None and expected_seq != sequence:
                raise JournalRefused(
                    "E_SEQ", f"expected line {expected_seq}, the next is {sequence}"
                )
            value = {
                "schema": SCHEMA,
                "minor": self.minor,
                "run_id": self.run_id,
                "emitted_wall_ns": time.time_ns(),
                "sequence_no": sequence,
                "record": record,
                "mono_ns": self._clock(),
                "clock_domain": self.clock_domain,
                "prev_sha256": self._last_sha,
                **fields,
            }
            try:
                event = aeri.validate(value, "run_event")
            except aeri.AeriError as exc:
                raise JournalRefused("E_CONTRACT", str(exc)) from None
            self._replay.check(event)
            line = aeri.dump(event)
            if b"\n" in line or len(line) >= aeri.MAX_BYTES:
                raise JournalRefused("E_CONTRACT", "the line is too large")
            self._write(line + b"\n")
            self._replay.apply(event)
            self._events.append(event)
            self._last_sha = line_sha(line)
            if record == "committed" or record == "run_header":
                self._snapshot()
            return event

    def _write(self, data: bytes) -> None:
        try:
            view = memoryview(data)
            while view:
                written = os.write(self._fd, view)
                view = view[written:]
            os.fsync(self._fd)
        except OSError as exc:
            # Part of the line may be on disk: no more appends from this
            # writer; reopening sets the torn bytes aside.
            self._broken = f"a write failed ({exc}); reopen the journal"
            raise JournalRefused("E_BROKEN", self._broken) from None

    def _snapshot(self) -> None:
        open_tx = self._replay.open_tx
        value = {
            "run_id": self.run_id,
            "state": self.state,
            "completed": self.completed,
            "sequence_no": len(self._events) - 1,
            "control_epoch": self.control_epoch,
            "open_transaction": open_tx.transaction_id if open_tx else None,
            "last_line_sha256": self._last_sha,
            "derived_from": JOURNAL,
        }
        try:
            write_durable(
                self.directory / SNAPSHOT,
                (json.dumps(value, sort_keys=True, indent=1) + "\n").encode(),
            )
            self.snapshot_error = None
        except OSError as exc:
            # The journal holds the truth; a missing snapshot is rebuilt.
            self.snapshot_error = str(exc)

    # ---------------------------------------------- transaction helpers

    def prepare(
        self,
        to_state: str,
        reason: str,
        *,
        kind: str,
        authority: dict,
        control_epoch: int,
        non_idempotent: bool = False,
        params=None,
        step: int | None = None,
        episode_id: str | None = None,
        episode_role: str | None = None,
        policy_epoch: int | None = None,
        evidence_ids=(),
        expected_seq: int | None = None,
        retry_of: str | None = None,
    ):
        """Open a transaction from the current state (synced on return:
        only now may the caller act)."""
        sequence = self.next_seq
        return self.append(
            "prepared",
            expected_seq=expected_seq,
            transaction_id=f"{self.run_id}:tx{sequence}",
            from_state=self.state,
            to_state=to_state,
            reason=reason,
            authority=authority,
            control_epoch=control_epoch,
            policy_epoch=policy_epoch,
            episode_id=episode_id,
            episode_role=episode_role,
            evidence_ids=list(evidence_ids),
            action={
                "kind": kind,
                "idempotency_key": action_key(self.run_id, episode_id, kind, step),
                "non_idempotent": non_idempotent,
                "params_sha256": params_sha(params),
                "step": step,
                "retry_of": retry_of,
            },
        )

    def _open(self):
        tx = self._replay.open_tx
        if tx is None:
            raise JournalRefused("E_PROTOCOL", "no transaction is open")
        return tx

    def acknowledge(self, executed: str, source: str, detail_code: str, *, authority):
        tx = self._open()
        return self.append(
            "acknowledged",
            transaction_id=tx.transaction_id,
            authority=authority,
            control_epoch=self.control_epoch,
            episode_id=tx.episode_id,
            episode_role=tx.episode_role,
            ack={"executed": executed, "source": source, "detail_code": detail_code},
        )

    def commit(self, *, authority, episode_result=None):
        tx = self._open()
        return self.append(
            "committed",
            transaction_id=tx.transaction_id,
            from_state=tx.from_state,
            to_state=tx.to_state,
            reason=tx.reason,
            authority=authority,
            control_epoch=self.control_epoch,
            episode_id=tx.episode_id,
            episode_role=tx.episode_role,
            policy_epoch=tx.policy_epoch,
            episode_result=episode_result,
        )

    def abort(self, *, authority, reason: str | None = None):
        tx = self._open()
        return self.append(
            "aborted",
            transaction_id=tx.transaction_id,
            reason=reason,
            authority=authority,
            control_epoch=self.control_epoch,
            episode_id=tx.episode_id,
            episode_role=tx.episode_role,
        )

    def note(self, code: str, detail: str = "", *, authority, transaction_id=None):
        return self.append(
            "note",
            transaction_id=transaction_id,
            authority=authority,
            control_epoch=self.control_epoch,
            note={"code": code, "detail": detail},
        )

    # ---------------------------------------------------------- recovery

    def recover(
        self,
        *,
        authority: dict,
        episode_id: str | None = None,
        episode_role: str | None = None,
        episode_result: dict | None = None,
    ) -> Recovery:
        """Make the journal safe after a restart (see the module text).
        ``authority`` must be a ``recovery`` principal. The orchestrator may
        pass the result of the episode the crash cut short (task_outcome
        unknown, stop_reason orchestrator_crash): it goes on the committed
        move to FAULT_LOCKED, which then names that episode."""
        if self.corrupt is not None:
            return Recovery(
                "FAULT_LOCKED", "journal_corrupt", None, detail=self.corrupt
            )
        previous = self.state
        if self.completed:
            return Recovery("COMPLETED", None, previous)
        if authority.get("principal_kind") != "recovery":
            raise JournalRefused(
                "E_PROTOCOL", "recovery is done by a recovery principal"
            )
        written = []
        dangling = self._replay.open_tx
        if dangling is not None:
            written.append(self.abort(authority=authority, reason="recovery_ambiguous"))
            written.append(
                self.note(
                    "crash_before_commit",
                    f"{dangling.action.kind} from {dangling.from_state} to "
                    f"{dangling.to_state} was prepared and never committed",
                    authority=authority,
                    transaction_id=dangling.transaction_id,
                )
            )
        epoch = self.control_epoch + 1
        written.append(
            self.prepare(
                "FAULT_LOCKED",
                "recovery_ambiguous",
                kind="none",
                authority=authority,
                control_epoch=epoch,
                params={"previous_state": previous},
                episode_id=episode_id if episode_result is not None else None,
                episode_role=episode_role if episode_result is not None else None,
            )
        )
        written.append(self.commit(authority=authority, episode_result=episode_result))
        return Recovery(
            "FAULT_LOCKED",
            "recovery_ambiguous",
            previous,
            dangling.transaction_id if dangling else None,
            written,
        )
