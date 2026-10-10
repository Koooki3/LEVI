"""The automatic evaluation run (T-C-06): one orchestrator drives the 13
AERI states through the durable journal, with the robot, policy, recorder
and the A-side providers injected.

Every state change is a journal transaction checked against the state
machine first (``state_machine.check_transition``):

    prepare (fsync'd) -> act -> acknowledge (yes | no | unknown) -> commit

``executed: no`` aborts the transaction and moves the run to
``WAIT_HUMAN`` or ``FAULT_LOCKED``; ``unknown`` always to ``FAULT_LOCKED``.
Nothing is retried automatically. Motion needs the current token of the
motion fence: it is issued only after the transaction that allows the
motion is on disk (policy steps after the commit into an active state; a
home between its prepared and acknowledged lines) and revoked before the
next transaction is checked, so a late chunk or a late thread cannot move
the robot. Physical actions (``policy_steps``, ``home``) carry a step the
orchestrator assigns per episode (0: the episode's steps, 1: its home) and
episode numbers are never reused (read back from the journal), so the
journal's idempotency key refuses a second attempt in any epoch, after any
recovery.

**Threads.** ``run()`` belongs to one thread. ``stop()`` and ``resume()``
may be called from any thread: one re-entrant state lock serialises every
"read the state -> check -> prepare -> commit" sequence, so no transaction
is ever prepared from a state other than the one it was checked against.
A stop during an episode only sets a flag that the loop reads at every
step and before every new episode.

**Exceptions.** Any exception escaping the loop (an adapter, the journal)
first revokes the motion token, then holds the robot, closes an open
transaction as ``executed: unknown`` and moves the run to ``FAULT_LOCKED``
(``watchdog_timeout``), and only then propagates. When even that cannot be
written, the orchestrator locks itself in memory (``halted``) and refuses
to run again; a restart (``restore``) recovers from the journal.

**Notes.** Audit notes from the control loop (dropped chunks, dropped
judgements, bad events) are counted in memory and written as one summary
line per code at the next transaction boundary, at most
``note_lines_per_episode`` lines per episode (then one
``notes_suppressed`` line). Nothing is fsync'd per note inside the loop.

After a restart (``restore``) the journal's recovery moves every run that
had not completed to ``FAULT_LOCKED`` (``recovery_ambiguous``); nothing is
replayed. Only an operator's ``resume`` (a command id, the expected
sequence number and both confirmations) leads back to ``PREFLIGHT``, and
from there through a fresh initial-state check.

The control loop never blocks on a judgement: requests are submitted and
collected without waiting; a chunk is waited for at most its deadline. The
final judgement and scene assessments are waited for (the policy is
quiesced by then), each with its own timeout.

Whether a scene may skip a reset is decided by the Initial State Contract
(``scene_assessment.arbitrate``) and what to do otherwise by the reset
strategy (``reset_manager``), both T-C-08.
"""

import threading
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from levi.domain import aeri

from . import reset_manager as rm
from . import scene_assessment as sa
from . import state_machine as sm
from .adapters import events as ev
from .journal import Journal, JournalRefused, process_identity
from .recorder import EvidenceStore
from .termination import TerminationArbiter, TerminationConfig, verification_of

HOLD_STATES = ("WAIT_HUMAN", "FAULT_LOCKED", "COMPLETED")
SCENE_VERIFICATION = {
    "ready": "verified",
    "reset_required": "contradicted",
    "unknown": "undecided",
    "unavailable": "unavailable",
}
SCENE_RESET = {
    "ready": "succeeded",
    "reset_required": "failed",
    "unknown": "unknown",
    "unavailable": "unknown",
}
MEMORY_NOTES = 5_000
# Notes always written, whatever the episode's budget (one line per code
# per flush): they explain a lock, a lost acknowledgement or a disputed
# verdict.
IMPORTANT_NOTES = frozenset(
    {
        "orchestrator_exception",
        "hold_failed",
        "motion_unacknowledged",
        "recorder_error",
        "early_stop_disputed",
        "stop_command_lost",
    }
)
# Actions that stop motion: they act before the backlog of notes is written.
URGENT_KINDS = frozenset({"hold", "policy_quiesce"})
# How long stop() tries for the state lock before it only registers the stop.
STOP_LOCK_WAIT_S = 0.05
# A person's scene check is collected in slices this long (a stop is seen
# between two).
HUMAN_SLICE_NS = 500_000_000


class ConfigError(ValueError):
    pass


class OrchestratorHalted(RuntimeError):
    """The orchestrator locked itself in memory (the journal could not take
    the move to FAULT_LOCKED): restart it with ``restore``."""


