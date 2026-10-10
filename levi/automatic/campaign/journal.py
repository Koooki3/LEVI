"""The durable journal of one campaign: its single source of truth.

Files in ``<LEVI_AERI_HOME>/campaigns/<campaign_id>/``:

- ``journal.jsonl``: append-only ``levi.aeri.campaign_event.v1`` lines, one
  ``write`` + ``fsync`` each, hash-chained (``prev_sha256``) like a run's
  state journal (``levi.automatic.journal``, whose primitives this module
  reuses: durable writes, the directory ``flock``, the torn-line rules).
  Line 0 is the campaign header (``campaign_sha256``, ``settings_sha256``,
  robot, arms, segment count, schedule kind).
- ``plan.json``: the campaign plan (``campaign.plan.json`` of the job
  folder), written once before the header and checked against the header's
  ``campaign_sha256`` whenever the journal is created or opened.
- ``state.json``: a snapshot derived after each commit; never read back.
- ``torn/``: the bytes of a torn last line, kept before the file is cut.

Transactions are the run journal's: ``prepared`` (synced before any side
effect) -> the conductor acts -> ``acknowledged`` -> ``committed`` (the
state changes only now) or ``aborted``. The idempotency key of a
transaction is ``<id>:s<NN>:<state entered>``. Starting a child run
(``launch_run``) is the one non-idempotent action: a key once prepared is
never prepared again; a later attempt has a new attempt number and is
allowed only after the previous attempt was closed without a commit.

Reading back: a last line without its newline, or one that cannot be
decoded, is torn (a crash) and set aside; any other bad line (a broken
chain, a contract or protocol violation) makes the journal corrupt: it is
never written to again and its effective state is ``FAULT_LOCKED``.
"""

import contextlib
import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from levi.domain import aeri

from ..journal import _Lock, fsync_dir, line_sha, params_sha, write_durable

JOURNAL = "journal.jsonl"
SNAPSHOT = "state.json"
TORN = "torn"
PLAN_FILE = "plan.json"
INITIAL_STATE = "DRAFT"
ZEROS = "0" * 64
SCHEMA = aeri.CAMPAIGN_SCHEMAS["campaign_event"]
TERMINAL = ("REPORTED", "ABORTED")
# Where the campaign waits for a person.
HUMAN_STATES = ("WAIT_HUMAN", "FAULT_LOCKED", "PAUSED")
# Where a person's resume may lead (``conductor.resume_target``).
RESUME_TARGETS = ("SEGMENT_PREPARE", "ARM_RUNNING", "ANALYZING")
SIDE_EFFECTS = ("policy_stop", "policy_start", "launch_run", "report")
FAULT_REASONS = ("child_fault", "child_crashed", "stop_rule_faults")
FORWARD = {
    "DRAFT": ("PLANNED",),
    "PLANNED": ("SEGMENT_PREPARE", "PAUSED"),
    "SEGMENT_PREPARE": ("POLICY_STOP",),
    "POLICY_STOP": ("POLICY_START",),
    "POLICY_START": ("POLICY_READY",),
    "POLICY_READY": ("ENV_CONFIRM",),
    "ENV_CONFIRM": ("ARM_RUNNING",),
    "ARM_RUNNING": ("SEGMENT_SEALED",),
    "SEGMENT_SEALED": ("SEGMENT_PREPARE", "ANALYZING", "PAUSED"),
    "ANALYZING": ("REPORTED",),
    "PAUSED": RESUME_TARGETS,
    "WAIT_HUMAN": RESUME_TARGETS,
    "FAULT_LOCKED": RESUME_TARGETS,
}
# The only action each forward move may carry (others: ``none``).
ACTIONS = {
    ("POLICY_STOP", "POLICY_START"): ("policy_stop", "none"),
    ("POLICY_START", "POLICY_READY"): ("policy_start", "none"),
    ("ENV_CONFIRM", "ARM_RUNNING"): ("launch_run",),
    ("ANALYZING", "REPORTED"): ("report",),
}
RESERVED = frozenset(
    {
        "schema",
        "minor",
        "campaign_id",
        "emitted_wall_ns",
        "sequence_no",
        "record",
        "mono_ns",
        "clock_domain",
        "prev_sha256",
    }
)


