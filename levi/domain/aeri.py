"""AERI integration contracts v1: the five ``levi.aeri.*`` messages between
event intelligence (A), the performance runtime (B) and the automatic
evaluation pipeline (C). See docs/AUTOMATIC_PIPELINE.md.

A neutral module (no HTTP, model, robot or accelerator dependency) so that
A, B and C import it without depending on one another.

Every message is read through ``parse(raw, contract)``, which

1. refuses a body over ``MAX_BYTES``;
2. decodes JSON refusing duplicate keys, ``NaN``/``Infinity`` and numbers
   that overflow to infinity (``1e400``);
3. refuses control keys anywhere in an A or B message (``CONTROL_KEYS``,
   ``CONTROL_PREFIXES``, after NFKC normalisation and case folding);
4. checks the schema id and refuses a newer minor version
   (``E_SCHEMA_TOO_NEW``, fail closed);
5. validates strictly: no coercion (``True`` is not ``1``, ``"1"`` is not
   ``1``), unknown fields refused at every level, no infinities, plus the
   cross-field rules of each contract (``E_INCONSISTENT``).

``model_validate_json`` is never used: with pydantic 2.13 it silently keeps
the last of two duplicate keys (tests/automatic/test_aeri_contracts.py
pins this). In pydantic's default lax mode ``True`` and ``"1"`` both become
the integer 1; strict mode refuses them (pinned by the same tests).

Results are three distinct things that are never mixed: a ``confirmed`` or
``rejected`` decision; ``decision=unknown`` (the evidence was read and does
not settle it: still a judgement); and ``kind=unavailable`` (no judgement was
produced: gate closed, busy, timeout, model error...). Neither kind of "not
known" is ever a success.
"""

import hashlib
import json
import math
import os
import re
import subprocess
import time
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)
from pydantic_core import PydanticCustomError

from .contracts import Contract

MAJOR = 1
# The minor version this code writes and the newest it reads. Readers of
# persisted records accept every older minor of the same major.
MINOR = 0
MAX_BYTES = 256 * 1024
MAX_TEXT = 2000
INT64_MAX = 2**63 - 1

SCHEMAS = {
    "event": "levi.aeri.event.v1",
    "judgement": "levi.aeri.judgement.v1",
    "scene": "levi.aeri.scene.v1",
    "runtime": "levi.aeri.runtime.v1",
    "run_event": "levi.aeri.run_event.v1",
}
# Messages A and B produce: a control key anywhere in them is refused. The
# run event is written by C alone (its ``action.kind`` is a value, not a key).
CONTROL_SCANNED = ("event", "judgement", "scene", "runtime")
CONTROL_KEYS = (
    "robot_stop",
    "stop_robot",
    "execute_reset",
    "start_reset",
    "go_home",
    "home_robot",
    "halt",
    "estop",
    "e_stop",
    "clearerr",
    "clear_error",
    "jointreset",
    "resume",
    "override",
    "command",
    "action_request",
)
CONTROL_PREFIXES = ("robot_", "execute_", "cmd_", "force_")
# Words naming operator or evaluation data: a request to A must not carry a
# key containing one (same word list as the online judgement, C5).
OPERATOR_WORDS = (
    "operator",
    "outcome",
    "success",
    "fail",
    "label",
    "eval",
    "verdict",
    "truth",
    "score",
    "judg",
)
EVENT_TYPES = (
    "gripper_close",
    "gripper_open",
    "motion_start",
    "motion_stop",
    "height_turn",
    "object_settled",
    "object_lifted",
    "contact_change",
    "visual_change",
)
UNAVAILABLE_CODES = (
    "gate_closed",
    "busy",
    "admission_rejected",
    "cold_start",
    "no_room",
    "shutting_down",
    "timeout",
    "model_error",
    "invalid_answer",
    "provider_error",
    "cancelled",
    "stale",
    "contract_violation",
    "not_supported",
)
RUNTIME_UNAVAILABLE_CODES = (*UNAVAILABLE_CODES, "epoch_fenced", "server_error")
# Only these may say ``retryable``; ``admission_rejected`` only together with
# ``retry_after_ms``.
RETRYABLE_CODES = ("gate_closed", "busy", "admission_rejected")
UNKNOWN_REASONS = (
    "insufficient_evidence",
    "occluded",
    "conflicting_predicates",
    "model_undecided",
    "out_of_scope",
    "stale_inputs",
)
# P0 (safety, watchdog) is deliberately absent: it never asks a broker.
WORKLOAD_CLASSES = (
    "policy_realtime",
    "judge_critical",
    "vision_light",
    "background_annotation",
    "batch",
)
WORKLOAD_PRIORITY = {name: rank for rank, name in enumerate(WORKLOAD_CLASSES, 1)}
ADMISSION_CODES = (
    "policy_priority",
    "episode_imminent",
    "unknown_client",
    "busy",
    "no_memory",
    "deadline_infeasible",
    "cold_start_forbidden",
    "evaluation_active",
    "standby_settling",
    "shutting_down",
    "broker_unavailable",
)
# Ports a policy endpoint may never name: the real robot servers (5000,
# 5001), the tunnel to the second robot (5100), the learner (7470) and the
# real robot's policy server (8000).
ROBOT_PORTS = (5000, 5001, 5100, 7470, 8000)
# The longest a judgement or scene assessment may stay valid after it was
# produced, and the longest lease; a producer cannot keep a result "fresh"
# by writing a far-away expiry.
MAX_RESULT_VALIDITY_MS = 30_000
MAX_LEASE_MS = 600_000
# How far ahead of the consumer's own clock a ``produced_ns``/``granted_ns``
# may be (same host, same monotonic clock: only scheduling jitter).
FUTURE_TOLERANCE_NS = 100_000_000
# v1 is not released yet: a change that is breaking against the base
# branch's snapshot is reported but does not fail ``check-contracts``. Set
# True when v1 is released; from then on such a change fails, and
# ``--write --accept-breaking`` is refused.
RELEASED = False
AERI_STATES = (
    "PREFLIGHT",
    "VERIFY_INITIAL",
    "FORWARD_ACTIVE",
    "FORWARD_STOPPING",
    "FORWARD_FINALIZE",
    "ROBOT_HOME",
    "SCENE_ASSESS",
    "RESET_ACTIVE",
    "RESET_VERIFY",
    "RESET_FINALIZE",
    "WAIT_HUMAN",
    "FAULT_LOCKED",
    "COMPLETED",
)
# States inside an episode (its result is not committed yet): an episode
# that faults from one of them still gets a result (task_outcome unknown).
EPISODE_STATES = (
    "FORWARD_ACTIVE",
    "FORWARD_STOPPING",
    "FORWARD_FINALIZE",
    "RESET_ACTIVE",
    "RESET_VERIFY",
    "RESET_FINALIZE",
)
RECORDS = ("run_header", "prepared", "acknowledged", "committed", "aborted", "note")
TRANSITION_REASONS = (
    "goal_verified",
    "horizon_exhausted",
    "operator_stop",
    "safety_stop",
    "policy_error",
    "watchdog_timeout",
    "recovery_ambiguous",
    "journal_corrupt",
    "preflight_passed",
    "preflight_failed",
    "robot_home_reached",
    "scene_ready",
    "scene_reset_required",
    "scene_unknown",
    # The human-assisted reset strategy: a person confirmed a scene the
    # assessment could not verify (no contract or no evidence).
    "operator_confirmed_scene",
    "reset_horizon_exhausted",
    "reset_verified",
    "home_failed",
    "recorder_failed",
    "human_resumed",
    "run_completed",
    "contract_violation_limit",
)
STOP_REASONS = (
    "goal_verified",
    "horizon_exhausted",
    "operator_stop",
    "safety_stop",
    "policy_error",
    "watchdog_timeout",
    "orchestrator_crash",
    # An episode that ended in FAULT_LOCKED (its result is still written).
    "recorder_failed",
    "home_failed",
)
# Actions that move the robot: never idempotent, always with a step.
PHYSICAL_KINDS = ("policy_steps", "home")
ACTION_KINDS = (
    "none",
    "policy_steps",
    "hold",
    "home",
    "recorder_open",
    "recorder_seal",
    "recorder_abort",
    "policy_acquire",
    "policy_quiesce",
    "judge_request",
    "scene_request",
    "notify",
)
ERROR_CODES = (
    # parsing and contract
    "E_JSON",
    "E_DUPLICATE_KEY",
    "E_NONFINITE",
    "E_UNKNOWN_FIELD",
    "E_CONTROL_FIELD",
    "E_SCHEMA",
    "E_SCHEMA_TOO_NEW",
    "E_INCONSISTENT",
    "E_SPEC_MISMATCH",
    "E_TOO_LARGE",
    # fences (raised by consumers, e.g. ``check_fresh``)
    "E_STALE_RUN",
    "E_STALE_EPISODE",
    "E_STALE_EPOCH",
    "E_EXPIRED",
    "E_CLOCK_DOMAIN",
    "E_FUTURE",
    "E_UNSOLICITED",
)
# Predicate names each judgement spec / initial-state contract declares,
# keyed by (id, version). v1 registers the online judgement's final-state
# spec only (its two answer fields); multi-predicate specs belong to A.
BUILTIN_SPECS: dict[tuple[str, str], frozenset[str]] = {
    ("generic-final", "1"): frozenset({"object_state", "stable"}),
}
SNAPSHOT_DIR = "docs/architecture/aeri/v1"


