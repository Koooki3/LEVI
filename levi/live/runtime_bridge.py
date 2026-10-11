"""Explicit bounded runtime bridge: no observer, GPU or backend is installed.

Trusted composition supplies actual driver/flock/incarnation observations.
Client completion is not backend completion. Physical leases are never renewed.
"""

import re
import threading
import time
from dataclasses import dataclass

from levi.domain import aeri
from levi.live.runtime_ownership import (
    RequestStopProof,
    RuntimeObservation,
    RuntimeOwnership,
)
from levi.live.workload_broker import ExitProof, SafeWindow, WorkerIdentity


@dataclass(frozen=True)
class DriverIdentity:
    lease_id: str
    generation: int
    session: str
    owner: WorkerIdentity

    def __post_init__(self):
        if (
            not isinstance(self.lease_id, str)
            or not self.lease_id
            or not isinstance(self.session, str)
            or not self.session
            or type(self.generation) is not int
            or self.generation <= 0
            or not isinstance(self.owner, WorkerIdentity)
        ):
            raise ValueError(
                "exact driver token, generation, session and owner required"
            )


@dataclass(frozen=True)
class DriverObservation:
    driver: DriverIdentity
    observed_ns: int
    clock_domain: str
    driver_active: bool | None
    driver_cancelled: bool | None
    gpu_resource_held: bool | None
    exact_flock_held: bool | None


@dataclass(frozen=True)
class BackendCapability:
    """Frozen, independently verified implementation and exclusive ingress."""

    version: str
    code_sha256: str
    exclusive: bool
    single_engine: bool
    pause_synchronizes_device: bool

    def __post_init__(self):
        if (
            not self.version
            or not re.fullmatch(r"[0-9a-f]{64}", self.code_sha256)
            or any(
                value is not True
                for value in (
                    self.exclusive,
                    self.single_engine,
                    self.pause_synchronizes_device,
                )
            )
        ):
            raise ValueError(
                "frozen exclusive backend idle-barrier capability required"
            )


@dataclass(frozen=True)
class AuthorityObservation:
    driver: DriverIdentity
    worker: WorkerIdentity
    observed_ns: int
    clock_domain: str
    driver_active: bool | None
    driver_cancelled: bool | None
    gpu_resource_held: bool | None
    exact_flock_held: bool | None
    process_running: bool | None
    capability: BackendCapability
    endpoint_owner: WorkerIdentity


@dataclass(frozen=True)
class BackendAck:
    worker: WorkerIdentity
    request_epoch: int
    capability: BackendCapability
    mode: str


class BridgeRequest:
    def __init__(self, token, episode_id=None):
        self.token = token
        self.episode_id = episode_id
        self.cancelled = threading.Event()
        self.done = threading.Event()
        self.permitted = False
        self.drainer = None
        self.workers = ()
        self.callback = None


