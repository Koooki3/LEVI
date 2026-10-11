"""Explicit CPU preparation and borrowed dispatch; no SDK or transport fallback.

Preparation and decoding are reviewed injection contracts. This module installs
no physical observer, model factory, prompt, deployment or real FR3 binding.
"""

import hashlib
import json
from dataclasses import dataclass

from ..intervention import CompletionEvidence, InterventionStopped, InterventionUnproven

MAX_BYTES = 16 * 1024 * 1024
POLL_NS = 100_000_000


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


@dataclass(frozen=True)
class SourceBinding:
    run_id: str
    episode_id: str
    episode_role: str
    policy_epoch: int
    clock_domain: str
    observed_ns: int
    source_kind: str
    payload_sha256: str


@dataclass(frozen=True)
class PreparedCall:
    """Immutable CPU wire input, exact AERI request and explicit source binding.

    Scene preparers must return JPEG refs/bytes and put those same refs/hashes
    in their frozen model payload. Goal preparers must use PausedFrames.freeze;
    policy preparers freeze actual raw/pose/prompt, never a later robot.raw.
    """

    request: bytes
    payload: bytes
    binding: SourceBinding
    frames: tuple = ()


@dataclass(frozen=True)
class PreparationContext:
    kind: str
    policy_epoch: int
    deadline_ns: int
    evidence: object = None


class OwnedCalls:
    """Explicit injected codecs over one original borrowed executor."""

    def __init__(
        self,
        execution,
        cycle,
        *,
        prepare_scene,
        prepare_goal,
        prepare_chunk,
        decode_scene,
        decode_goal,
        decode_chunk,
        completion_adapter,
    ):
        callbacks = (
            prepare_scene,
            prepare_goal,
            prepare_chunk,
            decode_scene,
            decode_goal,
            decode_chunk,
            completion_adapter,
        )
        if any(not callable(callback) for callback in callbacks):
            raise ValueError(
                "every CPU preparer, decoder and completion adapter is required"
            )
        self.execution, self.cycle = execution, cycle
        self.preparers = {
            "scene": prepare_scene,
            "goal": prepare_goal,
            "chunk": prepare_chunk,
        }
        self.decoders = {
            "scene": decode_scene,
            "goal": decode_goal,
            "chunk": decode_chunk,
        }
        self.completion_adapter = completion_adapter
        self.validate(cycle)

    def validate(self, cycle):
        # Required public contract, never choose a transport by method presence.
        if (
            self.cycle is not cycle
            or self.execution.authority is not cycle.authority
            or self.execution.runtime is not cycle.runtime
            or self.execution.lease.lease_id != cycle.runtime.lease_id
            or self.execution.lease.lease_epoch != cycle.runtime.lease_epoch
            or not callable(self.execution.dispatch_existing)
            or not callable(self.execution.collect_existing)
        ):
            raise ValueError("same original Cycle authority/runtime/lease required")

    def invocation(
        self,
        kind,
        request,
        *,
        role,
        policy_epoch,
        deadline_ns,
        interrupted,
        observation=None,
        observe=None,
        min_observed_ns,
        evidence=None,
    ):
        self.validate(self.cycle)
        if kind not in self.preparers or (observation is None) == (observe is None):
            raise InterventionUnproven(
                "one explicit current observation source required"
            )
        if observe is not None and not callable(observe):
            raise InterventionUnproven("explicit fresh scene observer required")
        if request.get("run_id") != self.cycle.run_id:
            raise InterventionUnproven("CPU request belongs to another frozen run")
        return Invocation(
            self,
            kind,
            canonical(request),
            role,
            policy_epoch,
            deadline_ns,
            interrupted,
            observation,
            observe,
            min_observed_ns,
            evidence,
        )

    def completion(self, request, returned):
        # The injected borrowed executor verifies actual SDK ACK provenance.
        receipt = self.completion_adapter(request, returned)
        if type(receipt) is not CompletionEvidence or receipt.request is not request:
            raise InterventionUnproven("explicit exact completion receipt required")
        return receipt