class AeriError(ValueError):
    """A message refused by the contract; ``code`` is one of ``ERROR_CODES``."""

    def __init__(self, code: str, detail: str):
        assert code in ERROR_CODES, code
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _bad(message: str):
    raise PydanticCustomError("aeri_inconsistent", message)


# --- field types ----------------------------------------------------------------

Id = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]
# ``<run_id>.<role>.<NNNN>``: NNNN matches the ``demo_NNNN`` folder.
EpisodeId = Annotated[
    str,
    StringConstraints(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\.(?:forward|reset)\.[0-9]{4,6}$"
    ),
]
TransactionId = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}:tx[0-9]{1,19}$"),
]
Name = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Label = Annotated[str, StringConstraints(min_length=1, max_length=200)]
FolderName = Annotated[str, StringConstraints(pattern=r"^[^/\x00]{1,200}$")]
# ``host-mono:<boot_id>`` is this machine's CLOCK_MONOTONIC since boot
# ``<boot_id>``; other domains (``robot:...``, ``ptp:...``) may be carried but
# compare as expired.
ClockDomain = Annotated[
    str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,31}:[A-Za-z0-9._-]{1,128}$")
]
Detail = Annotated[str, StringConstraints(max_length=300)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Ns = Annotated[int, Field(ge=0, le=INT64_MAX)]
Count = Annotated[int, Field(ge=0, le=INT64_MAX)]
Unit = Annotated[float, Field(ge=0.0, le=1.0)]
Role = Literal["forward", "reset"]
Provider = Literal["vlm", "rule", "human", "fake"]
ConfidenceKind = Literal["none", "ordinal", "calibrated"]
UnknownReason = Literal[UNKNOWN_REASONS]
AeriState = Literal[AERI_STATES]


class AeriContract(Contract):
    """Strict: no coercion on top of ``Contract`` (unknown fields refused,
    no infinities, frozen)."""

    model_config = ConfigDict(strict=True, serialize_by_alias=True)


class Part(AeriContract):
    """A nested object of a message."""


class Envelope(AeriContract):
    """Fields every message carries (its ``schema`` is declared per class)."""

    minor: Annotated[int, Field(ge=0, le=MINOR)]
    run_id: Id
    # CLOCK_REALTIME, for audit only: never compared with anything.
    emitted_wall_ns: Ns


def episode_parts(episode_id: str) -> tuple[str, str, int]:
    """``(run_id, role, number)`` of an episode id."""
    run_id, role, number = episode_id.rsplit(".", 2)
    return run_id, role, int(number)


def _same_run(message, episode_id, role=None):
    if episode_id is None:
        return
    run_id, found_role, _ = episode_parts(episode_id)
    if run_id != message.run_id:
        _bad(f"episode {episode_id} is not of run {message.run_id}")
    if role is not None and found_role != role:
        _bad(f"episode {episode_id} is not a {role} episode")


def _unique(names, what):
    if len(set(names)) != len(names):
        _bad(f"{what} repeat a name")


def _check_source(message):
    if message.provider == "vlm" and not message.model_name:
        _bad("a vlm judgement names its model")
    calibrated = message.confidence_kind == "calibrated"
    present = (
        message.calibrated_probability is not None,
        message.calibration_ref is not None,
    )
    if calibrated and not all(present):
        _bad("calibrated confidence needs a probability and a calibration reference")
    if not calibrated and any(present):
        _bad("only calibrated confidence carries a probability")


def _check_times(observed_ns, produced_ns, valid_until_ns):
    if produced_ns < observed_ns:
        _bad("produced before the evidence it reads")
    if valid_until_ns <= produced_ns:
        _bad("valid_until_ns must be later than produced_ns")
    if valid_until_ns - produced_ns > MAX_RESULT_VALIDITY_MS * 1_000_000:
        _bad(f"a result stays valid at most {MAX_RESULT_VALIDITY_MS} ms")


# --- shared parts -----------------------------------------------------------------


class SignalRef(Part):
    feature: Label
    dimension: Annotated[int, Field(ge=0, le=4096)] | None = None
    kind: Literal["measured", "commanded"]
    units: Annotated[str, StringConstraints(max_length=32)] | None = None


class EvidenceRef(Part):
    kind: Literal["frame", "signal", "clip", "event", "judgement"]
    ref: Annotated[str, StringConstraints(min_length=1, max_length=MAX_TEXT)]
    sha256: Sha256 | None = None
    step: Count | None = None


class Detector(Part):
    name: Label
    version: Label


class PredicateResult(Part):
    name: Name
    # None: the predicate could not be read.
    value: bool | None
    required: bool
    evidence_refs: Annotated[
        list[EvidenceRef], Field(default_factory=list, max_length=32)
    ]


class Veto(Part):
    id: Id
    state: Literal["confirmed", "cleared", "undecided"]


class Cost(Part):
    elapsed_ms: Count
    prompt_tokens: Count | None = None
    completion_tokens: Count | None = None


class LegacyC5(Part):
    """The online judgement's own values before mapping (audit only)."""

    reading: Literal["supported", "contradicted", "unknown"]
    outcome: Literal["success", "failure"]
    undecided: bool


# --- levi.aeri.event.v1 (A -> C) --------------------------------------------------


class EventProposal(Envelope):
    """An event candidate. ``candidate`` is never ``verified``: there is no
    such status. ``salience`` is an evidence priority, not a probability."""

    schema_id: Literal["levi.aeri.event.v1"] = Field(alias="schema")
    event_id: Id
    episode_id: EpisodeId
    episode_role: Role
    # Strictly increasing per (episode, detector): a gap is a lost event.
    seq: Count
    # Open enum (EVENT_TYPES lists the registered names).
    event_type: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,63}$")]
    status: Literal["candidate", "retracted"]
    retracts: Id | None = None
    priority: Literal["routine", "goal_candidate", "safety_relevant"]
    actor_id: Id
    step: Count
    window_steps: Annotated[list[Count], Field(min_length=2, max_length=2)] | None = (
        None
    )
    observed_ns: Ns
    clock_domain: ClockDomain
    signal_refs: Annotated[list[SignalRef], Field(default_factory=list, max_length=16)]
    evidence_refs: Annotated[
        list[EvidenceRef], Field(default_factory=list, max_length=32)
    ]
    salience: Unit | None = None
    detector: Detector

    @model_validator(mode="after")
    def _consistent(self):
        _same_run(self, self.episode_id, self.episode_role)
        if (self.status == "retracted") != (self.retracts is not None):
            _bad("retracts is set exactly when status is retracted")
        if self.retracts == self.event_id:
            _bad("an event cannot retract itself")
        if self.window_steps is not None:
            start, end = self.window_steps
            if not start <= self.step <= end:
                _bad("window_steps must contain step")
        return self


