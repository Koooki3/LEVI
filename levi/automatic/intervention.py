"""Injected event-pause coordination over the EXISTING runtime authority.

No scheduler, hardware observer or backend implementation lives here. Real FR3
factories do not supply these hooks and therefore cannot enable this mode.
"""

import hashlib
import json
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass

from levi.domain import aeri


class InterventionUnproven(ValueError):
    """Permission is unknown: keep ownership and never retry motion."""


class InterventionStopped(InterventionUnproven):
    pass


def hold_contract_sha256(config):
    return hashlib.sha256(
        json.dumps(asdict(config), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class PhysicalHoldProof:
    run_id: str
    episode_id: str
    policy_epoch: int
    plan_sha256: str
    condition_sha256: str
    clock_domain: str
    observed_from_ns: int
    observed_through_ns: int
    expires_ns: int
    admission_closed: bool | None
    no_new_commands: bool | None
    physically_stable: bool | None


@dataclass(frozen=True)
class CompletionEvidence:
    """Explicit receipt; the original authority alone validates stop and idle."""

    request: object
    result: object
    stop_proof: object
    observation: object


@dataclass(frozen=True)
class InterventionHooks:
    # Trusted observers must come from owned real backends, never HTTP/client_done.
    stop_admission: Callable
    resume_admission: Callable
    observe_hold: Callable
    observe_runtime: Callable
    role_window: Callable
    stop_proof: Callable
    prepare_evidence: Callable


class InferenceCycle:
    """Only a wrapper around RuntimeOwnership's single request slot.

    begin/finish are the original role authority. Proof failure retains its slot;
    cancellation revokes results without waiting for a model lock or a drainer.
    The caller journals epoch changes and supplies fresh observation after resume.
    """

    def __init__(
        self, *, authority, runtime, hooks, clock, run_id, plan_sha256, condition_sha256
    ):
        if (
            not isinstance(hooks, InterventionHooks)
            or any(
                not callable(getattr(hooks, name))
                for name in hooks.__dataclass_fields__
            )
            or any(
                not callable(getattr(authority, name, None))
                for name in ("begin_role_request", "finish_request", "revoke_request")
            )
        ):
            raise InterventionUnproven(
                "owned runtime role authority and trusted proof hooks required"
            )
        self.authority, self.runtime, self.hooks = authority, runtime, hooks
        self.clock, self.run_id, self.plan_sha256 = clock, run_id, plan_sha256
        self.condition_sha256 = condition_sha256
        self.hold_conditions = None
        self.authorization_epoch = 0
        self._request = None
        self._lock = threading.Lock()  # token bookkeeping only; never held across I/O
        self.failed = False
        self.operator_stopped = False  # latched by a stop, not by a failed proof
        self.proof_failed = False  # a proof failed: never rearmed
        self.revocation_error = None

    @property
    def request_open(self) -> bool:
        """A role request is still granted (its slot is not released)."""
        with self._lock:
            return self._request is not None

    def validate(self, config, clock):
        if (
            self.run_id != config.run_id
            or self.plan_sha256 != config.plan_sha256
            or self.clock is not clock
            or self.condition_sha256
            != hold_contract_sha256(config.termination.physical_hold)
            or self.failed
        ):
            raise InterventionUnproven("cycle differs from frozen run/hold contract")
        self.hold_conditions = config.termination.physical_hold

    def note_operator_stop(self) -> None:
        """Called under the orchestrator's stop-registry lock, before the stop
        is visible to the loop, so a loop-side interrupt() cannot win the race
        and make an operator's stop look like a failed proof."""
        with self._lock:
            if not self.failed:
                self.operator_stopped = True

    def rearm(self) -> bool:
        """Open the cycle again after an operator's stop, only when no proof
        failed and the request slot is confirmed released; else it stays latched."""
        with self._lock:
            if (
                not (self.failed and self.operator_stopped)
                or self.proof_failed
                or self._request is not None
            ):
                return False
            if self.revocation_error is not None:
                return False
            self.failed = self.operator_stopped = False
            return True

    def interrupt(self, *, operator: bool = False, proof_failed: bool = False):
        """Revoke immediately; a failed proof stays latched even with a stop."""
        with self._lock:
            if operator and not self.failed:
                self.operator_stopped = True
            self.proof_failed |= proof_failed
            self.failed = True
            request = self._request
        if request is not None:
            try:
                self.authority.revoke_request(request, now_ns=self.clock.now())
            except ValueError:
                return  # a completed barrier already cleared the request
            except Exception as exc:  # noqa: BLE001 - P0 cannot wait or re-enter I/O
                # Keep the failure observable without backend or journal I/O.
                # Unknown authority never justifies clearing its ownership.
                self.revocation_error = type(exc).__name__

    @contextmanager
    def _proof_boundary(self):
        """All trusted observations and grants share one irreversible latch."""
        try:
            if self.failed:
                raise InterventionStopped("execution already stopped or proof failed")
            yield
        except BaseException as exc:
            self.interrupt(proof_failed=not isinstance(exc, InterventionStopped))
            if isinstance(exc, Exception) and not isinstance(exc, InterventionUnproven):
                raise InterventionUnproven(
                    "runtime authority/physical evidence unproven"
                ) from exc
            raise

    def hold(self, episode_id, policy_epoch, *, since_ns, deadline_ns):
        with self._proof_boundary():
            proof = self.hooks.observe_hold(episode_id, policy_epoch, deadline_ns)
            now = self.clock.now()
            if not isinstance(proof, PhysicalHoldProof) or not (
                proof.run_id == self.run_id
                and proof.episode_id == episode_id
                and proof.policy_epoch == policy_epoch
                and proof.plan_sha256 == self.plan_sha256
                and proof.condition_sha256 == self.condition_sha256
                and proof.clock_domain == self.clock.domain
                and type(proof.observed_from_ns) is int
                and type(proof.observed_through_ns) is int
                and type(proof.expires_ns) is int
                and self.hold_conditions is not None
                and proof.observed_through_ns - proof.observed_from_ns
                >= self.hold_conditions.stable_window_ns
                and since_ns
                <= proof.observed_from_ns
                <= proof.observed_through_ns
                <= now
                < proof.expires_ns
                <= deadline_ns
                and proof.admission_closed is True
                and proof.no_new_commands is True
                and proof.physically_stable is True
            ):
                self.failed = self.proof_failed = True
                raise InterventionUnproven(
                    "independent fresh physical hold proof required"
                )
            return proof

    def pause(self, episode_id, old_epoch, new_epoch, *, deadline_ns):
        with self._proof_boundary():
            began = self.clock.now()
            if type(new_epoch) is not int or new_epoch <= old_epoch:
                raise InterventionUnproven("pause must advance policy epoch")
            if self.hooks.stop_admission(episode_id, old_epoch, new_epoch) is not True:
                self.failed = self.proof_failed = True
                raise InterventionUnproven("policy admission/old buffer fence unproven")
            return self.hold(
                episode_id, new_epoch, since_ns=began, deadline_ns=deadline_ns
            )

    def invoke(
        self,
        role,
        episode_id,
        policy_epoch,
        *,
        deadline_ns,
        call=None,
        request_call=None,
        completion_adapter=None,
        interrupted,
        hold_since_ns=None,
    ):
        with self._proof_boundary():
            if (call is None) == (request_call is None) or any(
                value is not None and not callable(value)
                for value in (call, request_call)
            ):
                raise InterventionUnproven(
                    "exactly one callable execution entry required"
                )
            if completion_adapter is not None and (
                request_call is None or not callable(completion_adapter)
            ):
                raise InterventionUnproven(
                    "completion adapter requires the explicit request callback"
                )
            if interrupted():
                raise InterventionStopped("execution already stopped")
            now = self.clock.now()
            if role == "judge":
                self.hold(
                    episode_id,
                    policy_epoch,
                    since_ns=now if hold_since_ns is None else hold_since_ns,
                    deadline_ns=deadline_ns,
                )
            self.authorization_epoch += 1
            window = self.hooks.role_window(
                role, episode_id, policy_epoch, self.authorization_epoch, deadline_ns
            )
            observation = self.hooks.observe_runtime()
            deadline = aeri.Deadline(
                due_ns=deadline_ns,
                clock_domain=self.clock.domain,
                budget_ms=max(1, (deadline_ns - now) // 1_000_000),
                hardness="hard",
            )
            request = self.authority.begin_role_request(
                self.runtime,
                role,
                deadline,
                observation,
                window,
                episode_id=episode_id,
                policy_epoch=policy_epoch,
                now_ns=self.clock.now(),
            )
            with self._lock:
                self._request = request
            if self.failed or interrupted():
                raise InterventionStopped("stopped after role grant")
            # Borrow the exact original grant; this cycle alone begins/finishes.
            # A callback failure is proof failure, never a signature retry signal.
            result = call() if request_call is None else request_call(request)
            if self.failed or interrupted():
                raise InterventionStopped("stopped during request")
            if completion_adapter is None:
                proof = self.hooks.stop_proof(request, deadline_ns)
                observation = self.hooks.observe_runtime()
            else:
                evidence = completion_adapter(request, result)
                if (
                    not isinstance(evidence, CompletionEvidence)
                    or evidence.request is not request
                    or evidence.stop_proof.request is not request
                ):
                    raise InterventionUnproven(
                        "completion evidence must bind the exact original request"
                    )
                proof, observation, result = (
                    evidence.stop_proof,
                    evidence.observation,
                    evidence.result,
                )
            if self.failed or interrupted():
                raise InterventionStopped("stopped during completion proof")
            permitted = self.authority.finish_request(
                request, proof, observation, now_ns=self.clock.now()
            )
            if permitted is not True or self.clock.now() >= deadline_ns:
                raise InterventionUnproven("late/revoked completion cannot publish")
            if role == "judge":
                self.hold(
                    episode_id,
                    policy_epoch,
                    since_ns=now if hold_since_ns is None else hold_since_ns,
                    deadline_ns=deadline_ns,
                )
            if self.failed or interrupted():
                raise InterventionStopped("stopped before publication")
            with self._lock:
                self._request = None
            return result
