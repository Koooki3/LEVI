"""Resident models remain physically owned after HTTP/request completion."""

import gc
import weakref
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event, Thread

import pytest

from levi.domain import aeri
from levi.live.runtime_ownership import (
    RequestStopProof,
    RuntimeObservation,
    RuntimeOwnership,
)
from levi.live.workload_broker import (
    ExitProof,
    IdleProof,
    SafeWindow,
    WorkerIdentity,
    WorkloadBroker,
)

NOW = 1_000_000_000
CLOCK = "host-mono:boot"
PROFILE = "ab" * 32
MS = 1_000_000


@pytest.fixture
def resident():
    broker = WorkloadBroker(run_id="run", clock_domain=CLOCK, profile_sha256=PROFILE)
    envelope = {
        "schema": aeri.SCHEMAS["runtime"],
        "minor": aeri.MINOR,
        "run_id": "run",
        "emitted_wall_ns": 0,
    }
    broker.prove_idle(
        IdleProof(broker.broker_id, NOW, CLOCK, True, True, True), now_ns=NOW
    )
    workload = aeri.WorkloadSpec(
        **envelope,
        kind="workload_spec",
        workload_id="model",
        workload_class="background_annotation",
        requester="c",
        device_preference="gpu",
        memory_estimate_mib=1000,
        memory_source="measured",
        duration_p99_ms=100,
        duration_source="measured",
        preemptibility="request_boundary",
    )
    broker.submit(workload, due(), profile_sha256=PROFILE, now_ns=NOW)
    pressure = aeri.ResourcePressure(
        **envelope,
        kind="pressure",
        observed_ns=NOW,
        clock_domain=CLOCK,
        source="fake",
        free_mib=5000,
        vllm_state="awake",
        gate_open=True,
        gate_code="open",
        policy_server_seen=False,
        age_ms=0,
    )
    window = SafeWindow(
        broker.broker_id, "run", None, PROFILE, NOW, NOW + 500 * MS, CLOCK
    )
    lease = broker.admit(pressure, window, now_ns=NOW)
    worker = WorkerIdentity(100, 123, "boot")
    observation = RuntimeObservation(
        lease.lease_id, lease.lease_epoch, worker, NOW, CLOCK, True, True, True, True
    )
    owner = RuntimeOwnership(broker)
    runtime = owner.bind_runtime(lease, worker, observation, now_ns=NOW)
    return owner, runtime, observation, lease


def due(**over):
    return aeri.Deadline(
        **{
            "due_ns": NOW + 500 * MS,
            "clock_domain": CLOCK,
            "budget_ms": 500,
            "hardness": "hard",
        }
        | over
    )


def stopped(request, **over):
    return replace(RequestStopProof(request, NOW, CLOCK, True, True), **over)


def exited(owner, runtime, **over):
    return replace(
        ExitProof(
            owner.broker.broker_id,
            runtime.lease_id,
            runtime.lease_epoch,
            runtime.worker,
            NOW,
            CLOCK,
            True,
            True,
            True,
        ),
        **over,
    )


def test_request_rotation_retains_same_resident_physical_owner(resident):
    owner, runtime, observation, lease = resident
    first = owner.begin_request(runtime, due(), observation, now_ns=NOW)
    assert owner.accepts_result(first, observation, now_ns=NOW)
    assert owner.finish_request(first, stopped(first), observation, now_ns=NOW)
    assert owner.broker.snapshot(now_ns=NOW)["active"] == lease.model_dump(
        by_alias=True
    )
    second = owner.begin_request(runtime, due(), observation, now_ns=NOW)
    assert second.runtime == first.runtime
    assert second.request_epoch == first.request_epoch + 1
    assert not owner.accepts_result(first, observation, now_ns=NOW)
    with pytest.raises(ValueError, match="backend-stop"):
        owner.finish_request(second, stopped(first), observation, now_ns=NOW)
    assert owner.snapshot(now_ns=NOW)["request"] == second


def test_ignored_cancel_and_completed_calling_thread_do_not_clear_live_work(resident):
    owner, runtime, observation, _ = resident
    request = owner.begin_request(runtime, due(), observation, now_ns=NOW)
    release, started = Event(), Event()

    def backend():
        started.set()
        release.wait()

    worker = Thread(target=backend)
    worker.start()
    started.wait(1)
    try:
        owner.revoke_request(request, now_ns=NOW)  # HTTP/calling thread has finished.
        assert not owner.accepts_result(request, observation, now_ns=NOW)
        with pytest.raises(ValueError, match="all-worker"):
            owner.finish_request(
                request,
                stopped(request, request_workers_exited=not worker.is_alive()),
                observation,
                now_ns=NOW,
            )
        with pytest.raises(ValueError, match="still owns"):
            owner.begin_request(runtime, due(), observation, now_ns=NOW)
    finally:
        release.set()
        worker.join(1)
    assert not worker.is_alive()
    assert not owner.finish_request(request, stopped(request), observation, now_ns=NOW)
    owner.begin_request(runtime, due(), observation, now_ns=NOW)