# --- unavailable (judgement, scene, runtime) ----------------------------------------


class _Unavailable(Envelope):
    """No result was produced (not a judgement, never a success)."""

    kind: Literal["unavailable"]
    request_id: Id
    retryable: bool
    retry_after_ms: Count | None = None
    detail: Detail = ""
    produced_ns: Ns
    clock_domain: ClockDomain

    @model_validator(mode="after")
    def _consistent(self):
        if self.retryable and self.code not in RETRYABLE_CODES:
            _bad(f"{self.code} is not retryable")
        if (
            self.retryable
            and self.code == "admission_rejected"
            and self.retry_after_ms is None
        ):
            _bad("admission_rejected is retryable only with retry_after_ms")
        if self.retry_after_ms is not None and not self.retryable:
            _bad("retry_after_ms only on a retryable result")
        return self


class JudgementUnavailable(_Unavailable):
    schema_id: Literal["levi.aeri.judgement.v1"] = Field(alias="schema")
    code: Literal[UNAVAILABLE_CODES]


class SceneUnavailable(_Unavailable):
    schema_id: Literal["levi.aeri.scene.v1"] = Field(alias="schema")
    code: Literal[UNAVAILABLE_CODES]


class RuntimeUnavailable(_Unavailable):
    schema_id: Literal["levi.aeri.runtime.v1"] = Field(alias="schema")
    code: Literal[RUNTIME_UNAVAILABLE_CODES]


# --- levi.aeri.judgement.v1 (A -> C) ----------------------------------------------


class Judgement(Envelope):
    """``confirmed`` needs every required predicate true and no confirmed or
    undecided veto; ``rejected`` needs a false required predicate or a
    confirmed veto; ``unknown`` may stand with any predicates."""

    schema_id: Literal["levi.aeri.judgement.v1"] = Field(alias="schema")
    kind: Literal["judgement"]
    judgement_id: Id
    # Issued by C and answered once.
    request_id: Id
    episode_id: EpisodeId
    episode_role: Role
    target: Literal["forward_goal", "reset_goal", "subgoal"]
    subgoal: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = (
        None
    )
    event_ids: Annotated[list[Id], Field(default_factory=list, max_length=16)]
    decision: Literal["confirmed", "rejected", "unknown"]
    unknown_reason: UnknownReason | None = None
    predicate_results: Annotated[list[PredicateResult], Field(max_length=32)]
    vetoes: Annotated[list[Veto], Field(default_factory=list, max_length=32)]
    observed_from_step: Count
    observed_through_step: Count
    observed_through_ns: Ns
    produced_ns: Ns
    valid_until_ns: Ns
    clock_domain: ClockDomain
    spec_id: Label
    spec_version: Label
    provider: Provider
    model_name: Label | None = None
    model_digest: Label | None = None
    confidence_kind: ConfidenceKind
    calibrated_probability: Unit | None = None
    calibration_ref: Label | None = None
    cost: Cost | None = None
    legacy_c5: LegacyC5 | None = None

    @model_validator(mode="after")
    def _consistent(self):
        _same_run(self, self.episode_id, self.episode_role)
        if (self.target == "subgoal") != (self.subgoal is not None):
            _bad("subgoal is set exactly when target is subgoal")
        if (self.decision == "unknown") != (self.unknown_reason is not None):
            _bad("unknown_reason is set exactly when decision is unknown")
        if self.observed_from_step > self.observed_through_step:
            _bad("observed_from_step is after observed_through_step")
        _check_times(self.observed_through_ns, self.produced_ns, self.valid_until_ns)
        _check_source(self)
        _unique([p.name for p in self.predicate_results], "predicate_results")
        _unique([v.id for v in self.vetoes], "vetoes")
        required = [p.value for p in self.predicate_results if p.required]
        vetoes = {v.state for v in self.vetoes}
        if self.decision == "confirmed":
            if not required or not all(value is True for value in required):
                _bad("confirmed needs at least one required predicate, all true")
            if vetoes & {"confirmed", "undecided"}:
                _bad("confirmed with a confirmed or undecided veto")
        if (
            self.decision == "rejected"
            and False not in required
            and "confirmed" not in vetoes
        ):
            _bad("rejected needs a false required predicate or a confirmed veto")
        if self.legacy_c5 is not None:
            _check_legacy(self)
        return self


def _check_legacy(message):
    """The online judgement's values (C5) mapped as the design says: an
    undecided answer is ``unknown`` (model_undecided), never a rejection."""
    c5 = message.legacy_c5
    if message.provider not in ("vlm", "rule"):
        _bad("legacy_c5 comes from a vlm or rule provider")
    if c5.undecided:
        # Whatever the outcome: anchored.undecided() also makes a success
        # with an undecided veto, a disputed exemption or a missing input
        # undecided (X1 §2.2: unknown / model_undecided).
        if message.decision != "unknown" or message.unknown_reason != "model_undecided":
            _bad("an undecided online judgement maps to unknown (model_undecided)")
    elif c5.outcome == "success" and message.decision != "confirmed":
        # A decided success whose answer fields do not both support it is
        # kept as unknown (conflicting_predicates), never confirmed.
        if not (
            message.decision == "unknown"
            and message.unknown_reason == "conflicting_predicates"
        ):
            _bad("a decided online success maps to confirmed")
    elif c5.outcome == "failure" and message.decision != "rejected":
        _bad("a decided online failure maps to rejected")


# --- levi.aeri.scene.v1 (A -> C) --------------------------------------------------


