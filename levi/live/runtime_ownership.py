"""Opt-in resident-runtime ownership; no runtime or backend bridge is installed.

Trusted callers provide observations of the real process, resource lease and
backend request. HTTP completion/cancellation is never a backend-stop proof.
All physical releases still pass through WorkloadBroker's strict ExitProof.
"""

import threading
import uuid
from dataclasses import dataclass
from weakref import WeakKeyDictionary

from pydantic import TypeAdapter

from levi.domain import aeri
from levi.live.workload_broker import (
    ExitProof,
    SafeWindow,
    WorkerIdentity,
    WorkloadBroker,
    _copy,
)

# One request authority per physical broker, even if a wrapper is forgotten.
_BINDINGS = WeakKeyDictionary()
_BINDINGS_LOCK = threading.RLock()


@dataclass(frozen=True)
class OwnedMember:
    role: str
    worker: WorkerIdentity
    capability_id: str


@dataclass(frozen=True)
class OwnedGroup:
    """Supervisor-created complete membership; no external-server adoption."""

    group_id: str
    supervisor: WorkerIdentity
    members: tuple[OwnedMember, ...]

    def __post_init__(self):
        declaration = aeri.ResidentGroupSpec(
            group_id=self.group_id,
            members=[
                {"role": m.role, "capability_id": m.capability_id} for m in self.members
            ],
        )
        if (
            not isinstance(self.members, tuple)
            or not isinstance(self.supervisor, WorkerIdentity)
            or any(not isinstance(m.worker, WorkerIdentity) for m in self.members)
            or len({m.worker for m in self.members}) != len(declaration.members)
            or self.supervisor in {m.worker for m in self.members}
        ):
            raise ValueError("complete distinct owned member identities required")


@dataclass(frozen=True)
class MemberObservation:
    member: OwnedMember
    process_running: bool | None
    capability_owned: bool | None
    backend_idle: bool | None
    request_workers_exited: bool | None


@dataclass(frozen=True)
class GroupObservation:
    group: OwnedGroup
    members: tuple[MemberObservation, ...]
    driver_lease_id: str
    driver_generation: int
    driver_session: str
    driver_held: bool | None
    flock_held: bool | None
    membership_complete: bool | None


@dataclass(frozen=True)
class RoleWindow(SafeWindow):
    role: str
    policy_epoch: int
    authorization_epoch: int


@dataclass(frozen=True)
class MemberExitProof:
    member: OwnedMember
    process_exited: bool | None
    descendants_exited: bool | None
    owned_gpu_released: bool | None