class RuntimeBridge:
    """Compose the original broker and one serial runtime/request authority.

    observer.observe(lease, worker) returns AuthorityObservation, inspecting the
    actual driver, kernel flock and incarnation. observer.window(episode, now)
    supplies an authorized SafeWindow. observer.exit_proof(lease, worker) returns
    strict ExitProof. No lab import, environment claim or free VRAM substitutes
    for those dependencies. A trusted launcher admits before launching and calls
    bind_runtime after recording its actual leader; legacy launch/adopt is closed.
    """

    def __init__(
        self,
        broker,
        *,
        observer,
        backend,
        driver,
        capability,
        clock=time.monotonic_ns,
        barrier_timeout_s=1.0,
    ):
        if (
            not isinstance(driver, DriverIdentity)
            or not driver.lease_id
            or (
                type(driver.generation) is not int
                or driver.generation <= 0
                or not driver.session
                or not isinstance(driver.owner, WorkerIdentity)
            )
        ):
            raise ValueError("exact resource-driver identity required")
        if not isinstance(capability, BackendCapability):
            raise TypeError("frozen backend capability required")
        if not 0 < barrier_timeout_s <= 30:
            raise ValueError("bounded backend barrier required")
        self.broker = broker
        self.ownership = RuntimeOwnership(broker)
        self.observer, self.backend = observer, backend
        self.driver, self.capability = driver, capability
        self.clock, self.barrier_timeout_s = clock, barrier_timeout_s
        self._mutex = threading.RLock()
        self._closed = threading.Event()
        self._lease = self._runtime = self._request = None
        self._worker = None
        self._idle_ns = None

    def _driver_held(self, now):
        observed = self.observer.driver(now)
        now = self.clock()
        if (
            not isinstance(observed, DriverObservation)
            or observed.driver != self.driver
            or observed.clock_domain != self.broker.clock_domain
            or type(observed.observed_ns) is not int
            or not 0 <= observed.observed_ns <= now
            or now - observed.observed_ns
            > self.broker.config.pressure_ttl_ms * 1_000_000
            or observed.driver_cancelled is not False
            or any(
                value is not True
                for value in (
                    observed.driver_active,
                    observed.gpu_resource_held,
                    observed.exact_flock_held,
                )
            )
        ):
            self.close()
            raise ValueError("actual active driver and kernel flock required")

    def admit_runtime(self, spec, deadline, pressure, window, idle_proof):
        """Trusted launcher reserves through the original physical broker."""
        with self._mutex:
            if self._lease is not None or self._closed.is_set():
                raise ValueError("existing physical owner cannot be replaced")
            now = self.clock()
            self._driver_held(now)
            now = self.clock()
            self.broker.prove_idle(idle_proof, now_ns=now)
            rejected = self.broker.submit(
                spec,
                deadline,
                profile_sha256=self.broker.profile_sha256,
                now_ns=now,
            )
            if rejected is not None:
                return rejected
            admitted = self.broker.admit(pressure, window, now_ns=now)
            if isinstance(admitted, aeri.ResourceLease):
                self._lease = admitted
            return admitted

    def _observe(self, worker, *, idle=False, workers_exited=False):
        observed = self.observer.observe(self._lease, worker)
        now = self.clock()
        if (
            not isinstance(observed, AuthorityObservation)
            or observed.driver != self.driver
            or observed.worker != worker
            or observed.endpoint_owner != worker
            or observed.capability != self.capability
            or observed.clock_domain != self.broker.clock_domain
            or type(observed.observed_ns) is not int
            or not self._lease.granted_ns <= observed.observed_ns <= now
            or now - observed.observed_ns
            > self.broker.config.pressure_ttl_ms * 1_000_000
            or observed.driver_cancelled is not False
            or any(
                value is not True
                for value in (
                    observed.driver_active,
                    observed.gpu_resource_held,
                    observed.exact_flock_held,
                    observed.process_running,
                )
            )
        ):
            self.close()
            raise ValueError("driver/flock/backend incarnation unknown or changed")
        return RuntimeObservation(
            self._lease.lease_id,
            self._lease.lease_epoch,
            worker,
            observed.observed_ns,
            self.broker.clock_domain,
            True,
            True,
            idle,
            workers_exited,
        )

    def _barrier(self, worker, epoch, mode):
        self._observe(worker)
        ack = self.backend.pause(worker, epoch, mode, self.barrier_timeout_s)
        if (
            not isinstance(ack, BackendAck)
            or type(ack.request_epoch) is not int
            or ack != BackendAck(worker, epoch, self.capability, mode)
        ):
            raise ValueError("exact current-backend idle barrier ACK required")
        observation = self._observe(worker, idle=True, workers_exited=True)
        self._idle_ns = observation.observed_ns
        return observation

    def bind_runtime(self, worker):
        """An explicit owned launch, never an external/restart adoption."""
        with self._mutex:
            if self._lease is None or self._runtime is not None:
                raise ValueError("original physical admission required")
            if self._worker is not None and worker != self._worker:
                raise ValueError("original physical worker cannot be replaced")
            self.broker.bind_worker(self._lease, worker, now_ns=self.clock())
            self._worker = worker
            observation = self._barrier(worker, 0, "wait")
            self._runtime = self.ownership.bind_runtime(
                self._lease,
                worker,
                observation,
                now_ns=self.clock(),
            )
            return self._runtime

    def available(self):
        with self._mutex:
            if self._closed.is_set() or self._runtime is None:
                return False
            try:
                self._observe(self._runtime.worker)
                return self.broker.accepts_result(self._lease, now_ns=self.clock())
            except Exception:  # noqa: BLE001 - unknown dependency remains closed
                self.close()
                return False

    def close(self):
        """P0-safe revocation: no mutex, observer, transport or drainer wait."""
        self._closed.set()
        request = self._request
        if request is not None:
            request.cancelled.set()

    def cancel(self, request):
        request.cancelled.set()

    def matches_record(self, record):
        """Check the Controller's owned launch record before any model action."""
        if self._worker is None or not isinstance(record, dict):
            return False
        worker = self._worker
        identity = record.get("identity")
        return (
            record.get("pid") == worker.pid
            and isinstance(identity, dict)
            and str(identity.get("start_ticks")) == str(worker.start_ticks)
            and identity.get("boot") == worker.boot_id
        )

    def _check_record(self, vllm):
        try:
            return self.matches_record(vllm._record())
        except Exception:  # noqa: BLE001 - unknown dependency remains closed
            return False

    def begin_request(self, *, due_ns, episode_id=None, profile_sha256=None):
        with self._mutex:
            if not self.available() or self._request is not None:
                raise ValueError("strict runtime unavailable or backend slot busy")
            now = self.clock()
            if (
                profile_sha256 is not None
                and profile_sha256 != self.broker.profile_sha256
            ):
                raise ValueError("frozen request profile differs")
            window = self.observer.window(episode_id, now)
            if (
                not isinstance(window, SafeWindow)
                or window.broker_id != self.broker.broker_id
                or window.run_id != self.broker.run_id
                or window.episode_id != episode_id
                or window.profile_sha256 != self.broker.profile_sha256
                or window.clock_domain != self.broker.clock_domain
                or not 0 <= window.observed_ns <= now < window.expires_ns
                or now - window.observed_ns
                > self.broker.config.pressure_ttl_ms * 1_000_000
            ):
                raise ValueError("fresh authorized request safety window required")
            due_ns = min(due_ns, window.expires_ns)
            observation = self._observe(
                self._runtime.worker,
                idle=self._idle_ns is not None,
                workers_exited=True,
            )
            now = self.clock()
            deadline = aeri.Deadline(
                due_ns=due_ns,
                clock_domain=self.broker.clock_domain,
                budget_ms=max(1, (due_ns - now) // 1_000_000),
                hardness="hard",
            )
            token = self.ownership.begin_request(
                self._runtime,
                deadline,
                observation,
                now_ns=now,
            )
            request = BridgeRequest(token, episode_id)
            self._request = request
            self._idle_ns = None
            try:
                self.backend.resume(
                    self._runtime.worker, token.request_epoch, self.barrier_timeout_s
                )
                self._observe(self._runtime.worker)
                if self._closed.is_set() or self.clock() >= token.expires_ns:
                    raise ValueError("request revoked during resume")
            except Exception:  # noqa: BLE001 - unknown dependency remains closed
                request.cancelled.set()
                # Resume may have reached the backend: retain the execution slot.
                # Return the cancelled token so the caller retains its Judge too.
            return request

    def check(self, request):
        with self._mutex:
            if self._closed.is_set() or request.cancelled.is_set():
                return False
            try:
                return self.ownership.accepts_result(
                    request.token,
                    self._observe(request.token.runtime.worker),
                    now_ns=self.clock(),
                )
            except Exception:  # noqa: BLE001 - unknown dependency remains closed
                request.cancelled.set()
                return False

    def drain(self, request, workers, callback):
        """Join all real local workers, then collect the backend barrier off caller.

        Timeout/unknown ACK retains ownership. retry_drain may collect a later
        valid ACK for this epoch; it never revives the cancelled result.
        """
        with self._mutex:
            if request is not self._request or request.drainer is not None:
                return
            request.workers, request.callback = tuple(workers), callback
            thread = threading.Thread(
                target=self._drain,
                args=(request,),
                daemon=True,
                name="live-runtime-drain",
            )
            request.drainer = thread
            thread.start()

    def _drain(self, request):
        try:
            for worker in request.workers:
                worker.join()
            with self._mutex:
                if request is not self._request:
                    return
                if request.cancelled.is_set() or self._closed.is_set():
                    self.ownership.revoke_request(request.token, now_ns=self.clock())
                observation = self._barrier(
                    request.token.runtime.worker,
                    request.token.request_epoch,
                    "abort" if request.cancelled.is_set() else "wait",
                )
                # Backend idle does not vouch for a safety window that changed
                # during the barrier. Resolve the slot, but withdraw its result.
                now = self.clock()
                try:
                    window = self.observer.window(request.episode_id, now)
                    safe = (
                        isinstance(window, SafeWindow)
                        and window.broker_id == self.broker.broker_id
                        and window.run_id == self.broker.run_id
                        and window.episode_id == request.episode_id
                        and window.profile_sha256 == self.broker.profile_sha256
                        and window.clock_domain == self.broker.clock_domain
                        and 0 <= window.observed_ns <= now < window.expires_ns
                        and now - window.observed_ns
                        <= self.broker.config.pressure_ttl_ms * 1_000_000
                    )
                except Exception:  # noqa: BLE001 - unknown authorization withdraws results
                    safe = False
                if not safe or request.cancelled.is_set() or self._closed.is_set():
                    self.ownership.revoke_request(request.token, now_ns=self.clock())
                permitted = self.ownership.finish_request(
                    request.token,
                    RequestStopProof(
                        request.token,
                        observation.observed_ns,
                        self.broker.clock_domain,
                        True,
                        True,
                    ),
                    observation,
                    now_ns=self.clock(),
                )
                request.permitted = (
                    permitted
                    and not request.cancelled.is_set()
                    and not self._closed.is_set()
                )
                self._request = None
                request.done.set()
            request.callback()
        except Exception:  # noqa: BLE001 - unknown dependency remains closed
            request.cancelled.set()
            # Never clear busy, owner, physical lease or execution slot on error.
        finally:
            request.drainer = None

    def result_allowed(self, request):
        """Last receiver-clock/authority check after ACK, before publication."""
        with self._mutex:
            return (
                request.done.is_set()
                and request.permitted
                and not request.cancelled.is_set()
                and not self._closed.is_set()
                and self._runtime == request.token.runtime
                and self._request is None
                and self.clock() < request.token.expires_ns
                and self.available()
            )

    def retry_drain(self):
        with self._mutex:
            request = self._request
            if (
                request is not None
                and request.drainer is None
                and request.callback is not None
            ):
                self.drain(request, request.workers, request.callback)

    def release_after_exit(self):
        """Only strict original-worker exit can hand off the physical owner."""
        if not self._mutex.acquire(blocking=False):
            return False
        callback = None
        try:
            if self._lease is None or self._worker is None:
                return False
            proof = self.observer.exit_proof(self._lease, self._worker)
            now = self.clock()
            if (
                not isinstance(proof, ExitProof)
                or proof.worker != self._worker
                or proof.broker_id != self.broker.broker_id
                or proof.lease_id != self._lease.lease_id
                or proof.lease_epoch != self._lease.lease_epoch
                or proof.clock_domain != self.broker.clock_domain
                or not self._lease.granted_ns <= proof.observed_ns <= now
                or now - proof.observed_ns
                > self.broker.config.pressure_ttl_ms * 1_000_000
                or any(
                    value is not True
                    for value in (
                        proof.process_exited,
                        proof.descendants_exited,
                        proof.owned_gpu_released,
                    )
                )
            ):
                return False
            request = self._request
            if request is not None:
                # A caller has not yet registered its complete local worker set.
                if request.callback is None or any(
                    worker.is_alive() for worker in request.workers
                ):
                    return False
                self.ownership.revoke_request(request.token, now_ns=now)
                observation = RuntimeObservation(
                    self._lease.lease_id,
                    self._lease.lease_epoch,
                    proof.worker,
                    proof.observed_ns,
                    self.broker.clock_domain,
                    False,
                    False,
                    True,
                    True,
                )
                self.ownership.finish_request(
                    request.token,
                    RequestStopProof(
                        request.token,
                        proof.observed_ns,
                        self.broker.clock_domain,
                        True,
                        True,
                    ),
                    observation,
                    now_ns=now,
                )
                self._request = None
                request.done.set()
                callback = request.callback
            if self._runtime is None:
                self.broker.release_after_exit(self._lease, proof, now_ns=now)
            else:
                self.ownership.release_after_exit(self._runtime, proof, now_ns=now)
            self._runtime = None
            self._closed.set()
            return True
        finally:
            self._mutex.release()
            # RequestOwnership may hold its own lock while submitting drain;
            # invoke its release callback only outside the lifecycle mutex.
            if callback is not None:
                callback()