class SceneAssessment(Envelope):
    """Whether the scene is ready. ``failed_predicates`` and
    ``unknown_predicates`` repeat the required predicates that are false or
    unreadable. There is no recommended action: C decides on a reset."""

    schema_id: Literal["levi.aeri.scene.v1"] = Field(alias="schema")
    kind: Literal["scene"]
    assessment_id: Id
    request_id: Id
    # For ``initial_state``: the episode about to start.
    episode_id: EpisodeId
    target: Literal["initial_state", "post_reset", "post_forward"]
    decision: Literal["ready", "reset_required", "unknown"]
    contract_id: Label
    contract_version: Label
    predicate_results: Annotated[list[PredicateResult], Field(max_length=32)]
    failed_predicates: Annotated[list[Name], Field(max_length=32)]
    unknown_predicates: Annotated[list[Name], Field(max_length=32)]
    unknown_reason: UnknownReason | None = None
    evidence_refs: Annotated[list[EvidenceRef], Field(max_length=32)]
    observed_ns: Ns
    produced_ns: Ns
    valid_until_ns: Ns
    clock_domain: ClockDomain
    provider: Provider
    model_name: Label | None = None
    model_digest: Label | None = None
    confidence_kind: ConfidenceKind
    calibrated_probability: Unit | None = None
    calibration_ref: Label | None = None
    cost: Cost | None = None

    @model_validator(mode="after")
    def _consistent(self):
        _same_run(self, self.episode_id)
        if (self.decision == "unknown") != (self.unknown_reason is not None):
            _bad("unknown_reason is set exactly when decision is unknown")
        _check_times(self.observed_ns, self.produced_ns, self.valid_until_ns)
        _check_source(self)
        _unique([p.name for p in self.predicate_results], "predicate_results")
        _unique(self.failed_predicates, "failed_predicates")
        _unique(self.unknown_predicates, "unknown_predicates")
        required = [p for p in self.predicate_results if p.required]
        failed = {p.name for p in required if p.value is False}
        unread = {p.name for p in required if p.value is None}
        if set(self.failed_predicates) != failed:
            _bad("failed_predicates must list the required predicates that are false")
        if set(self.unknown_predicates) != unread:
            _bad("unknown_predicates must list the required predicates not read")
        if self.decision == "ready" and (not required or failed or unread):
            _bad("ready needs at least one required predicate, all true")
        if self.decision == "reset_required" and not failed:
            _bad("reset_required needs a failed predicate")
        return self


# --- levi.aeri.runtime.v1 (B <-> A/C) ----------------------------------------------


class Deadline(Part):
    due_ns: Ns
    clock_domain: ClockDomain
    budget_ms: Count
    # hard: a result after ``due_ns`` is discarded.
    hardness: Literal["hard", "soft"]


class _Runtime(Envelope):
    schema_id: Literal["levi.aeri.runtime.v1"] = Field(alias="schema")


class WorkloadSpec(_Runtime):
    """No free priority number: the priority follows from the class."""

    kind: Literal["workload_spec"]
    workload_id: Id
    workload_class: Literal[WORKLOAD_CLASSES]
    requester: Literal["a", "b", "c", "live"]
    episode_id: EpisodeId | None = None
    device_preference: Literal["gpu", "cpu", "any"]
    memory_estimate_mib: Count | None = None
    memory_source: Literal["measured", "declared", "unknown"]
    duration_p99_ms: Count | None = None
    duration_source: Literal["measured", "declared", "unknown"]
    preemptibility: Literal["none", "request_boundary", "cooperative"]

    @model_validator(mode="after")
    def _consistent(self):
        _same_run(self, self.episode_id)
        for value, source, what in (
            (self.memory_estimate_mib, self.memory_source, "memory"),
            (self.duration_p99_ms, self.duration_source, "duration"),
        ):
            if (value is None) != (source == "unknown"):
                _bad(f"a {what} estimate is given exactly when its source is known")
        return self


class ResourceLease(_Runtime):
    """Renewal or re-grant raises ``lease_epoch``; results under an older
    epoch, or after expiry or revocation, are void."""

    kind: Literal["lease"]
    lease_id: Id
    lease_epoch: Count
    workload_id: Id
    workload_class: Literal[WORKLOAD_CLASSES]
    device: Literal["gpu", "cpu"]
    memory_reserved_mib: Count | None = None
    granted_ns: Ns
    expires_ns: Ns
    clock_domain: ClockDomain
    revocable: bool

    @model_validator(mode="after")
    def _consistent(self):
        if self.expires_ns <= self.granted_ns:
            _bad("a lease expires after it is granted")
        if self.expires_ns - self.granted_ns > MAX_LEASE_MS * 1_000_000:
            _bad(f"a lease lasts at most {MAX_LEASE_MS} ms")
        return self


class AdmissionRejected(_Runtime):
    kind: Literal["admission_rejected"]
    workload_id: Id
    code: Literal[ADMISSION_CODES]
    retryable: bool
    retry_after_ms: Count | None = None
    # The live service's own code (``policy_inferring``...), for audit.
    legacy_code: Annotated[str, StringConstraints(max_length=64)] | None = None
    detail: Detail = ""

    @model_validator(mode="after")
    def _consistent(self):
        if self.retry_after_ms is not None and not self.retryable:
            _bad("retry_after_ms only on a retryable rejection")
        return self


class ResourcePressure(_Runtime):
    kind: Literal["pressure"]
    observed_ns: Ns
    clock_domain: ClockDomain
    source: Literal["c4", "broker", "fake"]
    free_mib: Count | None = None
    policy_mib: Count | None = None
    vllm_state: Literal["absent", "starting", "awake", "asleep", "unknown"]
    gate_open: bool | None = None
    gate_code: Annotated[str, StringConstraints(max_length=64)] | None = None
    policy_server_seen: bool | None = None
    age_ms: Count


class Checkpoint(Part):
    config: Label
    # The folder name, never a full path.
    dir_name: FolderName
    sha256: Sha256 | None = None


class Endpoint(Part):
    host: Literal["127.0.0.1", "::1", "localhost"]
    port: Annotated[int, Field(ge=1, le=65535)]

    @model_validator(mode="after")
    def _consistent(self):
        if self.port in ROBOT_PORTS:
            _bad(f"port {self.port} is a robot, learner or robot policy port")
        return self


class PolicyHandle(_Runtime):
    kind: Literal["policy_handle"]
    handle_id: Id
    role: Role
    checkpoint: Checkpoint
    action_contract: Annotated[
        str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_.-]{0,62}@[0-9]{1,6}$")
    ]
    action_dims: Annotated[int, Field(ge=1, le=64)]
    action_horizon: Annotated[int, Field(ge=1, le=64)]
    endpoint: Endpoint
    policy_epoch: Count


class ChunkRequest(_Runtime):
    """The observation itself travels out of band; only its step is here.
    The serving path has no hard prefix option: real-time chunking is soft
    guidance."""

    kind: Literal["chunk_request"]
    request_id: Id
    episode_id: EpisodeId
    handle_id: Id
    policy_epoch: Count
    obs_step: Count
    obs_ns: Ns
    clock_domain: ClockDomain
    action_start_index: Count
    committed_prefix_steps: Count
    previous_chunk_ref: Id | None = None
    prefix_conditioning: Literal["none", "soft_guidance"]
    deadline: Deadline

    @model_validator(mode="after")
    def _consistent(self):
        _same_run(self, self.episode_id)
        if self.deadline.clock_domain != self.clock_domain:
            _bad("the deadline uses the message's clock domain")
        return self