class Invocation:
    """One local call/receipt; never stores or discovers an authority grant."""

    def __init__(
        self,
        owner,
        kind,
        request,
        role,
        epoch,
        deadline,
        interrupted,
        observation,
        observe,
        min_observed,
        evidence,
    ):
        self.owner, self.kind, self.request, self.role = owner, kind, request, role
        self.epoch, self.deadline, self.interrupted = epoch, deadline, interrupted
        self.observation, self.observe = observation, observe
        self.min_observed, self.evidence = min_observed, evidence
        self.prepared = None

    def _check_source(self, prepared, observation):
        request = json.loads(self.request)
        binding = prepared.binding if type(prepared) is PreparedCall else None
        if (
            type(prepared) is not PreparedCall
            or type(binding) is not SourceBinding
            or prepared.request != self.request
            or type(prepared.request) is not bytes
            or type(prepared.payload) is not bytes
            or not 0 < len(prepared.payload) <= MAX_BYTES
            or type(binding.policy_epoch) is not int
            or type(binding.observed_ns) is not int
            or binding.run_id != request["run_id"]
            or binding.episode_id != request["episode_id"]
            or binding.episode_role != request["episode_id"].rsplit(".", 2)[1]
            or binding.policy_epoch != self.epoch
            or binding.clock_domain != self.owner.cycle.clock.domain
            or binding.source_kind
            != {"scene": "scene", "goal": "paused", "chunk": "policy"}[self.kind]
            or binding.payload_sha256 != hashlib.sha256(prepared.payload).hexdigest()
            or type(observation.get("observed_ns")) is not int
            or binding.observed_ns != observation["observed_ns"]
            or not self.min_observed
            <= binding.observed_ns
            <= self.owner.cycle.clock.now()
            or self.owner.cycle.clock.now() >= self.deadline
        ):
            raise InterventionUnproven(
                "frozen CPU request/source/epoch/time binding mismatch"
            )
        if (
            self.kind == "goal"
            and request["observed_through_ns"] != binding.observed_ns
        ):
            raise InterventionUnproven("paused endpoint differs from goal request")
        if self.kind == "chunk" and request["obs_ns"] != binding.observed_ns:
            raise InterventionUnproven("policy request does not use this observation")
        if type(prepared.frames) is not tuple or any(
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or not item[0]
            or type(item[1]) is not bytes
            or not item[1]
            for item in prepared.frames
        ):
            raise InterventionUnproven("immutable retained frame refs/bytes required")
        if len({ref for ref, _ in prepared.frames}) != len(prepared.frames):
            raise InterventionUnproven("duplicate source frame refs")
        if sum(len(data) for _, data in prepared.frames) > MAX_BYTES:
            raise InterventionUnproven("retained frames exceed original byte budget")
        if self.kind == "scene":
            if self.evidence is None or not prepared.frames:
                raise InterventionUnproven("scene requires retained current frames")
            from pathlib import Path

            from ..fr3_providers import retained_hash

            for ref, data in prepared.frames:
                saved = self.evidence.keep_frame(ref, data)
                relative = Path(saved.get("file") or "")
                digest = hashlib.sha256(data).hexdigest()
                if (
                    saved.get("skipped")
                    or not saved.get("file")
                    or relative.is_absolute()
                    or ".." in relative.parts
                    or saved.get("sha256") != digest
                    or retained_hash(self.evidence.folder / relative) != digest
                ):
                    raise InterventionUnproven("scene frame was not durably retained")

    def call(self, request):
        self.owner.validate(self.owner.cycle)
        expected = json.loads(self.request)
        if (
            self.prepared is not None
            or request.runtime is not self.owner.cycle.runtime
            or (request.role, request.episode_id, request.policy_epoch)
            != (self.role, expected["episode_id"], self.epoch)
        ):
            raise InterventionUnproven(
                "exact current role request and one dispatch required"
            )
        if self.interrupted():
            raise InterventionStopped("stop before CPU preparation")
        before = self.owner.cycle.clock.now()
        observation = self.observe() if self.observe is not None else self.observation
        if self.observe is not None and (
            type(observation.get("observed_ns")) is not int
            or not before <= observation["observed_ns"] <= self.owner.cycle.clock.now()
        ):
            raise InterventionUnproven(
                "scene observation must be captured after role grant"
            )
        prepared = self.owner.preparers[self.kind](
            json.loads(self.request),
            observation,
            PreparationContext(self.kind, self.epoch, self.deadline, self.evidence),
        )
        self._check_source(prepared, observation)
        self.prepared = prepared
        if self.interrupted():
            raise InterventionStopped("stop before SDK dispatch")
        self.owner.execution.dispatch_existing(
            request,
            prepared.payload,
            role=self.role,
            episode_id=expected["episode_id"],
            policy_epoch=self.epoch,
        )
        while self.owner.cycle.clock.now() < self.deadline:
            if self.interrupted():
                raise InterventionStopped("stop during SDK collection")
            left = self.deadline - self.owner.cycle.clock.now()
            try:
                return self.owner.execution.collect_existing(
                    request,
                    timeout_s=min(left, POLL_NS) / 1e9,
                )
            except TimeoutError:
                continue
        raise InterventionUnproven("borrowed execution exceeded request deadline")

    def decode(self, result):
        """Run CPU codec only after Cycle has returned from original finish."""
        try:
            if self.prepared is None or result is None or self.owner.cycle.failed:
                raise InterventionUnproven("missing/prohibited SDK result")
            decoded = self.owner.decoders[self.kind](
                self.prepared, result, self.owner.cycle.clock.now()
            )
            expected = dict if self.kind == "chunk" else bytes
            if type(decoded) is not expected:
                raise InterventionUnproven(
                    "CPU codec did not return the AERI wire type"
                )
            message = json.loads(decoded) if expected is bytes else decoded
            request = json.loads(self.request)
            if any(
                message.get(key) != request[key]
                for key in ("run_id", "episode_id", "request_id")
            ):
                raise InterventionUnproven(
                    "CPU codec changed the frozen request identity"
                )
            if message.get("clock_domain") != self.prepared.binding.clock_domain:
                raise InterventionUnproven("CPU codec changed the source clock domain")
            if self.kind in ("scene", "goal"):
                field = "observed_ns" if self.kind == "scene" else "observed_through_ns"
                if (
                    type(message.get(field)) is not int
                    or message[field] != self.prepared.binding.observed_ns
                    or message.get("target") != request["target"]
                ):
                    raise InterventionUnproven(
                        "CPU codec observation differs from frozen source"
                    )
            if self.kind == "scene":
                retained = {
                    ref: hashlib.sha256(data).hexdigest()
                    for ref, data in self.prepared.frames
                }
                cited = list(message.get("evidence_refs", []))
                for predicate in message.get("predicate_results", []):
                    refs = predicate.get("evidence_refs", [])
                    if (
                        predicate.get("required") is True
                        and predicate.get("value") is not None
                        and not refs
                    ):
                        raise InterventionUnproven(
                            "required predicate lacks current frame evidence"
                        )
                    cited.extend(refs)
                if not cited or any(
                    ref.get("kind") != "frame"
                    or retained.get(ref.get("ref")) != ref.get("sha256")
                    or ref.get("ref") not in retained
                    for ref in cited
                ):
                    raise InterventionUnproven("scene codec cited another source frame")
            return decoded
        except Exception as exc:
            self.owner.cycle.interrupt()
            if isinstance(exc, InterventionUnproven):
                raise
            raise InterventionUnproven("CPU decoding failed; no fallback") from exc
