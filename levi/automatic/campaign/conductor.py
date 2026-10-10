"""The campaign conductor: moves a planned campaign through its states and
recovers it after a crash (design X3 §1.6).

States (``aeri.CAMPAIGN_STATES``)::

    DRAFT -> PLANNED -> { SEGMENT_PREPARE -> POLICY_STOP -> POLICY_START
        -> POLICY_READY -> ENV_CONFIRM (a person) -> ARM_RUNNING (child run)
        -> SEGMENT_SEALED } x segments -> ANALYZING -> REPORTED
    aside: PAUSED (only at a segment boundary), WAIT_HUMAN, FAULT_LOCKED,
           ABORTED (an operator's command only)

``advance`` takes at most one step and returns what happened; ``run``
repeats it until the campaign waits. The world is reached only through
four interfaces, so tests drive fakes and the integration plugs the real
ones in: ``PolicyHost`` (stop, start, passive readiness of a policy
service; ``switch.SystemdPolicyHost``), ``RunLauncher`` (start a child run,
read its state; the launch core, ``launch.py``), ``Confirmations`` (a
person's answers, each bound to one question by a nonce) and
``SessionWriter`` (the C2 session that says ``waiting_reset`` while the
policy is switched).

Safety rules:

- One campaign per robot: ``<LEVI_AERI_HOME>/campaign-<robot>.lock``
  (``flock``, held while the conductor lives).
- A child run is never started again by itself: the launch is the one
  non-idempotent action; after a crash or a failed child the campaign
  waits for a person (``WAIT_HUMAN``), who resumes it.
- After a crash, ``attach`` aborts a dangling transaction and waits for a
  person: ``FAULT_LOCKED`` when the dangling transaction had a side effect
  (policy stop or start, launch), ``WAIT_HUMAN`` otherwise; a campaign that
  was already waiting for a person (or in ``ENV_CONFIRM``, which asks one)
  stays where it was. Nothing moves until a person answers.
- A pause takes effect only at a segment boundary; a stop rule (faults in a
  row, unplanned interventions) waits for a person, never skips an arm.
"""

import fcntl
import hmac
import json
import os
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..journal import process_identity
from . import spec
from .journal import (
    HUMAN_STATES,
    SIDE_EFFECTS,
    TERMINAL,
    CampaignJournal,
    CampaignJournalError,
)

HOME_ENV = "LEVI_AERI_HOME"
# States a recovered campaign may stay in: nothing in them acts without a
# person (ENV_CONFIRM asks one again) or touches the robot (ANALYZING).
REST_STATES = ("ENV_CONFIRM", "ANALYZING", *HUMAN_STATES)
ENV_CHECKS = ("arm_still", "layout_ready")
STOP_RULE_REASONS = ("stop_rule_faults", "stop_rule_interventions")
DEFAULT_READY_TIMEOUT_S = 600.0


def aeri_home() -> Path:
    """``$LEVI_AERI_HOME``, default ``~/.levi-aeri``."""
    found = os.environ.get(HOME_ENV)
    return Path(found) if found else Path.home() / ".levi-aeri"


def campaign_dir(home, campaign_id: str) -> Path:
    return Path(home) / "campaigns" / campaign_id


class ConductorError(Exception):
    """``code``: E_BUSY (another campaign holds the robot), E_CORRUPT,
    E_PLAN."""

    def __init__(self, code: str, detail: str, holder=None):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.holder = holder


# --- the interfaces -------------------------------------------------------------------


@dataclass(frozen=True)
class ArmRef:
    campaign_id: str
    arm: str
    unit: str
    checkpoint_dir: str
    config: str
    port: int
    cfg_scale: float | None = None


@dataclass(frozen=True)
class HostResult:
    """``executed``: yes, no or unknown. ``mismatch``: the policy port is
    held by a process that is not this arm's unit (``FAULT_LOCKED``)."""

    executed: str
    detail_code: str
    mismatch: bool = False
    detail: str = ""


@dataclass(frozen=True)
class Readiness:
    status: str  # ready | not_ready | mismatch
    detail: str = ""


@dataclass(frozen=True)
class ChildStatus:
    """A child run as its own journal says: absent, running, wait_human
    (its own planned wait, e.g. a person resets the scene), fault_locked,
    completed or crashed (its process is gone before COMPLETED)."""

    state: str
    episodes_complete: int = 0
    faults: int = 0
    unplanned_interventions: int = 0
    detail: str = ""