@dataclass(frozen=True)
class GroupExitProof(ExitProof):
    group: OwnedGroup
    members: tuple[MemberExitProof, ...]
    membership_complete: bool | None


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
    group: GroupObservation | None = None


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
    role: str | None = None
    episode_id: str | None = None
    policy_epoch: int | None = None


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
        self._group = None
        self._driver = None
        self._policy_epoch = -1
        self._authorization_epoch = -1
        self._episode_id = None

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

    def _group_observed(self, observation, *, idle=False):
        if self._group is None:
            return observation.group is None
        group = observation.group
        if (
            not isinstance(group, GroupObservation)
            or group.group != self._group
            or not isinstance(group.members, tuple)
            or tuple(m.member for m in group.members) != self._group.members
            or group.membership_complete is not True
            or group.driver_held is not True
            or group.flock_held is not True
            or type(group.driver_generation) is not int
            or group.driver_generation < 1
            or not isinstance(group.driver_lease_id, str)
            or not group.driver_lease_id
            or not isinstance(group.driver_session, str)
            or not group.driver_session
        ):
            return False
        driver = (group.driver_lease_id, group.driver_generation, group.driver_session)
        if self._driver is not None and driver != self._driver:
            return False
        return all(
            m.process_running is True
            and m.capability_owned is True
            and (
                not idle
                or (m.backend_idle is True and m.request_workers_exited is True)
            )
            for m in group.members
        )

    def bind_group(self, lease, group, observation, *, now_ns):
        """Bind supervisor-owned declared capabilities after every backend barrier."""
        with self._lock, _BINDINGS_LOCK, self.broker._lock:
            if self._runtime is not None or not isinstance(group, OwnedGroup):
                raise ValueError("new exact owned group required")
            active = self.broker._token(lease)
            declaration = aeri.ResidentGroupSpec(
                group_id=group.group_id,
                members=[
                    {"role": m.role, "capability_id": m.capability_id}
                    for m in group.members
                ],
            )
            if active.request.spec.resident_group != declaration:
                raise ValueError("original broker resident declaration differs")
            self._group = group
            try:
                if not isinstance(
                    observation, RuntimeObservation
                ) or not self._group_observed(observation, idle=True):
                    raise ValueError("fresh complete owned group quiescence required")
                active.owned_group = group
                try:
                    runtime = self.bind_runtime(
                        lease, group.supervisor, observation, now_ns=now_ns
                    )
                except Exception:
                    active.owned_group = None
                    raise
                g = observation.group
                self._driver = (
                    g.driver_lease_id,
                    g.driver_generation,
                    g.driver_session,
                )
                return runtime
            except Exception:
                self._group = None
                raise

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
            and self._group_observed(observation)
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
            # Lock order is owner -> bindings -> original physical broker.
            with _BINDINGS_LOCK, self.broker._lock:
                if (
                    self.broker._token(lease).request.spec.resident_group is not None
                    and self._group is None
                ):
                    raise ValueError("resident declaration requires bind_group")
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

    def begin_request(
        self,
        runtime,
        deadline,
        observation,
        *,
        now_ns,
        role=None,
        episode_id=None,
        policy_epoch=None,
        window=None,
    ):
        deadline = _copy(aeri.Deadline, deadline)
        with self._lock:
            self._tick(now_ns)
            self._runtime_token(runtime)
            if not self._healthy(observation, now_ns):
                raise ValueError("runtime identity or physical permission unproven")
            if self._request is not None:
                raise ValueError("backend request still owns execution slot")
            if self._group is not None:
                if (
                    role not in {m.role for m in self._group.members}
                    or type(policy_epoch) is not int
                    or not max(0, self._policy_epoch) <= policy_epoch <= aeri.INT64_MAX
                    or not isinstance(window, RoleWindow)
                    or window.role != role
                    or type(window.policy_epoch) is not int
                    or window.policy_epoch != policy_epoch
                    or type(window.authorization_epoch) is not int
                    or not self._authorization_epoch
                    < window.authorization_epoch
                    <= aeri.INT64_MAX
                    or window.episode_id != episode_id
                    or episode_id is None
                    or window.broker_id != self.broker.broker_id
                    or window.run_id != self.broker.run_id
                    or window.profile_sha256 != self.broker.profile_sha256
                    or not self._fresh(
                        window.observed_ns,
                        window.clock_domain,
                        now_ns,
                        since_ns=self._quiescent_ns,
                    )
                    or type(window.expires_ns) is not int
                    or not now_ns < window.expires_ns <= aeri.INT64_MAX
                    or not self._group_observed(observation, idle=True)
                ):
                    raise ValueError(
                        "fresh current-episode/epoch role window and all-member idle proof required"
                    )
                # Epochs never move backward or migrate an old authorization to another episode.
                if (
                    self._episode_id != episode_id
                    and self._episode_id is not None
                    and policy_epoch <= self._policy_epoch
                ):
                    raise ValueError("new episode requires a new policy epoch")
                TypeAdapter(aeri.EpisodeId).validate_python(episode_id)
                if aeri.episode_parts(episode_id)[0] != self.broker.run_id:
                    raise ValueError("role episode belongs to another run")
            elif any(x is not None for x in (role, episode_id, policy_epoch, window)):
                raise ValueError("role authority requires an owned group")
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
                window.expires_ns if window is not None else self._lease.expires_ns,
            )
            if end <= now_ns:
                raise ValueError("request deadline expired")
            self._request_epoch += 1
            self._request = RequestToken(
                runtime,
                self._request_epoch,
                now_ns,
                end,
                role,
                episode_id,
                policy_epoch,
            )
            if self._group is not None:
                self._episode_id, self._policy_epoch = episode_id, policy_epoch
                self._authorization_epoch = window.authorization_epoch
            self._revoked = False
            return self._request

    def begin_role_request(
        self,
        runtime,
        role,
        deadline,
        observation,
        window,
        *,
        episode_id,
        policy_epoch,
        now_ns,
    ):
        return self.begin_request(
            runtime,
            deadline,
            observation,
            now_ns=now_ns,
            role=role,
            episode_id=episode_id,
            policy_epoch=policy_epoch,
            window=window,
        )

    def renew_resident(
        self, runtime, deadline, pressure, window, observation, *, now_ns
    ):
        """Explicit bounded reauthorization, never a heartbeat or owner resurrection."""
        with self._lock, self.broker._lock:
            self._tick(now_ns)
            self._runtime_token(runtime)
            if (
                self._group is None
                or self._request is not None
                or not self._known
                or not self._healthy(observation, now_ns)
                or not self._group_observed(observation, idle=True)
                or observation.observed_ns < self._quiescent_ns
            ):
                raise ValueError("valid exact quiescent owned group required for renew")
            lease, request = self.broker._prepare_resident_renew(
                self._lease, deadline, pressure, window, now_ns=now_ns
            )
            token = RuntimeToken(
                self.owner_id,
                self._runtime_epoch + 1,
                lease.lease_id,
                lease.lease_epoch,
                runtime.worker,
            )
            # All validation/construction completed before either side is changed.
            self.broker._active.lease = lease
            self.broker._active.request = request
            self.broker._epoch = lease.lease_epoch
            self._lease, self._runtime = lease, token
            self._runtime_epoch = token.runtime_epoch
            self._quiescent_ns = observation.observed_ns
            return token

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
                or self._group is not None
                and (
                    not self._observed(
                        observation,
                        self._lease,
                        self._runtime.worker,
                        now_ns,
                        since_ns=request.granted_ns,
                    )
                    or not self._group_observed(observation, idle=True)
                    or observation.observed_ns < proof.observed_ns
                )
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
            if self._request is not None and self._group is None:
                raise ValueError("request worker ownership must first be resolved")
            if self._group is not None and (
                not isinstance(proof, GroupExitProof)
                or proof.group != self._group
                or proof.membership_complete is not True
                or not isinstance(proof.members, tuple)
                or tuple(m.member for m in proof.members) != self._group.members
                or any(
                    flag is not True
                    for m in proof.members
                    for flag in (
                        m.process_exited,
                        m.descendants_exited,
                        m.owned_gpu_released,
                    )
                )
            ):
                raise ValueError(
                    "complete exact member/descendant/GPU group exit proof required"
                )
            with _BINDINGS_LOCK:
                self.broker.release_after_exit(self._lease, proof, now_ns=now_ns)
                del _BINDINGS[self.broker]
            # Complete physical group exit also resolves a stranded request; it
            # never permits that request's result to publish.
            self._request = None
            self._revoked = True
            self._runtime = self._lease = self._group = self._driver = None
            self._known = False

    def snapshot(self, *, now_ns):
        with self._lock:
            self._tick(now_ns)
            return {
                "owner_id": self.owner_id,
                "runtime": self._runtime,
                "identity_known": self._known,
                "group": self._group,
                "lease": self._lease,
                "request": self._request,
                "request_revoked": self._revoked if self._request else None,
            }