@dataclass(frozen=True)
class RunConfig:
    run_id: str
    plan_sha256: str
    episodes: int
    forward_folder: str = "forward"
    reset_folder: str = "reset"
    forward_max_steps: int = 120
    reset_max_steps: int = 80
    reset_enabled: bool = True
    max_reset_attempts: int = 1
    # Scene unknown or unavailable never skips a reset: run it, or ask a person.
    on_scene_unknown: str = "reset"
    # After an operator's stop: home first (the design's path), or wait for
    # a person where the arm stands. The home still passes the safety check.
    home_after_operator_stop: bool = True
    control_period_ns: int = 100_000_000
    chunk_timeout_ns: int = 500_000_000
    chunk_failure_limit: int = 3
    # A judgement request still unanswered this long is withdrawn.
    judge_request_timeout_ns: int = 3_000_000_000
    final_judge_timeout_ns: int = 5_000_000_000
    scene_timeout_ns: int = 5_000_000_000
    scene_violation_limit: int = 3
    home_timeout_ns: int = 30_000_000_000
    # Observations with unchanged camera frames before evidence is unusable.
    camera_stall_limit: int = 10
    note_lines_per_episode: int = 32
    action_dims: int = 7
    termination: TerminationConfig = field(default_factory=TerminationConfig)
    specs: dict = field(default_factory=lambda: dict(ev.SPECS))
    # T-C-08: how a scene that is not ready is put back (reset_manager), and
    # the task's Initial State Contract (scene_assessment; None: the scene
    # provider's decision stands, the behaviour before T-C-08).
    reset_strategy: str = "single_reset_policy"
    initial_state: sa.InitialStateContract | None = None
    principal_id: str = "aeri-orchestrator"
    session_id: str = "s-aeri"
    # Who assesses the scene (design X2 §1.2): the machine provider, or
    # (human_assisted only) a person answering every required predicate on
    # a frame captured for the check (``adapters.human``); their check
    # waits up to ``human_scene_timeout_ns``, then counts as unavailable.
    scene_check: str = "provider"
    human_scene_timeout_ns: int = 600_000_000_000

    def __post_init__(self):
        if type(self.episodes) is not int or self.episodes < 0:
            raise ConfigError("episodes is a whole number >= 0")
        for name in (
            "forward_max_steps",
            "reset_max_steps",
            "control_period_ns",
            "chunk_timeout_ns",
            "chunk_failure_limit",
            "camera_stall_limit",
            "judge_request_timeout_ns",
            "final_judge_timeout_ns",
            "scene_timeout_ns",
            "human_scene_timeout_ns",
            "scene_violation_limit",
            "home_timeout_ns",
            "note_lines_per_episode",
            "action_dims",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ConfigError(f"{name} is a whole number >= 1")
        if self.on_scene_unknown not in ("reset", "wait_human"):
            raise ConfigError("on_scene_unknown is reset or wait_human")
        if type(self.max_reset_attempts) is not int or self.max_reset_attempts < 0:
            raise ConfigError("max_reset_attempts is a whole number >= 0")
        for name in ("reset_enabled", "home_after_operator_stop"):
            if type(getattr(self, name)) is not bool:
                raise ConfigError(f"{name} is true or false")
        if not isinstance(self.termination, TerminationConfig):
            raise ConfigError("termination is a TerminationConfig")
        if self.initial_state is not None and not isinstance(
            self.initial_state, sa.InitialStateContract
        ):
            raise ConfigError("initial_state is an InitialStateContract or None")
        try:
            self.strategy()
        except rm.StrategyError as exc:
            raise ConfigError(str(exc)) from None
        if self.scene_check not in aeri.SCENE_CHECKS:
            raise ConfigError(f"scene_check is one of {', '.join(aeri.SCENE_CHECKS)}")
        if self.scene_check == "operator_attested":
            if self.reset_strategy != "human_assisted":
                raise ConfigError(
                    "scene_check operator_attested belongs to the human_assisted "
                    "strategy"
                )
            if self.initial_state is None:
                raise ConfigError(
                    "scene_check operator_attested needs an Initial State "
                    "Contract: the person answers its predicates"
                )

    @property
    def scene_wait_ns(self) -> int:
        """How long one scene check is waited for."""
        if self.scene_check == "operator_attested":
            return self.human_scene_timeout_ns
        return self.scene_timeout_ns

    def strategy(self):
        """The reset strategy these settings describe."""
        return rm.strategy_for(
            self.reset_strategy,
            enabled=self.reset_enabled,
            max_attempts=self.max_reset_attempts,
            on_unknown=self.on_scene_unknown,
        )


@dataclass
class Episode:
    role: str
    episode_id: str
    number: int
    policy_epoch: int
    rollout: object = None
    stream: str | None = None
    token: sm.MotionToken | None = None
    arbiter: TerminationArbiter | None = None
    chunks: int = 0
    last_step: int = 0
    steps_done: int = 0
    evidence_ok: bool = True
    stop_authority: dict | None = None
    stop_judgement: str | None = None
    sealed: str | None = None  # "complete" once sealed, "incomplete" once aborted
    result_written: bool = False
    scene_decision: str | None = None
    judgement_ids: list = field(default_factory=list)


@dataclass
class TxResult:
    executed: str
    detail: str
    prepared: object = None


@dataclass
class CommandResult:
    ok: bool
    code: str
    state: str
    repeated: bool = False
    sequence_no: int | None = None


def _scene_facts(message) -> dict:
    """What an evidence record keeps of a parsed scene message."""
    if message.kind != "scene":
        return {"provider_decision": "unavailable", "provider_code": message.code}
    return {
        "assessment_id": message.assessment_id,
        "provider": message.provider,
        "provider_decision": message.decision,
        "contract": {"id": message.contract_id, "version": message.contract_version},
        "failed_predicates": list(message.failed_predicates),
        "unknown_predicates": list(message.unknown_predicates),
        "unknown_reason": message.unknown_reason,
        "predicate_results": [
            {"name": p.name, "value": p.value, "required": p.required}
            for p in message.predicate_results
        ],
        "evidence_refs": [
            {"kind": r.kind, "ref": r.ref, "sha256": r.sha256}
            for r in message.evidence_refs
        ],
        "observed_ns": message.observed_ns,
        "produced_ns": message.produced_ns,
    }


def _code(text: str) -> str:
    cleaned = "".join(c if c.isalnum() or c == "_" else "_" for c in str(text).lower())
    cleaned = cleaned.strip("_") or "unspecified"
    if not cleaned[0].isalpha():
        cleaned = f"c_{cleaned}"
    return cleaned[:64]


class Orchestrator:
    def __init__(
        self,
        journal: Journal,
        *,
        config: RunConfig,
        robot,
        policy,
        recorder,
        events,
        verifier,
        scene,
        clock,
        fence: sm.MotionFence,
        crash_hook: Callable[[str], None] | None = None,
        listener: Callable[[str, dict], None] | None = None,
        evidence=True,
    ):
        self.journal = journal
        self.config = config
        self.robot = robot
        self.policy = policy
        self.recorder = recorder
        self.events = events
        self.verifier = verifier
        self.scene = scene
        self.clock = clock
        self.fence = fence
        self.crash_hook = crash_hook
        # Told every committed state (the session files, C2); never decides.
        self.listener = listener
        self.strategy = config.strategy()
        contract = config.initial_state
        self._specs = {**config.specs, **(contract.spec() if contract else {})}
        self.notes: list = []  # (code, detail), the latest MEMORY_NOTES
        self.note_counts: Counter = Counter()
        self._pending: dict = {}  # code -> [count, first detail]
        self._episode_note_lines = 0
        self._suppressed = 0
        self._lock = threading.RLock()
        self._process = process_identity()
        # Stops not yet consumed: command id -> registration generation.
        # Guarded by its own small lock, never held across an adapter call.
        self._stop_lock = threading.Lock()
        self._stops: dict[str, int] = {}
        self._stop_gen = 0
        # Stop ids consumed since the last resume (``repeated`` if sent again).
        self._consumed_stops: set = set()
        self._violation_hold = False
        self._scene_violations = 0
        self._reset_attempts = 0
        self._last_forward: str | None = None
        self.halted: str | None = None
        self.ctx: Episode | None = None
        self.recovery = None
        # Scene evidence (T-CL-04): ``True`` keeps it in the run's folder,
        # an ``EvidenceStore`` there, ``None``/``False`` nowhere.
        self.evidence = None
        if evidence is True:
            try:
                self.evidence = EvidenceStore(journal.directory)
            except OSError as exc:
                self._note("evidence_write_failed", f"open: {exc}")
        elif evidence:
            self.evidence = evidence
        attach = getattr(scene, "use_evidence", None)
        if self.evidence is not None and callable(attach):
            # A person's check keeps its frames before it asks (T-CL-03).
            attach(self.evidence)
        if self.evidence is not None and contract is not None:
            try:
                self.evidence.keep_contract(contract.describe())
            except Exception as exc:  # noqa: BLE001 - evidence only
                self._note("evidence_write_failed", f"contract: {exc}")
        self._read_back()

    # --- construction --------------------------------------------------------------

    @classmethod
    def create(
        cls, directory, config: RunConfig, *, plan: dict | None = None, **parts
    ) -> "Orchestrator":
        """Start a run: its header names the reset mode and the scene check;
        ``plan`` (the job's normalised plan, hashing to the config's
        ``plan_sha256``) is kept as the run folder's ``plan.json``."""
        clock = parts["clock"]
        process = process_identity()
        journal = Journal.create(
            directory,
            run_id=config.run_id,
            plan_sha256=config.plan_sha256,
            authority=cls._authority_of(config, process, "orchestrator"),
            clock=clock,
            clock_domain=clock.domain,
            reset_mode=config.reset_strategy,
            scene_check=config.scene_check,
            plan=plan,
        )
        orchestrator = cls(journal, config=config, **parts)
        orchestrator._tell(orchestrator.state)
        return orchestrator

    @classmethod
    def restore(
        cls, directory, config: RunConfig, *, plan: dict | None = None, **parts
    ) -> "Orchestrator":
        """Take over a run after a restart: the journal's recovery runs
        first (never replays anything). A config whose reset mode or scene
        check differs from the run header's is refused (``E_PLAN``, with a
        ``run_header_mismatch`` note)."""
        clock = parts["clock"]
        journal = Journal.open(
            directory,
            plan_sha256=config.plan_sha256,
            clock=clock,
            clock_domain=clock.domain,
            reset_mode=config.reset_strategy,
            scene_check=config.scene_check,
            authority=cls._authority_of(config, process_identity(), "recovery"),
            plan=plan,
        )
        orchestrator = cls(journal, config=config, **parts)
        found = journal.recover(
            authority=cls._authority_of(config, orchestrator._process, "recovery"),
            **orchestrator._crash_result(),
        )
        orchestrator.recovery = found
        orchestrator._recover_rollouts()
        orchestrator._tell(orchestrator.state)
        return orchestrator

    def _recover_rollouts(self) -> None:
        """Rollouts whose seal the journal did not commit become
        ``incomplete_*`` (a recorder that writes folders has ``recover``)."""
        recover = getattr(self.recorder, "recover", None)
        if recover is None or self.journal.corrupt:
            return
        from .recorder import sealed_episodes

        try:
            renamed = recover(sealed_episodes(self.journal.events))
        except Exception as exc:  # noqa: BLE001 - the run is locked anyway
            self._note("recorder_error", f"recover: {exc}")
            return
        if renamed:
            self._note("rollouts_recovered", ", ".join(renamed)[:300])

    def _tell(self, state: str, reason: str = "", episode_id=None) -> None:
        if self.listener is None:
            return
        try:
            self.listener(
                state,
                {
                    "control_epoch": self.journal.control_epoch,
                    "stopped": state == "COMPLETED" and reason == "operator_stop",
                    "reason": reason,
                    "episode_id": episode_id,
                },
            )
        except Exception as exc:  # noqa: BLE001 - a session file never stops a run
            self._note("session_write_failed", f"{type(exc).__name__}: {exc}")

    def close(self) -> None:
        self.journal.close()

    def _read_back(self) -> None:
        """Counters that survive restarts, from the journal itself. An
        episode counts as done once its rollout was sealed complete."""
        self.numbers = {"forward": 0, "reset": 0}
        self.policy_epoch = 0
        self.completed_forward = 0
        for event in self.journal.events:
            if event.episode_id:
                _, role, number = aeri.episode_parts(event.episode_id)
                self.numbers[role] = max(self.numbers[role], number)
            if event.policy_epoch is not None:
                self.policy_epoch = max(self.policy_epoch, event.policy_epoch)
            if self._counts(event):
                self.completed_forward += 1

    def _crash_result(self) -> dict:
        """The result of the episode a crash cut short, for the recovery's
        move to FAULT_LOCKED: the last committed state is inside an episode
        whose result was not written. ``{}`` otherwise (no episode open, the
        run completed, or the journal is corrupt). The outcome is unknown,
        the stop reason ``orchestrator_crash``; a home that was prepared and
        never committed counts as failed (it may or may not have run)."""
        journal = self.journal
        if journal.corrupt or journal.completed:
            return {}
        events = journal.events
        last = next((e for e in reversed(events) if e.record == "committed"), None)
        if (
            last is None
            or last.to_state not in aeri.EPISODE_STATES
            or not last.episode_id
        ):
            return {}
        episode = last.episode_id
        mine = [e for e in events if e.episode_id == episode]
        if any(e.episode_result is not None for e in mine):
            return {}
        committed = {e.transaction_id for e in mine if e.record == "committed"}
        sealed = any(
            e.record == "prepared"
            and e.action.kind == "recorder_seal"
            and e.transaction_id in committed
            for e in mine
        )
        homed = any(
            e.record == "prepared"
            and e.action.kind == "home"
            and e.transaction_id not in committed
            for e in mine
        )
        _, role, number = aeri.episode_parts(episode)
        folder = (
            self.config.forward_folder
            if role == "forward"
            else self.config.reset_folder
        )
        return {
            "episode_id": episode,
            "episode_role": role,
            "episode_result": {
                "task_outcome": "unknown",
                "stop_reason": "orchestrator_crash",
                "goal_verification": "unavailable",
                "robot_home": "failed" if homed else "not_attempted",
                "scene_reset": "unknown",
                "label_kind": "autonomous_verdict",
                "judgement_ids": [],
                "rollout": {
                    "task_folder": folder,
                    "demo": f"demo_{number:04d}",
                    "sealed": "complete" if sealed else "incomplete",
                },
            },
        }

    @staticmethod
    def _counts(event) -> bool:
        return (
            event.record == "committed"
            and (event.from_state, event.to_state) == ("FORWARD_FINALIZE", "ROBOT_HOME")
            and event.episode_result is not None
            and event.episode_result.rollout.sealed == "complete"
        )

    # --- authority -----------------------------------------------------------------

    @staticmethod
    def _authority_of(config, process, kind, command_id=None) -> dict:
        return {
            "principal_kind": kind,
            "principal_id": config.principal_id if kind != "operator" else "operator",
            "session_id": config.session_id,
            "process": process,
            "command_id": command_id,
        }

    def _auth(self, kind="orchestrator", command_id=None) -> dict:
        return self._authority_of(self.config, self._process, kind, command_id)

    # --- views ---------------------------------------------------------------------

    @property
    def state(self) -> str:
        if self.journal.corrupt or self.halted:
            return "FAULT_LOCKED"
        return self.journal.state

    def _now(self) -> int:
        return self.clock.now()

    def _crash(self, point: str) -> None:
        if self.crash_hook is not None:
            self.crash_hook(point)

    # --- notes ---------------------------------------------------------------------

    def _note(self, code: str, detail: str = "") -> None:
        """Count a note; it reaches the journal at the next flush."""
        code = _code(code)
        detail = str(detail)[:300]
        self.notes.append((code, detail))
        if len(self.notes) > MEMORY_NOTES:
            del self.notes[: len(self.notes) - MEMORY_NOTES]
        self.note_counts[code] += 1
        found = self._pending.setdefault(code, [0, detail])
        found[0] += 1

    def _flush_notes(self, *, final: bool = False) -> None:
        """One journal line per pending code, within the episode's budget;
        ``IMPORTANT_NOTES`` beyond it. Suppressed notes are summed in one
        ``notes_suppressed`` line when the budget is first reached and on a
        final flush (the end of ``run()``), so their count is never lost."""
        if self.journal.corrupt or self.halted:
            return
        if not self._pending and not (final and self._suppressed):
            return
        with self._lock:
            pending, self._pending = self._pending, {}
            budget = self.config.note_lines_per_episode
            for code in sorted(pending):
                count, first = pending[code]
                text = first if count == 1 else f"{count}x, first: {first}"
                if code in IMPORTANT_NOTES:
                    self.journal.note(code, text[:300], authority=self._auth())
                elif self._episode_note_lines < budget:
                    self.journal.note(code, text[:300], authority=self._auth())
                    self._episode_note_lines += 1
                else:
                    self._suppressed += count
            first_time = self._episode_note_lines == budget
            if self._suppressed and (first_time or final):
                self.journal.note(
                    "notes_suppressed",
                    f"{self._suppressed} notes over the budget of {budget}",
                    authority=self._auth(),
                )
                self._episode_note_lines = budget + 1
                self._suppressed = 0

    # --- one transaction -------------------------------------------------------------

    def _tx(
        self,
        to: str,
        reason: str,
        *,
        kind: str = "none",
        act=None,
        authority=None,
        ctx: Episode | None = None,
        step: int | None = None,
        result=None,
        evidence=(),
        expected_seq: int | None = None,
    ) -> TxResult:
        auth = authority or self._auth()
        non_idempotent = kind in sm.NON_IDEMPOTENT_KINDS
        role = ctx.role if ctx else None
        with self._lock:
            # Nothing moves on an old authorisation once a new transaction
            # starts, not even when this one turns out to be refused.
            self.fence.revoke()
            if self.halted:
                raise OrchestratorHalted(self.halted)
            # A move that stops motion acts first; the notes follow it.
            urgent = kind in URGENT_KINDS or to == "FAULT_LOCKED"
            if not urgent:
                self._flush_notes()
            # Checked under the lock against the state the prepare will use.
            sm.check_transition(
                self.state,
                to,
                reason=reason,
                principal_kind=auth["principal_kind"],
                command_id=auth.get("command_id"),
                action_kind=kind,
                non_idempotent=non_idempotent,
                step=step,
                episode_role=role,
                episode_result=result is not None,
            )
            moving = (
                to in sm.MOTION_STATES
                or self.state in sm.MOTION_STATES
                or non_idempotent
            )
            epoch = self.journal.control_epoch + (1 if moving else 0)
            self._crash("before_prepared")
            prepared = self.journal.prepare(
                to,
                reason,
                kind=kind,
                authority=auth,
                control_epoch=epoch,
                non_idempotent=non_idempotent,
                step=step,
                params={"to": to, "episode": ctx.episode_id if ctx else None},
                episode_id=ctx.episode_id if ctx else None,
                episode_role=role,
                policy_epoch=ctx.policy_epoch if ctx else None,
                evidence_ids=list(evidence)[:64],
                expected_seq=expected_seq,
            )
            self._crash("after_prepared")
            executed, detail = "yes", "none"
            if kind != "none":
                if act is None:
                    executed, source, detail = "yes", "none", "no_side_effect"
                else:
                    executed, source, detail = act(prepared)
                self._crash("after_execute")
                self.journal.acknowledge(
                    executed, source, _code(detail), authority=auth
                )
                self._crash("after_acknowledged")
            if executed != "yes":
                self.journal.abort(authority=auth, reason=reason)
                if urgent:
                    self._flush_notes()
                return TxResult(executed, detail, prepared)
            found = result() if callable(result) else result
            self.journal.commit(authority=auth, episode_result=found)
            if found is not None and ctx is not None:
                ctx.result_written = True
            self._crash("after_committed")
            self._tell(to, reason, ctx.episode_id if ctx else None)
            # The run reached a person: every stop registered so far took
            # effect, whatever reason brought it there (review C3 sugg. 1:
            # a stop left behind would block the operator's next stop).
            if to == "WAIT_HUMAN" or (to == "COMPLETED" and reason == "operator_stop"):
                self._consume_stops(auth.get("command_id"))
            if urgent:
                self._flush_notes()
            return TxResult("yes", detail, prepared)

    def _consume_stops(self, command_id: str | None) -> None:
        """The run reached a person on an operator's stop: every stop
        registered so far took effect. The extra ids are noted."""
        with self._stop_lock:
            taken = sorted(self._stops, key=self._stops.get)
            self._stops.clear()
            self._consumed_stops.update(taken)
            if command_id:
                self._consumed_stops.add(command_id)
        extra = [c for c in taken if c != command_id]
        if extra:
            self._note("stop_commands_merged", ", ".join(extra)[:300])

    def _fault(self, reason: str, *, guard: bool = False, hold: bool = True) -> None:
        """Lock the run. An episode still open gets its result (unknown,
        stopped by ``reason``)."""
        ctx = self.ctx
        if ctx is not None and ctx.rollout is not None and ctx.sealed is None:
            try:
                self.recorder.abort(ctx.rollout, reason)
                ctx.sealed = "incomplete"
            except Exception as exc:  # noqa: BLE001 - the lock matters more
                self._note("recorder_error", f"abort: {exc}")
        self._close_episode()
        kind = "hold" if hold and self.state in sm.POLICY_MAY_INFER else "none"

        def act(_):
            self.robot.hold()
            return "yes", "robot_server", "held"

        result = None
        if (
            ctx is not None
            and not ctx.result_written
            and self.state in aeri.EPISODE_STATES
        ):
            result = self._fault_result(ctx, reason)
        self._tx(
            "FAULT_LOCKED",
            reason,
            kind=kind,
            act=act,
            ctx=ctx if result is not None else None,
            result=result,
            authority=self._auth("safety_guard" if guard else "orchestrator"),
        )
        self.ctx = None

    def _fault_result(self, ctx: Episode, reason: str) -> dict:
        """pipeline §4.3: an episode ended by a fault is recorded, its outcome
        unknown and its stop reason the fault (X1 §2.5 gave it no result)."""
        stop = reason if reason in aeri.STOP_REASONS else "watchdog_timeout"
        folder = (
            self.config.forward_folder
            if ctx.role == "forward"
            else self.config.reset_folder
        )
        return {
            "task_outcome": "unknown",
            "stop_reason": stop,
            "goal_verification": "unavailable",
            "robot_home": "failed" if reason == "home_failed" else "not_attempted",
            "scene_reset": SCENE_RESET.get(ctx.scene_decision, "unknown")
            if ctx.role == "reset"
            else "unknown",
            "label_kind": "autonomous_verdict",
            "judgement_ids": ctx.judgement_ids[-64:],
            "rollout": {
                "task_folder": folder,
                "demo": f"demo_{ctx.number:04d}",
                "sealed": ctx.sealed if ctx.sealed == "complete" else "incomplete",
            },
        }

    def _contain(self, exc: Exception) -> None:
        """An exception escaped the loop: no motion stays authorised, the
        robot holds, the run locks (or the orchestrator halts in memory)."""
        self.fence.revoke()
        try:
            self.robot.hold()
        except Exception as held:  # noqa: BLE001
            self._note("hold_failed", f"{type(held).__name__}: {held}")
        try:
            with self._lock:
                self._note("orchestrator_exception", f"{type(exc).__name__}: {exc}")
                if self.journal.open_transaction is not None:
                    auth = self._auth()
                    try:
                        self.journal.acknowledge(
                            "unknown", "none", "orchestrator_exception", authority=auth
                        )
                    except JournalRefused:
                        pass  # already acknowledged
                    self.journal.abort(authority=auth, reason="watchdog_timeout")
                if self.state not in ("FAULT_LOCKED", "COMPLETED"):
                    self._fault("watchdog_timeout", hold=False)
                else:
                    self._flush_notes(final=True)
        except Exception as second:  # noqa: BLE001
            self.halted = (
                f"{type(exc).__name__} and then {type(second).__name__}: {second}"
            )[:300]
        finally:
            self.fence.revoke()

    def _guarded(self, fn, *args, **kwargs):
        if self.halted:
            raise OrchestratorHalted(self.halted)
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            self._contain(exc)
            raise

    def _to_human(self, reason: str, authority=None, ctx=None) -> None:
        self._close_episode()
        self._tx("WAIT_HUMAN", reason, authority=authority)

    def _close_episode(self) -> None:
        ctx = self.ctx
        if ctx is None:
            return
        if ctx.stream is not None:
            stream, ctx.stream = ctx.stream, None
            self.events.close(stream)

    # --- operator commands ------------------------------------------------------------

    def resume(
        self,
        command_id: str,
        *,
        expected_seq: int,
        environment_handled: bool,
        health_rechecked: bool,
    ) -> CommandResult:
        """WAIT_HUMAN or FAULT_LOCKED -> PREFLIGHT. Repeating a command
        returns its first result; a stale ``expected_seq`` is refused."""
        with self._lock:
            with self._stop_lock:
                # Stops registered from now on are later than this resume.
                generation = self._stop_gen
            prior = self.journal.by_command(command_id)
            if prior:
                if prior[0].reason == "human_resumed":
                    return CommandResult(
                        True, "repeated", self.state, True, prior[0].sequence_no
                    )
                return CommandResult(False, "command_used", self.state)
            if self.journal.corrupt:
                return CommandResult(False, "journal_corrupt", self.state)
            if self.halted:
                return CommandResult(False, "halted", self.state)
            if self.state not in sm.HUMAN_STATES:
                return CommandResult(False, "not_waiting", self.state)
            if not (environment_handled is True and health_rechecked is True):
                return CommandResult(False, "confirmations_missing", self.state)
            try:
                found = self._tx(
                    "PREFLIGHT",
                    "human_resumed",
                    authority=self._auth("operator", command_id),
                    expected_seq=expected_seq,
                )
            except JournalRefused as exc:
                if exc.code == "E_SEQ":
                    return CommandResult(False, "stale_sequence", self.state)
                self._contain(exc)
                raise
            except Exception as exc:
                self._contain(exc)
                raise
            with self._stop_lock:
                # Only the stops registered before this resume began are
                # overridden by it; a later stop stays and takes effect at
                # the next decision point.
                lost = sorted(c for c, g in self._stops.items() if g <= generation)
                for command in lost:
                    del self._stops[command]
                self._consumed_stops.clear()
            if lost:
                # Stops that never took effect (the run locked or waited
                # first): kept in the journal, then cleared by the resume.
                self._note("stop_command_lost", ", ".join(lost)[:300])
            self._violation_hold = False
            self._scene_violations = 0
            self._reset_attempts = 0
            return CommandResult(
                True, "resumed", self.state, False, found.prepared.sequence_no
            )

    def stop(self, command_id: str) -> CommandResult:
        """An operator's stop. Never waits for an adapter: the stop is
        registered at once (the loop reads the registry at every step and
        before every episode) and ``stop_requested`` is returned; only when
        the run is waiting for a person and the state lock is free within
        ``STOP_LOCK_WAIT_S`` does it end the run here (``completed``).

        A stop stays registered until the run reaches a person on it; a
        resume clears only the stops registered before the resume began.
        The same stop again (pending, or taken effect since the last resume)
        returns ``repeated`` and changes nothing; an id another command used
        is refused (``command_used``): send the stop again with a new id."""
        with self._stop_lock:
            if command_id in self._stops:
                return CommandResult(True, "stop_requested", self.state, True)
            if command_id in self._consumed_stops:
                return CommandResult(True, "repeated", self.state, True)
        prior = self.journal.by_command(command_id)
        if prior:
            return CommandResult(False, "command_used", self.state)
        if self.state in ("FAULT_LOCKED", "COMPLETED"):
            return CommandResult(False, "not_running", self.state)
        with self._stop_lock:
            self._stop_gen += 1
            self._stops[command_id] = self._stop_gen  # the loop sees it now
        if not self._lock.acquire(timeout=STOP_LOCK_WAIT_S):
            return CommandResult(True, "stop_requested", self.state)
        try:
            if (
                self.state == "WAIT_HUMAN"
                and not self.halted
                and self._registered(command_id)
            ):
                found = self._guarded(
                    self._tx,
                    "COMPLETED",
                    "operator_stop",
                    authority=self._auth("operator", command_id),
                )
                return CommandResult(
                    True, "completed", self.state, False, found.prepared.sequence_no
                )
            return CommandResult(True, "stop_requested", self.state)
        finally:
            self._lock.release()

    def _registered(self, command_id: str) -> bool:
        with self._stop_lock:
            return command_id in self._stops

    def _first_stop(self) -> str | None:
        with self._stop_lock:
            if not self._stops:
                return None
            return min(self._stops, key=self._stops.get)

    # --- the loop -----------------------------------------------------------------------

    def run(self, max_decisions: int = 100_000) -> str:
        """Drive the run until it needs a person or ends."""
        if self.halted:
            return "FAULT_LOCKED"
        try:
            self._guarded(self._loop, max_decisions)
        finally:
            try:
                self._flush_notes(final=True)
            except Exception as exc:  # noqa: BLE001 - never hide the first error
                self.notes.append(("note_flush_failed", str(exc)[:300]))
        return self.state

    def _loop(self, max_decisions: int) -> None:
        for _ in range(max_decisions):
            state = self.state
            if state in HOLD_STATES:
                return
            if state == "PREFLIGHT":
                self._preflight()
            elif state in ("VERIFY_INITIAL", "SCENE_ASSESS"):
                self._decide()
            else:
                # Mid-chain states are only ever passed through inside one
                # call; finding one here means a chain broke off.
                self._fault("watchdog_timeout")

    def _operator(self) -> dict | None:
        """The authority of the oldest stop not yet consumed, if any."""
        command = self._first_stop()
        if command is None:
            return None
        return self._auth("operator", command)

    def _preflight(self) -> None:
        report = self.robot.preflight()
        if report["ok"]:
            self._tx("VERIFY_INITIAL", "preflight_passed")
        elif self.robot.latch_state() or self.robot.health()["state"] == "red":
            self._fault("safety_stop", guard=True)
        else:
            self._to_human("preflight_failed")

    def _decide(self) -> None:
        if self._stop_before_start():
            return
        # A provider that broke the contract is looked at by a person before
        # anything else, the end of the run included.
        if self._violation_hold:
            self._to_human("contract_violation_limit")
            return
        if self.completed_forward >= self.config.episodes:
            self._tx("COMPLETED", "run_completed")
            return
        if self.state == "VERIFY_INITIAL" or self._last_forward is None:
            target = "initial_state"
            episode = f"{self.config.run_id}.forward.{self.numbers['forward'] + 1:04d}"
        else:
            target, episode = "post_forward", self._last_forward
        decision = self._assess(target, episode)
        # A stop that arrived while the scene was being assessed.
        if self._stop_before_start():
            return
        if self._scene_violations >= self.config.scene_violation_limit:
            self._to_human("contract_violation_limit")
            return
        if self.config.initial_state is None:
            # Without a contract no scene can ever be ready, and no reset
            # could make it so: a person decides (review C3 fixes, I-b).
            self._to_human(rm.scene_reason(decision))
            return
        # Only a ready scene skips a reset; the strategy decides the rest.
        try:
            plan = rm.check_plan(self.strategy, decision, self._reset_attempts)
        except rm.StrategyError as exc:
            self._note("strategy_refused", str(exc))
            plan = rm.ResetPlan(rm.WAIT_HUMAN, rm.scene_reason(decision))
        if plan.action == rm.FORWARD:
            self._reset_attempts = 0
            self._start("forward", plan.reason)
        elif plan.action == rm.RESET:
            self._reset_attempts += 1
            self._start("reset", plan.reason)
        else:
            self._to_human(plan.reason)

    def _stop_before_start(self) -> bool:
        operator = self._operator()
        if operator is None:
            return False
        self._to_human("operator_stop", authority=operator)
        return True

    # --- scene assessment ---------------------------------------------------------------

    def _assess(self, target: str, episode_id: str) -> str:
        """ready | reset_required | unknown | unavailable (never ready
        without a fresh, fenced assessment that says so). Every assessment
        is kept in ``evidence/`` (T-CL-04), the refused ones too."""
        # Unique across restarts: the journal's next line number.
        request_id = f"{episode_id}:scene{self.journal.next_seq}"
        record = {
            "request_id": request_id,
            "episode_id": episode_id,
            "target": target,
            "journal_seq": self.journal.next_seq,
            # After the home or the reset's end: evidence older than this
            # request shows a scene that may have changed since (review C3, I1).
            "asked_ns": self._now(),
        }
        decision = "unavailable"
        try:
            decision, reason, message = self._assess_once(record)
            record["decision"], record["reason"] = decision, reason
            if message is not None:
                record.update(_scene_facts(message))
        finally:
            self._keep_evidence(record)
        return decision

    def _assess_once(self, record: dict) -> tuple:
        """``(decision, reason, parsed message or None)``."""
        request_id = record["request_id"]
        episode_id, target = record["episode_id"], record["target"]
        asked_ns = record["asked_ns"]
        request = ev.make_request(
            request_id=request_id,
            run_id=self.config.run_id,
            episode_id=episode_id,
            target=target,
        )
        try:
            got = self.scene.submit(request)
            if isinstance(got, bytes | bytearray):
                raw = bytes(got)
            else:
                try:
                    raw, why = self._collect_scene(got)
                except BaseException:
                    # Never leave a question open behind an exception.
                    self.scene.cancel(got, "error")
                    raise
                if raw is None:
                    self.scene.cancel(got, why)
                    self._note(f"scene_{why}", request_id)
                    return "unavailable", why, None
        finally:
            self._scene_notes()
        try:
            message = aeri.parse(raw, "scene", specs=self._specs)
        except aeri.AeriError as exc:
            self._scene_violations += 1
            self._note("contract_violation", f"scene: {exc}")
            return "unavailable", f"contract_violation:{exc.code}", None
        if message.run_id != self.config.run_id or message.request_id != request_id:
            self._note("scene_dropped_unsolicited", message.request_id)
            return "unavailable", "unsolicited", None
        if message.kind == "unavailable":
            self._note("scene_unavailable", f"{request_id}: {message.code}")
            return "unavailable", f"unavailable:{message.code}", message
        if message.episode_id != episode_id or message.target != target:
            self._note("scene_dropped_stale", f"{request_id}: {message.episode_id}")
            return "unavailable", "stale:other_episode", message
        try:
            aeri.check_fresh(message, now_ns=self._now(), local=self.clock.domain)
        except aeri.AeriError as exc:
            self._note("scene_dropped_stale", f"{request_id}: {exc.code}")
            return "unavailable", f"stale:{exc.code}", message
        if message.kind == "scene" and message.observed_ns < asked_ns:
            self._note(
                "scene_dropped_stale",
                f"{request_id}: observed {asked_ns - message.observed_ns} ns "
                "before the request",
            )
            return "unavailable", "stale:observed_before_request", message
        verdict = sa.arbitrate(message, self.config.initial_state)
        if verdict.reason != "provider":
            # A ready claim that does not meet the contract: never a skip.
            self._note(
                f"scene_{verdict.reason}",
                f"{request_id}: {message.decision} -> {verdict.decision}",
            )
        return verdict.decision, verdict.reason, message

    def _keep_evidence(self, record: dict) -> None:
        """Write the record and the frames the provider captured for it (a
        person's check). Never decides and never stops the run: a failure
        is a note."""
        take = getattr(self.scene, "frames", None)
        lost = getattr(self.scene, "unsaved", None)
        try:
            if callable(lost):
                unsaved = lost(record["request_id"])
                if unsaved:
                    # Shown to the person but not kept: no evidence.
                    record["frame_unsaved"] = unsaved
            frames = take(record["request_id"]) if callable(take) else {}
        except Exception as exc:  # noqa: BLE001 - evidence only
            self._note("evidence_write_failed", f"frames: {exc}")
            frames = {}
        if self.evidence is None:
            return
        record.setdefault("decision", "unavailable")
        record.setdefault("reason", "provider_error")
        try:
            if self.evidence.write(record, frames) is None:
                self._note("evidence_budget_exhausted", record["request_id"])
        except Exception as exc:  # noqa: BLE001 - evidence only
            self._note("evidence_write_failed", f"{type(exc).__name__}: {exc}")

    def _collect_scene(self, ticket) -> tuple:
        """``(raw, None)``, or ``(None, why)`` (``timeout``, or
        ``stopped``). A person's check (``operator_attested``) may take
        minutes: it is collected in slices, and an operator's stop ends the
        wait at once (the stop then takes effect before anything starts)."""
        wait = self.config.scene_wait_ns
        if self.config.scene_check != "operator_attested":
            raw = self.scene.collect(ticket, timeout_ns=wait)
            return (raw, None) if raw is not None else (None, "timeout")
        start = self._now()
        while True:
            left = wait - (self._now() - start)
            if left <= 0:
                return None, "timeout"
            raw = self.scene.collect(ticket, timeout_ns=min(left, HUMAN_SLICE_NS))
            if raw is not None:
                return raw, None
            if self._operator() is not None:
                return None, "stopped"

    def _scene_notes(self) -> None:
        """Notes a provider keeps (a person's dropped or refused answers)."""
        drain = getattr(self.scene, "drain_notes", None)
        if drain is None:
            return
        for code, detail in drain():
            self._note(code, detail)

    # --- episodes ------------------------------------------------------------------------

    def _start(self, role: str, reason: str) -> None:
        number = self.numbers[role] + 1
        self.numbers[role] = number
        self.policy_epoch += 1
        ctx = Episode(
            role, f"{self.config.run_id}.{role}.{number:04d}", number, self.policy_epoch
        )
        self.ctx = ctx
        self._episode_note_lines = 0
        self._suppressed = 0
        folder = (
            self.config.forward_folder
            if role == "forward"
            else self.config.reset_folder
        )
        problem = {}

        def act(_):
            if self._operator() is not None:
                problem["what"] = "operator"
                return "no", "none", "operator_stop"
            handle = self.policy.acquire(role, policy_epoch=ctx.policy_epoch)
            if handle.get("kind") == "unavailable":
                problem["what"] = "policy"
                return "no", "policy_adapter", "policy_unavailable"
            try:
                tell = getattr(self.recorder, "before_forward", None)
                if role == "forward" and tell is not None:
                    tell(ctx.episode_id, **self._preceding())
                ctx.rollout = self.recorder.open(
                    task_folder=folder, episode_id=ctx.episode_id, number=number
                )
            except Exception:  # noqa: BLE001 - any recorder failure
                problem["what"] = "recorder"
                # Give the policy back before giving up on the episode.
                self.policy.quiesce(policy_epoch=ctx.policy_epoch)
                return "no", "recorder", "recorder_open_failed"
            ctx.stream = self.events.open(ctx.episode_id, role)
            return "yes", "policy_adapter", "episode_opened"

        to = "FORWARD_ACTIVE" if role == "forward" else "RESET_ACTIVE"
        found = self._tx(to, reason, kind="policy_steps", act=act, ctx=ctx, step=0)
        if found.executed != "yes":
            self.ctx = None
            if problem.get("what") == "recorder":
                self._fault("recorder_failed")
            elif problem.get("what") == "operator":
                self._to_human("operator_stop", authority=self._operator())
            else:
                self._to_human("policy_error")
            return
        if role == "forward":
            self._last_forward = ctx.episode_id
        max_steps = self._max_steps(role)
        ctx.token = self.fence.issue(
            sm.MotionToken(
                run_id=self.config.run_id,
                transaction_id=found.prepared.transaction_id,
                control_epoch=found.prepared.control_epoch,
                kind="policy_steps",
                episode_id=ctx.episode_id,
                policy_epoch=ctx.policy_epoch,
                expires_mono_ns=self._now()
                + (max_steps + 10) * self.config.control_period_ns * 4,
            )
        )
        self._drive(ctx)

    def _preceding(self) -> dict:
        """What put the scene back since the previous forward episode
        started, from the journal (design X2 §1.2, "data ownership"): the
        reset policy (a reset episode), a person (a wait for a reset,
        ``WAIT_HUMAN`` with a ``scene_*`` reason, that an operator's resume
        ended; a resume after a stop, a fault or a failed preflight is not
        a human reset: those stay in the journal only), or nothing. The most recent of the two wins. A wait
        and its resume in different clock domains (a restart in between)
        have no ``wait_ms``."""
        events = self.journal.events
        start = 0
        for index, event in enumerate(events):
            if event.record == "committed" and event.to_state == "FORWARD_ACTIVE":
                start = index + 1
        waiting, last_reset, human = None, -1, []
        for event in events[start:]:
            if event.record != "committed":
                continue
            if event.to_state == "RESET_ACTIVE":
                last_reset = event.sequence_no
            elif event.to_state in sm.HUMAN_STATES:
                # Only a wait for a reset (``scene_*``) is a human reset; a
                # resume after a stop, a fault or a failed preflight is not.
                waiting = event if event.reason.startswith("scene_") else None
            elif event.reason == "human_resumed" and waiting is not None:
                same = waiting.clock_domain == event.clock_domain
                human.append(
                    {
                        "wait_seq": waiting.sequence_no,
                        "resume_seq": event.sequence_no,
                        "wait_ms": (event.mono_ns - waiting.mono_ns) // 1_000_000
                        if same
                        else None,
                        "principal_id": event.authority.principal_id,
                        "reason": waiting.reason,
                    }
                )
                waiting = None
        if human and human[-1]["resume_seq"] > last_reset:
            found = "human_reset"
        elif last_reset >= 0:
            found = "reset_policy"
        else:
            found = "none"
        return {"preceded_by": found, "human_resets": human}

    def _max_steps(self, role: str) -> int:
        return (
            self.config.forward_max_steps
            if role == "forward"
            else self.config.reset_max_steps
        )

    def _drive(self, ctx: Episode) -> None:
        cfg = self.config
        target = "forward_goal" if ctx.role == "forward" else "reset_goal"
        arbiter = TerminationArbiter(
            cfg.termination,
            run_id=cfg.run_id,
            episode_id=ctx.episode_id,
            role=ctx.role,
            target=target,
        )
        ctx.arbiter = arbiter
        tickets: dict = {}
        buffer: list = []
        next_index = 0
        chunk_failures = 0
        last_frames, stalled = None, 0
        stop_reason = None
        for step in range(self._max_steps(ctx.role)):
            ctx.last_step = step
            operator = self._operator()
            if operator is not None:
                stop_reason, ctx.stop_authority = "operator_stop", operator
                break
            if self.robot.latch_state() or self.robot.health()["state"] == "red":
                self._fault("safety_stop", guard=True)
                return
            obs = self.robot.observe()
            frames = tuple(sorted(obs["frames"].items()))
            stalled = stalled + 1 if frames == last_frames else 0
            last_frames = frames
            if stalled >= cfg.camera_stall_limit and ctx.evidence_ok:
                ctx.evidence_ok = False
                self._note(
                    "evidence_unavailable", f"camera frames unchanged at step {step}"
                )
            try:
                self.recorder.stage(ctx.rollout, obs)
            except Exception as exc:  # noqa: BLE001
                self._note("recorder_error", str(exc))
                self._fault("recorder_failed")
                return
            if not arbiter.events_disabled:
                for raw in self.events.poll(
                    ctx.stream, through_step=step, max_events=16
                ):
                    for note in arbiter.on_event(raw):
                        self._note(note.code, note.detail)
                    if arbiter.events_disabled:
                        # Cut off: the rest of this batch is not even read.
                        self._note(
                            "events_ignored", f"{arbiter.event_violations} violations"
                        )
                        break
            for request_id, ticket in list(tickets.items()):
                raw = self.verifier.collect(ticket, timeout_ns=0)
                if raw is None:
                    if (
                        self._now() - ticket.submitted_ns
                        >= cfg.judge_request_timeout_ns
                    ):
                        self.verifier.cancel(ticket, "timeout")
                        arbiter.withdraw(request_id)
                        del tickets[request_id]
                        self._note("judgement_timeout", request_id)
                    continue
                del tickets[request_id]
                verdict = arbiter.on_result(
                    raw,
                    now_ns=self._now(),
                    clock_domain=self.clock.domain,
                    specs=cfg.specs,
                    expected=request_id,
                )
                for note in verdict.notes:
                    self._note(note.code, note.detail)
                if verdict.stop and stop_reason is None:
                    stop_reason = "goal_verified"
                    ctx.stop_judgement = verdict.judgement_id
            if stop_reason is not None:
                break
            want = arbiter.want_request(step) if ctx.evidence_ok else None
            if want is not None:
                request = ev.make_request(
                    run_id=cfg.run_id,
                    episode_id=ctx.episode_id,
                    episode_role=ctx.role,
                    target=target,
                    observed_through_ns=obs["observed_ns"],
                    **want,
                )
                arbiter.issued(want["request_id"], step)
                got = self.verifier.submit(request)
                if isinstance(got, bytes | bytearray):
                    verdict = arbiter.on_result(
                        bytes(got),
                        now_ns=self._now(),
                        clock_domain=self.clock.domain,
                        specs=cfg.specs,
                        expected=want["request_id"],
                    )
                    for note in verdict.notes:
                        self._note(note.code, note.detail)
                else:
                    tickets[want["request_id"]] = got
            if not buffer:
                chunk = self._chunk(ctx, step, next_index)
                if chunk is None:
                    chunk_failures += 1
                    if chunk_failures >= cfg.chunk_failure_limit:
                        stop_reason = "policy_error"
                        break
                    self.clock.advance(cfg.control_period_ns)
                    continue
                chunk_failures = 0
                buffer = chunk
            action = buffer.pop(0)
            result = self.robot.step(action, token=ctx.token)
            if result.executed == "unknown":
                self._note("motion_unacknowledged", f"step {step}: {result.detail}")
                self._fault("watchdog_timeout")
                return
            if result.executed != "yes":
                if self.robot.latch_state():
                    self._fault("safety_stop", guard=True)
                    return
                self._note("motion_refused", f"step {step}: {result.detail}")
                stop_reason = "policy_error"
                break
            ctx.steps_done += 1
            try:
                self.recorder.commit(ctx.rollout, result)
            except Exception as exc:  # noqa: BLE001
                self._note("recorder_error", str(exc))
                self._fault("recorder_failed")
                return
            next_index += 1
            self.clock.advance(cfg.control_period_ns)
        else:
            stop_reason = (
                "horizon_exhausted"
                if ctx.role == "forward"
                else "reset_horizon_exhausted"
            )
        for request_id, ticket in tickets.items():
            self.verifier.cancel(ticket, "episode_ended")
            arbiter.withdraw(request_id)
        ctx.judgement_ids = list(arbiter.judgement_ids)
        if arbiter.disabled or arbiter.events_disabled:
            self._violation_hold = True
        if ctx.role == "forward":
            self._finish_forward(ctx, stop_reason)
        else:
            self._finish_reset(ctx, stop_reason)

    # --- chunks --------------------------------------------------------------------------

    def _chunk(self, ctx: Episode, step: int, start: int):
        cfg = self.config
        request_id = f"{ctx.episode_id}:c{ctx.chunks}"
        ctx.chunks += 1
        now = self._now()
        request = {
            "request_id": request_id,
            "run_id": cfg.run_id,
            "episode_id": ctx.episode_id,
            "handle_id": f"h-{ctx.role}-{ctx.policy_epoch}",
            "policy_epoch": ctx.policy_epoch,
            "obs_step": step,
            "obs_ns": now,
            "clock_domain": self.clock.domain,
            "action_start_index": start,
            "committed_prefix_steps": 0,
            "previous_chunk_ref": None,
            "prefix_conditioning": "none",
            "deadline": {
                "due_ns": now + cfg.chunk_timeout_ns,
                "clock_domain": self.clock.domain,
                "budget_ms": cfg.chunk_timeout_ns // 1_000_000,
                "hardness": "hard",
            },
        }
        ticket = self.policy.request(request)
        if isinstance(ticket, dict):
            self._note("policy_unavailable", f"{request_id}: {ticket.get('code')}")
            return None
        raw = self.policy.collect(ticket, timeout_ns=cfg.chunk_timeout_ns)
        if raw is None:
            self._note("chunk_timeout", request_id)
            return None
        if raw.get("kind") == "unavailable":
            self._note("policy_unavailable", f"{request_id}: {raw.get('code')}")
            return None
        return self.accept_chunk(ctx, raw, request)

    def accept_chunk(self, ctx: Episode, raw: dict, request: dict):
        """The actions of a chunk response that may run now, or None (with
        a note). C fills ``received_ns`` from its own clock; the deadline
        is judged on that clock, never on a time the server wrote."""
        message = dict(raw)
        message["received_ns"] = self._now()
        try:
            chunk = aeri.validate(message, "runtime")
        except aeri.AeriError as exc:
            self._note("chunk_dropped_contract", f"{request['request_id']}: {exc}")
            return None
        if chunk.kind != "chunk_response":
            self._note("chunk_dropped_contract", f"kind {chunk.kind}")
            return None
        current = self.ctx is ctx and self.state in sm.MOTION_STATES
        if not current or chunk.policy_epoch != ctx.policy_epoch:
            self._note(
                "chunk_dropped_epoch", f"{chunk.request_id}: epoch {chunk.policy_epoch}"
            )
            return None
        if (
            chunk.request_id != request["request_id"]
            or chunk.episode_id != ctx.episode_id
        ):
            self._note("chunk_dropped_unsolicited", chunk.request_id)
            return None
        deadline = aeri.Deadline.model_validate(request["deadline"], strict=True)
        try:
            aeri.check_deadline(deadline, now_ns=self._now(), local=self.clock.domain)
        except aeri.AeriError as exc:
            self._note("chunk_dropped_stale", f"{chunk.request_id}: {exc.code}")
            return None
        if any(len(row) != self.config.action_dims for row in chunk.actions):
            self._note("chunk_dropped_contract", f"{chunk.request_id}: dimensions")
            return None
        start = request["action_start_index"]
        if chunk.valid_from_action_index < start:
            self._note(
                "chunk_dropped_contract", f"{chunk.request_id}: valid_from before start"
            )
            return None
        skip = chunk.valid_from_action_index - start
        actions = [list(row) for row in chunk.actions[skip:]]
        return actions or None

    # --- finishing episodes ----------------------------------------------------------------

    def _hold(self, _):
        self.robot.hold()
        return "yes", "robot_server", "held"

    def _quiesce(self, ctx):
        def act(_):
            self.robot.hold()
            ack = self.policy.quiesce(policy_epoch=ctx.policy_epoch)
            if ack.get("ok"):
                return "yes", "policy_adapter", "quiesced"
            return "unknown", "policy_adapter", "quiesce_unconfirmed"

        return act

    def _seal(self, ctx, sealed: dict):
        def act(_):
            try:
                arbiter = ctx.arbiter
                found = self.recorder.seal(
                    ctx.rollout,
                    {
                        "episode_id": ctx.episode_id,
                        # Control episodes: where the detector would have
                        # stopped (the false-early-stop measurement, §5.5).
                        "control": bool(arbiter and arbiter.control),
                        "would_stop_step": arbiter.would_stop_step if arbiter else None,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                self._note("recorder_error", f"seal: {exc}")
                return "no", "recorder", "seal_failed"
            sealed.update(found)
            ctx.sealed = "complete"
            return "yes", "recorder", "sealed"

        return act

    def _discard(self, ctx, sealed: dict):
        """An episode that ran no step: its recording is not kept."""

        def act(_):
            try:
                self.recorder.abort(ctx.rollout, "no_steps")
            except Exception as exc:  # noqa: BLE001
                self._note("recorder_error", f"abort: {exc}")
                return "no", "recorder", "abort_failed"
            sealed["sealed"] = "incomplete"
            ctx.sealed = "incomplete"
            return "yes", "recorder", "discarded"

        return act

    def _home(self, ctx):
        def act(prepared):
            token = self.fence.issue(
                sm.MotionToken(
                    run_id=self.config.run_id,
                    transaction_id=prepared.transaction_id,
                    control_epoch=prepared.control_epoch,
                    kind="home",
                    episode_id=ctx.episode_id,
                    policy_epoch=ctx.policy_epoch,
                    expires_mono_ns=self._now() + self.config.home_timeout_ns,
                )
            )
            try:
                found = self.robot.home(token=token)
            finally:
                self.fence.revoke()
            return found.executed, "robot_server", found.detail

        return act

    def _safe_to_home(self) -> bool:
        """The home is a motion: never with a latched guard or a red light."""
        if self.robot.latch_state() or self.robot.health()["state"] == "red":
            self._fault("safety_stop", guard=True)
            return False
        return True

    def _final_judgement(self, ctx: Episode, arbiter: TerminationArbiter):
        """The last word on the goal, asked once the policy is quiesced."""
        if not ctx.evidence_ok:
            self._note("final_judgement_skipped", "evidence unavailable")
            return None
        if arbiter.disabled:
            self._note("final_judgement_skipped", "the judge broke the contract")
            return None
        request_id = f"{ctx.episode_id}:final"
        arbiter.outstanding[request_id] = None
        request = ev.make_request(
            request_id=request_id,
            run_id=self.config.run_id,
            episode_id=ctx.episode_id,
            episode_role=ctx.role,
            target="forward_goal" if ctx.role == "forward" else "reset_goal",
            observed_from_step=0,
            observed_through_step=ctx.last_step,
            observed_through_ns=self._now(),
            event_ids=[],
        )
        got = self.verifier.submit(request)
        if isinstance(got, bytes | bytearray):
            raw = bytes(got)
        else:
            raw = self.verifier.collect(
                got, timeout_ns=self.config.final_judge_timeout_ns
            )
            if raw is None:
                self.verifier.cancel(got, "timeout")
                arbiter.withdraw(request_id)
                self._note("judgement_timeout", request_id)
                return None
        found = arbiter.accept(
            raw,
            now_ns=self._now(),
            clock_domain=self.clock.domain,
            specs=self.config.specs,
            expected=request_id,
        )
        if not hasattr(found, "kind"):
            self._note(found.code, found.detail)
            return None
        if found.kind == "judgement" and (
            found.observed_from_step > 0
            or found.observed_through_step < ctx.last_step
            or found.observed_through_ns < request["observed_through_ns"]
        ):
            # The final word must see the end of the episode, after the
            # quiesce: a judgement of an earlier moment proves nothing.
            self._note(
                "judgement_dropped_stale",
                f"{request_id}: steps {found.observed_from_step}.."
                f"{found.observed_through_step}, the episode ended at {ctx.last_step}",
            )
            return None
        if found.kind == "judgement":
            ctx.judgement_ids.append(found.judgement_id)
        return found

    def _verification(self, ctx: Episode, reason: str) -> str:
        """goal_verification of a forward episode. An early stop is a
        success only when the final judgement after the quiesce confirms
        it too; otherwise it is undecided (or unavailable)."""
        if ctx.steps_done == 0:
            return "unavailable"
        final = self._final_judgement(ctx, ctx.arbiter)
        found = verification_of(final)
        if reason == "goal_verified" and found != "verified":
            self._note("early_stop_disputed", f"final judgement: {found}")
            return "unavailable" if found == "unavailable" else "undecided"
        return found

    def _finish_forward(self, ctx: Episode, reason: str) -> None:
        auth = ctx.stop_authority
        found = self._tx(
            "FORWARD_STOPPING",
            reason,
            kind="hold",
            act=self._hold,
            authority=auth,
            ctx=ctx,
        )
        if found.executed != "yes":
            self._fault("watchdog_timeout")
            return
        found = self._tx(
            "FORWARD_FINALIZE",
            reason,
            kind="policy_quiesce",
            act=self._quiesce(ctx),
            authority=auth,
            ctx=ctx,
        )
        if found.executed != "yes":
            self._fault("policy_error")
            return
        verification = self._verification(ctx, reason)
        sealed: dict = {}
        ran = ctx.steps_done > 0

        def result():
            return {
                "task_outcome": aeri.OUTCOME_OF_VERIFICATION[verification],
                "stop_reason": reason,
                "goal_verification": verification,
                "robot_home": "not_attempted",
                "scene_reset": "unknown",
                "label_kind": "autonomous_verdict",
                "judgement_ids": ctx.judgement_ids[-64:],
                "rollout": {
                    "task_folder": self.config.forward_folder,
                    "demo": f"demo_{ctx.number:04d}",
                    "sealed": sealed.get("sealed", "incomplete"),
                },
            }

        found = self._tx(
            "ROBOT_HOME",
            reason,
            kind="recorder_seal" if ran else "recorder_abort",
            act=self._seal(ctx, sealed) if ran else self._discard(ctx, sealed),
            authority=auth,
            ctx=ctx,
            result=result,
            evidence=ctx.judgement_ids[-64:],
        )
        if found.executed != "yes":
            self._fault("recorder_failed")
            return
        if ran:
            self.completed_forward += 1
        operator = self._stays_put(ctx, reason, auth)
        if operator is not None:
            # The arm did not move in this episode, or the operator wants it
            # left where it stands: a person takes over from here.
            self._close_episode()
            self.ctx = None
            self._tx("WAIT_HUMAN", "operator_stop", authority=operator)
            return
        if not self._safe_to_home():
            return
        found = self._tx(
            "SCENE_ASSESS",
            "robot_home_reached",
            kind="home",
            act=self._home(ctx),
            authority=auth,
            ctx=ctx,
            step=1,
        )
        if found.executed != "yes":
            self._fault("home_failed")
            return
        self._close_episode()
        self.ctx = None

    def _stays_put(self, ctx: Episode, reason: str, auth) -> dict | None:
        """The operator's authority when the arm must not be homed after a
        stop: the episode ran no step, or ``home_after_operator_stop`` is
        false. A stop that arrived after the steps ended (during the quiesce,
        the final judgement or the seal) counts too."""
        operator = auth if reason == "operator_stop" and auth else self._operator()
        if operator is None:
            return None
        if ctx.steps_done == 0 or not self.config.home_after_operator_stop:
            return operator
        return None

    def _finish_reset(self, ctx: Episode, reason: str) -> None:
        auth = ctx.stop_authority
        found = self._tx(
            "RESET_VERIFY",
            reason,
            kind="policy_quiesce",
            act=self._quiesce(ctx),
            authority=auth,
            ctx=ctx,
        )
        if found.executed != "yes":
            self._fault("policy_error")
            return
        decision = (
            self._assess("post_reset", ctx.episode_id)
            if ctx.evidence_ok
            else "unavailable"
        )
        ctx.scene_decision = decision
        if reason in (
            "operator_stop",
            "policy_error",
            "watchdog_timeout",
            "reset_horizon_exhausted",
        ):
            outcome = reason
        elif decision == "ready":
            outcome = "reset_verified"
        elif decision == "reset_required":
            outcome = "scene_reset_required"
        else:
            outcome = "scene_unknown"
        sealed: dict = {}
        ran = ctx.steps_done > 0
        found = self._tx(
            "RESET_FINALIZE",
            outcome,
            kind="recorder_seal" if ran else "recorder_abort",
            act=self._seal(ctx, sealed) if ran else self._discard(ctx, sealed),
            authority=auth,
            ctx=ctx,
        )
        if found.executed != "yes":
            self._fault("recorder_failed")
            return
        verification = SCENE_VERIFICATION[decision] if ran else "unavailable"
        stop = "horizon_exhausted" if reason == "reset_horizon_exhausted" else reason
        home = {"done": "succeeded"}

        def result():
            return {
                "task_outcome": aeri.OUTCOME_OF_VERIFICATION[verification],
                "stop_reason": stop,
                "goal_verification": verification,
                "robot_home": home["done"],
                "scene_reset": SCENE_RESET[decision] if ran else "unknown",
                "label_kind": "autonomous_verdict",
                "judgement_ids": ctx.judgement_ids[-64:],
                "rollout": {
                    "task_folder": self.config.reset_folder,
                    "demo": f"demo_{ctx.number:04d}",
                    "sealed": sealed.get("sealed", "incomplete"),
                },
            }

        operator = self._stays_put(ctx, reason, auth)
        if operator is not None:
            home["done"] = "not_attempted"
            self._tx(
                "WAIT_HUMAN",
                "operator_stop",
                ctx=ctx,
                result=result,
                authority=operator,
            )
            self._close_episode()
            self.ctx = None
            return
        try:
            to = rm.check_after(
                self.strategy,
                outcome,
                self._reset_attempts,
                self._operator() is not None,
            )
        except rm.StrategyError as exc:
            # A strategy that breaks the rules hands over to a person.
            self._note("strategy_refused", str(exc))
            to = rm.TO_PERSON
        if not self._safe_to_home():
            return
        found = self._tx(
            to,
            outcome,
            kind="home",
            act=self._home(ctx),
            ctx=ctx,
            step=1,
            result=result,
            authority=auth if to == "WAIT_HUMAN" else None,
        )
        if found.executed != "yes":
            self._fault("home_failed")
            return
        self._close_episode()
        self.ctx = None