class ModelVersion(Part):
    config: Label
    checkpoint_dir_name: FolderName


class Timing(Part):
    queued_ms: Count | None = None
    infer_ms: Count | None = None
    total_ms: Count


class ChunkResponse(_Runtime):
    """Does not promise that a prefix equals the actions already executed:
    C uses ``valid_from_action_index`` only, and clamps every action itself."""

    kind: Literal["chunk_response"]
    request_id: Id
    episode_id: EpisodeId
    policy_epoch: Count
    action_contract: Annotated[
        str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_.-]{0,62}@[0-9]{1,6}$")
    ]
    actions: Annotated[
        list[Annotated[list[float], Field(min_length=1, max_length=64)]],
        Field(min_length=1, max_length=64),
    ]
    valid_from_action_index: Count
    prefix_conditioning_applied: Literal["none", "soft_guidance"]
    model_version: ModelVersion
    timing: Timing
    received_ns: Ns = Field(
        description=(
            "Filled by the receiving side (C's adapter) from its own clock when "
            "it collects the response, never by the server. Audit only: a "
            "deadline is judged with check_deadline on the receiver's clock, "
            "never with this field."
        )
    )
    clock_domain: ClockDomain

    @model_validator(mode="after")
    def _consistent(self):
        _same_run(self, self.episode_id)
        if len({len(row) for row in self.actions}) != 1:
            _bad("every action has the same number of dimensions")
        return self


class QuiesceAck(_Runtime):
    kind: Literal["quiesce_ack"]
    handle_id: Id
    fenced_epoch: Count
    inflight_cancelled: Count
    ok: bool


# --- levi.aeri.run_event.v1 (C only) ------------------------------------------------


class ProcessIdentity(Part):
    """What makes a PID this process (as ``levi.children.identity``)."""

    pid: Annotated[int, Field(ge=1, le=2**31 - 1)]
    start_ticks: Count
    boot_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._-]{1,128}$")]


class Authority(Part):
    principal_kind: Literal["orchestrator", "operator", "safety_guard", "recovery"]
    # Opaque: never a name or an e-mail address.
    principal_id: Id
    session_id: Id
    process: ProcessIdentity
    command_id: Id | None = None


def action_key(run_id, episode_id, kind, step) -> str:
    """``idempotency_key`` of an action: sha256 of (run, episode, kind,
    step). No control epoch: recovery always raises it, and a physical
    action prepared before a crash must stay refused after it."""
    text = json.dumps([run_id, episode_id, kind, step])
    return hashlib.sha256(text.encode()).hexdigest()


class Action(Part):
    """One action of a transaction. A physical action (``PHYSICAL_KINDS``)
    is non-idempotent; a non-idempotent action has a ``step``, assigned by
    the orchestrator, increasing per (episode, kind): the same step is never
    prepared twice. A retry after ``executed: no`` is a new logical command:
    a new step, with ``retry_of`` naming the transaction that did not run."""

    kind: Literal[ACTION_KINDS]
    # action_key(run_id, episode_id, kind, step): checked by the run event.
    idempotency_key: Sha256
    non_idempotent: bool
    params_sha256: Sha256
    step: Count | None = None
    retry_of: TransactionId | None = None

    @model_validator(mode="after")
    def _consistent(self):
        if self.kind in PHYSICAL_KINDS and not self.non_idempotent:
            _bad(f"{self.kind} moves the robot: it is never idempotent")
        if self.non_idempotent and self.step is None:
            _bad("a non-idempotent action has a step assigned by the orchestrator")
        if self.retry_of is not None and not self.non_idempotent:
            _bad("only a non-idempotent action is retried")
        return self


class Ack(Part):
    """That the controller received or executed the action, not that its
    goal was reached."""

    executed: Literal["yes", "no", "unknown"]
    source: Literal["robot_server", "policy_adapter", "recorder", "broker", "none"]
    detail_code: Name


class RolloutRef(Part):
    task_folder: FolderName
    demo: Annotated[str, StringConstraints(pattern=r"^demo_[0-9]{4,6}$")]
    sealed: Literal["complete", "incomplete", "discarded"]


class EpisodeResult(Part):
    """The autonomous verdict only: operator labels, later review and
    adjudicated truth never go into a run event."""

    task_outcome: Literal["success", "failure", "unknown"]
    stop_reason: Literal[STOP_REASONS]
    goal_verification: Literal["verified", "contradicted", "undecided", "unavailable"]
    robot_home: Literal["succeeded", "failed", "not_attempted"]
    scene_reset: Literal["skipped", "succeeded", "failed", "unknown"]
    label_kind: Literal["autonomous_verdict"]
    judgement_ids: Annotated[list[Id], Field(default_factory=list, max_length=64)]
    rollout: RolloutRef

    @model_validator(mode="after")
    def _consistent(self):
        # Not judged (undecided, unavailable) is unknown: never a success,
        # and never a failure either.
        expected = OUTCOME_OF_VERIFICATION[self.goal_verification]
        if self.task_outcome != expected:
            _bad(
                f"goal_verification {self.goal_verification} means task_outcome "
                f"{expected}"
            )
        return self


OUTCOME_OF_VERIFICATION = {
    "verified": "success",
    "contradicted": "failure",
    "undecided": "unknown",
    "unavailable": "unknown",
}


class Note(Part):
    code: Name
    detail: Detail = ""


class ContractVersion(Part):
    schema_id: Literal[tuple(SCHEMAS.values())] = Field(alias="schema")
    minor: Annotated[int, Field(ge=0)]


class RunHeader(Part):
    plan_sha256: Sha256
    contracts: Annotated[list[ContractVersion], Field(min_length=1, max_length=16)]
    levi_commit: (
        Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{7,64}$")] | None
    ) = None


