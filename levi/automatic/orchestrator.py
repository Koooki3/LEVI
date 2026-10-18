"""The automatic evaluation run (T-C-06): one orchestrator drives the 15
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

import contextlib
import hashlib
import json
import math
import os
import re
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from levi.domain import aeri

from . import camera_meta
from . import reset_manager as rm
from . import scene_assessment as sa
from . import state_machine as sm
from . import subtask_loop as st
from .adapters import events as ev
from .adapters.owned_calls import OwnedCalls
from .intervention import InferenceCycle, InterventionStopped, InterventionUnproven
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
        "reverse_human_requested",
        "orchestrator_exception",
        "hold_failed",
        "motion_unacknowledged",
        "motion_refused",
        "recorder_error",
        "early_stop_disputed",
        "stop_command_lost",
        "home_measured",
        "home_failed_detail",
        "robot_homed",
        "start_pose_checked",
        # Which camera stopped (frame bus): it explains a camera_stale wait.
        "camera_stale",
        # A camera seq repeated on K steps (recorded only, never a stop).
        "camera_seq_repeat",
        "policy_unavailable",
        "gripper_reply_queued",
        "gripper_reply_confirmed",
        "action_guard_refused",
        # The bridge emits at most eight bounded diagnostic chunks.
        "action_guard_detail_0000",
        "action_guard_detail_0001",
        "action_guard_detail_0002",
        "action_guard_detail_0003",
        "action_guard_detail_0004",
        "action_guard_detail_0005",
        "action_guard_detail_0006",
        "action_guard_detail_0007",
    }
)
# Actions that stop motion: they act before the backlog of notes is written.
URGENT_KINDS = frozenset({"hold", "policy_quiesce"})
# How long stop() tries for the state lock before it only registers the stop.
STOP_LOCK_WAIT_S = 0.05
# A person's scene check is collected in slices this long (a stop is seen
# between two).
HUMAN_SLICE_NS = 500_000_000
# An operator's verdict on the forward episode under way (terminal D's s/f/d,
# L2-AERI-OPERATOR-EARLY-STOP): it ends this episode only; the run goes on.
VERDICT_LABELS = ("success", "failure", "discard")
VERDICT_SOURCES = ("terminal",)
# The operator label it becomes once the episode's result is committed
# (``metrics.label_operator``; ``discarded`` is left out of every truth).
OPERATOR_LABEL_OF_VERDICT = {
    "success": "success",
    "failure": "failure",
    "discard": "discarded",
}
VERDICT_NOTE = "operator_verdict"
VERDICT_DROPPED_NOTE = "operator_verdict_dropped"
VERDICT_LABEL_FAILED_NOTE = "operator_verdict_label_failed"
# A verdict before the first step: the round never ran, there is no rollout
# to label (the verdict's own note stays).
VERDICT_NOT_LABELLED_NOTE = "operator_verdict_not_labelled"
DISCARD_LIMIT_NOTE = "discard_limit_reached"
_VERDICT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


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
    reset_horizon_completion: str = "wait_human"
    reset_settle: rm.SettleConfig | None = None
    # After an operator's stop: home first (the design's path), or wait for
    # a person where the arm stands. The home still passes the safety check.
    home_after_operator_stop: bool = True
    start_home: bool = False
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
    # Rounds the operator discarded with ``d`` (L2-AERI-OPERATOR-EARLY-STOP)
    # do not count towards ``episodes``: one more round replaces each, at
    # most this many in a run (None: ``episodes``). Beyond it a discarded
    # round is not replaced (note ``discard_limit_reached``): it still uses
    # up a round but stays out of every statistic, so the run ends with
    # fewer valid rounds.
    max_discarded_rounds: int | None = None
    # --- reset_strategy subtask_loop (levi/automatic/subtask_loop.py) ---------
    # The task name the VLM plans from (the job's task.instruction).
    subtask_task: str | None = None
    # One subtask is one episode of at most this many steps.
    subtask_max_steps: int = 200
    # A subtask whose check says false runs again at most this many times.
    subtask_max_retries: int = 5
    # Optional cap on the subtasks of one loop's plan (None: no cap).
    max_subtasks_per_loop: int | None = None
    # subtask.early_check (subtask_loop.EarlyCheck; None, the default: every
    # subtask episode runs its whole step budget before its check).
    subtask_early_check: st.EarlyCheck | None = None
    # MemAvailable needed before a level-1 sleep (the weights go to pinned
    # host memory that cannot be swapped: about 18.5-20 GiB).
    vlm_min_free_mem_gib: float = 28.0
    # What happens below it: wait for a person, sleep at level 2 (weights
    # dropped, reloaded on wake), or keep the VLM awake and idle while the
    # policy runs (only when the GPU has ``vlm_stay_awake_min_free_gpu_mib``).
    vlm_sleep_fallback: str = "wait_human"
    vlm_stay_awake_min_free_gpu_mib: int = 4096
    # Free GPU memory (nvidia-smi) needed before a wake. None: derived from the
    # VLM's profile (``vlm_power.wake_need_mib()``: its budget less what it
    # keeps while asleep, plus the live service's wake margin), else
    # ``subtask_loop.DEFAULT_WAKE_NEED_MIB``.
    vlm_wake_min_free_gpu_mib: int | None = None
    # Added to that need: the wake threshold is need + headroom (>= 1024 MiB,
    # so a wake never leaves the card at the edge mid-round).
    vlm_wake_headroom_mib: int = 1024
    # sleep: the VLM sleeps whenever the policy runs (the default); co_resident:
    # a smaller VLM stays loaded beside the policy, never put to sleep, and is
    # sent no request while the policy runs (its GPU check is the startup's,
    # for both models together).
    vlm_alternation: str = "sleep"
    # co_resident only: free GPU memory the startup check needs with both
    # models (and anything else on the card) loaded.
    vlm_co_resident_headroom_mib: int = 2048
    # Home at the end of each loop (v1: always).
    home_after_loop: bool = True
    # Rollout folders of the forward and the reverse loop (None:
    # ``<forward_folder>__subtask`` and ``<forward_folder>__reverse``).
    subtask_forward_folder: str | None = None
    subtask_reverse_folder: str | None = None

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
        if self.max_discarded_rounds is not None and (
            type(self.max_discarded_rounds) is not int or self.max_discarded_rounds < 0
        ):
            raise ConfigError("max_discarded_rounds is a whole number >= 0 or None")
        if self.on_scene_unknown not in ("reset", "wait_human"):
            raise ConfigError("on_scene_unknown is reset or wait_human")
        if type(self.max_reset_attempts) is not int or self.max_reset_attempts < 0:
            raise ConfigError("max_reset_attempts is a whole number >= 0")
        if self.start_home and self.reset_strategy != "subtask_loop":
            raise ConfigError("start_home requires subtask_loop")
        if (
            self.subtask_early_check is not None
            and self.reset_strategy != "subtask_loop"
        ):
            raise ConfigError("subtask_early_check requires subtask_loop")
        for name in ("reset_enabled", "home_after_operator_stop", "start_home"):
            if type(getattr(self, name)) is not bool:
                raise ConfigError(f"{name} is true or false")
        if not isinstance(self.termination, TerminationConfig):
            raise ConfigError("termination is a TerminationConfig")
        if self.initial_state is not None and not isinstance(
            self.initial_state, sa.InitialStateContract
        ):
            raise ConfigError("initial_state is an InitialStateContract or None")
        if isinstance(self.reset_settle, dict):
            try:
                object.__setattr__(
                    self, "reset_settle", rm.SettleConfig(**self.reset_settle)
                )
            except (rm.StrategyError, TypeError) as exc:
                raise ConfigError(str(exc)) from None
        if self.reset_settle is not None and not isinstance(
            self.reset_settle, rm.SettleConfig
        ):
            raise ConfigError("reset_settle is a SettleConfig")
        if (
            self.reset_horizon_completion == "wait_human"
            and self.reset_settle is not None
        ):
            raise ConfigError("reset_settle belongs only to verify_final")
        if self.reset_horizon_completion not in ("wait_human", "verify_final"):
            raise ConfigError("reset_horizon_completion is wait_human or verify_final")
        if self.reset_horizon_completion == "verify_final" and (
            self.reset_strategy != "single_reset_policy"
            or not self.reset_enabled
            or self.initial_state is None
            or self.initial_state.status != "confirmed"
            or self.scene_check != "provider"
            or self.termination.allow_early_stop
            or self.reset_settle is None
        ):
            raise ConfigError(
                "verify_final needs enabled single_reset_policy, confirmed Initial State "
                "Contract, provider scene checks, explicit settle settings and allow_early_stop:false"
            )
        try:
            self.strategy()
        except rm.StrategyError as exc:
            raise ConfigError(str(exc)) from None
        if self.reset_strategy == "subtask_loop":
            self._check_subtask_loop()
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

    def _check_subtask_loop(self) -> None:
        for name in ("subtask_max_steps", "subtask_max_retries"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ConfigError(f"{name} is a whole number >= 1")
        cap = self.max_subtasks_per_loop
        if cap is not None and (type(cap) is not int or cap < 1):
            raise ConfigError("max_subtasks_per_loop is a whole number >= 1 or None")
        early = self.subtask_early_check
        if isinstance(early, dict):
            try:
                early = st.EarlyCheck(**early)
            except (TypeError, ValueError) as exc:
                raise ConfigError(str(exc)) from None
            object.__setattr__(self, "subtask_early_check", early)
        if early is not None and not isinstance(early, st.EarlyCheck):
            raise ConfigError("subtask_early_check is an EarlyCheck or None")
        for name in ("vlm_min_free_mem_gib",):
            value = getattr(self, name)
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ConfigError(f"{name} is a positive number (GiB)")
        headroom = self.vlm_wake_headroom_mib
        if type(headroom) is not int or headroom < 1024:
            raise ConfigError("vlm_wake_headroom_mib is a whole number >= 1024 (MiB)")
        need = self.vlm_wake_min_free_gpu_mib
        if need is not None and (type(need) is not int or need < 1):
            raise ConfigError("vlm_wake_min_free_gpu_mib is a whole number >= 1 (MiB)")
        for name in (
            "vlm_stay_awake_min_free_gpu_mib",
            "vlm_co_resident_headroom_mib",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ConfigError(f"{name} is a whole number >= 1 (MiB)")
        if self.vlm_alternation not in ("sleep", "co_resident"):
            raise ConfigError("vlm_alternation is sleep or co_resident")
        if self.vlm_sleep_fallback not in ("wait_human", "level2", "stay_awake"):
            raise ConfigError("vlm_sleep_fallback is wait_human, level2 or stay_awake")
        if self.home_after_loop is not True:
            raise ConfigError(
                "home_after_loop must be true: each loop ends at home, where the "
                "read-only start-pose check of the next loop finds the arm"
            )
        task = self.subtask_task
        if not isinstance(task, str) or not task.strip() or len(task) > aeri.MAX_TEXT:
            raise ConfigError(
                "subtask_loop needs the task name (task.instruction) the VLM plans from"
            )
        if self.termination.inference_mode != "legacy_async":
            raise ConfigError(
                "subtask_loop runs each subtask as an ordinary episode "
                "(legacy_async): event_pause_judge is refused"
            )
        if self.termination.allow_early_stop:
            raise ConfigError(
                "subtask_loop needs termination.allow_early_stop false: the VLM "
                "sleeps while the policy runs, so nothing judges mid-episode"
            )
        if self.scene_check != "provider":
            raise ConfigError("subtask_loop has no person's scene check")
        if not self.reset_enabled:
            raise ConfigError("subtask_loop needs reset enabled (its reverse loop)")
        base = self.forward_folder
        found = {
            "subtask_forward_folder": self.subtask_forward_folder or f"{base}__subtask",
            "subtask_reverse_folder": self.subtask_reverse_folder or f"{base}__reverse",
        }
        for name, value in found.items():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
                raise ConfigError(f"{name} {value!r} is not a folder name")
            object.__setattr__(self, name, value)
        if found["subtask_forward_folder"] == found["subtask_reverse_folder"]:
            raise ConfigError("the forward and the reverse loop need their own folders")

    @property
    def subtask_mode(self) -> bool:
        return self.reset_strategy == "subtask_loop"

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
    bounded_failure: bool = False
    policy_exception_before_motion: bool = False
    # The operator's verdict that ended this episode (``operator_verdict``).
    verdict: dict | None = None
    # subtask.early_check: the newest trigger event not yet checked, and the
    # trigger that ended this episode for an early check (stop reason
    # ``subtask_early_check``); steps on the attempt's scale.
    early_pending: dict | None = None
    early_trigger: dict | None = None


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
            {
                "name": p.name,
                "value": p.value,
                "required": p.required,
                "evidence_refs": [r.model_dump(mode="json") for r in p.evidence_refs],
            }
            for p in message.predicate_results
        ],
        "evidence_refs": [
            {"kind": r.kind, "ref": r.ref, "sha256": r.sha256}
            for r in message.evidence_refs
        ],
        "observed_ns": message.observed_ns,
        "produced_ns": message.produced_ns,
    }


def _discarded_by(event) -> str | None:
    """The episode an ``operator_verdict`` note discards (``d``), if any."""
    if event.record != "note" or event.note is None:
        return None
    if event.note.code != VERDICT_NOTE:
        return None
    try:
        value = json.loads(event.note.detail)
    except ValueError:
        return None
    if isinstance(value, dict) and value.get("label") == "discard":
        episode = value.get("episode_id")
        return episode if isinstance(episode, str) else None
    return None


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
        intervention: InferenceCycle | None = None,
        owned_calls: OwnedCalls | None = None,
        subtask_vlm=None,
        vlm_power=None,
        gate=None,
    ):
        self._check_intervention(config, clock, intervention, robot)
        self._check_owned_calls(owned_calls, intervention)
        self._check_subtask_parts(config, policy, subtask_vlm, vlm_power, gate)
        # subtask_loop (levi/automatic/subtask_loop.py): the VLM's three
        # questions, its sleep/wake, and terminal D's two questions.
        self.subvlm = subtask_vlm
        self.power = vlm_power
        self.gate = gate
        self.sub = st.SubtaskState() if config.subtask_mode else None
        self.owned_calls = owned_calls
        self.intervention = intervention
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
        # The operator's verdict on the forward episode under way, not yet
        # taken by the loop (one at most), and every verdict id seen.
        # Guarded by ``_stop_lock`` too.
        self._verdict: dict | None = None
        self._verdict_ids: set = set()
        self._reverse_window: dict | None = None
        self._reverse_requests: dict = {}
        self._reverse_pending: dict | None = None
        self._violation_hold = False
        # Set by a closed-gate home the bridge refused before its first command
        # (home_not_executed): the next hook use first rebuilds the binding's
        # closed interval with stop_admission.
        self._rebuild_admission = False
        # Set with it: the arm did not go home, so the next preflight checks the
        # start pose once, read-only (Robot.start_pose), before anything runs.
        # Episode history proves startup has ended after a process restart.
        # A startup crash without any episode remains closed to replay/resume.
        self.startup_home_active = config.start_home and not any(
            event.episode_id is not None for event in journal.events
        )
        self.startup_home_ready = None
        self._startup_gate_mark = None
        self._startup_stop_gen = None
        self._startup_failed = False
        self.startup_stop_seen = threading.Event()
        self._check_start_pose = False
        self._scene_violations = 0
        self._reset_attempts = 0
        self._last_forward: str | None = None
        self.halted: str | None = None
        self.fault_reason: str | None = None
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

    @staticmethod
    def _check_intervention(config, clock, intervention, robot=None):
        if (
            config.termination.inference_mode != "event_pause_judge"
            and intervention is not None
        ):
            raise ConfigError(
                "intervention injection belongs only to event_pause_judge"
            )
        if config.termination.inference_mode == "event_pause_judge":
            if not isinstance(intervention, InferenceCycle):
                raise ConfigError(
                    "event_pause_judge requires injected owned role authority, backend barrier and physical hold observer"
                )
            intervention.validate(config, clock)
            if config.forward_max_steps > 400 or config.reset_max_steps > 400:
                raise ConfigError(
                    "event_pause_judge permits at most 400 executed actions"
                )
            if config.scene_check != "provider":
                raise ConfigError(
                    "event_pause_judge requires the owned provider scene path"
                )
            # A finished paused episode is homed under the closed gate: a robot
            # without that entry would leave the arm where the judgement found it.
            if not callable(getattr(robot, "home_under_closed_gate", None)):
                raise ConfigError(
                    "event_pause_judge requires a robot with home_under_closed_gate"
                )

    @staticmethod
    def _check_subtask_parts(config, policy, subtask_vlm, vlm_power, gate):
        given = (subtask_vlm, vlm_power, gate)
        if not config.subtask_mode:
            if any(part is not None for part in given):
                raise ConfigError(
                    "subtask parts belong only to reset_strategy subtask_loop"
                )
            return
        if any(part is None for part in given):
            raise ConfigError(
                "subtask_loop needs the subtask VLM, its sleep/wake (vlm_power) "
                "and terminal D's gate"
            )
        import inspect

        try:
            accepts = "prompt" in inspect.signature(policy.acquire).parameters
        except (TypeError, ValueError):
            accepts = False
        if not accepts:
            raise ConfigError(
                "subtask_loop needs a policy whose acquire takes the episode's prompt"
            )

    @staticmethod
    def _check_owned_calls(owned_calls, intervention):
        if owned_calls is None:
            return
        if not isinstance(owned_calls, OwnedCalls) or intervention is None:
            raise ConfigError("owned_calls requires the explicit intervention cycle")
        try:
            owned_calls.validate(intervention)
        except Exception as exc:
            raise ConfigError("owned_calls original execution binding invalid") from exc

    @classmethod
    def create(
        cls, directory, config: RunConfig, *, plan: dict | None = None, **parts
    ) -> "Orchestrator":
        """Start a run: its header names the reset mode and the scene check;
        ``plan`` (the job's normalised plan, hashing to the config's
        ``plan_sha256``) is kept as the run folder's ``plan.json``."""
        cls._check_intervention(
            config, parts["clock"], parts.get("intervention"), parts.get("robot")
        )
        cls._check_owned_calls(parts.get("owned_calls"), parts.get("intervention"))
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
        cls._check_intervention(
            config, parts["clock"], parts.get("intervention"), parts.get("robot")
        )
        cls._check_owned_calls(parts.get("owned_calls"), parts.get("intervention"))
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
        orchestrator._restore_home_wait()
        orchestrator._recover_rollouts()
        orchestrator._tell(orchestrator.state)
        return orchestrator

    def _restore_home_wait(self) -> None:
        """A restart while the run waited on a home that sent nothing
        (``home_not_executed``) or on its failed pose check
        (``home_pose_not_reached``) keeps both duties of that wait: the start
        pose check before going on and the binding's interval rebuild. Taken
        from the journal: the last committed move into WAIT_HUMAN has one of
        those reasons and no episode started after it (an episode start comes
        after the rebuild). Re-checking a pose that already passed is harmless."""
        waiting = False
        for event in self.journal.events:
            if event.record != "committed":
                continue
            if event.to_state == "WAIT_HUMAN":
                waiting = event.reason in ("home_not_executed", "home_pose_not_reached")
            elif event.to_state in ("FORWARD_ACTIVE", "RESET_ACTIVE"):
                waiting = False
        if waiting:
            self._check_start_pose = True
            self._rebuild_admission = True

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
        # Rounds discarded with ``d`` and replaced by another round (at most
        # ``discard_limit``); the journal's ``operator_verdict`` notes say
        # which, in the same order as the live run decided it.
        self.discarded_forward = 0
        # Discarded rounds beyond the limit: not replaced, never valid.
        self.discarded_beyond_limit = 0
        discards: set = set()
        if self.config.subtask_mode:
            self._read_back_subtasks()
        for event in self.journal.events:
            if event.episode_id:
                _, role, number = aeri.episode_parts(event.episode_id)
                self.numbers[role] = max(self.numbers[role], number)
            if event.policy_epoch is not None:
                self.policy_epoch = max(self.policy_epoch, event.policy_epoch)
            discarded = _discarded_by(event)
            if discarded is not None:
                discards.add(discarded)
            if self._counts(event) and not self.config.subtask_mode:
                if (
                    event.episode_id in discards
                    and self.discarded_forward < self.discard_limit
                ):
                    self.discarded_forward += 1
                else:
                    self.completed_forward += 1
                    if event.episode_id in discards:
                        self.discarded_beyond_limit += 1

    @property
    def discard_limit(self) -> int:
        """How many discarded rounds a run replaces (``max_discarded_rounds``,
        by default the run's number of rounds)."""
        limit = self.config.max_discarded_rounds
        return self.config.episodes if limit is None else limit

    def _replaces(self, ctx: Episode) -> bool:
        """This sealed round was discarded with ``d`` and another round
        replaces it (within ``discard_limit``). Beyond the limit it is not
        replaced: it uses up one of the run's rounds but stays out of every
        statistic, so the run ends with fewer valid rounds
        (``discard_limit_reached`` says so)."""
        verdict = ctx.verdict
        if verdict is None or verdict.get("label") != "discard":
            return False
        if self.discarded_forward < self.discard_limit:
            return True
        self.discarded_beyond_limit += 1
        self.journal.note(
            DISCARD_LIMIT_NOTE,
            f"{ctx.episode_id}: discarded, not replaced (limit "
            f"{self.discard_limit}); the run ends with fewer valid rounds",
            authority=self._auth(),
        )
        return False

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
        folder = self._folder(role)
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
            and (event.from_state, event.to_state)
            in {("FORWARD_FINALIZE", "ROBOT_HOME"), ("FORWARD_JUDGING", "WAIT_HUMAN")}
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

    def note(self, code: str, detail: str = "") -> None:
        """A fact from the entry point (start pose, warm-up) for the journal."""
        self._note(code, detail)

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
        if to not in ("RESET_ACTIVE", "RESET_VERIFY", "RESET_FINALIZE"):
            self._close_reverse_window()
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

    def _fault(
        self,
        reason: str,
        *,
        guard: bool = False,
        hold: bool = True,
        stop_reason: str | None = None,
    ) -> None:
        """Lock the run. An episode still open gets its result (unknown,
        stopped by ``reason``)."""
        self._close_reverse_window()
        self.fault_reason = reason
        # A verdict registered for the episode the fault ends is never taken
        # (it would block the next episode's verdict): cleared now, noted
        # after the lock's hold.
        with self._stop_lock:
            dropped, self._verdict = self._verdict, None
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
            result = self._fault_result(ctx, reason, stop_reason)
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
        # Again once FAULT_LOCKED is committed: a verdict registered in the
        # gap before the commit (the state still FORWARD_ACTIVE) is cleared
        # too; none can be registered from now on.
        with self._stop_lock:
            late, self._verdict = self._verdict, None
        for found in (dropped, late):
            if found is not None:
                with contextlib.suppress(Exception):  # the lock matters more
                    self._drop_verdict_note(found, f"the run locked ({reason})")

    def _fault_result(
        self, ctx: Episode, reason: str, stop_reason: str | None = None
    ) -> dict:
        """pipeline §4.3: an episode ended by a fault is recorded, its outcome
        unknown and its stop reason the fault (X1 §2.5 gave it no result)."""
        stop = stop_reason or reason
        if stop not in aeri.STOP_REASONS:
            stop = "watchdog_timeout"
        folder = self._folder(ctx.role)
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
        self._close_reverse_window()
        self.fence.revoke()
        if self.intervention is not None:
            self.intervention.interrupt(
                proof_failed=not isinstance(exc, InterventionStopped)
            )
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

    def request_start_home(self, command_id, *, expected_seq, confirmation):
        """Explicit first-start-only home; no episode, retries or fault rearm."""
        with self._lock:
            if self.journal.by_command(command_id):
                return CommandResult(False, "command_used", self.state, True)
            if type(command_id) is not str or not command_id or len(command_id) > 128:
                return CommandResult(False, "bad_command", self.state)
            if type(expected_seq) is not int or expected_seq != self.journal.next_seq:
                return CommandResult(False, "stale_sequence", self.state)
            if (
                not self.config.start_home
                or not self.startup_home_active
                or self.state != "PREFLIGHT"
                or self.ctx is not None
            ):
                return CommandResult(False, "startup_home_unavailable", self.state)
            # A restart never authorizes replay, even after a generic resume.
            if self.recovery is not None or any(
                e.episode_id is not None
                or e.reason == "recovery_ambiguous"
                or (e.reason == "startup_home_requested" and e.record == "committed")
                for e in self.journal.events
            ):
                return CommandResult(False, "startup_home_already_used", self.state)
            if (
                not isinstance(confirmation, dict)
                or set(confirmation)
                != {"object_handled", "path_clear", "gripper_will_open"}
                or any(v is not True for v in confirmation.values())
            ):
                return CommandResult(False, "confirmations_missing", self.state)
            if not self._startup_ready():
                return CommandResult(False, "startup_safety_not_ready", self.state)

            def perform():
                if self.robot.admission_snapshot().mode != "open":
                    return CommandResult(False, "startup_gate_not_owned", self.state)
                with self._stop_lock:
                    if self._stops or self.startup_stop_seen.is_set():
                        return CommandResult(
                            False, "startup_stop_before_home", self.state
                        )
                    generation = self._stop_gen
                    closure = self.robot.close_admission_if_open()
                before = self._gate_counts()
                if before is None or not self.robot.admission_snapshot().closed:
                    raise ValueError("startup home closure or counters unavailable")

                reached = False

                def act(prepared):
                    nonlocal reached
                    ready = (
                        self._startup_ready()
                    )  # potentially slow health I/O outside stop lock
                    with self._stop_lock:
                        snap = self.robot.admission_snapshot()
                        if (
                            snap.generation,
                            snap.closed_from_ns,
                        ) != closure or not snap.closed:
                            return "no", "none", "startup_closure_changed"
                        if (
                            not ready
                            or self._stops
                            or self.startup_stop_seen.is_set()
                            or self._stop_gen != generation
                        ):
                            return "no", "none", "startup_stop_before_home"
                        token = self.fence.issue(
                            sm.MotionToken(
                                run_id=self.config.run_id,
                                transaction_id=prepared.transaction_id,
                                control_epoch=prepared.control_epoch,
                                kind="home",
                                episode_id=self.config.run_id + ":startup-home",
                                policy_epoch=None,
                                expires_mono_ns=self._now()
                                + self.config.home_timeout_ns,
                            )
                        )
                    try:
                        reached = True
                        result = self.robot.home_under_closed_gate(token=token)
                    finally:
                        self.fence.revoke()
                    self._result_notes(result)
                    if result.executed == "yes" and (
                        not self._startup_ready() or not self._start_pose_ok()
                    ):
                        return (
                            "unknown",
                            "robot_server",
                            "startup_home_postcheck_failed",
                        )
                    return result.executed, "robot_server", result.detail

                tx = self._tx(
                    "PREFLIGHT",
                    "startup_home_requested",
                    kind="home",
                    act=act,
                    authority=self._auth("operator", command_id),
                    expected_seq=expected_seq,
                    step=expected_seq,
                )
                self.fence.revoke()
                if tx.executed != "yes":
                    self._startup_failed = True
                    self._note("home_failed_detail", tx.detail)
                    if (
                        tx.executed == "no"
                        and reached
                        and self._home_sent_nothing(before)
                    ):
                        self._to_human("startup_home_not_executed")
                    else:
                        self._fault("home_failed")
                    return CommandResult(False, tx.detail, self.state)
                self._startup_stop_gen = generation
                self._startup_gate_mark = self.robot.admission_snapshot()
                mark = self._startup_gate_mark
                if (
                    not mark.closed
                    or mark.mode != "closed"
                    or mark.generation != closure[0] + 2
                    or mark.home_only_from_ns is None
                    or mark.home_only_from_ns < closure[1]
                    or self._stop_gen != generation
                ):
                    self._startup_gate_mark = None
                    raise ValueError("startup home did not preserve the owned closure")
                self._note(
                    "startup_home_completed",
                    "gate closed; independent round release required",
                )
                return CommandResult(True, "startup_home_completed", self.state)

            try:
                return self._guarded(perform)
            except JournalRefused as exc:
                if exc.code == "E_SEQ":
                    return CommandResult(False, "stale_sequence", self.state)
                raise

    def _startup_ready(self):
        try:
            return (
                callable(self.startup_home_ready)
                and self.startup_home_ready() is True
                and not self.robot.stopped()
                and not self.robot.latch_state()
                and self.robot.health()["state"] == "ok"
            )
        except Exception:  # noqa: BLE001 - missing safety evidence refuses motion
            return False

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
            if self.config.start_home and (
                self._startup_gate_mark is not None
                or self._startup_failed
                or (self.startup_home_active and self.recovery is not None)
                or any(
                    e.reason == "startup_home_not_executed" for e in self.journal.events
                )
            ):
                return CommandResult(False, "startup_new_run_required", self.state)
            with self._stop_lock:
                # Stops registered from now on are later than this resume.
                generation = self._stop_gen
            event_mode = self.config.termination.inference_mode == "event_pause_judge"
            prior = self.journal.by_command(command_id)
            if prior:
                if prior[0].reason == "human_resumed":
                    if event_mode:
                        outcomes = {
                            event.note.code
                            for event in prior
                            if event.record == "note"
                            and event.transaction_id == prior[0].transaction_id
                        }
                        if "resume_rearm_refused" in outcomes:
                            return CommandResult(
                                False, "intervention_latched", self.state, True
                            )
                        committed = any(
                            event.record == "committed"
                            and event.transaction_id == prior[0].transaction_id
                            for event in prior
                        )
                        if "resume_rearm_accepted" not in outcomes or not committed:
                            # Old event-mode logs remain readable, but no
                            # outcome marker means the result is unknown.
                            return CommandResult(
                                False, "command_used", self.state, True
                            )
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
            cycle = self.intervention
            rearm_needed = False
            if cycle is not None:
                with cycle._lock:
                    rearm_needed = cycle.failed and cycle.operator_stopped
                    if rearm_needed and (
                        cycle.proof_failed
                        or cycle._request is not None
                        or cycle.revocation_error is not None
                    ):
                        # Inspect eligibility without mutating the cycle. The
                        # journal's actual prepare CAS must succeed first.
                        return CommandResult(False, "intervention_latched", self.state)
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

            def record_outcome(code, detail):
                try:
                    self.journal.note(
                        code,
                        detail,
                        authority=self._auth("operator", command_id),
                        transaction_id=found.prepared.transaction_id,
                    )
                except Exception as exc:
                    self._contain(exc)
                    raise

            with self._stop_lock:
                # Rearm only after the journal CAS and durable commit. A stop
                # registered during this transaction must keep its latch; the
                # generation check and rearm share the stop registry lock.
                rearm_refused = (
                    rearm_needed and self._stop_gen == generation and not cycle.rearm()
                )
                if rearm_refused:
                    lost = []
                else:
                    # Only earlier stops are overridden; later stops remain.
                    lost = sorted(c for c, g in self._stops.items() if g <= generation)
                    for command in lost:
                        del self._stops[command]
                    self._consumed_stops.clear()
            if rearm_refused:
                # A proof may fail after the eligibility inspection. Never
                # leave that committed PREFLIGHT runnable or clear its stops.
                self._contain(InterventionUnproven("resume cycle became latched"))
                # A committed state transition alone cannot establish the
                # command result. Both outcomes must be durable in event mode.
                record_outcome("resume_rearm_refused", "intervention_latched")
                return CommandResult(False, "intervention_latched", self.state)
            if lost:
                # Stops that never took effect (the run locked or waited
                # first): kept in the journal, then cleared by the resume.
                self._note("stop_command_lost", ", ".join(lost)[:300])
            self._violation_hold = False
            self._scene_violations = 0
            self._reset_attempts = 0
            # subtask_loop: a resume never continues a round (R13).
            self._abandon_round("human_resumed")
            if event_mode:
                record_outcome("resume_rearm_accepted", "resumed")
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
            if self.startup_home_active:
                self.startup_stop_seen.set()
                self.fence.revoke()
            if self.intervention is not None:
                self.intervention.note_operator_stop()
        if self.intervention is not None:
            self.fence.revoke()
            self.intervention.interrupt(operator=True)
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

    def stop_requested(self) -> bool:
        """An operator's stop is registered and not yet consumed. Read-only
        and thread safe: an adapter that waits inside a transaction (a policy
        service switch) polls it to give up promptly."""
        return self._first_stop() is not None

    def reverse_human_scope(self) -> dict | None:
        """Snapshot of the current reverse-only terminal request window."""
        with self._stop_lock:
            return dict(self._reverse_window) if self._reverse_window else None

    def reverse_human_request(
        self,
        command_id: str,
        *,
        run_id: str,
        round_no: int,
        episode_id: str,
        policy_epoch: int,
        source: str = "terminal",
        by: str = "operator",
    ) -> CommandResult:
        """Register a scoped ordinary stop without waiting for adapters.

        Already admitted step calls (potentially several robot POSTs) may
        finish. Acceptance never means the robot is safe to approach.
        No token is revoked here: the owning loop quiesces and seals first.
        """
        from . import metrics

        if (
            not isinstance(command_id, str)
            or not _VERDICT_ID.fullmatch(command_id)
            or source != "terminal"
            or not isinstance(by, str)
            or not metrics.PRINCIPAL.fullmatch(by)
            or not isinstance(run_id, str)
            or not isinstance(episode_id, str)
            or type(round_no) is not int
            or round_no < 1
            or type(policy_epoch) is not int
            or policy_epoch < 1
        ):
            return CommandResult(False, "invalid", self.state)
        scope = {
            "run_id": run_id,
            "round_no": round_no,
            "episode_id": episode_id,
            "policy_epoch": policy_epoch,
        }
        payload = dict(scope, source=source, by=by)
        journal_used = bool(self.journal.by_command(command_id))
        with self._stop_lock:
            prior = self._reverse_requests.get(command_id)
            if prior is not None:
                if prior[0] != payload:
                    return CommandResult(False, "command_used", self.state)
                return prior[1]
            if self.sub is None or self.intervention is not None:
                return CommandResult(False, "not_supported", self.state)
            if self._reverse_window != scope or self.state not in (
                "RESET_ACTIVE",
                "RESET_VERIFY",
                "RESET_FINALIZE",
            ):
                return CommandResult(False, "stale_reverse", self.state)
            if (
                journal_used
                or command_id in self._verdict_ids
                or command_id in self._consumed_stops
                or command_id in self._stops
            ):
                return CommandResult(False, "command_used", self.state)
            if self._stops:
                return CommandResult(False, "stop_pending", self.state)
            # Memory only under this lock. In particular, no _lock, journal,
            # listener or adapter call: _tx may be waiting inside an adapter.
            self._stop_gen += 1
            self._stops[command_id] = self._stop_gen
            answer = CommandResult(True, "reverse_human_requested", self.state)
            self._reverse_requests[command_id] = (payload, answer)
            self._reverse_pending = dict(
                payload, command_id=command_id, step=self.ctx.last_step
            )
            return answer

    def _close_reverse_window(self) -> dict | None:
        # Closing and selecting the pending request is one linearization
        # point, before any next-subtask decision or terminal notification.
        with self._stop_lock:
            self._reverse_window = None
            pending, self._reverse_pending = self._reverse_pending, None
            command = min(self._stops, key=self._stops.get) if self._stops else None
        if pending is not None:
            self._note("reverse_human_requested", json.dumps(pending, sort_keys=True))
        return self._auth("operator", command) if command is not None else None

    def episode_verdict(
        self,
        command_id: str,
        label: str,
        *,
        source: str = "terminal",
        by: str = "operator",
        wall_ns: int | None = None,
    ) -> CommandResult:
        """An operator's verdict on the forward episode under way (terminal
        D's ``s``/``f``/``d``, L2-AERI-OPERATOR-EARLY-STOP): ``success``,
        ``failure`` or ``discard``. Like ``stop`` it only registers and never
        waits for the state lock; the loop takes it at its next step, after
        the latch and health check, and ends *this episode* with
        ``operator_verdict`` through the ordinary hold, quiesce, seal and home
        (no new motion, no skipped check). The run then goes on to the next
        episode; a stop (``stop``) still ends the run and wins over a verdict.

        Taken only while a forward episode runs (``FORWARD_ACTIVE``) of a
        ``legacy_async`` run, from the terminal only (``source``; the page
        and the command channel have no route to it), one per episode.
        Refused: ``invalid`` (label, source, id or principal), ``not_supported``
        (event_pause_judge), ``command_used``, ``stop_pending``,
        ``not_in_forward``, ``verdict_pending``. The same id again is
        ``repeated``. ``verdict_requested`` means it will be taken unless the
        episode ends first (then it is dropped, ``operator_verdict_dropped``)."""
        from . import metrics

        if (
            label not in VERDICT_LABELS
            or source not in VERDICT_SOURCES
            or not isinstance(command_id, str)
            or not _VERDICT_ID.fullmatch(command_id)
            or not isinstance(by, str)
            or not metrics.PRINCIPAL.fullmatch(by)
        ):
            return CommandResult(False, "invalid", self.state)
        if self.intervention is not None:
            return CommandResult(False, "not_supported", self.state)
        with self._stop_lock:
            if command_id in self._verdict_ids:
                return CommandResult(True, "repeated", self.state, True)
            used = command_id in self._stops or command_id in self._consumed_stops
        if used or self.journal.by_command(command_id):
            return CommandResult(False, "command_used", self.state)
        wall_ns = time.time_ns() if wall_ns is None else int(wall_ns)
        with self._stop_lock:
            if command_id in self._verdict_ids:
                return CommandResult(True, "repeated", self.state, True)
            if self._stops:
                return CommandResult(False, "stop_pending", self.state)
            ctx = self.ctx
            if self.state != "FORWARD_ACTIVE" or ctx is None or ctx.role != "forward":
                return CommandResult(False, "not_in_forward", self.state)
            if self._verdict is not None or ctx.verdict is not None:
                return CommandResult(False, "verdict_pending", self.state)
            self._verdict_ids.add(command_id)
            self._verdict = {
                "command_id": command_id,
                "label": label,
                "source": source,
                "by": by,
                "wall_ns": wall_ns,
                "episode_id": ctx.episode_id,
                # Where the episode was when the key came: its control step
                # and the index of the last frame recorded (-1: none yet).
                "step": ctx.last_step,
                "frame_index": ctx.steps_done - 1,
            }
        return CommandResult(True, "verdict_requested", self.state)

    def _take_verdict(self, ctx: Episode) -> dict | None:
        """The registered verdict for this episode, taken (``None``: none)."""
        with self._stop_lock:
            found, self._verdict = self._verdict, None
        if found is None:
            return None
        if found["episode_id"] != ctx.episode_id:
            self._drop_verdict_note(found, "another episode")
            return None
        return found

    def _drop_verdict(self, why: str) -> None:
        """A verdict the loop will not take (the episode ended first)."""
        with self._stop_lock:
            found, self._verdict = self._verdict, None
        if found is not None:
            self._drop_verdict_note(found, why)

    def _drop_verdict_note(self, found: dict, why: str) -> None:
        self.journal.note(
            VERDICT_DROPPED_NOTE,
            f"{found['label']} for {found['episode_id']} at step {found['step']}: "
            f"{why}"[:300],
            authority=self._auth("operator"),
        )

    def _record_verdict(self, ctx: Episode, verdict: dict, taken_step: int) -> None:
        """The ``operator_verdict`` journal note: the person's verdict, kept
        apart from the model's judgements (``judgement_*`` notes, the episode
        result) and written before the episode is brought to its end."""
        verdict["taken_step"] = taken_step
        fields = {
            k: verdict[k]
            for k in (
                "label",
                "source",
                "episode_id",
                "step",
                "frame_index",
                "taken_step",
                "wall_ns",
                "command_id",
            )
        }
        detail = json.dumps(fields, separators=(",", ":"), sort_keys=True)
        if len(detail) > 300:
            # The id is in the transition's authority as well.
            fields.pop("command_id")
            detail = json.dumps(fields, separators=(",", ":"), sort_keys=True)
        self.journal.note(VERDICT_NOTE, detail[:300], authority=self._auth("operator"))
        ctx.verdict = verdict

    def _label_verdict(self, ctx: Episode) -> None:
        """Once the episode's result is committed: the verdict becomes the
        operator's label of the episode (``labels/operator_label.jsonl``,
        apart from the journal; ``discard`` -> ``discarded``, left out of
        every truth and rate). A label that cannot be written is noted; the
        run goes on."""
        from . import metrics

        verdict = ctx.verdict
        try:
            metrics.label_operator(
                self.journal.directory,
                ctx.episode_id,
                OPERATOR_LABEL_OF_VERDICT[verdict["label"]],
                by=verdict["by"],
                note=(
                    f"{VERDICT_NOTE} {verdict['source']} step {verdict['step']} "
                    f"frame {verdict['frame_index']}"
                ),
            )
        except (metrics.LabelRefused, OSError) as exc:
            self._note(VERDICT_LABEL_FAILED_NOTE, f"{ctx.episode_id}: {exc}")

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

    def _start_pose_ok(self) -> bool:
        """The start gate's own read-only check (``Robot.start_pose``: the bound
        home pose, the home tolerance and the open gripper; it sends nothing).
        Only ``ok is True`` passes; anything else, a missing method or an
        exception is a refusal. The measurement goes to ``start_pose_checked``."""
        check = getattr(self.robot, "start_pose", None)
        if not callable(check):
            self._note("start_pose_checked", "refused: the robot has no start_pose")
            return False
        try:
            found = check()
        except Exception as exc:  # noqa: BLE001 - unreadable pose: do not resume
            self._note("start_pose_checked", f"refused: {type(exc).__name__}: {exc}")
            return False
        ok = isinstance(found, dict) and found.get("ok") is True
        text = found.get("text") if isinstance(found, dict) else None
        self._note(
            "start_pose_checked",
            ("" if ok else "refused: ") + str(text if text is not None else found),
        )
        if ok:
            self._check_start_pose = False
        return ok

    def _operator(self) -> dict | None:
        """The authority of the oldest stop not yet consumed, if any."""
        command = self._first_stop()
        if command is None:
            return None
        return self._auth("operator", command)

    def _preflight(self) -> None:
        report = self.robot.preflight()
        if report["ok"]:
            if self.startup_home_active and not self._startup_ready():
                self._note(
                    "startup_safety_not_ready",
                    "Startup refused: terminal, signal handlers and successful live heartbeat required; no home sent",
                )
                self._to_human("preflight_failed")
                return
            if self.startup_home_active and self._startup_failed:
                self._to_human("startup_home_not_executed")
                return
            if (
                self.startup_home_active
                and self.ctx is None
                and not self._start_pose_ok()
            ):
                prompt = getattr(self.gate, "confirm_start_home", None)
                if not callable(prompt) or not self._startup_ready():
                    self._to_human("home_pose_not_reached")
                    return
                self._flush_notes()
                sequence = self.journal.next_seq
                confirmation = prompt()
                found = self.request_start_home(
                    f"startup-home-{sequence}",
                    expected_seq=sequence,
                    confirmation=confirmation,
                )
                if not found.ok and self.state == "PREFLIGHT":
                    self._to_human("home_pose_not_reached")
                return
            if self.startup_home_active and self._startup_gate_mark is None:
                # Already home: no startup motion/closure to authorize. Later
                # round stop/resume semantics stay exactly as before.
                self.startup_home_active = False
            if self._check_start_pose and not self._start_pose_ok():
                # The arm is not proven at home after a home that sent nothing:
                # the person brings it home; the next resume checks again.
                self._to_human("home_pose_not_reached")
                return
            self._tx("VERIFY_INITIAL", "preflight_passed")
        elif self.robot.latch_state() or self.robot.health()["state"] == "red":
            self._fault("safety_stop", guard=True)
        else:
            self._to_human("preflight_failed")

    def _decide(self) -> None:
        if self.sub is not None:
            self._decide_subtask()
            return
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
        if self.state == "FAULT_LOCKED":
            return
        # A stop that arrived while the scene was being assessed.
        if self._stop_before_start():
            return
        if self._scene_evidence_failed:
            self._to_human(rm.scene_reason("unavailable"))
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

    # --- subtask_loop (levi/automatic/subtask_loop.py) ---------------------------------

    def _sidecar(self, name: str, value: dict) -> dict:
        """``value`` in full under ``<run_dir>/subtasks/``: ``{"file",
        "file_sha256"}`` for the note that refers to it (``{"file": None}``
        when it cannot be written: noted, the run goes on)."""
        data = (
            json.dumps(value, indent=1, sort_keys=True, default=str) + "\n"
        ).encode()
        relative = f"{st.SIDECAR_DIR}/{self.journal.next_seq:06d}-{name}.json"
        try:
            path = self.journal.directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            self._note("evidence_write_failed", f"{relative}: {exc}")
            return {"file": None}
        return {"file": relative, "file_sha256": hashlib.sha256(data).hexdigest()}

    def _sub_note(self, code: str, detail: dict, *, sidecar: dict | None = None):
        """One ``subtask_*``/``vlm_*`` journal note, its detail JSON. A
        ``sidecar`` (the request record, the plan) is written in full and
        named in the note; a detail over 300 characters keeps its short fields
        and moves the whole into a sidecar too."""
        detail = dict(detail)
        if sidecar is not None:
            detail.update(self._sidecar(code, sidecar))
        text = json.dumps(detail, separators=(",", ":"), sort_keys=True, default=str)
        if len(text) > 300:
            short = {
                k: v
                for k, v in detail.items()
                if v is None
                or isinstance(v, bool | int | float)
                or (isinstance(v, str) and len(v) <= 80)
            }
            if sidecar is None:
                short.update(self._sidecar(code + "-detail", detail))
            text = json.dumps(short, separators=(",", ":"), sort_keys=True)
            if len(text) > 300:
                text = json.dumps(
                    {k: short.get(k) for k in ("file", "file_sha256")},
                    separators=(",", ":"),
                )
        self.journal.note(code, text, authority=self._auth())
        # Also in memory, for the terminal's recent-notes lines.
        self.notes.append((code, text))
        return detail

    def _read_back_subtasks(self) -> None:
        """The frozen whitelist and the rounds the forward loops ended, from the
        journal (a restart never continues a round)."""
        sub = self.sub
        for event in self.journal.events:
            if event.record != "note" or event.note is None:
                continue
            code = event.note.code
            if code not in (st.NOTE_INVENTORY_CONFIRMED, st.NOTE_LOOP_DONE):
                continue
            try:
                detail = json.loads(event.note.detail)
            except ValueError:
                continue
            if code == st.NOTE_INVENTORY_CONFIRMED:
                sub.inventory = self._inventory_from(detail)
                sub.whitelist_sha256 = detail.get("sha256")
            elif detail.get("loop") == "forward":
                self._count_round(detail.get("outcome"))

    def _inventory_from(self, detail: dict):
        """The confirmed inventory from its sidecar (checked by its sha256)."""
        try:
            path = self.journal.directory / detail["file"]
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != detail["file_sha256"]:
                return None
            objects = json.loads(data)["objects"]
            found = st.parse_inventory(json.dumps({"objects": objects}))
        except (OSError, KeyError, TypeError, ValueError):
            return None
        if st.whitelist_sha256(found.whitelist) != detail.get("sha256"):
            return None
        return found

    def _count_round(self, outcome) -> None:
        """A forward loop ended: a round, or a discarded one (replaced within
        ``discard_limit``)."""
        if outcome == "discard" and self.discarded_forward < self.discard_limit:
            self.discarded_forward += 1
            return
        self.completed_forward += 1
        if outcome == "discard":
            self.discarded_beyond_limit += 1

    def _abandon_round(self, why: str) -> None:
        """A resume (or a restart) never continues a round: the next one starts
        with the start-pose check and terminal D's release (R13)."""
        sub = self.sub
        if sub is None or (sub.phase == "idle" and not sub.plan):
            return
        self._sub_note(
            st.NOTE_ROUND_ABANDONED,
            {"round": sub.round_no, "phase": sub.phase, "why": why},
        )
        sub.phase, sub.plan, sub.index, sub.attempt = "idle", [], 0, 1

    @staticmethod
    def _reading(probe):
        try:
            value = probe()
        except Exception:  # noqa: BLE001 - an unreadable reading is None
            return None
        if type(value) not in (int, float) or not math.isfinite(value):
            return None
        return round(value, 2) if isinstance(value, float) else value

    def _wake_need_mib(self) -> int:
        """Free GPU memory a wake needs: the configured value, else the VLM
        profile's (``vlm_power.wake_need_mib``), else the default. A provider
        may report zero additional memory; the headroom guard still applies."""
        if self.config.vlm_wake_min_free_gpu_mib is not None:
            return self.config.vlm_wake_min_free_gpu_mib
        probe = getattr(self.power, "wake_need_mib", None)
        found = self._reading(probe) if callable(probe) else None
        if type(found) is int and found >= 0:
            return found
        return st.DEFAULT_WAKE_NEED_MIB

    def _gpu_totals(self):
        """``[used, total]`` MiB (nvidia-smi, read-only) or None."""
        probe = getattr(self.power, "gpu_totals", None)
        if probe is None:
            return None
        try:
            used, total = probe()
        except Exception:  # noqa: BLE001 - a reading only
            return None
        return [used, total] if all(type(v) is int for v in (used, total)) else None

    def _power(self, call) -> bool:
        try:
            return call() is True
        except Exception as exc:  # noqa: BLE001 - a failure is a refusal
            self._note("vlm_power_error", f"{type(exc).__name__}: {exc}")
            return False

    def _vlm_wake(self) -> str | None:
        """Before every VLM call: the VLM awake (``None``), or why the run waits
        (R7: never woken into a GPU short of memory; a failed or timed-out wake
        moves nothing)."""
        sub, cfg = self.sub, self.config
        if sub.vlm in ("awake", "idle_awake") or cfg.vlm_alternation == "co_resident":
            sub.last_wake = {"was": sub.vlm, "wake_ms": 0}
            sub.vlm = "awake"
            return None
        free = self._reading(self.power.free_gpu_mib)
        need = self._wake_need_mib()
        threshold = need + cfg.vlm_wake_headroom_mib
        # R7: the one comparison that decides whether the VLM may wake.
        if free is None or free < threshold:
            self._sub_note(
                st.NOTE_VLM_WAKE_REFUSED,
                {
                    "reason": "gpu_unreadable" if free is None else "gpu_memory_short",
                    "free_mib": free,
                    "need_mib": need,
                    "headroom_mib": cfg.vlm_wake_headroom_mib,
                    "threshold_mib": threshold,
                    "decision": "refused",
                },
            )
            return st.R_VLM_UNAVAILABLE
        before = self._reading(self.power.mem_available_gib)
        gpu_before = self._gpu_totals()
        began = time.monotonic()
        ok = self._power(self.power.wake)
        ms = int((time.monotonic() - began) * 1000)
        self._sub_note(
            st.NOTE_VLM_WAKE,
            {
                "ok": ok,
                "mem_available_gib": before,
                "mem_after_gib": self._reading(self.power.mem_available_gib),
                "ms": ms,
                "wake_s": round(ms / 1000, 3),
                "free_mib": free,
                "need_mib": need,
                "threshold_mib": threshold,
                "decision": "woken" if ok else "failed",
                "gpu_before": gpu_before,
                "gpu_after": self._gpu_totals(),
                "was": sub.vlm,
            },
        )
        if not ok:
            self._sub_note(
                st.NOTE_VLM_WAKE_FAILED,
                {
                    "reason": getattr(self.power, "last_refusal", None)
                    or "wake_failed_or_timeout",
                    "ms": ms,
                },
            )
            sub.vlm = "unknown"
            return st.R_VLM_UNAVAILABLE
        sub.last_wake = {"was": sub.vlm, "wake_ms": ms}
        sub.vlm = "awake"
        return None

    def _vlm_sleep(self) -> str | None:
        """Before every policy episode: the VLM asleep (``None``), or why the
        run waits. A level-1 sleep moves the weights to pinned host memory, so
        MemAvailable is checked first; below ``vlm_min_free_mem_gib`` the
        fallback decides (R7)."""
        sub, cfg = self.sub, self.config
        if sub.vlm == "asleep":
            return None
        if cfg.vlm_alternation == "co_resident":
            # Loaded beside the policy: no sleep, and no request until the
            # episode has ended (every question sits between episodes).
            sub.vlm = "idle_awake"
            return None
        mem = self._reading(self.power.mem_available_gib)
        level = 1
        if mem is None or mem < cfg.vlm_min_free_mem_gib:
            fallback = cfg.vlm_sleep_fallback
            if fallback == "wait_human":
                self._sub_note(
                    st.NOTE_VLM_SLEEP_REFUSED,
                    {
                        "reason": "host_memory_short",
                        "mem_available_gib": mem,
                        "need_gib": cfg.vlm_min_free_mem_gib,
                    },
                )
                return st.R_VLM_UNAVAILABLE
            if fallback == "stay_awake":
                free = self._reading(self.power.free_gpu_mib)
                need = cfg.vlm_stay_awake_min_free_gpu_mib
                if free is None or free < need:
                    self._sub_note(
                        st.NOTE_VLM_SLEEP_REFUSED,
                        {
                            "reason": "stay_awake_gpu_short",
                            "mem_available_gib": mem,
                            "free_mib": free,
                            "need_mib": need,
                        },
                    )
                    return st.R_VLM_UNAVAILABLE
                self._sub_note(
                    st.NOTE_VLM_SLEEP,
                    {
                        "ok": True,
                        "level": "stay_awake",
                        "mem_available_gib": mem,
                        "ms": 0,
                        "free_mib": free,
                    },
                )
                # Awake but idle: no VLM request is made while the policy runs.
                sub.vlm = "idle_awake"
                return None
            level = 2
        began = time.monotonic()
        ok = self._power(lambda: self.power.sleep(level))
        ms = int((time.monotonic() - began) * 1000)
        self._sub_note(
            st.NOTE_VLM_SLEEP,
            {
                "ok": ok,
                "level": level,
                "mem_available_gib": mem,
                "mem_after_gib": self._reading(self.power.mem_available_gib),
                "ms": ms,
            },
        )
        if not ok:
            self._sub_note(
                st.NOTE_VLM_SLEEP_REFUSED,
                {
                    "reason": getattr(self.power, "last_refusal", None)
                    or "sleep_failed",
                    "level": level,
                },
            )
            sub.vlm = "unknown"
            return st.R_VLM_UNAVAILABLE
        sub.vlm = "asleep"
        return None

    def _sub_wait(self, reason: str, *, ctx=None, result=None) -> None:
        """Hold, no motion, and wait for a person (subtask_loop)."""
        self._close_reverse_window()
        try:
            self.robot.hold()
        except Exception as exc:  # noqa: BLE001 - the wait still happens
            self._note("hold_failed", f"{type(exc).__name__}: {exc}")
        self._tx("WAIT_HUMAN", reason, ctx=ctx, result=result)
        self._close_episode()
        self.ctx = None

    def _capture(self, purpose: str):
        try:
            frames = self.subvlm.capture(purpose)
        except Exception as exc:  # noqa: BLE001 - no frames: no answer
            self._note("subtask_capture_failed", f"{type(exc).__name__}: {exc}")
            return None
        if not isinstance(frames, list) or not frames:
            return None
        return frames

    def _ask_vlm(self, kind: str, call, record: dict, *, episode_id=None):
        """One VLM request (the VLM is awake by now); the raw answer, or None
        (timeout, exception). ``record`` gets the analysis fields
        (Lynx AERI-VLM-ANALYSIS-PLAN-20261017.md §1)."""
        # Host-owned association for future visibility checks, never from a
        # provider report or inferred from the numeric round. Inventory is run
        # scoped even when the previous round is still present in self.sub.
        context = None
        round_id = getattr(self.sub, "round_id", None)
        if kind == "inventory":
            context = {"kind": kind, "round_id": None, "episode_id": None}
        elif isinstance(round_id, str) and round_id:
            if kind in ("plan-forward", "plan-reverse") and record.get(
                "loop"
            ) == kind.removeprefix("plan-"):
                context = {"kind": kind, "round_id": round_id, "episode_id": None}
            elif (
                kind == "check"
                and isinstance(episode_id, str)
                and episode_id
                and record.get("episode_id") == episode_id
                and record.get("loop") == self.sub.loop_name()
                and record.get("loop") in ("forward", "reverse")
            ):
                context = {
                    "kind": f"check-{record['loop']}",
                    "round_id": round_id,
                    "episode_id": episode_id,
                }
        record.update(
            kind=kind,
            request_wall_ns=time.time_ns(),
            vllm_state=(getattr(self.sub, "last_wake", None) or {}).get("was"),
            wake_ms=(getattr(self.sub, "last_wake", None) or {}).get("wake_ms"),
        )
        began = time.monotonic()
        try:
            reply = call()
        except Exception as exc:  # noqa: BLE001 - an exception is no answer
            reply = None
            record["error"] = f"{type(exc).__name__}: {exc}"[:300]
        record["latency_ms"] = int((time.monotonic() - began) * 1000)
        record["response_wall_ns"] = time.time_ns()
        raw = reply
        if isinstance(reply, st.VlmReply):
            raw = reply.raw
            record.update(
                prompt_sha256=reply.prompt_sha256,
                prompt_tokens=reply.prompt_tokens,
                completion_tokens=reply.completion_tokens,
                usage_source=reply.usage_source,
                finish_reason=reply.finish_reason,
                vlm_request_id=reply.request_id,
                request_id=reply.client_request_id,
                image_refs=reply.image_refs,
                timeout_s=reply.timeout_s,
                response_schema_ok=reply.response_schema_ok,
            )
            if reply.image_meta:
                # Only with frame metadata: older records keep their keys.
                record["image_meta"] = reply.image_meta
            if reply.error:
                record["error"] = reply.error
            if reply.latency_ms is not None:
                record["latency_ms"] = reply.latency_ms
        if isinstance(raw, bytes | bytearray):
            raw = bytes(raw).decode("utf-8", "replace")
        from .external_report import text_snapshot

        answer_evidence = text_snapshot(raw)
        record["raw_content"] = raw[:20000] if isinstance(raw, str) else None
        # Separate, non-detail sidecar: existing subtask_view deliberately does
        # not unfold this reference. Free text is not exposed before a future
        # API implements exact round/member visibility checks.
        private_evidence = {
            "schema": "levi.aeri.request_evidence.v1",
            "evidence_context": context,
            "request_id": record.get("request_id"),
            "vlm_request_id": record.get("vlm_request_id"),
            "frames": record.get("frames", []),
            "image_refs": record.get("image_refs"),
            "prompt_sha256": record.get("prompt_sha256"),
            "prompt_text": reply.prompt_text
            if isinstance(reply, st.VlmReply)
            else None,
            "prompt_bytes": reply.prompt_bytes
            if isinstance(reply, st.VlmReply)
            else None,
            "prompt_text_truncated": reply.prompt_text_truncated
            if isinstance(reply, st.VlmReply)
            else False,
            "external_evidence": reply.external_evidence
            if isinstance(reply, st.VlmReply)
            else None,
            "raw_content": answer_evidence["text"],
            "raw_content_truncated": answer_evidence["truncated"],
            "answer_content_sha256": answer_evidence["sha256"],
            "answer_bytes": answer_evidence["bytes"],
        }
        if record.get("image_meta"):
            # Frame bus: the camera frame (seq, t_capture_ns) of each image.
            private_evidence["image_meta"] = record["image_meta"]
        record["request_evidence"] = self._sidecar("request-evidence", private_evidence)
        # Even valid JSON can be an incomplete answer when the provider hit
        # its output limit. Keep the raw evidence, but never validate it as a
        # complete inventory/plan/check; the existing refusal path waits.
        if isinstance(reply, st.VlmReply) and reply.finish_reason == "length":
            return None
        return raw

    def _refused(self, record: dict, exc: st.SubtaskRefused) -> None:
        record.update(
            valid=False,
            outcome="refused",
            reason=exc.code,
            detail=exc.detail,
            whitelist_violations=[exc.detail] if exc.code == "not_whitelisted" else [],
        )

    def _decide_subtask(self) -> None:
        """VERIFY_INITIAL / SCENE_ASSESS of a subtask_loop run."""
        sub = self.sub
        if self._stop_before_start():
            return
        if sub.phase == "forward":
            self._dispatch_subtask("forward")
            return
        if sub.phase == "reverse":
            self._dispatch_subtask("reset")
            return
        if sub.phase == "reverse_pending":
            self._plan_reverse()
            return
        if self.completed_forward >= self.config.episodes:
            self._tx("COMPLETED", "run_completed")
            return
        if not self._subtask_inventory():
            return
        # R11: the read-only start-pose check before every round.
        if not self._start_pose_ok():
            self._sub_wait("home_pose_not_reached")
            return
        round_no = self.completed_forward + self.discarded_forward + 1
        released = self.gate.release_round(round_no, self.config.episodes)
        # R1: only terminal D's release starts a round's forward loop.
        if released != "released":
            if not self._stop_before_start():
                self._sub_wait(st.R_NOT_RELEASED)
            return
        if self._startup_gate_mark is not None:
            # A delayed observation must never prevent stop registration.
            # The original stop generation is checked after these reads.
            safe = self._startup_ready() and self._start_pose_ok()
            with self._stop_lock:
                if (
                    not safe
                    or self._stops
                    or self.startup_stop_seen.is_set()
                    or self._stop_gen != self._startup_stop_gen
                ):
                    raise ValueError("startup home closure lost before round release")
                mark = self._startup_gate_mark
                self.robot.reopen_admission_if(
                    generation=mark.generation,
                    closed_from_ns=mark.closed_from_ns,
                )
                self._startup_gate_mark = None
                self.startup_home_active = False
        sub.round_id = f"{self.config.run_id}:round{round_no}:s{self.journal.next_seq}"
        sub.round_labelled = False
        self._sub_note(
            st.NOTE_ROUND_RELEASED,
            {"round": round_no, "by": "terminal", "round_id": sub.round_id},
        )
        if self._stop_before_start():
            return
        sub.round_no = round_no
        self._plan_forward()

    def _alternation_admitted(self) -> bool:
        """Once per run: journal the alternation mode; co_resident needs the
        measured GPU use of everything loaded (policy, VLM, others) plus
        ``vlm_co_resident_headroom_mib`` within the card (nvidia-smi totals)."""
        sub, cfg = self.sub, self.config
        if sub.alternation_checked:
            return True
        detail = {"mode": cfg.vlm_alternation, "ok": True}
        if cfg.vlm_alternation == "co_resident":
            totals = self._gpu_totals()
            headroom = None if totals is None else totals[1] - totals[0]
            need = cfg.vlm_co_resident_headroom_mib
            detail.update(
                used_mib=None if totals is None else totals[0],
                total_mib=None if totals is None else totals[1],
                headroom_mib=headroom,
                need_mib=need,
            )
            if headroom is None or headroom < need:
                detail["ok"] = False
                self._sub_note(st.NOTE_ALTERNATION, detail)
                self._sub_note(
                    st.NOTE_VLM_WAKE_REFUSED,
                    {
                        "reason": "co_resident_gpu_short",
                        "free_mib": headroom,
                        "need_mib": need,
                    },
                )
                self._sub_wait(st.R_VLM_UNAVAILABLE)
                return False
        self._sub_note(st.NOTE_ALTERNATION, detail)
        sub.alternation_checked = True
        return True

    def _subtask_inventory(self) -> bool:
        """The run's whitelist: the VLM's inventory that terminal D confirmed
        (R2). ``False``: the run waits for a person (or stops)."""
        sub = self.sub
        if not self._alternation_admitted():
            return False
        if sub.inventory is not None:
            return True
        why = self._vlm_wake()
        if why is not None:
            self._sub_wait(why)
            return False
        frames = self._capture("inventory")
        record = {"frames": st.frames_brief(frames or [])}
        raw = (
            None
            if frames is None
            else self._ask_vlm(
                "inventory", lambda: self.subvlm.inventory(frames=frames), record
            )
        )
        try:
            found = st.parse_inventory(raw)
        except st.SubtaskRefused as exc:
            self._refused(record, exc)
            self._sub_note(
                st.NOTE_INVENTORY_REJECTED,
                {"by": "validator", "reason": exc.code},
                sidecar=record,
            )
            self._sub_wait(st.R_INVENTORY_REJECTED)
            return False
        record.update(valid=True, outcome="asked", parsed=found.describe())
        self._sub_note(
            st.NOTE_INVENTORY,
            {"items": list(found.ids), "frames": st.frames_brief(frames)},
            sidecar=record,
        )
        if self._stop_before_start():
            return False
        answer = self.gate.confirm_inventory(found.describe())
        if answer == "yes":
            sha = st.whitelist_sha256(found.whitelist)
            self._sub_note(
                st.NOTE_INVENTORY_CONFIRMED,
                {"items": list(found.whitelist), "by": "terminal", "sha256": sha},
                sidecar={
                    "objects": found.describe(),
                    "whitelist": list(found.whitelist),
                    "graspable": list(found.graspable),
                },
            )
            sub.inventory, sub.whitelist_sha256 = found, sha
            return True
        if self._stop_before_start():
            return False
        self._sub_note(
            st.NOTE_INVENTORY_REJECTED, {"items": list(found.ids), "by": "terminal"}
        )
        self._sub_wait(st.R_INVENTORY_REJECTED)
        return False

    def _plan_forward(self) -> None:
        sub, cfg = self.sub, self.config
        why = self._vlm_wake()
        if why is not None:
            self._sub_wait(why)
            return
        frames = self._capture(f"r{sub.round_no}-start")
        # The round-start frames: the reference layout the reverse loop restores.
        sub.reference = frames or []
        record = {
            "round": sub.round_no,
            "loop": "forward",
            "frames": st.frames_brief(sub.reference),
        }
        raw = (
            None
            if frames is None
            else self._ask_vlm(
                "plan-forward",
                lambda: self.subvlm.plan(
                    loop="forward",
                    task=cfg.subtask_task,
                    whitelist=sub.inventory.whitelist,
                    frames=frames,
                    round_no=sub.round_no,
                ),
                record,
            )
        )
        plan = self._accept_plan(raw, record, "forward", cfg.subtask_task)
        if plan is None:
            return
        sub.start_loop("forward", cfg.subtask_task, plan)
        if self._stop_before_start():
            return
        self._dispatch_subtask("forward")

    def _plan_reverse(self) -> None:
        sub, cfg = self.sub, self.config
        # R11: the read-only start-pose check before the reverse loop.
        if not self._start_pose_ok():
            self._sub_wait("home_pose_not_reached")
            return
        why = self._vlm_wake()
        if why is not None:
            self._sub_wait(why)
            return
        frames = self._capture(f"r{sub.round_no}-reverse")
        task = st.reverse_task(cfg.subtask_task)
        record = {
            "round": sub.round_no,
            "loop": "reverse",
            "frames": st.frames_brief(frames or []),
            "reference": st.frames_brief(sub.reference),
        }
        raw = (
            None
            if frames is None or not sub.reference
            else self._ask_vlm(
                "plan-reverse",
                lambda: self.subvlm.plan(
                    loop="reverse",
                    task=task,
                    whitelist=sub.inventory.whitelist,
                    frames=frames,
                    reference=sub.reference,
                    round_no=sub.round_no,
                ),
                record,
            )
        )
        plan = self._accept_plan(raw, record, "reverse", task)
        if plan is None:
            return
        if not plan:
            # An empty reverse plan: the scene is as the round started. The
            # arm is home (checked above); the next round needs its release.
            sub.start_loop("reverse", task, [])
            self._loop_done_note("reverse", "already_restored")
            self._round_members()
            sub.phase, sub.plan, sub.index, sub.attempt = "idle", [], 0, 1
            return
        sub.start_loop("reverse", task, plan)
        if self._stop_before_start():
            return
        self._dispatch_subtask("reset")

    def _accept_plan(self, raw, record: dict, loop: str, task: str):
        """The plan, validated against the frozen whitelist and the template
        (R2, R3, R4), or ``None`` after a hold and a wait for a person."""
        sub = self.sub
        try:
            plan = st.parse_plan(
                raw,
                sub.inventory,
                loop=loop,
                max_subtasks=self.config.max_subtasks_per_loop,
            )
        except st.SubtaskRefused as exc:
            self._refused(record, exc)
            self._sub_note(
                st.NOTE_PLAN_REFUSED,
                {"round": sub.round_no, "loop": loop, "reason": exc.code},
                sidecar=record,
            )
            self._sub_wait(st.R_PLAN_REFUSED)
            return None
        record.update(
            valid=True, outcome="accepted", parsed=[s.describe() for s in plan]
        )
        kept = self._sidecar(
            f"r{sub.round_no:03d}-{loop}-plan",
            {
                "round": sub.round_no,
                "loop": loop,
                "task": task,
                "templates": st.INSTRUCTION_TEMPLATES,
                "whitelist_sha256": sub.whitelist_sha256,
                "subtasks": [s.describe() for s in plan],
                "reference": st.frames_brief(sub.reference),
                "request": record,
            },
        )
        self._sub_note(
            st.NOTE_PLAN,
            {
                "round": sub.round_no,
                "loop": loop,
                "task": task,
                "n": len(plan),
                "plan_file": kept.get("file"),
                "sha256": kept.get("file_sha256"),
            },
        )
        return plan

    def _dispatch_subtask(self, role: str) -> None:
        """One subtask as one ordinary episode with its rendered prompt."""
        sub = self.sub
        task = sub.current()
        # R7: the VLM sleeps before the policy runs.
        why = self._vlm_sleep()
        if why is not None:
            self._sub_wait(why)
            return
        # R8: a stop wins over a subtask already planned.
        if self._stop_before_start():
            return
        # R3: the prompt is a template text from the frozen whitelist only.
        if not st.instruction_allowed(task.instruction, sub.inventory):
            self._sub_note(
                st.NOTE_PLAN_REFUSED,
                {
                    "round": sub.round_no,
                    "loop": sub.loop_name(),
                    "reason": "not_template",
                },
            )
            self._sub_wait(st.R_PLAN_REFUSED)
            return
        number = self.numbers[role] + 1
        info = {
            "round": sub.round_no,
            "loop": sub.loop_name(),
            "index": task.index,
            "attempt": sub.attempt,
            "instruction": task.instruction,
            "episode_id": f"{self.config.run_id}.{role}.{number:04d}",
        }
        if sub.segment > 1:
            # subtask.early_check: this episode continues the attempt (only
            # then: a first episode's note and metadata are as before).
            info.update(
                segment=sub.segment,
                continues=sub.continues,
                max_steps=self._max_steps(role),
            )
        self._sub_note(st.NOTE_STARTED, info)
        if sub.segment == 1:
            sub.dispatched += 1
        self._start(
            role,
            st.R_DISPATCHED,
            prompt=task.instruction,
            subtask={
                **info,
                "task": sub.task,
                "done_when": [list(f) for f in task.done_when],
            },
        )

    def _check_subtask(self, ctx, *, early=None):
        """The VLM's answer to this subtask's ``done_when`` on still frames,
        asked once the arm is held and the policy quiesced (R6): ``True``,
        ``False``, ``None`` (null, malformed, timeout) or a wait reason.
        ``early``: the trigger of an early check (subtask.early_check); the
        same question, journaled with its trigger and step."""
        sub = self.sub
        task = sub.current()
        why = self._vlm_wake()
        if why is not None:
            return why
        frames = self._capture(f"check-{ctx.episode_id}")
        record = {
            "round": sub.round_no,
            "loop": sub.loop_name(),
            "index": task.index,
            "attempt": sub.attempt,
            "episode_id": ctx.episode_id,
            "steps": ctx.steps_done,
            "facts": task.facts_text(),
            "frames": st.frames_brief(frames or []),
        }
        if early is not None:
            record["early"] = dict(early)
        raw = (
            None
            if frames is None
            else self._ask_vlm(
                "check",
                lambda: self.subvlm.check(subtask=task, frames=frames),
                record,
                episode_id=ctx.episode_id,
            )
        )
        try:
            value = st.parse_check(raw, task)
            record.update(valid=True, parsed=json.loads(raw), decision=value)
        except st.SubtaskRefused as exc:
            value = None
            self._refused(record, exc)
            record["decision"] = None
        sub.checks += 1
        outcomes = (
            {True: "next", False: "retry", None: "person"}
            if early is None
            else {True: "next", False: "continue", None: "continue"}
        )
        record["outcome"] = outcomes[value]
        detail = {
            "round": sub.round_no,
            "loop": sub.loop_name(),
            "index": task.index,
            "attempt": sub.attempt,
            "episode_id": ctx.episode_id,
            "value": value,
            "frames": st.frames_brief(frames or []),
        }
        if early is not None:
            # Only an early check carries these: the ordinary check's note
            # is exactly as before.
            detail.update(early=True, trigger=early["trigger"], step=early["step"])
        self._sub_note(st.NOTE_CHECK, detail, sidecar=record)
        return value

    def _label_round(self, verdict: dict) -> None:
        """m30: the operator's s/f/d is the ROUND's verdict: one record in
        ``labels/round_label.jsonl`` (``metrics.label_round``) listing the
        round's forward subtask episodes that ran; none of them is labelled
        on its own (m31). A round none of whose episodes ran keeps no record
        (the rule of 6c30bf8d)."""
        from . import metrics

        sub = self.sub
        if not sub.round_episodes:
            self._note(VERDICT_NOT_LABELLED_NOTE, f"{sub.round_id}: no step ran")
            return
        try:
            metrics.label_round(
                self.journal.directory,
                sub.round_id,
                OPERATOR_LABEL_OF_VERDICT[verdict["label"]],
                by=verdict["by"],
                forward=sub.round_episodes,
                note=f"{VERDICT_NOTE} {verdict['source']} via {verdict['episode_id']}",
            )
            sub.round_labelled = True
        except (metrics.LabelRefused, OSError) as exc:
            self._note(VERDICT_LABEL_FAILED_NOTE, f"{sub.round_id}: {exc}")

    def _round_members(self) -> None:
        """The reverse episodes of a labelled round, once the reverse loop ended."""
        from . import metrics

        sub = self.sub
        if not sub.round_labelled:
            return
        try:
            metrics.round_members(
                self.journal.directory, sub.round_id, sub.reverse_episodes
            )
        except OSError as exc:
            self._note(VERDICT_LABEL_FAILED_NOTE, f"{sub.round_id} members: {exc}")

    def _home_after_subtask(self, ctx, reason: str = "robot_home_reached") -> bool:
        """ROBOT_HOME -> SCENE_ASSESS with the safe home (the end of a forward
        loop: ``subtask_forward_done``; a stop, a policy failure:
        ``robot_home_reached``)."""
        if not self._safe_to_home():
            return False
        found = self._tx(
            "SCENE_ASSESS",
            reason,
            kind="home",
            act=self._home(ctx),
            authority=ctx.stop_authority,
            ctx=ctx,
            step=1,
        )
        if found.executed != "yes":
            self._note("home_failed_detail", found.detail)
            self._fault("home_failed")
            return False
        self._close_episode()
        self.ctx = None
        return True

    def _after_forward_subtask(self, ctx, reason: str) -> None:
        """ROBOT_HOME after a forward subtask's seal (the arm held, the policy
        quiesced, the rollout sealed)."""
        verdict = ctx.verdict
        if ctx.steps_done > 0:
            self.sub.round_episodes.append(ctx.episode_id)
        # R9: s/f/d ends the forward loop; the reverse loop still restores.
        if verdict is not None:
            self._label_round(verdict)
            self._end_forward_loop(ctx, verdict["label"])
            return
        if reason == "policy_error":
            if self._home_after_subtask(ctx):
                self._to_human("policy_error")
            return
        early = (
            self._early_note(ctx)
            if reason == st.R_EARLY_CHECK and ctx.early_trigger is not None
            else None
        )
        value = self._check_subtask(ctx, early=early)
        if self.state == "FAULT_LOCKED":
            return
        operator = self._operator()
        if operator is not None:
            # R8: a stop during the check wins: home, then the stop waits there.
            if self.config.home_after_operator_stop:
                self._home_after_subtask(ctx)
            else:
                self._close_episode()
                self.ctx = None
                self._tx("WAIT_HUMAN", "operator_stop", authority=operator)
            return
        if early is not None and value is not True:
            # false, null, or no answer (the VLM could not be woken): the
            # subtask goes on; its ordinary check still comes at the end.
            self._continue_subtask(ctx, value)
            return
        if isinstance(value, str):
            self._sub_wait(value)
            return
        self._next_subtask(ctx, value)

    def _next_subtask(self, ctx, value, *, result=None, home=None) -> None:
        """After a check: the next subtask, the same one again, the loop's end
        or a person. ``result``: a reverse subtask's result (committed here)."""
        sub, cfg = self.sub, self.config
        if value is None:
            # R4: null, unreadable or no answer: a person, never done.
            self._sub_wait(
                st.R_CHECK_UNAVAILABLE, ctx=ctx if result else None, result=result
            )
            return
        if value is True:
            sub.index += 1
            sub.attempt = 1
            sub.new_attempt()
            if sub.index >= len(sub.plan):
                self._end_loop(ctx, result=result, home=home)
                return
        else:
            # R5: at most subtask_max_retries resends of one subtask.
            if sub.attempt > cfg.subtask_max_retries:
                self._sub_wait(
                    st.R_RETRY_LIMIT, ctx=ctx if result else None, result=result
                )
                return
            sub.attempt += 1
            sub.retries += 1
            sub.new_attempt()
            self._sub_note(
                st.NOTE_RETRY,
                {
                    "round": sub.round_no,
                    "loop": sub.loop_name(),
                    "index": sub.index,
                    "attempt": sub.attempt,
                },
            )
        self._tx("SCENE_ASSESS", st.R_CONTINUE, ctx=ctx, result=result)
        self._close_episode()
        self.ctx = None

    def _end_loop(self, ctx, *, result=None, home=None) -> None:
        if self.sub.phase == "forward":
            self._end_forward_loop(ctx, "done")
        else:
            self._end_reverse_loop(ctx, result, home)

    def _loop_done_note(self, loop: str, outcome: str) -> None:
        sub = self.sub
        self._sub_note(
            st.NOTE_LOOP_DONE,
            {
                "round": sub.round_no,
                "loop": loop,
                "subtasks": sub.dispatched,
                "retries": sub.retries,
                "checks": sub.checks,
                "outcome": outcome,
            },
        )

    def _end_forward_loop(self, ctx, outcome: str) -> None:
        """The forward loop ended (all done, or the operator's s/f/d): home
        (R11), the round counts (a ``d`` is replaced), the reverse loop next."""
        sub = self.sub
        replaced = outcome == "discard" and self.discarded_forward < self.discard_limit
        if not self._home_after_subtask(ctx, st.R_FORWARD_DONE):
            return
        self._loop_done_note("forward", outcome)
        if outcome == "discard" and not replaced:
            self._replaces(ctx)  # notes discard_limit_reached
        self._count_round(outcome)
        sub.phase, sub.outcome = "reverse_pending", outcome

    def _end_reverse_loop(self, ctx, result, home) -> None:
        sub = self.sub
        if not self._safe_to_home():
            return
        home["done"] = "succeeded"
        found = self._tx(
            "VERIFY_INITIAL",
            st.R_REVERSE_DONE,
            kind="home",
            act=self._home(ctx),
            ctx=ctx,
            step=1,
            result=result,
        )
        if found.executed != "yes":
            self._note("home_failed_detail", found.detail)
            self._fault("home_failed")
            return
        self._close_episode()
        self.ctx = None
        self._loop_done_note("reverse", "done")
        self._round_members()
        sub.phase, sub.plan, sub.index, sub.attempt = "idle", [], 0, 1

    def _reverse_stop_guard(self) -> bool:
        """A human wait needs a clear safety guard even when no home is due.

        Observe outside the registry lock; this neither checks arrival at
        home nor authorizes or performs any motion.
        """
        if self.robot.latch_state() or self.robot.health()["state"] == "red":
            self._fault("safety_stop", guard=True)
            return False
        return True

    def _finish_reverse_subtask(self, ctx, reason: str) -> None:
        """A reverse subtask episode ends: hold and quiesce, seal, then its
        check on still frames (R6); never the reset policy's scene check."""
        auth = ctx.stop_authority
        found = self._tx(
            "RESET_VERIFY",
            st.R_ENDED,
            kind="policy_quiesce",
            act=self._quiesce(ctx),
            authority=auth,
            ctx=ctx,
        )
        if found.executed != "yes":
            self._fault("policy_error")
            return
        sealed: dict = {}
        ran = ctx.steps_done > 0
        if ran:
            self.sub.reverse_episodes.append(ctx.episode_id)
        found = self._tx(
            "RESET_FINALIZE",
            st.R_ENDED,
            kind="recorder_seal" if ran else "recorder_abort",
            act=self._seal(ctx, sealed) if ran else self._discard(ctx, sealed),
            authority=auth,
            ctx=ctx,
        )
        if found.executed != "yes":
            self._fault("recorder_failed")
            return
        stop = "horizon_exhausted" if reason == "reset_horizon_exhausted" else reason
        home = {"done": "not_attempted"}

        def result():
            return {
                "task_outcome": "unknown",
                "stop_reason": stop,
                "goal_verification": "unavailable",
                "robot_home": home["done"],
                "scene_reset": "unknown",
                "label_kind": "autonomous_verdict",
                "judgement_ids": [],
                "rollout": {
                    "task_folder": self._folder("reset"),
                    "demo": f"demo_{ctx.number:04d}",
                    "sealed": sealed.get("sealed", "incomplete"),
                },
            }

        operator = self._stays_put(ctx, reason, auth)
        if operator is not None:
            self._close_reverse_window()
            if not self._reverse_stop_guard():
                return
            stop = "operator_stop"
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
        if reason == camera_meta.REASON:
            # No VLM check on frozen frames and no home: a person first.
            self._close_reverse_window()
            if not self._reverse_stop_guard():
                return
            self._tx("WAIT_HUMAN", camera_meta.REASON, ctx=ctx, result=result)
            self._close_episode()
            self.ctx = None
            return
        value = None
        early = None
        if reason != "policy_error" and self._operator() is None:
            if reason == st.R_EARLY_CHECK and ctx.early_trigger is not None:
                early = self._early_note(ctx)
            value = self._check_subtask(ctx, early=early)
            if self.state == "FAULT_LOCKED":
                return
        operator = self._close_reverse_window()
        # Ordinary run-level stops remain valid after the reverse-only window
        # closes; a reverse request cannot appear after this decision point.
        operator = operator or self._operator()
        if operator is not None:
            if not self._reverse_stop_guard():
                return
            stop = "operator_stop"
            if self._stays_put(ctx, "operator_stop", operator) is not None:
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
        if reason == "policy_error" or operator is not None:
            # A stop (R8) or a policy failure: home, then a person.
            if not self._safe_to_home():
                return
            home["done"] = "succeeded"
            found = self._tx(
                "WAIT_HUMAN",
                "operator_stop" if operator is not None else "policy_error",
                kind="home",
                act=self._home(ctx),
                ctx=ctx,
                step=1,
                result=result,
                authority=operator,
            )
            if found.executed != "yes":
                self._note("home_failed_detail", found.detail)
                self._fault("home_failed")
                return
            self._close_episode()
            self.ctx = None
            return
        if early is not None and value is not True:
            # As in the forward loop: the reverse subtask goes on.
            self._continue_subtask(ctx, value, result=result)
            return
        if isinstance(value, str):
            self._sub_wait(value, ctx=ctx, result=result)
            return
        self._next_subtask(ctx, value, result=result, home=home)

    # --- scene assessment ---------------------------------------------------------------

    def _assess(self, target: str, episode_id: str) -> str:
        """ready | reset_required | unknown | unavailable (never ready
        without a fresh, fenced assessment that says so). Every assessment
        is kept in ``evidence/`` (T-CL-04), the refused ones too."""
        if self.intervention is not None:
            # A scene judgement owns a real epoch too. In particular, the
            # subsequent reset may use a different episode identifier, which
            # the shared runtime permits only with a strictly newer epoch.
            # Persist before granting so restart cannot reuse this allocation.
            self.policy_epoch += 1
            self.journal.append(
                "note",
                authority=self._auth(),
                control_epoch=self.journal.control_epoch,
                policy_epoch=self.policy_epoch,
                note={"code": "inference_epoch_reserved", "detail": episode_id},
            )
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
        owned_frames = {}
        self._scene_evidence_failed = False
        require_retained = (
            self.config.reset_horizon_completion == "verify_final"
            and target in ("post_reset", "initial_state")
        )
        try:
            if self.intervention is not None:
                self.robot.hold()
                self._rebuild_admission_interval(episode_id, self.policy_epoch)
                deadline = self._now() + self.config.scene_timeout_ns
                if self.owned_calls is None:
                    decision, reason, message = self.intervention.invoke(
                        "judge",
                        episode_id,
                        self.policy_epoch,
                        deadline_ns=deadline,
                        call=lambda: self._assess_once(record),
                        interrupted=self._intervention_interrupted,
                    )
                else:
                    request = ev.make_request(
                        request_id=request_id,
                        run_id=self.config.run_id,
                        episode_id=episode_id,
                        target=target,
                    )
                    invocation = self.owned_calls.invocation(
                        "scene",
                        request,
                        role="judge",
                        policy_epoch=self.policy_epoch,
                        deadline_ns=deadline,
                        interrupted=self._intervention_interrupted,
                        observe=self.robot.observe,
                        min_observed_ns=record["asked_ns"],
                        evidence=self.evidence,
                    )
                    result = self.intervention.invoke(
                        "judge",
                        episode_id,
                        self.policy_epoch,
                        deadline_ns=deadline,
                        request_call=invocation.call,
                        completion_adapter=self.owned_calls.completion,
                        interrupted=self._intervention_interrupted,
                    )
                    raw = invocation.decode(result)
                    owned_frames = dict(invocation.prepared.frames)
                    decision, reason, message = self._parse_scene(record, raw)
                    if decision == "unavailable":
                        self.intervention.interrupt()
                        raise InterventionUnproven("owned scene contract unavailable")
            else:
                decision, reason, message = self._assess_once(record)
            record["decision"], record["reason"] = decision, reason
            if message is not None:
                record.update(_scene_facts(message))
        except InterventionUnproven as exc:
            if self.intervention is not None:
                self.intervention.interrupt(
                    proof_failed=not isinstance(exc, InterventionStopped)
                )
            self._note("intervention_unproven", str(exc))
            self._fault("judgement_unproven")
            decision = "unavailable"
        finally:
            retained = self._keep_evidence(
                record,
                require_retained=require_retained,
                frames=owned_frames if self.owned_calls is not None else None,
            )
        if require_retained and decision == "ready" and not retained:
            self._scene_evidence_failed = True
            self._note("scene_evidence_unavailable", request_id)
            return "unavailable"
        return decision

    def _assess_once(self, record: dict) -> tuple:
        """``(decision, reason, parsed message or None)``."""
        request_id = record["request_id"]
        episode_id, target = record["episode_id"], record["target"]
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
        return self._parse_scene(record, raw)

    def _parse_scene(self, record, raw):
        request_id = record["request_id"]
        episode_id, target = record["episode_id"], record["target"]
        asked_ns = record["asked_ns"]
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

    def _keep_evidence(
        self, record: dict, *, require_retained=False, frames=None
    ) -> bool:
        """Keep scene proof; verify_final ready gates require retained record/frames.

        Other modes keep the existing note-only failure behavior.
        """
        frames_failed = False
        if frames is None:
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
                frames_failed = True
        if self.evidence is None:
            return False
        record.setdefault("decision", "unavailable")
        record.setdefault("reason", "provider_error")
        try:
            kept = self.evidence.write(record, frames)
            if kept is None:
                self._note("evidence_budget_exhausted", record["request_id"])
                return False
            if require_retained:
                return not frames_failed and self.evidence.retained_scene(record, kept)
            return True
        except Exception as exc:  # noqa: BLE001 - unavailable evidence cannot prove ready
            self._note("evidence_write_failed", f"{type(exc).__name__}: {exc}")
            return False

    def _collect_scene(self, ticket) -> tuple:
        """``(raw, None)``, or ``(None, why)`` (``timeout``, or
        ``stopped``). A person's check (``operator_attested``) may take
        minutes: it is collected in slices, and an operator's stop ends the
        wait at once (the stop then takes effect before anything starts)."""
        person = self.config.scene_check == "operator_attested" or (
            # A provider's answer that a person confirms at the terminal
            # (console.ConfirmedScene, L2-UI2-PROVIDER-RELEASE).
            getattr(self.scene, "person_answers", False) is True
        )
        wait = (
            self.config.human_scene_timeout_ns if person else self.config.scene_wait_ns
        )
        if not person and self.intervention is None:
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

    def _start(
        self, role: str, reason: str, *, prompt: str | None = None, subtask=None
    ) -> None:
        number = self.numbers[role] + 1
        self.numbers[role] = number
        self.policy_epoch += 1
        ctx = Episode(
            role, f"{self.config.run_id}.{role}.{number:04d}", number, self.policy_epoch
        )
        self.ctx = ctx
        if role == "reset" and self.sub is not None and self.intervention is None:
            with self._stop_lock:
                self._reverse_window = {
                    "run_id": self.config.run_id,
                    "round_no": self.sub.round_no,
                    "episode_id": ctx.episode_id,
                    "policy_epoch": ctx.policy_epoch,
                }
        self._episode_note_lines = 0
        self._suppressed = 0
        folder = self._folder(role)
        problem = {}

        def act(_):
            if self._operator() is not None:
                problem["what"] = "operator"
                return "no", "none", "operator_stop"
            if self.intervention is not None:
                # After a home that sent nothing the interval is rebuilt first,
                # so the observation below is later than the closure it reopens.
                # Not reached in the current flow: every _start follows
                # _decide's _assess, which rebuilds first; kept so a path that
                # starts without a scene check cannot skip the rebuild (tested
                # directly in test_fr3_intervention).
                self._rebuild_admission_interval(ctx.episode_id, ctx.policy_epoch)
                before = self._now()
                observed = self.robot.observe()
                if (
                    type(observed.get("observed_ns")) is not int
                    or not before <= observed["observed_ns"] <= self._now()
                ):
                    raise InterventionUnproven(
                        "initial policy observation must be fresh"
                    )
                if (
                    self.intervention.hooks.resume_admission(
                        ctx.episode_id, ctx.policy_epoch, observed
                    )
                    is not True
                ):
                    raise InterventionUnproven("initial policy admission unproven")
            if prompt is None:
                handle = self.policy.acquire(role, policy_epoch=ctx.policy_epoch)
            else:
                # subtask_loop: this episode's own prompt, rendered by code from
                # the template; both loops run the evaluation policy.
                handle = self.policy.acquire(
                    "forward", policy_epoch=ctx.policy_epoch, prompt=prompt
                )
            if handle.get("kind") == "unavailable":
                if handle.get("fault") is True:
                    # The adapter cannot say what state it left its resources
                    # in (a policy service switch): lock, never a human wait.
                    problem["what"] = "policy_fault"
                    self._note(str(handle.get("code")), handle.get("detail", ""))
                    return "no", "policy_adapter", "policy_switch_fault"
                problem["what"] = "policy"
                return "no", "policy_adapter", "policy_unavailable"
            try:
                tell = getattr(self.recorder, "before_forward", None)
                if role == "forward" and tell is not None:
                    tell(ctx.episode_id, **self._preceding())
                describe = getattr(self.recorder, "describe_episode", None)
                if subtask is not None and describe is not None:
                    describe(ctx.episode_id, subtask)
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
            elif problem.get("what") == "policy_fault":
                self._fault("policy_error")
            elif problem.get("what") == "operator":
                self._to_human("operator_stop", authority=self._operator())
            else:
                self._to_human("policy_error")
            return
        if role == "forward":
            self._last_forward = ctx.episode_id
        max_steps = self._max_steps(role)
        if self.intervention is not None:
            self._issue_intervention_motion(ctx, found)
        else:
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

    def _folder(self, role: str) -> str:
        cfg = self.config
        if cfg.subtask_mode:
            if role == "forward":
                return cfg.subtask_forward_folder
            return cfg.subtask_reverse_folder
        return cfg.forward_folder if role == "forward" else cfg.reset_folder

    def _max_steps(self, role: str) -> int:
        if self.config.subtask_mode:
            # subtask.early_check: an episode that continues a subtask after an
            # early check gets what is left of the attempt's step budget (an
            # early check is never due within cooldown_steps of its end).
            used = self.sub.used_steps if self.sub is not None else 0
            return max(1, self.config.subtask_max_steps - used)
        return (
            self.config.forward_max_steps
            if role == "forward"
            else self.config.reset_max_steps
        )

    # --- subtask.early_check (subtask_loop.EarlyCheck) ----------------------------------

    def _early_event(self, ctx, raw) -> None:
        """A trigger event of this episode (the stream the arbiter reads):
        the newest one not yet checked waits in ``ctx.early_pending``. A
        message the arbiter refuses is ignored here too; a retraction drops
        the event it retracts. Only ever makes a check due, never moves."""
        early = self.config.subtask_early_check
        if self.sub is None or early is None or not early.on_events:
            return
        try:
            event = aeri.parse(raw, "event")
        except aeri.AeriError:
            return
        if event.episode_id != ctx.episode_id:
            return
        pending = ctx.early_pending
        if event.status == "retracted":
            if pending is not None and pending["event_id"] == event.retracts:
                ctx.early_pending = None
            return
        if event.event_type not in early.event_types:
            return
        step = self.sub.used_steps + event.step
        last = self.sub.early_last
        if last is not None and step <= last:
            return
        if pending is None or step >= pending["step"]:
            ctx.early_pending = {
                "event_type": event.event_type,
                "event_id": event.event_id,
                "step": step,
            }

    def _early_due(self, ctx, step: int) -> dict | None:
        early = self.config.subtask_early_check
        sub = self.sub
        if sub is None or early is None:
            return None
        return st.early_due(
            early,
            step=sub.used_steps + step,
            budget=self.config.subtask_max_steps,
            checks=sub.early_checks,
            last_check=sub.early_last,
            pending=ctx.early_pending,
        )

    def _early_note(self, ctx) -> dict:
        """Journal the early check about to be asked and count it towards
        the attempt's ``max_checks`` (also when the VLM cannot answer)."""
        sub = self.sub
        trigger = dict(ctx.early_trigger)
        sub.early_checks += 1
        sub.early_last = trigger["step"]
        self._sub_note(
            st.NOTE_EARLY_CHECK,
            {
                "round": sub.round_no,
                "loop": sub.loop_name(),
                "index": sub.index,
                "attempt": sub.attempt,
                "segment": sub.segment,
                "episode_id": ctx.episode_id,
                "steps": ctx.steps_done,
                "n": sub.early_checks,
                **trigger,
            },
        )
        return trigger

    def _continue_subtask(self, ctx, value, *, result=None) -> None:
        """After an early check that was not true (false, null, no answer):
        the same subtask, same attempt, goes on in a new episode with the rest
        of its step budget, without a home (``subtask_continue``). No retry is
        counted; the ordinary check still comes at the budget's end."""
        sub = self.sub
        sub.used_steps += ctx.last_step
        sub.segment += 1
        sub.continues = ctx.episode_id
        self._sub_note(
            st.NOTE_CONTINUED,
            {
                "round": sub.round_no,
                "loop": sub.loop_name(),
                "index": sub.index,
                "attempt": sub.attempt,
                "segment": sub.segment,
                "after": ctx.episode_id,
                "value": value if value in (True, False, None) else None,
                "used_steps": sub.used_steps,
                "remaining_steps": self.config.subtask_max_steps - sub.used_steps,
            },
        )
        self._tx("SCENE_ASSESS", st.R_CONTINUE, ctx=ctx, result=result)
        self._close_episode()
        self.ctx = None

    def _issue_intervention_motion(self, ctx, found):
        # Registration and issuing share the small stop-registry lock. A stop
        # cannot revoke a token and then have this thread restore authorization.
        with self._stop_lock:
            if self._stops or self.intervention.failed:
                raise InterventionStopped("stop before new motion token")
            ctx.token = self.fence.issue(
                sm.MotionToken(
                    run_id=self.config.run_id,
                    transaction_id=found.prepared.transaction_id,
                    control_epoch=found.prepared.control_epoch,
                    kind="policy_steps",
                    episode_id=ctx.episode_id,
                    policy_epoch=ctx.policy_epoch,
                    expires_mono_ns=self._now()
                    + (self._max_steps(ctx.role) - ctx.steps_done + 10)
                    * self.config.control_period_ns
                    * 4,
                )
            )

    def _intervention_interrupted(self):
        return (
            self._operator() is not None
            or self.robot.latch_state()
            or self.robot.health()["state"] == "red"
        )

    def _intervention_stop(self, ctx, exc):
        """No backend waits; an evidence failure cannot become a plain stop."""
        self.fence.revoke()
        self.intervention.interrupt(
            proof_failed=not isinstance(exc, InterventionStopped)
        )
        if self.journal.open_transaction is not None:
            # An observer can fail inside a prepared pause/resume action.
            # Preserve unknown acknowledgement and abort before faulting;
            # another transaction cannot be prepared while it is unresolved.
            self._contain(exc)
            return
        self.robot.hold()
        if self.robot.latch_state() or self.robot.health()["state"] == "red":
            self._fault("safety_stop", guard=True)
        elif self._operator() is not None:
            # An operator's stop is not a failed proof: the run still locks
            # (the request slot stays latched) but the episode says why.
            self._fault("judgement_unproven", stop_reason="operator_stop")
        else:
            self._fault("judgement_unproven")

    def _collect_intervention(self, provider, ticket, deadline_ns):
        if isinstance(ticket, bytes | bytearray):
            return bytes(ticket)
        try:
            while self._now() < deadline_ns:
                if self._intervention_interrupted():
                    raise InterventionStopped("stop/guard during inference")
                raw = provider.collect(
                    ticket, timeout_ns=min(HUMAN_SLICE_NS, deadline_ns - self._now())
                )
                if raw is not None:
                    return raw
            raise InterventionUnproven("inference timed out")
        except BaseException:
            provider.cancel(ticket, "intervention_ended")
            raise

    def _pause_intervention(self, ctx):
        old_epoch = ctx.policy_epoch
        self.policy_epoch += 1
        ctx.policy_epoch = self.policy_epoch
        deadline = self._now() + self.config.judge_request_timeout_ns
        proof = {}

        def act(_):
            self.robot.hold()
            found = self.intervention.pause(
                ctx.episode_id, old_epoch, ctx.policy_epoch, deadline_ns=deadline
            )
            # The pause fence alone does not acknowledge the last released
            # gripper command. Reuse the existing final quiesce/ACK check in
            # this policy_quiesce transaction before judging or sealing.
            executed, _, detail = self._quiesce(ctx)(None)
            if executed != "yes":
                raise InterventionUnproven("paused final quiesce/ACK: " + detail)
            from dataclasses import asdict

            proof.update(asdict(found))
            record = {
                "request_id": f"{ctx.episode_id}:hold{ctx.policy_epoch}",
                "run_id": self.config.run_id,
                "episode_id": ctx.episode_id,
                "target": "inference_hold",
                "proof": proof,
            }
            if self.evidence is None or self.evidence.write(record) is None:
                raise InterventionUnproven("physical hold proof was not retained")
            return "yes", "policy_adapter", "inference_paused"

        self._tx(
            f"{ctx.role.upper()}_JUDGING",
            "inference_paused",
            kind="policy_quiesce",
            act=act,
            ctx=ctx,
        )
        return proof["observed_from_ns"]

    def _resume_intervention(self, ctx):
        # Snapshot again AFTER VLM has exited and its backend barrier passed.
        before = self._now()
        obs = self.robot.observe()
        if (
            type(obs.get("observed_ns")) is not int
            or not before <= obs["observed_ns"] <= self._now()
        ):
            raise InterventionUnproven("fresh post-judge observation required")
        self.recorder.stage(ctx.rollout, obs)
        self.policy_epoch += 1
        ctx.policy_epoch = self.policy_epoch

        def act(_):
            if self._intervention_interrupted():
                raise InterventionStopped("stop before resume")
            if (
                self.intervention.hooks.resume_admission(
                    ctx.episode_id, ctx.policy_epoch, obs
                )
                is not True
            ):
                raise InterventionUnproven("fresh policy admission unproven")
            handle = self.policy.acquire(ctx.role, policy_epoch=ctx.policy_epoch)
            if handle.get("kind") == "unavailable":
                raise InterventionUnproven("policy reacquire unavailable")
            return "yes", "policy_adapter", "inference_resumed"

        found = self._tx(
            f"{ctx.role.upper()}_ACTIVE",
            "inference_resumed",
            kind="policy_steps",
            act=act,
            ctx=ctx,
            step=ctx.steps_done,
        )
        self._issue_intervention_motion(ctx, found)

    def _judge_intervention(self, ctx, want, *, final=False):
        hold_since = self._pause_intervention(ctx)
        # The pause transaction has committed. Bind this capture to its own
        # original-clock interval before staging, preparing evidence or granting.
        observation_since = self._now()
        obs = self.robot.observe()
        if (
            type(obs.get("observed_ns")) is not int
            or not observation_since <= obs["observed_ns"] <= self._now()
        ):
            raise InterventionUnproven("fresh post-pause observation required")
        self.recorder.stage(ctx.rollout, obs)
        request_id = want["request_id"]
        if final:
            ctx.arbiter.outstanding[request_id] = None
        else:
            ctx.arbiter.issued(request_id, ctx.last_step)
        request = ev.make_request(
            run_id=self.config.run_id,
            episode_id=ctx.episode_id,
            episode_role=ctx.role,
            target="forward_goal" if ctx.role == "forward" else "reset_goal",
            observed_through_ns=obs["observed_ns"],
            **want,
        )
        if self.intervention.hooks.prepare_evidence(request, obs) is not True:
            raise InterventionUnproven(
                "paused current-frame event evidence binding unproven"
            )
        deadline = self._now() + (
            self.config.final_judge_timeout_ns
            if final
            else self.config.judge_request_timeout_ns
        )
        if self.owned_calls is None:
            raw = self.intervention.invoke(
                "judge",
                ctx.episode_id,
                ctx.policy_epoch,
                deadline_ns=deadline,
                hold_since_ns=hold_since,
                call=lambda: self._collect_intervention(
                    self.verifier, self.verifier.submit(request), deadline
                ),
                interrupted=self._intervention_interrupted,
            )
        else:
            invocation = self.owned_calls.invocation(
                "goal",
                request,
                role="judge",
                policy_epoch=ctx.policy_epoch,
                deadline_ns=deadline,
                interrupted=self._intervention_interrupted,
                observation=obs,
                min_observed_ns=observation_since,
                evidence=self.evidence,
            )
            result = self.intervention.invoke(
                "judge",
                ctx.episode_id,
                ctx.policy_epoch,
                deadline_ns=deadline,
                hold_since_ns=hold_since,
                request_call=invocation.call,
                completion_adapter=self.owned_calls.completion,
                interrupted=self._intervention_interrupted,
            )
            raw = invocation.decode(result)
        # Parse only AFTER actual backend/worker barrier. Never publish raw SDK completion.
        if self._intervention_interrupted():
            raise InterventionStopped("stop before publishing judgement")
        message = ctx.arbiter.accept(
            raw,
            now_ns=self._now(),
            clock_domain=self.clock.domain,
            specs=self._specs,
            expected=request_id,
        )
        if not hasattr(message, "decision") or message.decision == "unknown":
            raise InterventionUnproven("unknown/unavailable/stale judgement")
        if (
            message.observed_from_step > want["observed_from_step"]
            or message.observed_through_step < want["observed_through_step"]
            or message.observed_through_ns < obs["observed_ns"]
        ):
            raise InterventionUnproven("judgement does not cover paused fresh evidence")
        if (
            not final
            and ctx.arbiter.outstanding[request_id].event_id in ctx.arbiter.retracted
        ):
            raise InterventionUnproven("candidate was retracted")
        ctx.arbiter.withdraw(request_id)
        ctx.judgement_ids.append(message.judgement_id)
        if (
            self.evidence is None
            or self.evidence.write(
                {
                    "request_id": request_id,
                    "run_id": self.config.run_id,
                    "episode_id": ctx.episode_id,
                    "target": "paused_judgement",
                    "policy_epoch": ctx.policy_epoch,
                    "judgement": message.model_dump(mode="json", by_alias=True),
                }
            )
            is None
        ):
            raise InterventionUnproven("paused judgement evidence not retained")
        return message

    def _seal_intervention(self, ctx, reason, message):
        if self._intervention_interrupted():
            raise InterventionStopped("stop before sealing judgement")
        if ctx.role == "forward":
            self._finish_paused_forward(ctx, reason, message)
            return
        sealed = {}
        verification = verification_of(message)

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
                    "task_folder": self._folder(ctx.role),
                    "demo": f"demo_{ctx.number:04d}",
                    "sealed": sealed.get("sealed", "incomplete"),
                },
            }

        found = self._tx(
            "WAIT_HUMAN",
            reason,
            kind="recorder_seal" if ctx.steps_done else "recorder_abort",
            act=self._seal(ctx, sealed)
            if ctx.steps_done
            else self._discard(ctx, sealed),
            ctx=ctx,
            result=result,
            evidence=ctx.judgement_ids[-64:],
        )
        if found.executed != "yes":
            self._fault("recorder_failed")
            return
        if ctx.role == "forward" and ctx.steps_done:
            self.completed_forward += 1
        self._close_episode()
        self.ctx = None

    def _finish_paused_forward(self, ctx, reason, message):
        """A forward episode ended by its paused judgement: finalize, seal the
        result, then home under the closed gate and hand over to the ordinary
        scene assessment. The judgement already in hand is the verdict (no
        second goal request); the policy stays fenced throughout and nothing
        but the home moves the arm."""
        found = self._tx(
            "FORWARD_FINALIZE",
            reason,
            kind="policy_quiesce",
            act=self._quiesce(ctx),
            ctx=ctx,
        )
        if found.executed != "yes":
            self._fault("policy_error")
            return
        verification = verification_of(message)
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
                    "task_folder": self._folder("forward"),
                    "demo": f"demo_{ctx.number:04d}",
                    "sealed": sealed.get("sealed", "incomplete"),
                },
            }

        found = self._tx(
            "ROBOT_HOME",
            reason,
            kind="recorder_seal" if ran else "recorder_abort",
            act=self._seal(ctx, sealed) if ran else self._discard(ctx, sealed),
            ctx=ctx,
            result=result,
            evidence=ctx.judgement_ids[-64:],
        )
        if found.executed != "yes":
            self._fault("recorder_failed")
            return
        if ran:
            self.completed_forward += 1
        # A stop after the seal leaves the arm where it stands, whatever
        # home_after_operator_stop says: the result is kept, nothing moves.
        if self._stay_after_seal(ctx):
            return
        if not self._safe_to_home():
            return
        gate_before = self._gate_counts()
        reached = {}
        found = self._tx(
            "SCENE_ASSESS",
            "robot_home_reached",
            kind="home",
            act=self._home_closed_gate(ctx, reached),
            ctx=ctx,
            step=1,
        )
        if found.executed != "yes":
            # Nothing was sent (a stop before the token, or before the first
            # POST): the arm stays where it stands, as for any stop after the seal.
            if found.executed == "no" and self._stay_after_seal(ctx):
                return
            self._note("home_failed_detail", found.detail)
            if (
                found.executed == "no"
                and reached.get("robot")
                and self._home_sent_nothing(gate_before)
            ):
                # Decision 2026-10-14 21:1x (L2-HOME-EXECNO-ORCH): the bridge
                # refused the home before its first command, by its own counters
                # not one fenced home POST: no lock, the gate stays closed, a
                # person checks and resumes. The binding's interval is rebuilt
                # (stop_admission) before its next hook, since the home window
                # left a new interval that the binding does not follow.
                self._rebuild_admission = True
                self._check_start_pose = True
                self._close_episode()
                self.ctx = None
                self._tx("WAIT_HUMAN", "home_not_executed")
                return
            self._fault("home_failed")
            return
        self._note(
            "robot_homed",
            # No verdict here: it is in the episode result, which the operator's
            # label must not be shown against before it is given.
            f"episode={ctx.episode_id} sealed={ctx.sealed} "
            f"{self._gate_facts(gate_before)} next=scene_assess",
        )
        self._close_episode()
        self.ctx = None

    def _gate_counts(self):
        """The robot's cumulative home-only counters, or None without them."""
        read = getattr(self.robot, "admission_snapshot", None)
        if not callable(read):
            return None
        try:
            snap = read()
            return (
                snap.home_pose_released,
                snap.home_gripper_released,
                snap.home_gate_rejected,
            )
        except Exception:  # noqa: BLE001 - a note never stops the run
            return ()

    def _home_sent_nothing(self, before) -> bool:
        """True only when the robot's own home-only counters say the home sent
        nothing: readable integers before and after, not one home pose or
        gripper POST released and none refused inside the window, the gate
        closed again, no fault latch, no red light and no latched or open
        intervention. Anything unreadable or inconsistent counts as executed."""
        if not before or len(before) != 3:
            return False
        try:
            snap = self.robot.admission_snapshot()
            after = (
                snap.home_pose_released,
                snap.home_gripper_released,
                snap.home_gate_rejected,
            )
            if not (
                all(type(v) is int for v in (*before, *after))
                and after == tuple(before)
                and snap.mode == "closed"
                and snap.closed is True
            ):
                return False
            if self.robot.latch_state() or self.robot.health()["state"] == "red":
                return False
            # A stop or signal the robot sees (Robot.stopped(), e.g. SIGINT)
            # is not a refusal to wait on: it locks as before.
            stopped = getattr(self.robot, "stopped", None)
            if callable(stopped) and stopped():
                return False
        except Exception:  # noqa: BLE001 - unreadable: taken as executed
            return False
        cycle = self.intervention
        return cycle is None or not (
            cycle.failed or cycle.proof_failed or cycle.request_open
        )

    def _rebuild_admission_interval(self, episode_id: str, epoch: int) -> None:
        """After a home that sent nothing: the binding adopts the gate's current
        closed interval (``adopt_unexecuted_home``), only if it continues the
        closure the binding made (its own continuity checks; a refusal closes
        the gate and latches the binding), so the next hook can follow it. A
        binding without the entry, an exception or any answer but True is an
        unproven fact: ``InterventionUnproven`` and the run locks."""
        if not self._rebuild_admission or self.intervention is None:
            return
        adopt = getattr(self.intervention.hooks, "adopt_unexecuted_home", None)
        if not callable(adopt):
            raise InterventionUnproven("the binding cannot adopt the home interval")
        try:
            adopted = adopt(episode_id, epoch - 1, epoch)
        except InterventionUnproven:
            raise
        except Exception as exc:  # any failure is unproven
            raise InterventionUnproven(
                f"admission interval not rebuilt after home: {type(exc).__name__}"
            ) from exc
        if adopted is not True:
            raise InterventionUnproven("admission interval not rebuilt after home")
        self._rebuild_admission = False

    def _gate_facts(self, before) -> str:
        """The admission gate after a home, for the journal note: the robot's
        own snapshot when it has one (counters are this window's increments,
        not the robot's running totals), else the bare fact that it is closed."""
        if before is None:
            return "gate=closed"
        try:
            snap = self.robot.admission_snapshot()
            pose, gripper, rejected = (
                now - was
                for now, was in zip(
                    (
                        snap.home_pose_released,
                        snap.home_gripper_released,
                        snap.home_gate_rejected,
                    ),
                    before,
                    strict=True,
                )
            )
            return (
                f"gate={snap.mode} home_only_from_ns={snap.home_only_from_ns} "
                f"home_pose_released={pose} home_gripper_released={gripper} "
                f"home_gate_rejected={rejected}"
            )
        except Exception:  # noqa: BLE001 - a note never stops the run
            return "gate=unreadable"

    def _stay_after_seal(self, ctx) -> bool:
        operator = self._operator()
        if operator is None:
            return False
        self._close_episode()
        self.ctx = None
        self._tx("WAIT_HUMAN", "operator_stop", authority=operator)
        return True

    def _home_closed_gate(self, ctx, reached=None):
        """The home of a paused episode: the token is issued under the stop
        registry's lock, so a stop cannot revoke it and then see it restored,
        and never while a stop, a failed proof or an open role request exists.
        ``reached["robot"]`` is set once the robot's home was called."""
        reached = {} if reached is None else reached

        def act(prepared):
            with self._stop_lock:
                if self._stops:
                    return "no", "none", "stop_before_home"
                if self.intervention.failed or self.intervention.proof_failed:
                    return "no", "none", "intervention_failed_before_home"
                if self.intervention.request_open:
                    return "no", "none", "role_request_open_before_home"
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
            # Defence in depth: the refusals above return "no" without calling
            # the robot (counters unchanged too), and must keep locking; only a
            # home the robot itself refused may become home_not_executed.
            reached["robot"] = True
            try:
                found = self.robot.home_under_closed_gate(token=token)
            finally:
                self.fence.revoke()
            self._result_notes(found)
            return found.executed, "robot_server", found.detail

        return act

    def _drive_intervention(self, ctx):
        cfg = self.config
        ctx.arbiter = TerminationArbiter(
            cfg.termination,
            run_id=cfg.run_id,
            episode_id=ctx.episode_id,
            role=ctx.role,
            target="forward_goal" if ctx.role == "forward" else "reset_goal",
        )
        buffer = []
        last_frames, stalled = None, 0
        try:
            while ctx.steps_done < self._max_steps(ctx.role):
                ctx.last_step = ctx.steps_done
                if self._intervention_interrupted():
                    raise InterventionStopped("stop before next action")
                observed_after = self._now()
                obs = self.robot.observe()
                if self.owned_calls is not None and (
                    type(obs.get("observed_ns")) is not int
                    or not observed_after <= obs["observed_ns"] <= self._now()
                ):
                    raise InterventionUnproven("fresh policy observation required")
                frames = tuple(sorted(obs["frames"].items()))
                stalled = stalled + 1 if frames == last_frames else 0
                last_frames = frames
                if stalled >= cfg.camera_stall_limit:
                    raise InterventionUnproven("camera evidence unavailable")
                self.recorder.stage(ctx.rollout, obs)
                for raw in self.events.poll(
                    ctx.stream, through_step=ctx.last_step, max_events=16
                ):
                    for note in ctx.arbiter.on_event(raw):
                        self._note(note.code, note.detail)
                        if note.code == "contract_violation":
                            raise InterventionUnproven("event contract invalid")
                want = ctx.arbiter.want_request(ctx.last_step)
                if want is not None:
                    buffer.clear()  # never restore a chunk from before the pause
                    message = self._judge_intervention(ctx, want)
                    if message.decision == "confirmed":
                        self._seal_intervention(ctx, "goal_verified", message)
                        return
                    ctx.arbiter.consume_candidate()
                    self._resume_intervention(ctx)
                    last_frames, stalled = None, 0
                    continue  # observe/stage again before asking for a NEW chunk
                if not buffer:
                    buffer = self._chunk(
                        ctx, ctx.last_step, ctx.steps_done, observation=obs
                    )
                    if not buffer:
                        raise InterventionUnproven(
                            "policy chunk unavailable; no motion retry"
                        )
                if self._intervention_interrupted():
                    raise InterventionStopped("stop after policy inference")
                result = self.robot.step(buffer.pop(0), token=ctx.token)
                self._result_notes(result)
                if result.executed != "yes":
                    raise InterventionUnproven(
                        "motion acknowledgement unproven; no retry: "
                        + str(result.detail)[:200]
                    )
                ctx.steps_done += 1  # waits and refused/unknown motions never count
                self.recorder.commit(ctx.rollout, result)
                self.clock.advance(cfg.control_period_ns)
            buffer.clear()
            ctx.last_step = max(0, ctx.steps_done - 1)
            message = self._judge_intervention(
                ctx,
                {
                    "request_id": f"{ctx.episode_id}:final",
                    "observed_from_step": 0,
                    "observed_through_step": ctx.last_step,
                    "event_ids": [],
                },
                final=True,
            )
            reason = (
                "goal_verified"
                if message.decision == "confirmed"
                else (
                    "horizon_exhausted"
                    if ctx.role == "forward"
                    else "reset_horizon_exhausted"
                )
            )
            self._seal_intervention(ctx, reason, message)
        except InterventionUnproven as exc:
            buffer.clear()
            self._note("intervention_unproven", str(exc))
            self._intervention_stop(ctx, exc)

    def _drive(self, ctx: Episode) -> None:
        if self.intervention is not None:
            self._drive_intervention(ctx)
            return
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
        # Frame bus: the same camera seq on K consecutive steps (K from the
        # robot adapter; no image_meta, no check) is recorded once per run of
        # repeats and never stops the episode (user 2026-10-18: a dropped
        # frame under CPU/GPU load is normal; record, do not stop).
        seq_repeat = False
        seq_watch = camera_meta.SeqWatch(
            getattr(self.robot, "camera_stale_steps", camera_meta.DEFAULT_STALE_STEPS)
        )
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
            # The operator's verdict (terminal s/f/d): after the stop and the
            # safety checks above, so neither loses its priority to it. It
            # ends this episode only; the run goes on.
            verdict = self._take_verdict(ctx) if ctx.role == "forward" else None
            if verdict is not None:
                self._record_verdict(ctx, verdict, step)
                stop_reason = "operator_verdict"
                ctx.stop_authority = self._auth("operator", verdict["command_id"])
                break
            # subtask.early_check: after the stop, the safety checks and the
            # operator's verdict (none loses its priority to it), before the
            # next observation and step. The episode ends the ordinary way
            # (hold, quiesce, seal); its check follows on still frames.
            trigger = self._early_due(ctx, step)
            if trigger is not None:
                ctx.early_trigger = trigger
                stop_reason = st.R_EARLY_CHECK
                break
            try:
                obs = self.robot.observe()
            except camera_meta.CameraStale as exc:
                ctx.evidence_ok = False
                self._note(camera_meta.REASON, f"step {step}: {exc}")
                stop_reason = camera_meta.REASON
                break
            stale = seq_watch.update(obs.get("image_meta"))
            if stale is not None and not seq_repeat:
                self._note(camera_meta.SEQ_REPEAT, f"step {step}: {stale}")
            seq_repeat = stale is not None
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
                    self._early_event(ctx, raw)
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
                        ctx.bounded_failure = True
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
                    if note.code != "early_stop_withheld":
                        ctx.bounded_failure = True
                if verdict.stop and stop_reason is None:
                    stop_reason = "goal_verified"
                    ctx.stop_judgement = verdict.judgement_id
            if stop_reason is not None:
                break
            # subtask_loop: the VLM sleeps while the policy runs (never asked).
            want = (
                arbiter.want_request(step)
                if ctx.evidence_ok and self.sub is None
                else None
            )
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
                    if ctx.policy_exception_before_motion:
                        stop_reason = "policy_error"
                        break
                    chunk_failures += 1
                    ctx.bounded_failure = True
                    if chunk_failures >= cfg.chunk_failure_limit:
                        stop_reason = "policy_error"
                        break
                    self.clock.advance(cfg.control_period_ns)
                    continue
                chunk_failures = 0
                buffer = chunk
            # Admission is after all observation/policy waits. A request
            # linearized later may allow this ONE step to finish, but never
            # the next one. Do not hold the registry lock across robot I/O.
            if ctx.role == "reset" and self.sub is not None:
                with self._stop_lock:
                    stopped = bool(self._stops)
                if stopped:
                    stop_reason, ctx.stop_authority = "operator_stop", self._operator()
                    break
            action = buffer.pop(0)
            result = self.robot.step(action, token=ctx.token)
            self._result_notes(result)
            if result.executed == "unknown":
                self._note("motion_unacknowledged", f"step {step}: {result.detail}")
                self._fault("watchdog_timeout")
                return
            if result.executed != "yes":
                self._note("motion_refused", f"step {step}: {result.detail}")
                if self.robot.latch_state():
                    self._fault("safety_stop", guard=True)
                    return
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

    def _chunk(self, ctx: Episode, step: int, start: int, *, observation=None):
        if self.owned_calls is not None:
            if observation is None:
                raise InterventionUnproven(
                    "owned policy requires this staged observation"
                )
            request = self._chunk_request(ctx, step, start, observation=observation)
            invocation = self.owned_calls.invocation(
                "chunk",
                request,
                role=ctx.role,
                policy_epoch=ctx.policy_epoch,
                deadline_ns=request["deadline"]["due_ns"],
                interrupted=self._intervention_interrupted,
                observation=observation,
                min_observed_ns=observation["observed_ns"],
            )
            result = self.intervention.invoke(
                ctx.role,
                ctx.episode_id,
                ctx.policy_epoch,
                deadline_ns=request["deadline"]["due_ns"],
                request_call=invocation.call,
                completion_adapter=self.owned_calls.completion,
                interrupted=self._intervention_interrupted,
            )
            raw = invocation.decode(result)
            if raw.get("kind") == "unavailable":
                self._note(
                    "policy_unavailable", f"{request['request_id']}: {raw.get('code')}"
                )
                return None
            return self.accept_chunk(ctx, raw, request)
        if self.intervention is not None:
            return self.intervention.invoke(
                ctx.role,
                ctx.episode_id,
                ctx.policy_epoch,
                deadline_ns=self._now() + self.config.chunk_timeout_ns,
                call=lambda: self._chunk_once(ctx, step, start),
                interrupted=lambda: (
                    self._operator() is not None
                    or self.robot.latch_state()
                    or self.robot.health()["state"] == "red"
                ),
            )
        return self._chunk_once(ctx, step, start)

    def _chunk_request(self, ctx, step, start, *, observation=None):
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
            "obs_ns": now if observation is None else observation["observed_ns"],
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
        return request

    def _chunk_once(self, ctx: Episode, step: int, start: int):
        cfg = self.config
        request = self._chunk_request(ctx, step, start)
        request_id = request["request_id"]
        ticket = self.policy.request(request)
        if isinstance(ticket, dict):
            self._note(
                "policy_unavailable",
                f"{request_id}: {ticket.get('code')}: {ticket.get('detail', '')}",
            )
            return None
        raw = self.policy.collect(ticket, timeout_ns=cfg.chunk_timeout_ns)
        if raw is None:
            self._note("chunk_timeout", request_id)
            return None
        if raw.get("kind") == "unavailable":
            if (
                ctx.role == "forward"
                and ctx.steps_done == 0
                and raw.get("code") == "policy_error"
            ):
                ctx.policy_exception_before_motion = True
            self._note(
                "policy_unavailable",
                f"{request_id}: {raw.get('code')}: {raw.get('detail', '')}",
            )
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
                confirm = getattr(self.robot, "confirm_pending_gripper", None)
                if confirm is not None:
                    try:
                        for code, detail in confirm():
                            self._note(code, detail)
                    except Exception as exc:  # noqa: BLE001 - unproven final acknowledgement blocks closure
                        self._note("motion_unacknowledged", str(exc))
                        return "unknown", "robot_server", str(exc)
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

    def _result_notes(self, result) -> None:
        """Facts the robot adapter attached to a step (raw gripper replies,
        their confirmation), kept verbatim in the journal."""
        for code, text in getattr(result, "notes", ()):
            self._note(code, text)

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
            self._result_notes(found)
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
            specs=self._specs,
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
            if ctx.role == "reset" and (
                found.observed_through_step != ctx.last_step
                or not self._reset_final_contract(found)
            ):
                self._note("reset_final_contract_invalid", request_id)
                return None
            ctx.judgement_ids.append(found.judgement_id)
        return found

    def _reset_final_contract(self, message) -> bool:
        """The approved ISC, not a forward spec or a shortened checklist."""
        contract = self.config.initial_state
        if contract is None or (message.spec_id, message.spec_version) != contract.key:
            return False
        required = set(contract.required)
        predicates = {p.name: p for p in message.predicate_results}
        if set(predicates) != required | set(contract.optional) or any(
            p.required != (name in required) for name, p in predicates.items()
        ):
            return False
        references = [ref for p in predicates.values() for ref in p.evidence_refs]
        if any(
            p.value is not None and not p.evidence_refs for p in predicates.values()
        ) or any(ref.kind != "frame" or ref.sha256 is None for ref in references):
            return False
        views = {ref.ref.split(":", 1)[0] for ref in references}
        if len({ref.ref for ref in references}) < contract.min_evidence_refs:
            return False
        return not contract.preferred_views or bool(
            set(contract.preferred_views) <= views
            if contract.require_all_views
            else set(contract.preferred_views) & views
        )

    def _settle_reset(self, ctx) -> bool:
        """No new commands plus fresh observed pose stability; not mechanical safety."""
        cfg = self.config.reset_settle
        began = self._now()
        samples, window = [], []
        passed = False
        try:
            if self.clock.domain != aeri.host_clock_domain():
                raise ValueError("settle needs the current host clock domain")
            while self._now() - began <= cfg.max_wait_ns and len(samples) < 1000:
                if self._operator() is not None or not self._safe_to_home():
                    break
                before = self._now()
                observed = self.robot.observe()
                after = self._now()
                if self._operator() is not None or not self._safe_to_home():
                    break
                stamp, pose = observed.get("observed_ns"), observed.get("pose")
                gripper = observed.get("robot_state", {}).get("gripper_position")
                measurement = observed.get("gripper_measurement")
                if not (
                    isinstance(measurement, dict)
                    and measurement.get("route") == "getstate"
                    and measurement.get("field") == "gripper_pos"
                    and measurement.get("clock_domain") == self.clock.domain
                    and type(measurement.get("generation")) is int
                    and measurement["generation"] > 0
                    and type(measurement.get("read_from_ns")) is int
                    and type(measurement.get("read_through_ns")) is int
                    and before
                    <= measurement["read_from_ns"]
                    <= measurement["read_through_ns"]
                    <= after
                    and type(measurement.get("value")) in (float, int)
                    and measurement["value"] == gripper
                    and (
                        not samples
                        or measurement["generation"]
                        > samples[-1]["gripper_measurement"]["generation"]
                    )
                ):
                    raise ValueError(
                        "settle needs current getstate/gripper_pos measurement provenance"
                    )
                if (
                    self.clock.domain != aeri.host_clock_domain()
                    or observed.get("state_fresh") is not True
                    or type(stamp) is not int
                    or not before <= stamp <= after
                    or (samples and stamp <= samples[-1]["observed_ns"])
                    or not isinstance(pose, (list, tuple))
                    or len(pose) != 7
                    or any(
                        type(v) not in (float, int) or not math.isfinite(v)
                        for v in pose
                    )
                    or not 0 <= pose[6] <= 1
                    or type(gripper) not in (float, int)
                    or not math.isfinite(gripper)
                    or not 0 <= gripper <= 1
                ):
                    raise ValueError(
                        "settle needs fresh finite XYZ/Euler/gripper observations"
                    )
                sample = {
                    "ref": f"{ctx.episode_id}:settle{len(samples)}",
                    "observed_ns": stamp,
                    "clock_domain": self.clock.domain,
                    "pose": list(pose),
                    "measured_gripper": gripper,
                    "gripper_measurement": dict(measurement),
                    "frame_sha256": dict(observed.get("frames", {})),
                }
                samples.append(sample)
                stable = all(
                    math.dist(pose[:3], old["pose"][:3]) <= cfg.position_tolerance_m
                    and math.sqrt(
                        sum(
                            math.remainder(a - b, 2 * math.pi) ** 2
                            for a, b in zip(pose[3:6], old["pose"][3:6], strict=True)
                        )
                    )
                    <= cfg.rotation_tolerance_rad
                    and abs(gripper - old["measured_gripper"]) <= cfg.gripper_tolerance
                    for old in window
                )
                window = [*window, sample] if stable else [sample]
                if (
                    len(window) >= 2
                    and stamp - window[0]["observed_ns"] >= cfg.stable_window_ns
                ):
                    passed = self._now() - began <= cfg.max_wait_ns
                    break
                left = cfg.max_wait_ns - (self._now() - began)
                if left < cfg.sample_interval_ns:
                    break
                remaining = cfg.sample_interval_ns
                while remaining > 0:
                    if self._operator() is not None or not self._safe_to_home():
                        break
                    wait = min(remaining, HUMAN_SLICE_NS)
                    self.clock.advance(wait)
                    remaining -= wait
        except Exception as exc:  # noqa: BLE001 - unavailable observations cannot prove stability
            self._note("reset_settle_unavailable", str(exc))
        if self.state == "FAULT_LOCKED":
            return False
        record = {
            "request_id": f"{ctx.episode_id}:settle",
            "run_id": self.config.run_id,
            "episode_id": ctx.episode_id,
            "target": "reset_settle",
            "plan_sha256": self.config.plan_sha256,
            "journal_seq": self.journal.next_seq,
            "parameters": cfg.__dict__,
            "parameters_source": "run.reset_settle",
            "observations": samples,
            "stable_window_refs": [sample["ref"] for sample in window]
            if passed
            else [],
            "observed_stable": passed,
        }
        try:
            kept = self.evidence is not None and self.evidence.write(record) is not None
        except Exception:  # noqa: BLE001 - proof must be retained
            kept = False
        self._note(
            "reset_settle_verified" if passed and kept else "reset_settle_unavailable",
            record["request_id"],
        )
        return passed and kept and self._operator() is None

    def _keep_reset_final(self, ctx, message) -> bool:
        """Keep final proof separately from the independently captured scene."""
        if self.evidence is None:
            return False
        try:
            return (
                self.evidence.write(
                    {
                        "request_id": message.request_id,
                        "run_id": self.config.run_id,
                        "episode_id": ctx.episode_id,
                        "target": "reset_goal",
                        "journal_seq": self.journal.next_seq,
                        "horizon_completion": "verify_final",
                        "judgement": message.model_dump(mode="json", by_alias=True),
                    }
                )
                is not None
            )
        except Exception as exc:  # noqa: BLE001 - missing proof never grants motion
            self._note("reset_final_evidence_failed", type(exc).__name__)
            return False

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
        # Out of FORWARD_ACTIVE (or locked): no verdict is registered from
        # now on; one that came after the loop's last check is not taken.
        self._drop_verdict("the episode had ended (" + reason + ")")
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
        if (
            ctx.steps_done == 0
            and ctx.policy_exception_before_motion
            and self._operator() is None
        ):
            # The first real inference failed. Hold and quiesce above, then
            # lock without homing or creating another empty forward episode.
            self._fault("policy_error")
            return
        # subtask_loop: no judgement of the whole task here (the subtask's own
        # check follows the seal); the result says unavailable, never success.
        verification = "unavailable" if self.sub else self._verification(ctx, reason)
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
                    # The recorder's folder (``<forward_folder>__subtask`` in
                    # subtask_loop), as the reverse side already records.
                    "task_folder": self._folder("forward"),
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
        if ran and self.sub is None:
            if self._replaces(ctx):
                self.discarded_forward += 1  # one more round replaces it
            else:
                self.completed_forward += 1
        if ctx.verdict is not None and ran and self.sub is None:
            # subtask_loop: no episode label; the round's (_label_round).
            self._label_verdict(ctx)
        elif ctx.verdict is not None:
            self._note(
                VERDICT_NOT_LABELLED_NOTE,
                f"{ctx.episode_id}: no rollout (aborted before the first step), "
                "not labelled",
            )
        operator = self._stays_put(ctx, reason, auth)
        if operator is not None:
            # The arm did not move in this episode, or the operator wants it
            # left where it stands: a person takes over from here.
            self._close_episode()
            self.ctx = None
            self._tx("WAIT_HUMAN", "operator_stop", authority=operator)
            return
        if reason == camera_meta.REASON:
            # The cameras stopped: no home (a person looks first), the arm
            # stays where it is, the run waits.
            if self.sub is not None and ctx.steps_done > 0:
                self.sub.round_episodes.append(ctx.episode_id)
            self._close_episode()
            self.ctx = None
            self._tx("WAIT_HUMAN", camera_meta.REASON)
            return
        if self.sub is not None and self._operator() is None:
            self._after_forward_subtask(ctx, reason)
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
            self._note("home_failed_detail", found.detail)
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
        if self.sub is not None:
            self._finish_reverse_subtask(ctx, reason)
            return
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
        bounded = (
            reason == "reset_horizon_exhausted"
            and self.config.reset_horizon_completion == "verify_final"
        )
        final_verified = False
        if bounded:
            if not self._safe_to_home():
                return
            operator = self._operator()
            if operator is not None:
                reason, auth = "operator_stop", operator
                ctx.stop_authority = operator
            elif (
                ctx.steps_done == self.config.reset_max_steps
                and ctx.evidence_ok
                and not ctx.bounded_failure
                and not self._violation_hold
                and not ctx.arbiter.violations
                and not ctx.arbiter.event_violations
                and not ctx.arbiter.unknowns
                and not ctx.arbiter.unavailables
            ):
                settled = self._settle_reset(ctx)
                if self.state == "FAULT_LOCKED":
                    return
                final = None
                if settled and self._operator() is None:
                    if not self._safe_to_home():
                        return
                    if self._operator() is None:
                        final = self._final_judgement(ctx, ctx.arbiter)
                if not self._safe_to_home():
                    return
                operator = self._operator()
                if operator is not None:
                    reason, auth = "operator_stop", operator
                    ctx.stop_authority = operator
                elif verification_of(final) == "verified":
                    final_verified = self._keep_reset_final(ctx, final)
        # Disk writes may also overlap a new stop/fault: recheck before capture.
        if bounded and not self._safe_to_home():
            return
        # The scene is an independent observation, even when final proof failed.
        decision = (
            self._assess("post_reset", ctx.episode_id)
            if ctx.evidence_ok and (not bounded or self._operator() is None)
            else "unavailable"
        )
        ctx.scene_decision = decision
        if bounded:
            if not self._safe_to_home():
                return
            operator = self._operator()
            if operator is not None:
                reason, auth = "operator_stop", operator
                ctx.stop_authority = operator
        if (
            bounded
            and reason == "reset_horizon_exhausted"
            and final_verified
            and decision == "ready"
            and not self._violation_hold
            and not self._scene_violations
        ):
            # Raw horizon remains refused by reset_manager: only complete proof
            # produces its existing verified outcome. The stop reason stays budget.
            outcome = "reset_verified"
        elif reason in (
            "operator_stop",
            "policy_error",
            "watchdog_timeout",
            "reset_horizon_exhausted",
            camera_meta.REASON,
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
                    "task_folder": self._folder("reset"),
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
        if outcome == camera_meta.REASON:
            # No home on frozen cameras: a person checks the scene first.
            home["done"] = "not_attempted"
            self._tx("WAIT_HUMAN", camera_meta.REASON, ctx=ctx, result=result)
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
            self._note("home_failed_detail", found.detail)
            self._fault("home_failed")
            return
        self._close_episode()
        self.ctx = None
