"""Opt-in, in-process GPU admission kernel for the existing live controller.

No probes, files, threads, services, locks or robot commands are started here.
The owner supplies fresh pressure, an authorized safe window and actual worker
exit proofs. A revoked result lease never proves that its worker left the GPU.
"""

import re
import threading
import uuid
from collections import deque
from dataclasses import dataclass

from pydantic import TypeAdapter

from levi.domain import aeri

NS_PER_MS = 1_000_000


@dataclass(frozen=True)
class BrokerConfig:
    max_queue: int = 128
    pressure_ttl_ms: int = 1000
    memory_margin_mib: int = 256
    deadline_margin_ms: int = 10

    def __post_init__(self):
        for name, low, high in (
            ("max_queue", 1, 1024),
            ("pressure_ttl_ms", 1, 30_000),
            ("memory_margin_mib", 0, 1_000_000),
            ("deadline_margin_ms", 0, 30_000),
        ):
            if (
                type(getattr(self, name)) is not int
                or not low <= getattr(self, name) <= high
            ):
                raise ValueError(f"invalid bounded {name}")


@dataclass(frozen=True)
class SafeWindow:
    """Caller-authorized window; never inferred from an unused flock."""

    broker_id: str
    run_id: str
    episode_id: str | None
    profile_sha256: str
    observed_ns: int
    expires_ns: int
    clock_domain: str


@dataclass(frozen=True)
class WorkerIdentity:
    pid: int
    start_ticks: int
    boot_id: str

    def __post_init__(self):
        if (
            type(self.pid) is not int
            or self.pid <= 0
            or type(self.start_ticks) is not int
            or self.start_ticks < 0
            or not isinstance(self.boot_id, str)
            or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", self.boot_id)
        ):
            raise ValueError("exact worker PID/start/boot identity required")


@dataclass(frozen=True)
class IdleProof:
    """Trusted controller checked all previous owned workers, not free VRAM."""

    broker_id: str
    observed_ns: int
    clock_domain: str
    workers_exited: bool | None
    descendants_exited: bool | None
    owned_gpu_released: bool | None
    lease_epoch: int = 0


@dataclass(frozen=True)
class ExitProof:
    broker_id: str
    lease_id: str
    lease_epoch: int
    worker: WorkerIdentity
    observed_ns: int
    clock_domain: str
    process_exited: bool | None
    descendants_exited: bool | None
    owned_gpu_released: bool | None


@dataclass(frozen=True)
class _Request:
    spec: aeri.WorkloadSpec
    deadline: aeri.Deadline
    submitted_ns: int
    order: int


@dataclass
class _Active:
    request: _Request
    lease: aeri.ResourceLease
    worker: WorkerIdentity | None = None
    revoked: bool = False
    preemption: str | None = None


def _copy(contract, value):
    # Revalidate even a model_copy update which bypassed Pydantic validation.
    if hasattr(value, "model_dump"):
        value = value.model_dump(by_alias=True)
    return contract.model_validate(value).model_copy(deep=True)