@pytest.mark.parametrize("field", ["backend_request_stopped", "request_workers_exited"])
@pytest.mark.parametrize("unproven", [False, None, 1])
def test_unproven_backend_or_worker_stop_keeps_slot(resident, field, unproven):
    owner, runtime, observation, _ = resident
    request = owner.begin_request(runtime, due(), observation, now_ns=NOW)
    with pytest.raises(ValueError):
        owner.finish_request(
            request, stopped(request, **{field: unproven}), observation, now_ns=NOW
        )
    assert owner.snapshot(now_ns=NOW)["request"] == request


def test_deadline_revokes_results_but_late_proven_stop_can_clear_request(resident):
    owner, runtime, observation, _ = resident
    request = owner.begin_request(runtime, due(budget_ms=10), observation, now_ns=NOW)
    late = NOW + 10 * MS
    fresh = replace(observation, observed_ns=late)
    assert not owner.accepts_result(request, fresh, now_ns=late)
    with pytest.raises(ValueError, match="still owns"):
        owner.begin_request(runtime, due(), fresh, now_ns=late)
    assert not owner.finish_request(
        request, stopped(request, observed_ns=late), fresh, now_ns=late
    )
    with pytest.raises(ValueError, match="quiescent"):
        owner.begin_request(runtime, due(), observation, now_ns=late)
    owner.begin_request(runtime, due(), fresh, now_ns=late)


@pytest.mark.parametrize(
    "change",
    [
        {"worker": WorkerIdentity(100, 124, "boot")},  # PID reuse
        {"worker": WorkerIdentity(100, 123, "new-boot")},
        {"lease_epoch": 2},
        {"process_running": None},
        {"resource_lease_held": False},
        {"observed_ns": NOW + 1},
        {"clock_domain": "host-mono:other"},
    ],
)
def test_unknown_changed_or_future_runtime_latches_closed(resident, change):
    owner, runtime, observation, lease = resident
    request = owner.begin_request(runtime, due(), observation, now_ns=NOW)
    assert not owner.accepts_result(request, replace(observation, **change), now_ns=NOW)
    assert not owner.accepts_result(request, observation, now_ns=NOW)
    assert not owner.finish_request(request, stopped(request), observation, now_ns=NOW)
    with pytest.raises(ValueError, match="unproven"):
        owner.begin_request(runtime, due(), observation, now_ns=NOW)
    assert owner.broker.snapshot(now_ns=NOW)["active"] == lease.model_dump(
        by_alias=True
    )


def test_stale_observation_and_stop_proof_fail_closed(resident):
    owner, runtime, observation, _ = resident
    later = NOW + 20 * MS
    request = owner.begin_request(
        runtime, due(), replace(observation, observed_ns=later), now_ns=later
    )
    with pytest.raises(ValueError, match="backend-stop"):
        owner.finish_request(
            request,
            stopped(request),
            replace(observation, observed_ns=later),
            now_ns=later,
        )
    assert owner.snapshot(now_ns=later)["request"] == request
    # Beyond the freshness TTL (and physical expiry) still keeps the owner.
    assert not owner.accepts_result(request, observation, now_ns=NOW + 1001 * MS)
    assert owner.snapshot(now_ns=NOW + 1001 * MS)["runtime"] == runtime


def test_new_wrapper_cannot_duplicate_or_adopt_request_authority(resident):
    owner, runtime, observation, lease = resident
    restarted = RuntimeOwnership(owner.broker)
    assert restarted.snapshot(now_ns=NOW)["runtime"] is None
    with pytest.raises(ValueError, match="stale"):
        restarted.begin_request(runtime, due(), observation, now_ns=NOW)
    with pytest.raises(ValueError, match="already has"):
        restarted.bind_runtime(lease, runtime.worker, observation, now_ns=NOW)
    new_broker = WorkloadBroker(
        run_id="run", clock_domain=CLOCK, profile_sha256=PROFILE
    )
    with pytest.raises(ValueError, match="stale"):
        RuntimeOwnership(new_broker).bind_runtime(
            lease, runtime.worker, observation, now_ns=NOW
        )


def test_only_one_concurrent_request_can_start(resident):
    owner, runtime, observation, _ = resident

    def begin(_):
        try:
            return owner.begin_request(runtime, due(), observation, now_ns=NOW)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        tokens = list(pool.map(begin, range(16)))
    assert sum(token is not None for token in tokens) == 1


