"""Opt-in resident-runtime ownership; no runtime or backend bridge is installed.

Trusted callers provide observations of the real process, resource lease and
backend request. HTTP completion/cancellation is never a backend-stop proof.
All physical releases still pass through WorkloadBroker's strict ExitProof.
"""

import threading
import uuid
from dataclasses import dataclass
from weakref import WeakKeyDictionary

from levi.domain import aeri
from levi.live.workload_broker import (
    ExitProof,
    WorkerIdentity,
    WorkloadBroker,
    _copy,
)

# One request authority per physical broker, even if a wrapper is forgotten.
_BINDINGS = WeakKeyDictionary()
_BINDINGS_LOCK = threading.RLock()


@dataclass(frozen=True)
class RuntimeObservation:
    """Actual runtime/lease probe, not HTTP status or available GPU memory."""

    lease_id: str
    lease_epoch: int
    worker: WorkerIdentity
    observed_ns: int
    clock_domain: str
    process_running: bool | None
    resource_lease_held: bool | None
    backend_idle: bool | None
    request_workers_exited: bool | None


@dataclass(frozen=True)
class RuntimeToken:
    owner_id: str
    runtime_epoch: int
    lease_id: str
    lease_epoch: int
    worker: WorkerIdentity


@dataclass(frozen=True)
class RequestToken:
    runtime: RuntimeToken
    request_epoch: int
    granted_ns: int
    expires_ns: int


@dataclass(frozen=True)
class RequestStopProof:
    """Trusted backend ACK plus exit of ALL request workers (including threads).

    A disconnected HTTP client or finished calling thread cannot establish either
    fact. This proof does not assert process exit or physical GPU release.
    """

    request: RequestToken
    observed_ns: int
    clock_domain: str
    backend_request_stopped: bool | None
    request_workers_exited: bool | None