class WorkloadBroker:
    """Bounded GPU queue with one physical owner, atomic across caller threads.

    Existing resource-driver/flock ownership remains the controller's job.
    Constructor/restart defaults to unknown ownership until prove_idle succeeds.
    P0 watchdog/stop paths never call or wait for this object.
    """

    def __init__(self, *, run_id, clock_domain, profile_sha256, config=None):
        TypeAdapter(aeri.Id).validate_python(run_id)
        TypeAdapter(aeri.ClockDomain).validate_python(clock_domain)
        if not isinstance(profile_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", profile_sha256
        ):
            raise ValueError("frozen profile SHA-256 required")
        self.run_id, self.clock_domain = run_id, clock_domain
        self.profile_sha256 = profile_sha256
        self.config = config or BrokerConfig()
        self.broker_id = "broker:" + uuid.uuid4().hex
        self._lock = threading.RLock()
        self._known_idle = False
        self._queue = {}
        self._active = None
        self._epoch, self._order, self._last_ns = 0, 0, 0
        self.rejections = deque(maxlen=self.config.max_queue)
        self.dropped_rejections = 0

    def _tick(self, now_ns):
        if type(now_ns) is not int or not self._last_ns <= now_ns <= aeri.INT64_MAX:
            raise ValueError("nonnegative advancing receiver clock required")
        self._last_ns = now_ns
        if self._active and now_ns >= self._active.lease.expires_ns:
            self._revoke("lease_expired")

    def _fresh(self, observed_ns, domain, now_ns):
        return (
            type(observed_ns) is int
            and domain == self.clock_domain
            and 0 <= observed_ns <= now_ns
            and now_ns - observed_ns <= self.config.pressure_ttl_ms * NS_PER_MS
        )

    def _rejected(
        self, workload_id, code, detail, *, retryable=False, legacy_code=None
    ):
        return aeri.AdmissionRejected(
            schema=aeri.SCHEMAS["runtime"],
            minor=aeri.MINOR,
            run_id=self.run_id,
            emitted_wall_ns=0,
            kind="admission_rejected",
            workload_id=workload_id,
            code=code,
            detail=detail[:300],
            retryable=retryable,
            retry_after_ms=100 if retryable else None,
            legacy_code=legacy_code,
        )

    def _notice(self, rejection):
        if len(self.rejections) == self.rejections.maxlen:
            self.dropped_rejections += 1
        self.rejections.append(rejection)

    def _revoke(self, reason):
        active = self._active
        if active:
            active.revoked = True
            if active.request.spec.preemptibility != "none":
                active.preemption = reason

    def prove_idle(self, proof: IdleProof, *, now_ns):
        with self._lock:
            self._tick(now_ns)
            if (
                self._active is not None
                or proof.lease_epoch != self._epoch
                or proof.broker_id != self.broker_id
                or not self._fresh(proof.observed_ns, proof.clock_domain, now_ns)
                or any(
                    x is not True
                    for x in (
                        proof.workers_exited,
                        proof.descendants_exited,
                        proof.owned_gpu_released,
                    )
                )
            ):
                raise ValueError("fresh complete current-broker idle proof required")
            self._known_idle = True

    def submit(self, spec, deadline, *, profile_sha256, now_ns):
        spec, deadline = _copy(aeri.WorkloadSpec, spec), _copy(aeri.Deadline, deadline)
        with self._lock:
            self._tick(now_ns)
            wid = spec.workload_id
            if (
                spec.run_id != self.run_id
                or profile_sha256 != self.profile_sha256
                or deadline.clock_domain != self.clock_domain
            ):
                return self._rejected(
                    wid, "unknown_client", "run, frozen profile or clock differs"
                )
            if spec.device_preference == "cpu":
                return self._rejected(
                    wid,
                    "broker_unavailable",
                    "GPU kernel does not manage CPU-only work",
                )
            if spec.memory_estimate_mib is None or spec.memory_estimate_mib <= 0:
                return self._rejected(
                    wid, "no_memory", "positive known workload memory estimate required"
                )
            if (
                spec.duration_p99_ms is None
                or spec.duration_p99_ms <= 0
                or deadline.budget_ms <= 0
                or deadline.due_ns <= now_ns
            ):
                return self._rejected(
                    wid,
                    "deadline_infeasible",
                    "positive known complete duration and future deadline required",
                )
            if (
                wid in self._queue
                or self._active
                and self._active.request.spec.workload_id == wid
            ):
                return self._rejected(
                    wid, "busy", "workload already queued or physically owned"
                )
            if len(self._queue) >= self.config.max_queue:
                return self._rejected(
                    wid, "busy", "bounded workload queue is full", retryable=True
                )
            self._order += 1
            self._queue[wid] = _Request(spec, deadline, now_ns, self._order)
            return None

    def _pressure_error(self, pressure, now_ns):
        if (
            pressure.run_id != self.run_id
            or not self._fresh(pressure.observed_ns, pressure.clock_domain, now_ns)
            or pressure.age_ms * NS_PER_MS + now_ns - pressure.observed_ns
            > self.config.pressure_ttl_ms * NS_PER_MS
        ):
            return (
                "broker_unavailable",
                "pressure is future, stale or from another run/clock",
            )
        if pressure.gate_open is not True or pressure.gate_code not in (None, "open"):
            code = {
                "policy_inferring": "policy_priority",
                "episode_imminent": "episode_imminent",
                "unknown_client": "unknown_client",
                "standby_settling": "standby_settling",
            }.get(pressure.gate_code, "evaluation_active")
            return code, "existing GPU gate is closed or unproven"
        if (
            pressure.free_mib is None
            or pressure.policy_server_seen is None
            or pressure.vllm_state in ("unknown", "starting")
            or pressure.policy_server_seen is True
            and pressure.policy_mib is None
        ):
            return (
                "broker_unavailable",
                "GPU memory, policy identity or model state is unknown",
            )
        if pressure.policy_server_seen is False and pressure.policy_mib not in (
            None,
            0,
        ):
            return "unknown_client", "policy absence and policy memory disagree"
        return None

    def _window_ok(self, window, request, now_ns):
        return (
            window is not None
            and window.broker_id == self.broker_id
            and window.run_id == self.run_id
            and window.profile_sha256 == self.profile_sha256
            and window.episode_id == request.spec.episode_id
            and self._fresh(window.observed_ns, window.clock_domain, now_ns)
            and type(window.expires_ns) is int
            and now_ns < window.expires_ns <= aeri.INT64_MAX
        )

    def admit(self, pressure, window: SafeWindow | None, *, now_ns):
        pressure = _copy(aeri.ResourcePressure, pressure)
        with self._lock:
            self._tick(now_ns)
            error = self._pressure_error(pressure, now_ns)
            if (
                error
                and self._active
                and self._active.request.spec.workload_class != "policy_realtime"
            ):
                self._revoke("gate_closed_or_unknown")
            # Expired queue entries cannot block later feasible requests forever.
            for wid, request in tuple(self._queue.items()):
                end = min(
                    request.deadline.due_ns,
                    request.submitted_ns + request.deadline.budget_ms * NS_PER_MS,
                )
                if end <= now_ns:
                    self._notice(
                        self._rejected(
                            wid,
                            "deadline_infeasible",
                            "deadline expired in the bounded queue",
                        )
                    )
                    del self._queue[wid]
            if not self._queue:
                return None
            request = min(
                self._queue.values(),
                key=lambda r: (
                    aeri.WORKLOAD_PRIORITY[r.spec.workload_class],
                    r.deadline.due_ns,
                    r.order,
                ),
            )
            spec, wid = request.spec, request.spec.workload_id
            if error:
                return self._rejected(
                    wid, *error, retryable=True, legacy_code=pressure.gate_code
                )
            if not self._window_ok(window, request, now_ns):
                return self._rejected(
                    wid,
                    "evaluation_active",
                    "fresh authorized episode/profile safety window required",
                    retryable=True,
                )
            if not self._known_idle:
                return self._rejected(
                    wid,
                    "broker_unavailable",
                    "previous GPU worker ownership is unknown",
                    retryable=True,
                )
            if self._active:
                active = self._active
                if (
                    aeri.WORKLOAD_PRIORITY[spec.workload_class]
                    < aeri.WORKLOAD_PRIORITY[active.request.spec.workload_class]
                ):
                    if active.request.spec.preemptibility != "none":
                        self._revoke("higher_priority")
                    code = (
                        "policy_priority"
                        if spec.workload_class == "policy_realtime"
                        else "busy"
                    )
                else:
                    code = "busy"
                return self._rejected(
                    wid,
                    code,
                    "physical worker/reservation still owns GPU; wait for verified exit",
                    retryable=True,
                )
            if (
                spec.workload_class == "judge_critical"
                and pressure.vllm_state == "absent"
            ):
                return self._rejected(
                    wid,
                    "cold_start_forbidden",
                    "critical judgement requires a known warm or sleeping model",
                )
            if (
                spec.memory_estimate_mib + self.config.memory_margin_mib
                > pressure.free_mib
            ):
                return self._rejected(
                    wid,
                    "no_memory",
                    "fresh free VRAM does not cover workload plus margin",
                    retryable=True,
                )
            end = min(
                request.deadline.due_ns,
                request.submitted_ns + request.deadline.budget_ms * NS_PER_MS,
                window.expires_ns,
                now_ns + aeri.MAX_LEASE_MS * NS_PER_MS,
            )
            required = (
                spec.duration_p99_ms + self.config.deadline_margin_ms
            ) * NS_PER_MS
            if now_ns + required > end:
                return self._rejected(
                    wid,
                    "deadline_infeasible",
                    "p99 including startup/wake and margin cannot finish inside deadline/window",
                )
            self._epoch += 1
            lease = aeri.ResourceLease(
                schema=aeri.SCHEMAS["runtime"],
                minor=aeri.MINOR,
                run_id=self.run_id,
                emitted_wall_ns=0,
                kind="lease",
                lease_id=f"{self.broker_id}:l{self._epoch}",
                lease_epoch=self._epoch,
                workload_id=wid,
                workload_class=spec.workload_class,
                device="gpu",
                memory_reserved_mib=spec.memory_estimate_mib,
                granted_ns=now_ns,
                expires_ns=end,
                clock_domain=self.clock_domain,
                revocable=spec.preemptibility != "none",
            )
            self._active = _Active(request, lease)
            del self._queue[wid]
            return lease

    def _token(self, lease):
        lease = _copy(aeri.ResourceLease, lease)
        if self._active is None or lease != self._active.lease:
            raise ValueError("stale or foreign lease identity/epoch")
        return self._active

    def bind_worker(self, lease, worker: WorkerIdentity, *, now_ns):
        with self._lock:
            self._tick(now_ns)
            active = self._token(lease)
            if (
                not isinstance(worker, WorkerIdentity)
                or active.worker is not None
                and active.worker != worker
            ):
                raise ValueError("one exact worker identity per physical lease")
            # Binding an already revoked reservation still records its live owner.
            active.worker = worker

    def cancel(self, workload_id, *, now_ns):
        with self._lock:
            self._tick(now_ns)
            if self._queue.pop(workload_id, None) is not None:
                return True
            if self._active and self._active.request.spec.workload_id == workload_id:
                self._revoke("cancelled")
                return True
            return False

    def accepts_result(self, lease, *, now_ns):
        with self._lock:
            self._tick(now_ns)
            try:
                active = self._token(lease)
            except ValueError:
                return False
            return (
                active.worker is not None
                and not active.revoked
                and active.lease.granted_ns <= now_ns < active.lease.expires_ns
                and now_ns <= active.request.deadline.due_ns
            )

    def preemption_request(self):
        with self._lock:
            active = self._active
            if active is None or active.preemption is None:
                return None
            return {
                "lease_id": active.lease.lease_id,
                "lease_epoch": active.lease.lease_epoch,
                "mode": active.request.spec.preemptibility,
                "reason": active.preemption,
                "worker": active.worker,
            }

    def release_after_exit(self, lease, proof: ExitProof, *, now_ns):
        with self._lock:
            self._tick(now_ns)
            active = self._token(lease)
            if (
                active.worker is None
                or proof.lease_id != active.lease.lease_id
                or proof.lease_epoch != active.lease.lease_epoch
                or proof.worker != active.worker
                or proof.broker_id != self.broker_id
                or not self._fresh(proof.observed_ns, proof.clock_domain, now_ns)
                or proof.observed_ns < active.lease.granted_ns
                or any(
                    x is not True
                    for x in (
                        proof.process_exited,
                        proof.descendants_exited,
                        proof.owned_gpu_released,
                    )
                )
            ):
                raise ValueError(
                    "exact fresh worker/descendant/GPU exit proof required"
                )
            self._active = None
            self._known_idle = True

    def release_unstarted(self, lease, proof: IdleProof, *, now_ns):
        """Explicit confirmed never-started reservation, never inferred by expiry."""
        with self._lock:
            self._tick(now_ns)
            active = self._token(lease)
            if (
                active.worker is not None
                or proof.lease_epoch != active.lease.lease_epoch
                or proof.broker_id != self.broker_id
                or not self._fresh(proof.observed_ns, proof.clock_domain, now_ns)
                or proof.observed_ns < active.lease.granted_ns
                or any(
                    x is not True
                    for x in (
                        proof.workers_exited,
                        proof.descendants_exited,
                        proof.owned_gpu_released,
                    )
                )
            ):
                raise ValueError(
                    "fresh complete never-started reservation proof required"
                )
            self._active = None
            self._known_idle = True

    def drain_rejections(self, *, limit=128):
        if type(limit) is not int or not 1 <= limit <= self.config.max_queue:
            raise ValueError("bounded rejection-drain limit required")
        with self._lock:
            return [
                self.rejections.popleft()
                for _ in range(min(limit, len(self.rejections)))
            ]

    def snapshot(self, *, now_ns):
        with self._lock:
            self._tick(now_ns)
            active = self._active
            return {
                "broker_id": self.broker_id,
                "run_id": self.run_id,
                "profile_sha256": self.profile_sha256,
                "ownership_known": self._known_idle,
                "lease_epoch": self._epoch,
                "queued": len(self._queue),
                "active": active.lease.model_dump(by_alias=True) if active else None,
                "worker": active.worker if active else None,
                "revoked": active.revoked if active else None,
                "dropped_rejections": self.dropped_rejections,
            }