@dataclass(frozen=True)
class LaunchResult:
    executed: str  # yes | no | unknown
    detail_code: str


@dataclass(frozen=True)
class ConfirmRequest:
    """One question to a person; an answer must carry its ``nonce`` (an
    answer written before the question cannot)."""

    request_id: str
    nonce: str
    kind: str  # env_confirm | resume
    campaign_id: str
    segment: int | None
    arm_code: str | None
    slots: tuple = ()
    wait_reason: str | None = None
    checks: tuple = ()


@dataclass(frozen=True)
class Confirmation:
    request_id: str
    nonce: str
    command_id: str
    principal_id: str  # opaque, never a name
    decision: str  # confirm | resume | relaunch | abort
    checks: Mapping = field(default_factory=dict)


@dataclass(frozen=True)
class PauseRequest:
    command_id: str
    principal_id: str


class PolicyHost(Protocol):
    def stop(self, arms: list) -> HostResult: ...
    def start(self, arm: ArmRef) -> HostResult: ...
    def passive_ready(self, arm: ArmRef) -> Readiness: ...


class RunLauncher(Protocol):
    def launch(self, job_path: Path, run_id: str) -> LaunchResult: ...
    def status(self, run_id: str) -> ChildStatus: ...
    def robot_busy(self) -> str | None: ...


class Confirmations(Protocol):
    def ask(self, request: ConfirmRequest) -> None: ...
    def take(self, request: ConfirmRequest) -> Confirmation | None: ...
    def withdraw(self, request: ConfirmRequest) -> None: ...
    def pause_requested(self) -> PauseRequest | None: ...


class SessionWriter(Protocol):
    def waiting_reset(self, campaign_id: str, segment: int) -> None: ...
    def release(self, campaign_id: str) -> None: ...


class NoSession:
    def waiting_reset(self, campaign_id, segment):
        pass

    def release(self, campaign_id):
        pass


@dataclass(frozen=True)
class Outcome:
    kind: str  # moved | waiting | done
    state: str
    detail: str = ""


# --- one campaign per robot ------------------------------------------------------------


class RobotLock:
    """``flock`` on ``<home>/campaign-<robot>.lock``; the file says who
    holds it. The kernel drops it when the holder dies."""

    def __init__(self, home, robot: str, campaign_id: str):
        home = Path(home)
        home.mkdir(parents=True, exist_ok=True)
        self.path = home / f"campaign-{robot}.lock"
        self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                holder = json.loads(self.path.read_text() or "null")
            except (OSError, ValueError):
                holder = None
            os.close(self._fd)
            self._fd = None
            raise ConductorError(
                "E_BUSY", f"robot {robot} runs another campaign: {holder}", holder
            ) from None
        me = json.dumps({"pid": os.getpid(), "campaign_id": campaign_id}).encode()
        os.ftruncate(self._fd, 0)
        os.pwrite(self._fd, me, 0)
        os.fsync(self._fd)

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


# --- the conductor ---------------------------------------------------------------------