class RunEvent(Envelope):
    """One line of a run's state journal (append-only, hash-chained)."""

    schema_id: Literal["levi.aeri.run_event.v1"] = Field(alias="schema")
    # From 0 per run, +1 per line, no gaps.
    sequence_no: Count
    record: Literal[RECORDS]
    # ``<run_id>:tx<sequence_no of its prepared line>``.
    transaction_id: TransactionId | None = None
    episode_id: EpisodeId | None = None
    episode_role: Role | None = None
    from_state: AeriState | None = None
    to_state: AeriState | None = None
    reason: Literal[TRANSITION_REASONS] | None = None
    authority: Authority
    control_epoch: Count
    policy_epoch: Count | None = None
    action: Action | None = None
    ack: Ack | None = None
    evidence_ids: Annotated[list[Id], Field(default_factory=list, max_length=64)]
    episode_result: EpisodeResult | None = None
    mono_ns: Ns
    clock_domain: ClockDomain
    # sha256 of the previous line's bytes (without its newline); zeros for
    # the header.
    prev_sha256: Sha256
    note: Note | None = None
    header: RunHeader | None = None

    @model_validator(mode="after")
    def _consistent(self):
        record = self.record
        if (record == "run_header") != (self.sequence_no == 0):
            _bad("the run header is line 0, and only it")
        if (record == "run_header") != (self.header is not None):
            _bad("header is set exactly on the run header")
        if record == "run_header" and self.prev_sha256 != "0" * 64:
            _bad("the run header chains to zeros")
        if record == "run_header" and self.transaction_id is not None:
            _bad("the run header has no transaction")
        if record not in ("run_header", "note") and self.transaction_id is None:
            _bad(f"a {record} record names its transaction")
        if self.transaction_id is not None:
            run, _, number = self.transaction_id.rpartition(":tx")
            if run != self.run_id:
                _bad("the transaction belongs to another run")
            if record == "prepared" and int(number) != self.sequence_no:
                _bad("a transaction is named after its prepared line")
        if record in ("prepared", "committed"):
            if self.from_state is None or self.to_state is None:
                _bad(f"a {record} record names from_state and to_state")
            if self.reason is None:
                _bad(f"a {record} record gives a reason")
        if (record == "prepared") != (self.action is not None):
            _bad("action is set exactly on a prepared record")
        if self.action is not None and self.action.idempotency_key != action_key(
            self.run_id, self.episode_id, self.action.kind, self.action.step
        ):
            _bad("idempotency_key is action_key(run_id, episode_id, kind, step)")
        if (record == "acknowledged") != (self.ack is not None):
            _bad("ack is set exactly on an acknowledged record")
        if (record == "note") != (self.note is not None):
            _bad("note is set exactly on a note record")
        if (self.episode_id is None) != (self.episode_role is None):
            _bad("episode_id and episode_role go together")
        _same_run(self, self.episode_id, self.episode_role)
        if self.episode_result is not None:
            transition = (self.from_state, self.to_state)
            faulted = (
                self.to_state == "FAULT_LOCKED"
                and self.from_state in EPISODE_STATES
                and self.episode_id is not None
            )
            if record != "committed" or not (
                transition == ("FORWARD_FINALIZE", "ROBOT_HOME")
                or self.from_state == "RESET_FINALIZE"
                or faulted
            ):
                _bad(
                    "episode_result only on a committed FORWARD_FINALIZE->ROBOT_HOME, "
                    "RESET_FINALIZE->* or episode->FAULT_LOCKED record"
                )
            if faulted and self.episode_result.task_outcome != "unknown":
                _bad("an episode that ended in FAULT_LOCKED has task_outcome unknown")
        return self


# --- the contracts as one validator each -------------------------------------------

JudgementMessage = Annotated[
    Judgement | JudgementUnavailable, Field(discriminator="kind")
]
SceneMessage = Annotated[
    SceneAssessment | SceneUnavailable, Field(discriminator="kind")
]
RuntimeMessage = Annotated[
    WorkloadSpec
    | ResourceLease
    | AdmissionRejected
    | ResourcePressure
    | PolicyHandle
    | ChunkRequest
    | ChunkResponse
    | QuiesceAck
    | RuntimeUnavailable,
    Field(discriminator="kind"),
]
ADAPTERS: dict[str, TypeAdapter] = {
    "event": TypeAdapter(EventProposal),
    "judgement": TypeAdapter(JudgementMessage),
    "scene": TypeAdapter(SceneMessage),
    "runtime": TypeAdapter(RuntimeMessage),
    "run_event": TypeAdapter(RunEvent),
}


# --- parsing ------------------------------------------------------------------------


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise AeriError("E_DUPLICATE_KEY", f"the key {key[:60]!r} appears twice")
        out[key] = value
    return out


def _constant(name):
    raise AeriError("E_NONFINITE", f"{name} is not a finite number")


def _float(text):
    value = float(text)
    if not math.isfinite(value):
        raise AeriError("E_NONFINITE", f"{text[:40]} is not a finite number")
    return value