def test_physical_exit_stays_strict_and_request_ownership_blocks_handoff(resident):
    owner, runtime, observation, lease = resident
    request = owner.begin_request(runtime, due(), observation, now_ns=NOW)
    with pytest.raises(ValueError, match="request worker"):
        owner.release_after_exit(runtime, exited(owner, runtime), now_ns=NOW)
    owner.finish_request(request, stopped(request), observation, now_ns=NOW)
    for field in ("process_exited", "descendants_exited", "owned_gpu_released"):
        with pytest.raises(ValueError, match="GPU exit"):
            owner.release_after_exit(
                runtime, exited(owner, runtime, **{field: None}), now_ns=NOW
            )
        assert owner.broker.snapshot(now_ns=NOW)["active"] == lease.model_dump(
            by_alias=True
        )
    with pytest.raises(ValueError):
        owner.release_after_exit(
            runtime,
            exited(owner, runtime, worker=WorkerIdentity(100, 124, "boot")),
            now_ns=NOW,
        )
    owner.release_after_exit(runtime, exited(owner, runtime), now_ns=NOW)
    assert owner.broker.snapshot(now_ns=NOW)["active"] is None
    assert owner.snapshot(now_ns=NOW)["runtime"] is None


def test_broker_revoke_or_expiry_does_not_release_resident(resident):
    owner, runtime, observation, lease = resident
    request = owner.begin_request(runtime, due(), observation, now_ns=NOW)
    owner.broker.cancel(lease.workload_id, now_ns=NOW)
    assert not owner.accepts_result(request, observation, now_ns=NOW)
    assert not owner.finish_request(request, stopped(request), observation, now_ns=NOW)
    with pytest.raises(ValueError, match="unproven"):
        owner.begin_request(runtime, due(), observation, now_ns=NOW)
    assert owner.broker.snapshot(now_ns=NOW)["worker"] == runtime.worker


@pytest.mark.parametrize(
    "changes", [{"clock_domain": "host-mono:other"}, {"budget_ms": 0}, {"due_ns": NOW}]
)
def test_wrong_or_expired_deadline_never_grants_request(resident, changes):
    owner, runtime, observation, _ = resident
    with pytest.raises(ValueError):
        owner.begin_request(runtime, due(**changes), observation, now_ns=NOW)
    assert owner.snapshot(now_ns=NOW)["request"] is None


def test_old_runtime_epoch_and_foreign_request_cannot_revoke_current(resident):
    owner, runtime, observation, _ = resident
    request = owner.begin_request(runtime, due(), observation, now_ns=NOW)
    stale = replace(
        request, runtime=replace(runtime, runtime_epoch=runtime.runtime_epoch + 1)
    )
    assert not owner.accepts_result(stale, observation, now_ns=NOW)
    with pytest.raises(ValueError, match="stale"):
        owner.revoke_request(stale, now_ns=NOW)
    assert owner.accepts_result(request, observation, now_ns=NOW)


def test_physical_replacement_outside_wrapper_latches_closed(resident):
    owner, runtime, observation, lease = resident
    request = owner.begin_request(runtime, due(), observation, now_ns=NOW)
    # Trusted physical proof is simulated; no actual process/GPU probe runs.
    owner.broker.release_after_exit(lease, exited(owner, runtime), now_ns=NOW)
    assert not owner.accepts_result(request, observation, now_ns=NOW)
    assert not owner.snapshot(now_ns=NOW)["identity_known"]


def test_backend_not_idle_cannot_bind_or_begin(resident):
    owner, runtime, observation, _ = resident
    with pytest.raises(ValueError, match="quiescent"):
        owner.begin_request(
            runtime, due(), replace(observation, backend_idle=None), now_ns=NOW
        )
    with pytest.raises(ValueError, match="quiescent"):
        owner.begin_request(
            runtime,
            due(),
            replace(observation, request_workers_exited=False),
            now_ns=NOW,
        )
    assert owner.snapshot(now_ns=NOW)["request"] is None


def test_forgotten_wrapper_does_not_implicitly_release_request_authority():
    owner, runtime, observation, lease = resident.__wrapped__()
    broker = owner.broker
    reference = weakref.ref(owner)
    owner.begin_request(runtime, due(), observation, now_ns=NOW)
    del owner
    gc.collect()
    assert reference() is None
    with pytest.raises(ValueError, match="already has"):
        RuntimeOwnership(broker).bind_runtime(
            lease, runtime.worker, observation, now_ns=NOW
        )
    assert broker.snapshot(now_ns=NOW)["worker"] == runtime.worker


@pytest.mark.parametrize(
    "change",
    [
        {"observed_ns": NOW - 1},
        {"observed_ns": NOW + 1},
        {"clock_domain": "host-mono:other"},
    ],
)
def test_foreign_or_future_completion_never_clears_slot(resident, change):
    owner, runtime, observation, _ = resident
    request = owner.begin_request(runtime, due(), observation, now_ns=NOW)
    with pytest.raises(ValueError, match="backend-stop"):
        owner.finish_request(
            request, stopped(request, **change), observation, now_ns=NOW
        )
    assert owner.snapshot(now_ns=NOW)["request"] == request