class RuntimeOwnership:
    """One resident physical owner with one serial request execution slot.

    In-memory only. New instances know no owner; unknown or changed identities
    latch closed until verified physical exit. A bridge must serialize lifecycle
    changes with these calls and keep the real resource-driver/flock lease held.
    No method starts work, cancels a backend, probes a process or opens a gate.
    """

    def __init__(self, broker: WorkloadBroker):
        self.broker = broker
        self.owner_id = "runtime-owner:" + uuid.uuid4().hex
        self._lock = threading.RLock()
        self._runtime = None
        self._lease = None
        self._request = None
        self._known = False
        self._revoked = False
        self._runtime_epoch = self._request_epoch = self._last_ns = 0
        self._quiescent_ns = 0

    def _tick(self, now_ns):
        if type(now_ns) is not int or not self._last_ns <= now_ns <= aeri.INT64_MAX:
            raise ValueError("nonnegative advancing receiver clock required")
        self._last_ns = now_ns
        if self._request and now_ns >= self._request.expires_ns:
            self._revoked = True

    def _fresh(self, observed_ns, clock_domain, now_ns, *, since_ns):
        return (
            type(observed_ns) is int
            and clock_domain == self.broker.clock_domain
            and since_ns <= observed_ns <= now_ns
            and now_ns - observed_ns <= self.broker.config.pressure_ttl_ms * 1_000_000
        )

    def _observed(self, observation, lease, worker, now_ns, *, since_ns):
        return (
            isinstance(observation, RuntimeObservation)
            and observation.lease_id == lease.lease_id
            and type(observation.lease_epoch) is int
            and observation.lease_epoch == lease.lease_epoch
            and observation.worker == worker
            and self._fresh(
                observation.observed_ns,
                observation.clock_domain,
                now_ns,
                since_ns=since_ns,
            )
            and observation.process_running is True
            and observation.resource_lease_held is True
        )

    def bind_runtime(self, lease, worker: WorkerIdentity, observation, *, now_ns):
        """Bind one already-admitted resident owner; no adoption after restart."""
        lease = _copy(aeri.ResourceLease, lease)
        with self._lock:
            self._tick(now_ns)
            if (
                self._runtime is not None
                or not isinstance(worker, WorkerIdentity)
                or not self._observed(
                    observation, lease, worker, now_ns, since_ns=lease.granted_ns
                )
                or observation.backend_idle is not True
                or observation.request_workers_exited is not True
            ):
                raise ValueError("fresh exact quiescent resident owner required")
            # The original broker validates the exact physical lease/epoch.
            with _BINDINGS_LOCK:
                if self.broker in _BINDINGS:
                    raise ValueError("physical broker already has a request authority")
                self.broker.bind_worker(lease, worker, now_ns=now_ns)
                _BINDINGS[self.broker] = self.owner_id
            self._runtime_epoch += 1
            self._runtime = RuntimeToken(
                self.owner_id,
                self._runtime_epoch,
                lease.lease_id,
                lease.lease_epoch,
                worker,
            )
            self._lease = lease
            self._known = True
            self._quiescent_ns = observation.observed_ns
            return self._runtime

    def _runtime_token(self, runtime):
        if self._runtime is None or runtime != self._runtime:
            raise ValueError("stale or foreign runtime identity/epoch")

    def _healthy(self, observation, now_ns):
        if self._runtime is None or not self._known:
            return False
        state = self.broker.snapshot(now_ns=now_ns)
        if (
            state["active"] != self._lease.model_dump(by_alias=True)
            or state["worker"] != self._runtime.worker
            or not self._observed(
                observation,
                self._lease,
                self._runtime.worker,
                now_ns,
                since_ns=self._lease.granted_ns,
            )
        ):
            self._known = False
            self._revoked = True
            return False
        # Expiry/revocation closes execution, never erases physical ownership.
        return self.broker.accepts_result(self._lease, now_ns=now_ns)

    def begin_request(self, runtime, deadline, observation, *, now_ns):
        deadline = _copy(aeri.Deadline, deadline)
        with self._lock:
            self._tick(now_ns)
            self._runtime_token(runtime)
            if not self._healthy(observation, now_ns):
                raise ValueError("runtime identity or physical permission unproven")
            if self._request is not None:
                raise ValueError("backend request still owns execution slot")
            if (
                observation.backend_idle is not True
                or observation.request_workers_exited is not True
                or observation.observed_ns < self._quiescent_ns
                or deadline.clock_domain != self.broker.clock_domain
                or deadline.budget_ms <= 0
            ):
                raise ValueError(
                    "fresh quiescent backend and bounded deadline required"
                )
            end = min(
                deadline.due_ns,
                now_ns + deadline.budget_ms * 1_000_000,
                self._lease.expires_ns,
            )
            if end <= now_ns:
                raise ValueError("request deadline expired")
            self._request_epoch += 1
            self._request = RequestToken(runtime, self._request_epoch, now_ns, end)
            self._revoked = False
            return self._request

    def _request_token(self, request):
        if self._request is None or request != self._request:
            raise ValueError("stale or foreign request identity/epoch")

    def revoke_request(self, request, *, now_ns):
        """HTTP completion, cancel or transport failure withdraws results only."""
        with self._lock:
            self._tick(now_ns)
            self._request_token(request)
            self._revoked = True

    def accepts_result(self, request, observation, *, now_ns):
        with self._lock:
            self._tick(now_ns)
            try:
                self._request_token(request)
            except ValueError:
                return False
            return self._healthy(observation, now_ns) and not self._revoked

    def finish_request(self, request, proof, observation, *, now_ns):
        """Clear only the request slot after backend ACK and all workers exit.

        Return whether its result was still permitted at this instant. A late or
        cancelled completion may clear the slot but can never publish a result.
        """
        with self._lock:
            self._tick(now_ns)
            self._request_token(request)
            healthy = self._healthy(observation, now_ns)
            if (
                not isinstance(proof, RequestStopProof)
                or proof.request != request
                or not self._fresh(
                    proof.observed_ns,
                    proof.clock_domain,
                    now_ns,
                    since_ns=request.granted_ns,
                )
                or proof.backend_request_stopped is not True
                or proof.request_workers_exited is not True
            ):
                raise ValueError(
                    "exact fresh backend-stop and all-worker proof required"
                )
            permitted = healthy and not self._revoked
            self._request = None
            self._quiescent_ns = proof.observed_ns
            return permitted

    def release_after_exit(self, runtime, proof: ExitProof, *, now_ns):
        """Delegate physical handoff to the original broker, never fake GPU exit."""
        with self._lock:
            self._tick(now_ns)
            self._runtime_token(runtime)
            if self._request is not None:
                raise ValueError("request worker ownership must first be resolved")
            with _BINDINGS_LOCK:
                self.broker.release_after_exit(self._lease, proof, now_ns=now_ns)
                del _BINDINGS[self.broker]
            self._runtime = self._lease = None
            self._known = False

    def snapshot(self, *, now_ns):
        with self._lock:
            self._tick(now_ns)
            return {
                "owner_id": self.owner_id,
                "runtime": self._runtime,
                "identity_known": self._known,
                "request": self._request,
                "request_revoked": self._revoked if self._request else None,
            }