_SEPARATORS = re.compile(r"[\s\-./:]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_COMPACT_KEYS = frozenset(key.replace("_", "") for key in CONTROL_KEYS)
# Combining marks and format characters (zero-width space, soft hyphen...).
_INVISIBLE = ("Mn", "Me", "Cf")


def normal_key(key: str) -> str:
    """A key as the control-key rules read it: compatibility forms folded,
    invisible and combining characters dropped, camelCase split, case
    folded, spaces, hyphens, dots, slashes and colons read as underscores."""
    text = unicodedata.normalize("NFKD", key)
    text = "".join(c for c in text if unicodedata.category(c) not in _INVISIBLE)
    text = _CAMEL.sub("_", unicodedata.normalize("NFKC", text))
    return _SEPARATORS.sub("_", text.casefold())


def is_control_key(key: str) -> bool:
    """Whether ``key`` names a robot or reset command (see ``normal_key``;
    also matched with the underscores removed)."""
    folded = normal_key(key)
    return (
        folded in CONTROL_KEYS
        or folded.replace("_", "") in _COMPACT_KEYS
        or folded.startswith(CONTROL_PREFIXES)
    )


def _walk_keys(value, path="$"):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key, f"{path}.{key}"
            yield from _walk_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_keys(item, f"{path}[{index}]")


def control_keys(value) -> list[str]:
    """Paths of every control key in a decoded message."""
    return [path for key, path in _walk_keys(value) if is_control_key(str(key))]


def operator_keys(value) -> list[str]:
    """Paths of keys naming operator or evaluation data (requests to A must
    have none: operator truth never reaches a model)."""
    return [
        path
        for key, path in _walk_keys(value)
        if any(
            word in unicodedata.normalize("NFKC", str(key)).casefold()
            for word in OPERATOR_WORDS
        )
    ]


def _contract_name(contract: str) -> str:
    if contract in SCHEMAS:
        return contract
    for name, schema in SCHEMAS.items():
        if schema == contract:
            return name
    raise ValueError(f"unknown AERI contract {contract!r}")


def _translate(exc: ValidationError) -> AeriError:
    errors = exc.errors(include_url=False, include_input=False)
    kinds = [error["type"] for error in errors]
    if "extra_forbidden" in kinds:
        code = "E_UNKNOWN_FIELD"
        first = errors[kinds.index("extra_forbidden")]
    elif "aeri_inconsistent" in kinds:
        code = "E_INCONSISTENT"
        first = errors[kinds.index("aeri_inconsistent")]
    elif "finite_number" in kinds:
        code = "E_NONFINITE"
        first = errors[kinds.index("finite_number")]
    else:
        code, first = "E_SCHEMA", errors[0]
    where = ".".join(str(part) for part in first["loc"]) or "$"
    more = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
    return AeriError(code, f"{where}: {first['msg']}{more}")


def _check_spec(message, specs: Mapping[tuple[str, str], frozenset[str]]):
    if isinstance(message, Judgement):
        key = (message.spec_id, message.spec_version)
    elif isinstance(message, SceneAssessment):
        key = (message.contract_id, message.contract_version)
    else:
        return
    names = specs.get(key)
    if names is None:
        raise AeriError("E_SPEC_MISMATCH", f"no registered spec {key[0]}@{key[1]}")
    unknown = sorted({p.name for p in message.predicate_results} - set(names))
    if unknown:
        raise AeriError(
            "E_SPEC_MISMATCH",
            f"{key[0]}@{key[1]} declares no predicate {', '.join(unknown)}",
        )


def validate(value: Any, contract: str, *, specs=None):
    """A decoded message checked like ``parse`` does (control keys, schema,
    minor, strict validation); for producers that build dicts in Python."""
    name = _contract_name(contract)
    if not isinstance(value, dict):
        raise AeriError("E_SCHEMA", "a message is a JSON object")
    if name in CONTROL_SCANNED:
        found = control_keys(value)
        if found:
            raise AeriError(
                "E_CONTROL_FIELD",
                f"{', '.join(found[:5])}: an A or B message never carries a robot "
                "or reset command (C alone decides and acts)",
            )
    if value.get("schema") != SCHEMAS[name]:
        raise AeriError(
            "E_SCHEMA",
            f"schema {str(value.get('schema'))[:60]!r} is not {SCHEMAS[name]}",
        )
    minor = value.get("minor")
    if type(minor) is int and minor > MINOR:
        raise AeriError(
            "E_SCHEMA_TOO_NEW",
            f"minor {minor} is newer than {MINOR}, the newest this reader knows",
        )
    try:
        message = ADAPTERS[name].validate_python(value, strict=True)
    except ValidationError as exc:
        raise _translate(exc) from None
    if specs is not None:
        _check_spec(message, specs)
    return message


def parse(raw: bytes | str, contract: str, *, specs=None):
    """The message in ``raw`` as its contract model; ``AeriError`` otherwise.

    ``contract`` is a name of ``SCHEMAS`` or its schema id: the caller says
    what it expects. ``specs`` (``{(id, version): predicate names}``, e.g.
    ``BUILTIN_SPECS``) also checks every predicate name against the spec the
    judgement or scene assessment cites (``E_SPEC_MISMATCH``)."""
    name = _contract_name(contract)
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(raw, (bytes, bytearray)):
        raise AeriError("E_JSON", "a message is bytes or text")
    if len(raw) > MAX_BYTES:
        raise AeriError("E_TOO_LARGE", f"{len(raw)} bytes is over {MAX_BYTES}")
    try:
        value = json.loads(
            bytes(raw).decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=_constant,
            parse_float=_float,
        )
    except AeriError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise AeriError("E_JSON", f"not JSON: {type(exc).__name__}") from None
    return validate(value, name, specs=specs)


def dump(message) -> bytes:
    """Canonical bytes of a message (sorted keys, no spaces, UTF-8)."""
    return json.dumps(
        message.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


# --- clocks and freshness -----------------------------------------------------------


def host_clock_domain() -> str:
    """``host-mono:<boot_id>``: CLOCK_MONOTONIC of this boot of this machine."""
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    return f"host-mono:{boot}"


def _now(now_ns, local):
    """The consumer's own clock: this host's monotonic clock and its domain.
    Tests may pass both; callers in production pass neither."""
    if (now_ns is None) != (local is None):
        raise ValueError("pass both now_ns and local, or neither")
    if now_ns is None:
        return time.monotonic_ns(), host_clock_domain()
    return now_ns, local


def _span(message) -> tuple[int, int]:
    if isinstance(message, Judgement | SceneAssessment):
        return message.produced_ns, message.valid_until_ns
    if isinstance(message, ResourceLease):
        return message.granted_ns, message.expires_ns
    raise TypeError(f"{type(message).__name__} carries no validity span")


def check_fresh(message, *, now_ns: int | None = None, local: str | None = None):
    """Refuse a judgement, scene assessment or lease that cannot be used now:
    another clock domain (cannot be compared: counts as expired,
    ``E_CLOCK_DOMAIN``), produced or granted more than
    ``FUTURE_TOLERANCE_NS`` ahead of our clock (``E_FUTURE``), or expired
    (``E_EXPIRED``). ``now`` is read from the consumer's own monotonic clock,
    never from a field the producer wrote. A necessary check, not a
    sufficient one: the run, episode, epoch and request fences come on top."""
    now_ns, local = _now(now_ns, local)
    if message.clock_domain != local:
        raise AeriError(
            "E_CLOCK_DOMAIN", f"{message.clock_domain} cannot be compared with {local}"
        )
    start, until = _span(message)
    if start > now_ns + FUTURE_TOLERANCE_NS:
        raise AeriError("E_FUTURE", "produced later than our own clock reads")
    if now_ns >= until:
        raise AeriError("E_EXPIRED", "the validity has passed")


def check_deadline(deadline, *, now_ns: int | None = None, local: str | None = None):
    """Whether a result collected now meets ``deadline``, judged on the
    receiver's own clock (never on a time the producer reported). A hard
    deadline that has passed raises ``E_EXPIRED``; a soft one returns False."""
    now_ns, local = _now(now_ns, local)
    if deadline.clock_domain != local:
        raise AeriError(
            "E_CLOCK_DOMAIN", f"{deadline.clock_domain} cannot be compared with {local}"
        )
    if now_ns <= deadline.due_ns:
        return True
    if deadline.hardness == "hard":
        raise AeriError("E_EXPIRED", "the hard deadline has passed")
    return False


# --- schema snapshots and compatibility ---------------------------------------------


def schema_documents() -> dict[str, dict]:
    """The JSON Schema of each contract, with the lists a snapshot must
    capture (control keys, codes) as ``x-levi-*`` annotations."""
    out = {}
    for name, schema_id in SCHEMAS.items():
        body = ADAPTERS[name].json_schema(by_alias=True)
        document = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": schema_id,
            "x-levi-major": MAJOR,
            "x-levi-minor": MINOR,
            "x-levi-control-keys": list(CONTROL_KEYS)
            if name in CONTROL_SCANNED
            else [],
            "x-levi-control-prefixes": list(CONTROL_PREFIXES)
            if name in CONTROL_SCANNED
            else [],
        }
        if name in ("judgement", "scene", "runtime"):
            document["x-levi-retryable-codes"] = list(RETRYABLE_CODES)
            document["x-levi-future-tolerance-ns"] = FUTURE_TOLERANCE_NS
        if name in ("judgement", "scene"):
            document["x-levi-max-result-validity-ms"] = MAX_RESULT_VALIDITY_MS
        if name == "runtime":
            document["x-levi-max-lease-ms"] = MAX_LEASE_MS
            document["x-levi-forbidden-ports"] = list(ROBOT_PORTS)
        if name == "event":
            document["x-levi-registered-event-types"] = list(EVENT_TYPES)
        out[name] = {**document, **body}
    return out


def render_schema(document: dict) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def snapshot_texts() -> dict[str, str]:
    """``{relative path: text}`` of every committed schema file."""
    return {
        f"{SNAPSHOT_DIR}/{name}.schema.json": render_schema(document)
        for name, document in schema_documents().items()
    }


# Annotations that never change what a message may contain.
_NEUTRAL = {"description", "title", "examples", "$comment", "x-levi-minor"}
# An upper bound may grow in a minor version; a lower bound may shrink.
_UPPER = {"maxLength", "maxItems", "maximum", "exclusiveMaximum"}
_LOWER = {"minLength", "minItems", "minimum", "exclusiveMinimum"}


def breaking_changes(old, new, path="$") -> list[str]:
    """Differences between two schemas that need a new major version.

    Compatible within a major: a new optional property, a new ``$defs``
    entry, a looser bound, a new registered event type, a longer control-key
    list, annotations. Everything else is breaking (fail closed): a removed
    property or definition, a new required field (or one no longer
    required), any change to an enum, const, pattern, type, default,
    reference, discriminator or union."""
    if type(old) is not type(new):
        return [f"{path}: type of the schema node changed"]
    if isinstance(old, list):
        if len(old) != len(new):
            return [f"{path}: alternatives changed"]
        return [
            change
            for index, (a, b) in enumerate(zip(old, new))
            for change in breaking_changes(a, b, f"{path}[{index}]")
        ]
    if not isinstance(old, dict):
        return [] if old == new else [f"{path}: {old!r} became {new!r}"]
    changes = []
    for key in sorted(set(old) | set(new)):
        here = f"{path}.{key}"
        if key in _NEUTRAL or key == "x-levi-registered-event-types":
            continue
        if key not in new:
            if key not in _UPPER and key not in _LOWER:
                changes.append(f"{here}: removed")
            continue
        if key not in old:
            if key in ("properties", "$defs") or key in _NEUTRAL:
                continue
            changes.append(f"{here}: added")
            continue
        a, b = old[key], new[key]
        if key in _UPPER:
            if b < a:
                changes.append(f"{here}: {a} lowered to {b}")
        elif key in _LOWER:
            if b > a:
                changes.append(f"{here}: {a} raised to {b}")
        elif key == "x-levi-control-keys" or key == "x-levi-control-prefixes":
            missing = sorted(set(a) - set(b))
            if missing:
                changes.append(f"{here}: no longer refuses {', '.join(missing)}")
        elif key == "enum":
            added, removed = sorted(set(b) - set(a)), sorted(set(a) - set(b))
            if added or removed:
                changes.append(
                    f"{here}: closed enum changed (added {added}, removed {removed})"
                )
        elif key == "required":
            if set(a) != set(b):
                changes.append(f"{here}: {sorted(a)} became {sorted(b)}")
        elif key in ("properties", "$defs"):
            for name in sorted(a):
                if name not in b:
                    changes.append(f"{here}.{name}: removed")
                else:
                    changes += breaking_changes(a[name], b[name], f"{here}.{name}")
        else:
            changes += breaking_changes(a, b, here)
    return changes


def check_snapshots(root: Path) -> list[str]:
    """Problems with the committed schema files under ``root``: missing,
    stale (with any breaking difference spelled out) or left over."""
    root = Path(root)
    expected = snapshot_texts()
    problems = []
    for relative, text in expected.items():
        path = root / relative
        try:
            current = path.read_text()
        except OSError:
            problems.append(f"{relative} is missing")
            continue
        if current == text:
            continue
        problems.append(f"{relative} is out of date")
        try:
            breaking = breaking_changes(json.loads(current), json.loads(text))
        except ValueError:
            breaking = ["the committed file is not JSON"]
        problems += [f"{relative}: breaking within v{MAJOR}: {b}" for b in breaking]
    folder = root / SNAPSHOT_DIR
    if folder.is_dir():
        known = {Path(relative).name for relative in expected}
        problems += [
            f"{SNAPSHOT_DIR}/{p.name} is not a contract of this version"
            for p in sorted(folder.iterdir())
            if p.name not in known
        ]
    return problems


def write_snapshots(root: Path, *, accept_breaking: bool = False) -> list[str]:
    """Rewrite the schema files; refuses (writing nothing) when a change is
    breaking within the major version, unless ``accept_breaking``. Returns
    the refusals."""
    root = Path(root)
    expected = snapshot_texts()
    refusals = []
    for relative, text in expected.items():
        path = root / relative
        if not path.is_file() or accept_breaking:
            continue
        try:
            old = json.loads(path.read_text())
        except ValueError:
            continue
        refusals += [
            f"{relative}: breaking within v{MAJOR}: {b}"
            for b in breaking_changes(old, json.loads(text))
        ]
    if refusals:
        return refusals
    for relative, text in expected.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file() or path.read_text() != text:
            path.write_text(text)
    return []


# --- comparison with the base branch -----------------------------------------------


def _git(root: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )


BASE_ENV = "LEVI_CONTRACT_BASE"
DEFAULT_BASES = ("main", "origin/main")
_BASE_FIX = (
    "pass --base <ref> or set LEVI_CONTRACT_BASE (in CI: --base origin/main, "
    "with the full history fetched)"
)


class BaseUnavailable(ValueError):
    """The base branch's snapshots cannot be read: the check fails."""


def resolve_base(root: Path, base: str | None = None) -> str:
    """The ref to compare with: ``base`` (``--base``), else
    ``$LEVI_CONTRACT_BASE``, else local ``main``, else ``origin/main``. A
    named ref that does not resolve is not replaced by another one."""
    root = Path(root)
    try:
        top = _git(root, "rev-parse", "--show-toplevel")
    except OSError as exc:
        raise BaseUnavailable(f"git is unavailable ({exc}); {_BASE_FIX}") from None
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != root.resolve():
        raise BaseUnavailable(f"{root} is not the top of a git checkout; {_BASE_FIX}")
    named = base or os.environ.get(BASE_ENV) or None
    candidates = (named,) if named else DEFAULT_BASES
    for ref in candidates:
        if (
            _git(
                root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"
            ).returncode
            == 0
        ):
            return ref
    raise BaseUnavailable(f"no {' or '.join(candidates)} in {root}; {_BASE_FIX}")


def check_against_base(
    root: Path, base: str | None = None
) -> tuple[list[str], list[str]]:
    """``(problems, notes)``: the current models against the snapshots at
    ``git merge-base HEAD <base>`` (``resolve_base`` picks the base). The
    same-tree check cannot see a breaking change committed together with its
    snapshot; this one can, before the merge (on main itself it compares
    main with main).

    A base that cannot be read (no git, not the top of a checkout, no such
    ref, shallow history) is a problem, never a pass. A snapshot missing at
    the base is a new contract (a note). A breaking difference is a problem
    once ``RELEASED``, a note before."""
    root = Path(root)
    try:
        base = resolve_base(root, base)
    except BaseUnavailable as exc:
        return [f"cannot read the base snapshots: {exc}"], []
    found = _git(root, "merge-base", "HEAD", base)
    if found.returncode != 0:
        detail = (found.stderr.strip() or "no common ancestor").splitlines()[0]
        message = (
            f"cannot read the base snapshots: `git merge-base HEAD {base}` failed "
            f"({detail}); fetch {base} with its history, or {_BASE_FIX}"
        )
        return [message], []
    commit = found.stdout.strip()
    where = f"{base} ({commit[:12]})"
    problems, notes = [], []
    for name, document in schema_documents().items():
        relative = f"{SNAPSHOT_DIR}/{name}.schema.json"
        listed = _git(root, "ls-tree", "--name-only", commit, "--", relative)
        if listed.returncode != 0:
            problems.append(f"{relative}: cannot list it at {where}")
            continue
        if not listed.stdout.strip():
            notes.append(f"{relative}: new since {where}")
            continue
        shown = _git(root, "show", f"{commit}:{relative}")
        try:
            old = json.loads(shown.stdout) if shown.returncode == 0 else None
        except ValueError:
            old = None
        if not isinstance(old, dict):
            problems.append(f"{relative}: unreadable at {where}")
            continue
        for change in breaking_changes(old, document):
            line = f"{relative}: breaking against {where}: {change}"
            if RELEASED:
                problems.append(line)
            else:
                notes.append(f"{line} (allowed: v{MAJOR} is not released)")
    return problems, notes