class CampaignJournalError(Exception):
    """``code``: E_BUSY, E_EXISTS, E_EMPTY, E_PLAN, E_CORRUPT, E_BROKEN,
    E_SEQ, E_PROTOCOL, E_CONTRACT."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class CampaignJournalBusy(CampaignJournalError):
    def __init__(self, holder):
        super().__init__("E_BUSY", f"another writer holds the campaign: {holder}")
        self.holder = holder


def refuse(text: str):
    raise CampaignJournalError("E_PROTOCOL", text)


def allowed(from_state: str, to_state: str) -> bool:
    if from_state in TERMINAL:
        return False
    if to_state in ("WAIT_HUMAN", "FAULT_LOCKED", "ABORTED"):
        return True
    return to_state in FORWARD.get(from_state, ())


@dataclass
class Launch:
    transaction_id: str
    attempt: int
    run_id: str
    executed: str | None = None  # the acknowledgement
    committed: bool = False
    closed: bool = False


@dataclass
class Replay:
    """The campaign as its journal tells it, rebuilt line by line."""

    campaign_id: str | None = None
    header: aeri.CampaignEvent | None = None
    state: str = INITIAL_STATE
    segment: int | None = None
    open_tx: aeri.CampaignEvent | None = None
    open_ack: aeri.CampaignEvent | None = None
    control_epoch: int = 0
    transactions: set = field(default_factory=set)
    attempted: dict = field(default_factory=dict)  # key -> tx id
    commands: dict = field(default_factory=dict)  # command id -> [sequence_no]
    sealed: dict = field(default_factory=dict)  # segment -> counts dict
    launches: dict = field(default_factory=dict)  # segment -> [Launch]
    # Child faults since the last sealed segment (a recovered, sealed
    # segment does not count towards faults in a row).
    faults_since_seal: int = 0
    accepted_short: set = field(default_factory=set)
    wait_reason: str | None = None  # why it waits (last move into a human state)
    last_reason: str | None = None  # the reason of the last commit
    serving: int | None = None  # the segment whose policy was found ready
    pending_pause: str | None = None  # command id of a pause not yet taken
    last_mono: dict = field(default_factory=dict)

    @property
    def segments(self) -> int:
        return self.header.header.segments if self.header else 0

    def launched(self, segment: int) -> Launch | None:
        found = self.launches.get(segment) or []
        return found[-1] if found else None

    def check(self, event: aeri.CampaignEvent) -> None:
        if event.record == "campaign_header":
            if self.header is not None:
                refuse("a second campaign header")
            return
        if self.header is None:
            refuse("the first line is the campaign header")
        if event.campaign_id != self.campaign_id:
            refuse(f"{event.campaign_id} in the journal of {self.campaign_id}")
        if event.control_epoch < self.control_epoch:
            refuse("control_epoch went back")
        last = self.last_mono.get(event.clock_domain)
        if last is not None and event.mono_ns < last:
            refuse("mono_ns went back within one clock domain")
        record, tx = event.record, event.transaction_id
        if record == "prepared":
            self._check_prepared(event)
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
            for name in ("from_state", "to_state", "reason", "segment", "run_id"):
                if getattr(event, name) != getattr(prepared, name):
                    refuse(f"a commit keeps the {name} it prepared")
            if prepared.action.kind != "none":
                if self.open_ack is None:
                    refuse(f"{tx} acts and is not acknowledged")
                if self.open_ack.ack.executed != "yes":
                    refuse(f"{tx} was not executed: abort it")

    def _check_prepared(self, event) -> None:
        if self.open_tx is not None:
            refuse(f"{self.open_tx.transaction_id} is still open")
        if event.transaction_id in self.transactions:
            refuse(f"{event.transaction_id} was used before")
        if event.from_state != self.state:
            refuse(f"from_state {event.from_state} but the state is {self.state}")
        to, action, who = event.to_state, event.action, event.authority
        if not allowed(self.state, to):
            refuse(f"{self.state} does not lead to {to}")
        kinds = ACTIONS.get((self.state, to), ("none",))
        if action.kind not in kinds:
            refuse(
                f"{self.state} -> {to} acts with {'/'.join(kinds)}, not {action.kind}"
            )
        operator = who.principal_kind == "operator" and who.command_id is not None
        if who.principal_kind == "recovery" and (
            to not in ("WAIT_HUMAN", "FAULT_LOCKED") or action.kind != "none"
        ):
            refuse(
                "a recovery only moves to WAIT_HUMAN or FAULT_LOCKED, acting on nothing"
            )
        if to == "ABORTED" and not operator:
            refuse("only an operator's command aborts a campaign")
        leaving = to not in HUMAN_STATES and to != "ABORTED"
        if self.state in HUMAN_STATES and leaving and not operator:
            refuse(f"only an operator's command leaves {self.state}")
        if self.state == "FAULT_LOCKED" and to == "WAIT_HUMAN":
            refuse("FAULT_LOCKED is left only by an operator's command")
        if to == "ANALYZING" and len(self.sealed) != self.segments:
            refuse("ANALYZING needs every segment sealed")
        self._check_segment(event)
        if action.kind == "launch_run" and not operator:
            refuse("only an operator's command starts a child run")
        if action.non_idempotent:
            if action.idempotency_key in self.attempted:
                refuse(
                    "a non-idempotent action is never repeated: tried in "
                    f"{self.attempted[action.idempotency_key]}"
                )
            before = self.launches.get(event.segment) or []
            if action.attempt != len(before) + 1:
                refuse(
                    f"attempt {action.attempt}: the next attempt is {len(before) + 1}"
                )
            if before and (
                before[-1].committed
                or not before[-1].closed
                or before[-1].executed == "yes"
            ):
                refuse("a child run that was started is never started again")

    def _check_segment(self, event) -> None:
        to, segment = event.to_state, event.segment
        if to not in aeri.CAMPAIGN_SEGMENT_STATES:
            return
        sealed_next = max(self.sealed, default=0) + 1
        if to == "SEGMENT_PREPARE":
            if segment != sealed_next or segment > self.segments:
                refuse(f"the next segment to prepare is {sealed_next}, not {segment}")
            return
        if segment != self.segment:
            refuse(f"segment {segment}, but the campaign is in segment {self.segment}")
        if segment in self.sealed:
            refuse(f"segment {segment} is sealed")
        watch = to == "ARM_RUNNING" and self.state in HUMAN_STATES
        if watch and self.launched(segment) is None:
            refuse("ARM_RUNNING is watched again only after a launch")

    def apply(self, event: aeri.CampaignEvent) -> None:
        self.check(event)
        record = event.record
        if record == "campaign_header":
            self.header, self.campaign_id = event, event.campaign_id
        elif record == "prepared":
            self.open_tx, self.open_ack = event, None
            self.transactions.add(event.transaction_id)
            self.attempted.setdefault(
                event.action.idempotency_key, event.transaction_id
            )
            if event.action.kind == "launch_run":
                self.launches.setdefault(event.segment, []).append(
                    Launch(event.transaction_id, event.action.attempt, event.run_id)
                )
        elif record == "acknowledged":
            self.open_ack = event
            launch = self._launch_of(event.transaction_id)
            if launch is not None:
                launch.executed = event.ack.executed
        elif record == "committed":
            self._commit(event)
        elif record == "aborted":
            launch = self._launch_of(event.transaction_id)
            if launch is not None:
                launch.closed = True
            self.open_tx = self.open_ack = None
        elif record == "note":
            who = event.authority
            if event.note.code == "pause_requested" and who.command_id is not None:
                self.pending_pause = who.command_id
        self.control_epoch = max(self.control_epoch, event.control_epoch)
        self.last_mono[event.clock_domain] = event.mono_ns
        command = event.authority.command_id
        if command is not None:
            self.commands.setdefault(command, []).append(event.sequence_no)

    def _launch_of(self, tx):
        prepared = self.open_tx
        if prepared is None or prepared.transaction_id != tx:
            return None
        if prepared.action.kind != "launch_run":
            return None
        return self.launches[prepared.segment][-1]

    def _commit(self, event) -> None:
        launch = self._launch_of(event.transaction_id)
        if launch is not None:
            launch.committed = launch.closed = True
        to = event.to_state
        self.state = to
        self.last_reason = event.reason
        if event.segment is not None and to in aeri.CAMPAIGN_SEGMENT_STATES:
            self.segment = event.segment
        if to == "SEGMENT_SEALED":
            counts = event.counts.model_dump() if event.counts else {}
            self.sealed[event.segment] = counts
            self.faults_since_seal = 0
        if event.reason == "operator_accept_short":
            self.accepted_short.add(event.segment)
        if to == "ENV_CONFIRM":
            self.serving = event.segment
        if to == "POLICY_START" and event.reason == "policy_stopped":
            self.serving = None
        if to in HUMAN_STATES or to in TERMINAL:
            self.wait_reason = event.reason
            self.serving = None
            if event.reason in FAULT_REASONS:
                self.faults_since_seal += 1
        if to == "PAUSED" or to in TERMINAL:
            self.pending_pause = None
        self.open_tx = self.open_ack = None


@dataclass
class Scan:
    events: list = field(default_factory=list)
    lines: list = field(default_factory=list)
    good_bytes: int = 0
    torn: bytes = b""
    corrupt: str | None = None
    replay: Replay = field(default_factory=Replay)

    @property
    def effective_state(self) -> str:
        return "FAULT_LOCKED" if self.corrupt else self.replay.state


def scan(path: Path) -> Scan:
    """Read a campaign journal without changing it."""
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
        try:
            raw = json.loads(line)
        except (ValueError, RecursionError):
            if last:
                result.torn = data[offset:]
            else:
                result.corrupt = f"line {index}: not JSON"
            return result
        if not isinstance(raw, dict) or raw.get("prev_sha256") != previous:
            result.corrupt = f"line {index}: the hash chain is broken"
            return result
        try:
            event = aeri.parse_campaign_event(line)
        except aeri.AeriError as exc:
            result.corrupt = f"line {index}: {exc}"
            return result
        problem = None
        if event.sequence_no != index:
            problem = f"line {index} says sequence_no {event.sequence_no}"
        else:
            try:
                result.replay.apply(event)
            except CampaignJournalError as exc:
                problem = f"line {index}: {exc.detail}"
        if problem is not None:
            result.corrupt = problem
            return result
        result.events.append(event)
        result.lines.append(line)
        previous = line_sha(line)
        offset += len(line) + 1
        result.good_bytes = offset
    result.torn = tail
    return result


# --- the plan copy -------------------------------------------------------------------


def read_plan(directory) -> dict | None:
    path = Path(directory) / PLAN_FILE
    try:
        value = json.loads(path.read_bytes())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise CampaignJournalError("E_PLAN", f"{path}: {exc}") from None
    from .spec import campaign_sha256

    if not isinstance(value, dict) or campaign_sha256(value) != value.get(
        "campaign_sha256"
    ):
        raise CampaignJournalError(
            "E_PLAN", f"{path} does not match its campaign_sha256"
        )
    return value


def keep_plan(directory, plan: dict) -> None:
    from .spec import campaign_sha256

    if campaign_sha256(plan) != plan.get("campaign_sha256"):
        raise CampaignJournalError("E_PLAN", "the plan does not hash to its sha256")
    kept = read_plan(directory)
    if kept is not None:
        if kept["campaign_sha256"] != plan["campaign_sha256"]:
            raise CampaignJournalError("E_PLAN", f"{PLAN_FILE} holds another plan")
        return
    text = json.dumps(plan, sort_keys=True, indent=1, ensure_ascii=False) + "\n"
    write_durable(Path(directory) / PLAN_FILE, text.encode())


# --- the writer ----------------------------------------------------------------------


class CampaignJournal:
    """The one writer of a campaign's journal. Use ``create`` or ``open``."""

    def __init__(self, directory, lock, result: Scan, clock, domain):
        self.directory = Path(directory)
        self.path = self.directory / JOURNAL
        self._lock = lock
        self._events = result.events
        self._replay = result.replay
        self.corrupt = result.corrupt
        self._last_sha = line_sha(result.lines[-1]) if result.lines else ZEROS
        self.minor = (
            result.events[0].minor
            if result.events
            else aeri.CAMPAIGN_MINORS["campaign_event"]
        )
        self._clock = clock
        self.clock_domain = domain
        self._mutex = threading.Lock()
        self._broken: str | None = None
        self._fd = None
        self._campaign_id = None
        if self.corrupt is None:
            self._fd = os.open(self.path, os.O_WRONLY | os.O_APPEND)

    @staticmethod
    def _take_lock(directory: Path) -> _Lock:
        try:
            return _Lock(directory)
        except Exception as exc:  # the run journal's busy error
            holder = getattr(exc, "holder", None)
            if getattr(exc, "code", None) == "E_BUSY":
                raise CampaignJournalBusy(holder) from None
            raise

    @staticmethod
    def _cut_torn(directory: Path, result: Scan) -> None:
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
        directory,
        *,
        plan: dict,
        authority: dict,
        levi_commit: str | None = None,
        clock: Callable[[], int] = time.monotonic_ns,
        clock_domain: str | None = None,
    ) -> "CampaignJournal":
        """Start the journal of a planned campaign (refused when one exists):
        ``plan.json`` first, then the header."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        fsync_dir(directory.parent)
        lock = cls._take_lock(directory)
        journal = None
        try:
            result = scan(directory / JOURNAL)
            if result.events or result.corrupt:
                raise CampaignJournalError("E_EXISTS", f"{directory} has a journal")
            keep_plan(directory, plan)
            path = directory / JOURNAL
            if path.exists():
                cls._cut_torn(directory, result)
            else:
                os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644))
                fsync_dir(directory)
            journal = cls(
                directory, lock, result, clock, clock_domain or aeri.host_clock_domain()
            )
            journal._campaign_id = plan["campaign_id"]
            schedule = plan["schedule"]
            journal.append(
                "campaign_header",
                authority=authority,
                control_epoch=0,
                header={
                    "campaign_sha256": plan["campaign_sha256"],
                    "settings_sha256": plan["settings_sha256"],
                    "robot": plan["robot"],
                    "arms": list(schedule["arms"]),
                    "segments": len(plan["children"]),
                    "schedule_kind": schedule["kind"],
                    "conclusion_level": schedule["conclusion_level"],
                    "levi_commit": levi_commit,
                },
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
        directory,
        *,
        clock: Callable[[], int] = time.monotonic_ns,
        clock_domain: str | None = None,
    ) -> "CampaignJournal":
        """Take over an existing journal: a torn last line is set aside; the
        kept ``plan.json`` must be the header's plan. A corrupt journal opens
        read-only (``corrupt``)."""
        directory = Path(directory)
        if not (directory / JOURNAL).is_file():
            raise CampaignJournalError("E_EMPTY", f"{directory} has no journal")
        lock = cls._take_lock(directory)
        try:
            result = scan(directory / JOURNAL)
            if result.corrupt is None:
                if not result.events:
                    raise CampaignJournalError("E_EMPTY", f"{directory}: no header")
                kept = read_plan(directory)
                header = result.events[0].header
                if kept is None or kept["campaign_sha256"] != header.campaign_sha256:
                    raise CampaignJournalError(
                        "E_PLAN", f"{PLAN_FILE} is missing or is not the header's plan"
                    )
                cls._cut_torn(directory, result)
            return cls(
                directory, lock, result, clock, clock_domain or aeri.host_clock_domain()
            )
        except BaseException:
            lock.close()
            raise

    @staticmethod
    def read(directory) -> Scan:
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
    def campaign_id(self) -> str | None:
        return self._replay.campaign_id or self._campaign_id

    @property
    def replay(self) -> Replay:
        return self._replay

    @property
    def state(self) -> str:
        return "FAULT_LOCKED" if self.corrupt else self._replay.state

    @property
    def control_epoch(self) -> int:
        return self._replay.control_epoch

    @property
    def open_transaction(self):
        return self._replay.open_tx

    @property
    def next_seq(self) -> int:
        return len(self._events)

    @property
    def events(self) -> list:
        return list(self._events)

    def plan(self) -> dict:
        return read_plan(self.directory)

    def by_command(self, command_id: str) -> list:
        return [self._events[n] for n in self._replay.commands.get(command_id, [])]

    # ----------------------------------------------------------- write

    def append(self, record: str, *, expected_seq: int | None = None, **fields):
        reserved = set(fields) & RESERVED
        if reserved:
            raise CampaignJournalError(
                "E_CONTRACT", f"{sorted(reserved)} are set by the journal"
            )
        with self._mutex:
            if self.corrupt is not None:
                raise CampaignJournalError("E_CORRUPT", self.corrupt)
            if self._broken is not None:
                raise CampaignJournalError("E_BROKEN", self._broken)
            if self._fd is None:
                raise CampaignJournalError("E_BROKEN", "the journal is closed")
            sequence = len(self._events)
            if expected_seq is not None and expected_seq != sequence:
                raise CampaignJournalError(
                    "E_SEQ", f"expected line {expected_seq}, the next is {sequence}"
                )
            value = {
                "schema": SCHEMA,
                "minor": self.minor,
                "campaign_id": self.campaign_id,
                "emitted_wall_ns": time.time_ns(),
                "sequence_no": sequence,
                "record": record,
                "mono_ns": self._clock(),
                "clock_domain": self.clock_domain,
                "prev_sha256": self._last_sha,
                **fields,
            }
            try:
                event = aeri.validate_campaign_event(value)
            except aeri.AeriError as exc:
                raise CampaignJournalError("E_CONTRACT", str(exc)) from None
            self._replay.check(event)
            line = aeri.dump(event)
            if b"\n" in line or len(line) >= aeri.MAX_BYTES:
                raise CampaignJournalError("E_CONTRACT", "the line is too large")
            self._write(line + b"\n")
            self._replay.apply(event)
            self._events.append(event)
            self._last_sha = line_sha(line)
            if record in ("committed", "campaign_header"):
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
            self._broken = f"a write failed ({exc}); reopen the journal"
            raise CampaignJournalError("E_BROKEN", self._broken) from None

    def _snapshot(self) -> None:
        replay = self._replay
        value = {
            "campaign_id": self.campaign_id,
            "state": self.state,
            "segment": replay.segment,
            "sealed": sorted(replay.sealed),
            "sequence_no": len(self._events) - 1,
            "control_epoch": self.control_epoch,
            "wait_reason": replay.wait_reason,
            "last_line_sha256": self._last_sha,
            "derived_from": JOURNAL,
        }
        with contextlib.suppress(OSError):
            write_durable(
                self.directory / SNAPSHOT,
                (json.dumps(value, sort_keys=True, indent=1) + "\n").encode(),
            )

    # ---------------------------------------------- transaction helpers

    def prepare(
        self,
        to_state: str,
        reason: str,
        *,
        kind: str = "none",
        authority: dict,
        control_epoch: int | None = None,
        segment: int | None = None,
        run_id: str | None = None,
        params=None,
        attempt: int = 1,
        expected_seq: int | None = None,
    ):
        sequence = self.next_seq
        return self.append(
            "prepared",
            expected_seq=expected_seq,
            transaction_id=f"{self.campaign_id}:tx{sequence}",
            from_state=self.state,
            to_state=to_state,
            reason=reason,
            authority=authority,
            control_epoch=self.control_epoch
            if control_epoch is None
            else control_epoch,
            segment=segment,
            run_id=run_id,
            action={
                "kind": kind,
                "idempotency_key": aeri.campaign_key(
                    self.campaign_id, segment, to_state, attempt
                ),
                "non_idempotent": kind in aeri.CAMPAIGN_NON_IDEMPOTENT,
                "attempt": attempt,
                "params_sha256": params_sha(params),
            },
        )

    def _open(self):
        tx = self._replay.open_tx
        if tx is None:
            raise CampaignJournalError("E_PROTOCOL", "no transaction is open")
        return tx

    def acknowledge(self, executed: str, source: str, detail_code: str, *, authority):
        tx = self._open()
        return self.append(
            "acknowledged",
            transaction_id=tx.transaction_id,
            authority=authority,
            control_epoch=self.control_epoch,
            segment=tx.segment,
            run_id=tx.run_id,
            ack={"executed": executed, "source": source, "detail_code": detail_code},
        )

    def commit(self, *, authority, counts: dict | None = None):
        tx = self._open()
        return self.append(
            "committed",
            transaction_id=tx.transaction_id,
            from_state=tx.from_state,
            to_state=tx.to_state,
            reason=tx.reason,
            authority=authority,
            control_epoch=self.control_epoch,
            segment=tx.segment,
            run_id=tx.run_id,
            counts=counts,
        )

    def abort(self, *, authority, reason: str | None = None):
        tx = self._open()
        return self.append(
            "aborted",
            transaction_id=tx.transaction_id,
            reason=reason,
            authority=authority,
            control_epoch=self.control_epoch,
            segment=tx.segment,
            run_id=tx.run_id,
        )

    def note(self, code: str, detail: str = "", *, authority, transaction_id=None):
        return self.append(
            "note",
            transaction_id=transaction_id,
            authority=authority,
            control_epoch=self.control_epoch,
            note={"code": code, "detail": detail[:300]},
        )

    def move(self, to_state: str, reason: str, *, authority, segment=None, **extra):
        """A ``none`` transaction, committed at once (no side effect)."""
        self.prepare(to_state, reason, authority=authority, segment=segment, **extra)
        return self.commit(authority=authority)