class Conductor:
    def __init__(
        self,
        journal: CampaignJournal,
        lock: RobotLock,
        job_dir: Path,
        *,
        host: PolicyHost,
        launcher: RunLauncher,
        confirmations: Confirmations,
        session: SessionWriter | None = None,
        reporter=None,
        principal_id: str = "conductor",
        ready_timeout_s: float = DEFAULT_READY_TIMEOUT_S,
        clock=time.monotonic,
    ):
        self.journal = journal
        self.lock = lock
        self.job_dir = Path(job_dir)
        self.plan = journal.plan()
        self.host = host
        self.launcher = launcher
        self.confirmations = confirmations
        self.session = session or NoSession()
        self.reporter = reporter
        self.principal_id = principal_id
        self.session_id = f"conductor-{os.getpid()}-{time.time_ns()}"
        self.ready_timeout_s = ready_timeout_s
        self._clock = clock
        self._process = process_identity()
        self._request: ConfirmRequest | None = None
        self._ready_since: float | None = None
        self.campaign_id = self.plan["campaign_id"]
        self._children = {c["segment"]: c for c in self.plan["children"]}
        block = self.plan["campaign"]
        self._codes = _arm_codes(block, self.plan["schedule"])

    # ------------------------------------------------------------ open

    @classmethod
    def create(cls, job_dir, *, home=None, planner=None, levi_commit=None, **deps):
        """Start the journal of a planned campaign (``job_dir`` holds its
        ``campaign.plan.json`` and child files) and freeze its plan."""
        job_dir = Path(job_dir)
        plan = spec.read_plan(job_dir / spec.PLAN_FILE)
        spec.verify_children(plan, job_dir, planner)
        home = Path(home) if home is not None else aeri_home()
        lock = RobotLock(home, plan["robot"], plan["campaign_id"])
        try:
            authority = _authority("conductor", deps.get("principal_id", "conductor"))
            journal = CampaignJournal.create(
                campaign_dir(home, plan["campaign_id"]),
                plan=plan,
                authority=authority,
                levi_commit=levi_commit,
            )
        except BaseException:
            lock.close()
            raise
        return cls(journal, lock, job_dir, **deps)

    @classmethod
    def attach(cls, campaign_id: str, job_dir, *, home=None, planner=None, **deps):
        """Take over a campaign after a stop or a crash: check the journal
        and the child files, then recover (see the module text)."""
        home = Path(home) if home is not None else aeri_home()
        folder = campaign_dir(home, campaign_id)
        try:
            kept = json.loads((folder / "plan.json").read_bytes())
            robot = kept["robot"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ConductorError(
                "E_PLAN", f"{folder}: no readable plan ({exc})"
            ) from None
        lock = RobotLock(home, robot, campaign_id)
        try:
            journal = CampaignJournal.open(folder)
        except BaseException:
            lock.close()
            raise
        try:
            if journal.corrupt is not None:
                raise ConductorError("E_CORRUPT", journal.corrupt)
            spec.verify_children(journal.plan(), job_dir, planner)
            conductor = cls(journal, lock, job_dir, **deps)
            conductor.recover()
            return conductor
        except BaseException:
            journal.close()
            lock.close()
            raise

    def close(self) -> None:
        if self._request is not None:
            self.confirmations.withdraw(self._request)
            self._request = None
        self.session.release(self.campaign_id)
        self.journal.close()
        self.lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ helpers

    @property
    def state(self) -> str:
        return self.journal.state

    @property
    def replay(self):
        return self.journal.replay

    def _auth(self, kind="conductor", principal_id=None, command_id=None) -> dict:
        return {
            "principal_kind": kind,
            "principal_id": principal_id or self.principal_id,
            "session_id": self.session_id,
            "process": self._process,
            "command_id": command_id,
        }

    def arm_of(self, segment: int) -> str:
        return self._children[segment]["arm"]

    def arm_ref(self, arm: str) -> ArmRef:
        policy = self.plan["campaign"]["arms"][arm]["policy_forward"]
        return ArmRef(
            campaign_id=self.campaign_id,
            arm=arm,
            unit=f"levi-policy-{self.campaign_id}-{arm}",
            checkpoint_dir=policy["checkpoint_dir"],
            config=policy["config"],
            port=policy["port"],
            cfg_scale=policy.get("cfg_scale"),
        )

    def _all_arms(self) -> list:
        return [self.arm_ref(a) for a in sorted(self.plan["campaign"]["arms"])]

    def _moved(self, detail="") -> Outcome:
        return Outcome("moved", self.state, detail)

    def _waiting(self, detail="") -> Outcome:
        return Outcome("waiting", self.state, detail)

    def _to_human(self, target: str, reason: str, segment=None) -> Outcome:
        self.journal.move(target, reason, authority=self._auth(), segment=segment)
        self.session.release(self.campaign_id)
        self._drop_request()
        self._ready_since = None
        return self._moved(reason)

    def _drop_request(self) -> None:
        if self._request is not None:
            self.confirmations.withdraw(self._request)
            self._request = None

    # ------------------------------------------------------------ recovery

    def recover(self) -> str:
        """After a restart: abort a dangling transaction and wait for a
        person (module text). Returns the state reached."""
        journal = self.journal
        replay = journal.replay
        state = replay.state
        if state in TERMINAL:
            return state
        who = self._auth("recovery")
        epoch = journal.control_epoch + 1
        dangling = replay.open_tx
        if dangling is not None:
            journal.abort(authority=who, reason="recovery_ambiguous")
            journal.note(
                "crash_before_commit",
                f"{dangling.action.kind} from {dangling.from_state} to "
                f"{dangling.to_state} was prepared and never committed",
                authority=who,
                transaction_id=dangling.transaction_id,
            )
        if state == "DRAFT":
            # Freezing the plan only checks files: done now, so the campaign
            # waits for a person below like every other one.
            self._in_draft()
            state = self.state
        if dangling is not None and (
            dangling.action.kind in SIDE_EFFECTS or state == "FAULT_LOCKED"
        ):
            target, reason = "FAULT_LOCKED", "recovery_ambiguous"
        elif dangling is None and state in REST_STATES:
            journal.note("conductor_attached", state, authority=who)
            return state
        else:
            target, reason = "WAIT_HUMAN", "conductor_restarted"
        journal.prepare(
            target,
            reason,
            authority=who,
            control_epoch=epoch,
            segment=replay.segment,
        )
        journal.commit(authority=who)
        self.session.release(self.campaign_id)
        return self.state

    # ------------------------------------------------------------ stepping

    def run(self, max_steps: int = 1000) -> Outcome:
        outcome = self._waiting()
        for _ in range(max_steps):
            outcome = self.advance()
            if outcome.kind != "moved":
                return outcome
        return outcome

    def advance(self) -> Outcome:
        if self.journal.corrupt is not None:
            return Outcome("done", "FAULT_LOCKED", "the journal is corrupt")
        self._poll_pause()
        state = self.state
        if state in TERMINAL:
            return Outcome("done", state)
        return getattr(self, f"_in_{state.lower()}")()

    def _poll_pause(self) -> None:
        found = self.confirmations.pause_requested()
        if found is None or self.state in TERMINAL:
            return
        if self.journal.by_command(found.command_id):
            return  # already on record
        self.journal.note(
            "pause_requested",
            "takes effect at the next segment boundary",
            authority=self._auth("operator", found.principal_id, found.command_id),
        )

    def _pause_due(self) -> bool:
        return self.replay.pending_pause is not None

    # --- DRAFT, PLANNED

    def _in_draft(self) -> Outcome:
        spec.verify_children(self.plan, self.job_dir)
        self.journal.move("PLANNED", "plan_frozen", authority=self._auth())
        return self._moved("plan_frozen")

    def _in_planned(self) -> Outcome:
        if self._pause_due():
            self.journal.move("PAUSED", "pause_at_boundary", authority=self._auth())
            return self._moved("paused")
        self.journal.move(
            "SEGMENT_PREPARE", "segment_started", authority=self._auth(), segment=1
        )
        return self._moved()

    # --- one segment

    def _in_segment_prepare(self) -> Outcome:
        segment = self.replay.segment
        busy = self.launcher.robot_busy()
        if busy:
            return self._waiting(f"robot busy: {busy}")
        self.journal.move(
            "POLICY_STOP", "robot_free", authority=self._auth(), segment=segment
        )
        return self._moved()

    def _in_policy_stop(self) -> Outcome:
        segment = self.replay.segment
        arm = self.arm_of(segment)
        serving = self.replay.serving
        if serving is not None and self.arm_of(serving) == arm:
            self.journal.move(
                "POLICY_START", "same_arm_kept", authority=self._auth(), segment=segment
            )
            return self._moved("same_arm_kept")
        self.session.waiting_reset(self.campaign_id, segment)
        self.journal.prepare(
            "POLICY_START",
            "policy_stopped",
            kind="policy_stop",
            authority=self._auth(),
            segment=segment,
            params={"stop": sorted(self.plan["campaign"]["arms"])},
        )
        result = _guarded(lambda: self.host.stop(self._all_arms()), HostResult)
        return self._finish_host(result, "policy_stop_failed", segment)

    def _in_policy_start(self) -> Outcome:
        segment = self.replay.segment
        if self.replay.last_reason == "same_arm_kept":
            self.journal.move(
                "POLICY_READY", "same_arm_kept", authority=self._auth(), segment=segment
            )
            self._ready_since = None
            return self._moved("same_arm_kept")
        arm = self.arm_ref(self.arm_of(segment))
        self.journal.prepare(
            "POLICY_READY",
            "policy_started",
            kind="policy_start",
            authority=self._auth(),
            segment=segment,
            params={"arm": arm.arm, "unit": arm.unit},
        )
        result = _guarded(lambda: self.host.start(arm), HostResult)
        self._ready_since = None
        return self._finish_host(result, "policy_start_failed", segment)

    def _finish_host(self, result: HostResult, failed: str, segment) -> Outcome:
        who = self._auth()
        self.journal.acknowledge(
            result.executed, "policy_host", result.detail_code, authority=who
        )
        if result.mismatch:
            self.journal.abort(authority=who, reason="listener_mismatch")
            return self._to_human("FAULT_LOCKED", "listener_mismatch", segment)
        if result.executed == "yes":
            self.journal.commit(authority=who)
            return self._moved()
        self.journal.abort(authority=who, reason=failed)
        return self._to_human("WAIT_HUMAN", failed, segment)

    def _check_ready(self, segment) -> Outcome | None:
        """None when the arm's policy is ready; otherwise the outcome."""
        found = self.host.passive_ready(self.arm_ref(self.arm_of(segment)))
        if found.status == "ready":
            self._ready_since = None
            return None
        if found.status == "mismatch":
            return self._to_human("FAULT_LOCKED", "listener_mismatch", segment)
        now = self._clock()
        if self._ready_since is None:
            self._ready_since = now
        if now - self._ready_since >= self.ready_timeout_s:
            return self._to_human("WAIT_HUMAN", "policy_not_ready", segment)
        return self._waiting(f"policy not ready: {found.detail}")

    def _in_policy_ready(self) -> Outcome:
        segment = self.replay.segment
        blocked = self._check_ready(segment)
        if blocked is not None:
            return blocked
        self.journal.move(
            "ENV_CONFIRM", "policy_ready", authority=self._auth(), segment=segment
        )
        self.session.release(self.campaign_id)
        return self._moved()

    def _ask(self, kind: str, segment, checks=()) -> ConfirmRequest:
        current = self._request
        if current is not None and current.kind == kind and current.segment == segment:
            return current
        self._drop_request()
        slots = ()
        arm_code = None
        if segment is not None and segment in self._children:
            arm_code = self._codes[self.arm_of(segment)]
            slots = tuple(self.plan["schedule"]["segments"][segment - 1]["slots"])
        tag = f"s{segment:02d}" if segment is not None else "c"
        request = ConfirmRequest(
            request_id=f"{self.campaign_id}:{tag}:{kind}:{self.journal.next_seq}",
            nonce=secrets.token_urlsafe(16),
            kind=kind,
            campaign_id=self.campaign_id,
            segment=segment,
            arm_code=arm_code,
            slots=slots,
            wait_reason=self.replay.wait_reason if kind == "resume" else None,
            checks=tuple(checks),
        )
        self._request = request
        self.confirmations.ask(request)
        return request

    def _answer(self, request: ConfirmRequest) -> Confirmation | None:
        """The answer to exactly this request, or None (an answer to another
        request, with a wrong nonce or a used command id is dropped and
        noted)."""
        found = self.confirmations.take(request)
        if found is None:
            return None
        good = found.request_id == request.request_id and hmac.compare_digest(
            str(found.nonce), request.nonce
        )
        repeated = good and bool(self.journal.by_command(found.command_id))
        if not good or repeated:
            self.journal.note(
                "repeated_command" if repeated else "unsolicited_answer",
                found.request_id[:200],
                authority=self._auth(),
            )
            return None
        # One answer per question: the next one needs a new question.
        self.confirmations.withdraw(request)
        self._request = None
        return found

    def _in_env_confirm(self) -> Outcome:
        segment = self.replay.segment
        request = self._ask("env_confirm", segment, ENV_CHECKS)
        answer = self._answer(request)
        if answer is None:
            return self._waiting("waiting for the operator to confirm the scene")
        operator = self._auth("operator", answer.principal_id, answer.command_id)
        if answer.decision == "abort":
            self.journal.move("ABORTED", "operator_abort", authority=operator)
            return self._moved("aborted")
        missing = [c for c in ENV_CHECKS if answer.checks.get(c) is not True]
        if answer.decision != "confirm" or missing:
            self.journal.note(
                "confirmations_missing",
                ",".join(missing) or answer.decision,
                authority=operator,
            )
            return self._waiting("the confirmation is incomplete")
        blocked = self._check_ready(segment)
        if blocked is not None:
            if blocked.kind == "waiting":
                return self._to_human("WAIT_HUMAN", "policy_not_ready", segment)
            return blocked
        child = self._children[segment]
        run_id = child["run_id"]
        if self.launcher.status(run_id).state != "absent":
            return self._to_human("FAULT_LOCKED", "foreign_run", segment)
        attempt = len(self.replay.launches.get(segment) or []) + 1
        self.journal.prepare(
            "ARM_RUNNING",
            "run_launched",
            kind="launch_run",
            authority=operator,
            segment=segment,
            run_id=run_id,
            attempt=attempt,
            params={"file": child["file"], "plan_sha256": child["plan_sha256"]},
        )
        result = _guarded(
            lambda: self.launcher.launch(self.job_dir / child["file"], run_id),
            LaunchResult,
        )
        who = self._auth()
        self.journal.acknowledge(
            result.executed, "run_launcher", result.detail_code, authority=who
        )
        if result.executed == "yes":
            self.journal.commit(authority=who)
            return self._moved("launched")
        self.journal.abort(authority=who, reason="launch_failed")
        return self._to_human("WAIT_HUMAN", "launch_failed", segment)

    def _in_arm_running(self) -> Outcome:
        segment = self.replay.segment
        launch = self.replay.launched(segment)
        status = self.launcher.status(launch.run_id)
        if status.state in ("running", "wait_human"):
            return self._waiting(f"child run {status.state}")
        if status.state == "completed":
            self.journal.prepare(
                "SEGMENT_SEALED",
                "run_completed",
                authority=self._auth(),
                segment=segment,
                run_id=launch.run_id,
            )
            self.journal.commit(
                authority=self._auth(),
                counts={
                    "episodes_complete": status.episodes_complete,
                    "faults": status.faults,
                    "unplanned_interventions": status.unplanned_interventions,
                },
            )
            return self._moved("sealed")
        if status.state == "absent":
            return self._to_human("WAIT_HUMAN", "child_missing", segment)
        reason = "child_fault" if status.state == "fault_locked" else "child_crashed"
        if self._faults_in_a_row(segment) >= self._rules()["consecutive_faults"]:
            reason = "stop_rule_faults"
        return self._to_human("WAIT_HUMAN", reason, segment)

    def _rules(self) -> dict:
        return self.plan["campaign"]["stop_rules"]

    def _faults_in_a_row(self, segment: int) -> int:
        faults = self.replay.fault_segments | {segment}
        count = 0
        while segment in faults:
            count += 1
            segment -= 1
        return count

    def _in_segment_sealed(self) -> Outcome:
        segment = self.replay.segment
        arm = self.arm_of(segment)
        sealed = self.replay.sealed
        of_arm = [s for s in sealed if self.arm_of(s) == arm]
        total = sum(sealed[s].get("unplanned_interventions", 0) for s in of_arm)
        before = total - sealed[segment].get("unplanned_interventions", 0)
        limit = self._rules()["unplanned_interventions_per_arm"]
        if before <= limit < total:
            return self._to_human("WAIT_HUMAN", "stop_rule_interventions", segment)
        if self._pause_due():
            self.journal.move("PAUSED", "pause_at_boundary", authority=self._auth())
            return self._moved("paused")
        if segment >= self.replay.segments:
            self.journal.move("ANALYZING", "campaign_complete", authority=self._auth())
            return self._moved("complete")
        self.journal.move(
            "SEGMENT_PREPARE",
            "segment_started",
            authority=self._auth(),
            segment=segment + 1,
        )
        return self._moved()

    def _in_analyzing(self) -> Outcome:
        if self.reporter is None:
            return self._waiting("no report generator is configured")
        self.journal.prepare(
            "REPORTED", "report_written", kind="report", authority=self._auth()
        )
        who = self._auth()
        try:
            code = self.reporter(self.plan, self.journal.events)
        except Exception as exc:  # noqa: BLE001 - recorded, the campaign waits
            self.journal.acknowledge("no", "reporter", "report_failed", authority=who)
            self.journal.abort(authority=who)
            self.journal.note("report_failed", str(exc)[:300], authority=who)
            return self._waiting("the report failed")
        self.journal.acknowledge(
            "yes", "reporter", code or "report_written", authority=who
        )
        self.journal.commit(authority=who)
        return self._moved("reported")

    # --- a person's resume

    def resume_target(self, relaunch: bool = False) -> tuple[str, int | None]:
        """Where a person's resume leads (never into a launch): the next
        segment to prepare; the watched child run when one was started (or
        may have been); ANALYZING when every segment is sealed."""
        replay = self.replay
        following = max(replay.sealed, default=0) + 1
        if following > replay.segments:
            return "ANALYZING", None
        launch = replay.launched(following)
        if launch is None:
            return "SEGMENT_PREPARE", following
        if launch.committed or launch.executed == "yes":
            return "ARM_RUNNING", following
        if launch.executed == "no":
            return "SEGMENT_PREPARE", following
        # Prepared, then unknown or never acknowledged: start it again only
        # when the person asks and the run is not there.
        if relaunch and self.launcher.status(launch.run_id).state == "absent":
            return "SEGMENT_PREPARE", following
        return "ARM_RUNNING", following

    def _waiting_for_person(self) -> Outcome:
        reason = self.replay.wait_reason
        request = self._ask("resume", self.replay.segment)
        answer = self._answer(request)
        if answer is None:
            return self._waiting(f"waiting for a person ({reason})")
        operator = self._auth("operator", answer.principal_id, answer.command_id)
        if answer.decision == "abort":
            self.journal.move("ABORTED", "operator_abort", authority=operator)
            return self._moved("aborted")
        if answer.decision not in ("resume", "relaunch"):
            self.journal.note(
                "confirmations_missing", answer.decision, authority=operator
            )
            return self._waiting("unknown decision")
        if (
            reason in STOP_RULE_REASONS
            and answer.checks.get("override_stop_rule") is not True
        ):
            self.journal.note("stop_rule_needs_override", reason, authority=operator)
            return self._waiting("a stop rule needs override_stop_rule")
        target, segment = self.resume_target(answer.decision == "relaunch")
        run_id = None
        if target == "ARM_RUNNING":
            run_id = self.replay.launched(segment).run_id
        self.journal.move(
            target,
            "operator_resume",
            authority=operator,
            segment=segment,
            run_id=run_id,
        )
        self._ready_since = None
        return self._moved(f"resumed to {target}")

    _in_wait_human = _waiting_for_person
    _in_fault_locked = _waiting_for_person
    _in_paused = _waiting_for_person


def _guarded(call, kind):
    """A side effect that raised is one whose outcome is unknown: the
    transaction is acknowledged ``unknown`` and closed, never left open."""
    try:
        return call()
    except Exception as exc:  # noqa: BLE001 - recorded as unknown
        return kind(
            "unknown",
            "raised",
            **({"detail": str(exc)[:300]} if kind is HostResult else {}),
        )


def _authority(kind: str, principal_id: str) -> dict:
    return {
        "principal_kind": kind,
        "principal_id": principal_id,
        "session_id": f"conductor-{os.getpid()}-{time.time_ns()}",
        "process": process_identity(),
        "command_id": None,
    }


def _arm_codes(block: dict, schedule: dict) -> dict:
    """The codes a blinded operator sees (X1, X2...), in a seeded order;
    the arm letters when blinding is off."""
    from .schedule import permuted

    arms = sorted(block["arms"])
    if (block.get("blinding") or {}).get("operator") == "none":
        return {arm: arm for arm in arms}
    order = permuted(arms, schedule["seed"], "codes")
    return {arm: f"X{index}" for index, arm in enumerate(order, 1)}


__all__ = [
    "ArmRef",
    "CampaignJournalError",
    "ChildStatus",
    "Conductor",
    "ConductorError",
    "ConfirmRequest",
    "Confirmation",
    "Confirmations",
    "HostResult",
    "LaunchResult",
    "Outcome",
    "PauseRequest",
    "PolicyHost",
    "Readiness",
    "RobotLock",
    "RunLauncher",
    "SessionWriter",
    "aeri_home",
]
