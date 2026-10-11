"""Opt-in owned model execution using the original broker/role authority.

No hardware hold, paused-camera or role window is fabricated here. A caller
must supply independently authorized fresh windows and physical evidence to
its existing inference cycle. No default launch or external-server adoption.
"""

import threading
import time
from dataclasses import dataclass
from types import MappingProxyType

from levi.live.owned_runtime import SdkIdleAck
from levi.live.runtime_ownership import (
    GroupObservation,
    RequestStopProof,
    RequestToken,
    RuntimeObservation,
    RuntimeOwnership,
    RuntimeToken,
)


class OwnedRoleRuntime:
    """Software execution companion, not another scheduler.

    authority is the one RuntimeOwnership already bound to the physical
    broker. observer supplies actual owned group evidence. Tokens returned
    here can be used by the existing inference cycle; it must not create a
    second RuntimeOwnership or begin a second request for that same action.
    """

    def __init__(self, authority, runtime, lease, observer, *, clock=time.monotonic_ns):
        if (
            authority.broker is None
            or runtime.lease_id != lease.lease_id
            or runtime.lease_epoch != lease.lease_epoch
        ):
            raise ValueError("original broker admission and runtime token required")
        self.authority, self.runtime, self.lease = authority, runtime, lease
        self.observer, self.clock = observer, clock
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._active = None
        self._collected = None
        for worker in observer.workers.values():
            worker.bind_authority(authority, observer)

    def begin(self, role, deadline, window, payload, *, episode_id, policy_epoch):
        with self._lock:
            if self._closed.is_set() or self._active is not None:
                raise ValueError("owned role runtime closed or request unresolved")
            request = self.authority.begin_role_request(
                self.runtime,
                role,
                deadline,
                self.observer.observe(self.lease),
                window,
                episode_id=episode_id,
                policy_epoch=policy_epoch,
                now_ns=self.clock(),
            )
            # Record authority before dispatch: unknown send cannot clear it.
            self._active = request
            worker = self.observer.workers[role]
            try:
                worker.submit(request, payload)
            except Exception:
                self.authority.revoke_request(request, now_ns=self.clock())
                raise
            return request

    def collect(self, request, *, timeout_s=1.0):
        with self._lock:
            if request != self._active:
                raise ValueError("stale or foreign owned role request")
            try:
                if self._collected is None:
                    result = self.observer.workers[request.role].collect(
                        timeout_s=timeout_s
                    )
                    self._collected = (request, result)
                result = self._collected[1]
                observation = self.observer.observe(self.lease)
            except TimeoutError:
                raise  # a bounded poll is not an affirmative idle/result proof
            except Exception:
                self.authority.revoke_request(request, now_ns=self.clock())
                raise  # unknown ACK/probe retains the original slot and withdraws result
            if self._closed.is_set():
                self.authority.revoke_request(request, now_ns=self.clock())
            permitted = self.authority.finish_request(
                request,
                RequestStopProof(
                    request,
                    observation.observed_ns,
                    observation.clock_domain,
                    True,
                    True,
                ),
                observation,
                now_ns=self.clock(),
            )
            self._active = None
            self._collected = None
            return result if permitted and not self._closed.is_set() else None

    def renew(self, deadline, pressure, window):
        with self._lock:
            if self._closed.is_set() or self._active is not None:
                raise ValueError("closed or active owned role runtime cannot renew")
            self.runtime = self.authority.renew_resident(
                self.runtime,
                deadline,
                pressure,
                window,
                self.observer.observe(self.lease),
                now_ns=self.clock(),
            )
            self.lease = self.authority._lease
            return self.runtime

    def revoke(self, request):
        # The original authority validates the exact token and retains its
        # slot. No model/collection mutex is taken; stale tokens cannot close
        # a newer worker. A cancelled, proven-idle request can later drain.
        return self.authority.revoke_request(request, now_ns=self.clock())

    def close(self):
        # Supervisor stop withdraws all ingress without waiting for model work.
        self._closed.set()
        for worker in self.observer.workers.values():
            worker.close_ingress()
        request = self._active
        if request is not None:
            try:
                self.authority.revoke_request(request, now_ns=self.clock())
            except ValueError:
                pass  # a concurrently finished token cannot revoke a newer one


@dataclass(frozen=True)
class BorrowedCompletion:
    request: RequestToken
    sdk_ack: SdkIdleAck
    stop_proof: RequestStopProof
    observation: RuntimeObservation
    result: object


class BorrowedRoleExecution:
    """Execute Cycle's exact grant; never begin, finish or renew authority."""

    def __init__(self, authority, runtime, lease, observer, *, clock=time.monotonic_ns):
        from levi.live.owned_runtime import OwnedGroupObserver, OwnedModelWorker

        if (
            type(authority) is not RuntimeOwnership
            or any(
                not callable(getattr(authority, name, None))
                for name in ("owns_exact_request", "accepts_exact_request")
            )
            or type(runtime) is not RuntimeToken
            or any(
                type(value) is not int
                for value in (
                    runtime.runtime_epoch,
                    runtime.lease_epoch,
                    runtime.worker.pid,
                    runtime.worker.start_ticks,
                )
            )
            or type(observer) is not OwnedGroupObserver
            or runtime.lease_id != lease.lease_id
            or runtime.lease_epoch != lease.lease_epoch
            or runtime.worker != observer.group.supervisor
        ):
            raise ValueError("exact original owned runtime/lease/observer required")
        self._authority, self._runtime, self._observer = authority, runtime, observer
        self._broker = authority.broker
        self._lease = lease.model_copy(deep=True)
        self._lease_fields = self._lease.model_dump(by_alias=True)
        self._group = observer.group
        self._group_scope, self._driver_observer = (
            observer.group_scope,
            observer.driver_observer,
        )
        self._workers = MappingProxyType(dict(observer.workers))
        self._bindings = {}
        for member in self._group.members:
            worker = self._workers.get(member.role)
            if type(worker) is not OwnedModelWorker:
                raise ValueError("exact original owned worker required")
            worker.bind_authority(authority, observer)
            self._bindings[member.role] = (
                worker.worker,
                worker.role,
                worker.factory,
                worker.launch_id,
                worker._connection,
                worker.driver_observer,
                worker.group_scope,
                worker.factory.capability_id,
                getattr(worker, "scope", None),
                worker.submit.__func__,
                worker.collect_sdk_completion.__func__,
            )
            if (
                worker.worker != member.worker
                or worker.factory.capability_id != member.capability_id
            ):
                raise ValueError("original owned worker capability/identity required")
        self._clock = clock
        self._lock, self._closed = threading.Lock(), threading.Event()
        self._active = self._collected = None
        self._withdrawn = False
        self._driver_key = None
        observation = self._observe(idle=False)
        group = observation.group
        self._driver_key = (
            group.driver_lease_id,
            group.driver_generation,
            group.driver_session,
        )

    @property
    def authority(self):
        return self._authority

    @property
    def runtime(self):
        return self._runtime

    @property
    def lease(self):
        return self._lease.model_copy(deep=True)

    @property
    def observer(self):
        return self._observer

    def _binding(self):
        if (
            self._authority.broker is not self._broker
            or self._observer.group is not self._group
            or self._observer.group_scope is not self._group_scope
            or self._observer.driver_observer is not self._driver_observer
            or set(self._observer.workers) != set(self._workers)
            or self._lease.model_dump(by_alias=True) != self._lease_fields
        ):
            raise ValueError("borrowed original binding changed")
        # Snapshot verifies only resident binding, never supplies a grant/token.
        state = self._authority.snapshot(now_ns=self._clock())
        if (
            state["runtime"] != self._runtime
            or state["lease"] is None
            or state["lease"].model_dump(by_alias=True) != self._lease_fields
            or state["group"] != self._group
        ):
            raise ValueError("borrowed original runtime/lease epoch changed")
        for role, worker in self._workers.items():
            (
                identity,
                worker_role,
                factory,
                launch,
                connection,
                driver,
                scope,
                capability,
                role_scope,
                submit,
                collect,
            ) = self._bindings[role]
            if (
                self._observer.workers.get(role) is not worker
                or worker.worker != identity
                or worker.role != worker_role
                or worker.factory is not factory
                or worker.launch_id != launch
                or worker._connection is not connection
                or worker.factory.capability_id != capability
                or getattr(worker, "scope", None) is not role_scope
                or getattr(worker.submit, "__func__", None) is not submit
                or getattr(worker.collect_sdk_completion, "__func__", None)
                is not collect
                or worker.driver_observer is not driver
                or worker.group_scope is not scope
                or self._group_scope._launches.get(role) is not worker
                or worker._authority is not self._authority
                or worker._observer is not self._observer
            ):
                raise ValueError(
                    "borrowed owned worker/launch/capability binding changed"
                )

    def _observe(self, *, idle, request=None):
        self._binding()
        observation = self._observer.observe(self._lease)
        self._binding()
        now = self._clock()
        group = observation.group if type(observation) is RuntimeObservation else None
        if (
            type(observation) is not RuntimeObservation
            or observation.lease_id != self._runtime.lease_id
            or observation.lease_epoch != self._runtime.lease_epoch
            or observation.worker != self._runtime.worker
            or observation.clock_domain != self._broker.clock_domain
            or type(observation.observed_ns) is not int
            or not (request.granted_ns if request else self._lease.granted_ns)
            <= observation.observed_ns
            <= now
            or now - observation.observed_ns
            > self._broker.config.pressure_ttl_ms * 1_000_000
            or observation.process_running is not True
            or observation.resource_lease_held is not True
            or type(group) is not GroupObservation
            or group.group != self._group
            or not isinstance(group.members, tuple)
            or tuple(member.member for member in group.members) != self._group.members
            or group.driver_held is not True
            or group.flock_held is not True
            or group.membership_complete is not True
            or type(group.driver_generation) is not int
            or group.driver_generation < 1
            or type(group.driver_lease_id) is not str
            or not group.driver_lease_id
            or type(group.driver_session) is not str
            or not group.driver_session
            or self._driver_key is not None
            and (group.driver_lease_id, group.driver_generation, group.driver_session)
            != self._driver_key
            or any(
                member.process_running is not True
                or member.capability_owned is not True
                for member in group.members
            )
            or idle
            and (
                observation.backend_idle is not True
                or observation.request_workers_exited is not True
                or any(
                    member.backend_idle is not True
                    or member.request_workers_exited is not True
                    for member in group.members
                )
            )
        ):
            raise ValueError("fresh exact complete owned group observation required")
        return observation

    def _withdraw(self, request):
        self._withdrawn = True
        try:
            if self._authority.owns_exact_request(request) is True:
                self._authority.revoke_request(request, now_ns=self._clock())
        except ValueError:
            pass  # Cycle already resolved it; never revoke a different token.

    def dispatch_existing(self, request, payload, *, role, episode_id, policy_epoch):
        with self._lock:
            if (
                type(request) is not RequestToken
                or type(request.runtime) is not RuntimeToken
                or request.runtime != self._runtime
                or any(
                    type(value) is not int
                    for value in (
                        request.runtime.runtime_epoch,
                        request.runtime.lease_epoch,
                        request.runtime.worker.pid,
                        request.runtime.worker.start_ticks,
                    )
                )
                or any(
                    type(value) is not int
                    for value in (
                        request.request_epoch,
                        request.granted_ns,
                        request.expires_ns,
                        request.policy_epoch,
                    )
                )
                or request.role != role
                or request.episode_id != episode_id
                or type(policy_epoch) is not int
                or request.policy_epoch != policy_epoch
                or role not in self._workers
                or self._closed.is_set()
                or self._active is not None
                and request.request_epoch <= self._active.request_epoch
            ):
                raise ValueError(
                    "exact new borrowed role/episode/epoch request required"
                )
            # Reject equal copies before observation/dispatch/revoke side effects.
            if self._authority.owns_exact_request(request) is not True:
                raise ValueError("original active borrowed request object required")
            if self._active is not None and self._collected is None:
                raise ValueError("borrowed request unresolved")
            try:
                observation = self._observe(idle=True, request=request)
                if (
                    self._authority.accepts_exact_request(
                        request, observation, now_ns=self._clock()
                    )
                    is not True
                ):
                    raise ValueError(
                        "original authority did not accept borrowed request"
                    )
            except Exception:
                try:
                    if self._authority.owns_exact_request(request) is True:
                        self._authority.revoke_request(request, now_ns=self._clock())
                except ValueError:
                    pass
                raise
            self._active, self._collected, self._withdrawn = request, None, False
            try:
                self._workers[role].submit(request, payload)
            except Exception:
                self._withdraw(request)
                raise
            return request

    def collect_existing(self, request, *, timeout_s=1.0):
        from levi.live.owned_runtime import SdkCompletion, SdkIdleAck, _cpu_payload

        with self._lock:
            if (
                request is not self._active
                or self._authority.owns_exact_request(request) is not True
            ):
                raise ValueError("exact active borrowed request required")
            try:
                self._binding()
                if self._collected is None:
                    completion = self._workers[request.role].collect_sdk_completion(
                        request, timeout_s=timeout_s
                    )
                    worker = self._workers[request.role]
                    if (
                        type(completion) is not SdkCompletion
                        or completion.request is not request
                        or type(completion.sdk_ack) is not SdkIdleAck
                        or completion.sdk_ack
                        != SdkIdleAck(
                            worker.worker,
                            worker.launch_id,
                            request.request_epoch,
                            worker.factory.capability_id,
                        )
                    ):
                        raise ValueError("exact submitted SDK completion required")
                    _cpu_payload(completion.result)
                    self._collected = completion
                observation = self._observe(idle=True, request=request)
                permitted = self._authority.accepts_exact_request(
                    request, observation, now_ns=self._clock()
                )
                if permitted is not True or self._closed.is_set():
                    self._withdraw(request)
                return BorrowedCompletion(
                    request,
                    self._collected.sdk_ack,
                    RequestStopProof(
                        request,
                        observation.observed_ns,
                        observation.clock_domain,
                        True,
                        True,
                    ),
                    observation,
                    self._collected.result
                    if permitted and not self._withdrawn and not self._closed.is_set()
                    else None,
                )
            except TimeoutError:
                raise
            except Exception:
                self._withdraw(request)
                raise

    def revoke(self, request):
        if (
            request is not self._active
            or self._authority.owns_exact_request(request) is not True
        ):
            raise ValueError("exact active borrowed request required")
        self._withdraw(request)

    def close(self):
        self._closed.set()
        for worker in self._workers.values():
            worker.close_ingress()
        request = self._active
        if request is not None:
            self._withdraw(request)
