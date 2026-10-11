"""Original broker + role kernel + owned SDK receipt, all hardware explicitly fake."""

import pickle
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from levi.automatic.owned_runtime import OwnedRoleRuntime
from levi.domain import aeri
from levi.live import owned_runtime as owned
from levi.live.runtime_bridge import DriverIdentity, DriverObservation
from levi.live.runtime_ownership import (
    OwnedGroup,
    OwnedMember,
    RoleWindow,
    RuntimeOwnership,
)
from levi.live.workload_broker import (
    IdleProof,
    SafeWindow,
    WorkerIdentity,
    WorkloadBroker,
)

NOW = 1_000_000_000
CLOCK, PROFILE = "host-mono:fake", "ab" * 32


class Pipe:
    def __init__(self, worker):
        self.worker, self.answer, self.sent = worker, [], []

    def send_bytes(self, value):
        operation, epoch, payload = pickle.loads(value)
        self.sent.append((operation, epoch, payload))

    def poll(self, timeout):
        return bool(self.answer)

    def recv_bytes(self, limit):
        return pickle.dumps(self.answer.pop(0))

    def finish(self, value):
        epoch = self.worker._pending[1]
        self.answer.append(
            (
                "result",
                owned.SdkIdleAck(
                    self.worker.worker,
                    self.worker.launch_id,
                    epoch,
                    self.worker.factory.capability_id,
                ),
                value,
            )
        )


@pytest.fixture
def execution(monkeypatch):
    supervisor = WorkerIdentity(100, 1, "fake")
    driver = DriverIdentity("driver", 1, "session", WorkerIdentity(99, 1, "fake"))
    driver_observer = SimpleNamespace(
        supervisor=supervisor,
        clock=lambda: NOW,
        require_held=lambda: DriverObservation(
            driver, NOW, CLOCK, True, False, True, True
        ),
    )
    group_scope = object.__new__(owned.CgroupGroupScope)
    group_scope.supervisor = supervisor
    group_scope._launches = {}
    group_scope._lock = threading.Lock()
    group_scope.members = lambda: (100, 101, 102, 103)
    group_scope.require_supervisor = group_scope.members
    workers, members = {}, []
    for i, role in enumerate(("forward", "judge", "reset")):
        worker = object.__new__(owned.OwnedModelWorker)
        worker.worker, worker.role = WorkerIdentity(101 + i, 1, "fake"), role
        worker.factory = owned.FrozenTorchFactory(
            role + "_factory", "build", "ab" * 32, "frozen", "cuda:0"
        )
        worker.launch_id = "launch-" + role
        worker.driver_observer, worker.group_scope = driver_observer, group_scope
        worker._authority = worker._observer = None
        worker._lock, worker._closed = threading.Lock(), threading.Event()
        worker._pending = None
        worker._idle = owned.SdkIdleAck(
            worker.worker, worker.launch_id, 0, worker.factory.capability_id
        )
        worker._healthy = lambda: None
        worker._connection = Pipe(worker)
        workers[role] = worker
        group_scope._launches[role] = worker
        members.append(OwnedMember(role, worker.worker, worker.factory.capability_id))
    group = OwnedGroup("models", supervisor, tuple(members))
    broker = WorkloadBroker(run_id="run", clock_domain=CLOCK, profile_sha256=PROFILE)
    broker.prove_idle(
        IdleProof(broker.broker_id, NOW, CLOCK, True, True, True), now_ns=NOW
    )
    env = {
        "schema": aeri.SCHEMAS["runtime"],
        "minor": aeri.MINOR,
        "run_id": "run",
        "emitted_wall_ns": 0,
    }
    spec = aeri.WorkloadSpec(
        **env,
        kind="workload_spec",
        workload_id="models",
        workload_class="resident_group",
        requester="a",
        device_preference="gpu",
        memory_estimate_mib=1000,
        memory_source="declared",
        duration_p99_ms=10,
        duration_source="declared",
        preemptibility="request_boundary",
        resident_group={
            "group_id": "models",
            "members": [
                {"role": m.role, "capability_id": m.capability_id} for m in members
            ],
        },
    )
    deadline = aeri.Deadline(
        due_ns=NOW + 500_000_000, clock_domain=CLOCK, budget_ms=500, hardness="hard"
    )
    pressure = aeri.ResourcePressure(
        **env,
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
        broker.broker_id, "run", None, PROFILE, NOW, NOW + 500_000_000, CLOCK
    )
    assert broker.submit(spec, deadline, profile_sha256=PROFILE, now_ns=NOW) is None
    lease = broker.admit(pressure, window, now_ns=NOW)
    monkeypatch.setattr(owned, "process_alive", lambda worker: True)
    observer = owned.OwnedGroupObserver(
        group, workers, driver_observer, CLOCK, group_scope, clock=lambda: NOW
    )
    authority = RuntimeOwnership(broker)
    runtime = authority.bind_group(lease, group, observer.observe(lease), now_ns=NOW)
    runner = OwnedRoleRuntime(authority, runtime, lease, observer, clock=lambda: NOW)
    runner._test_pressure, runner._test_window = pressure, window
    return runner, workers, deadline


def begin(runner, deadline, role="judge", authorization=1):
    window = RoleWindow(
        runner.authority.broker.broker_id,
        "run",
        "run.forward.0001",
        PROFILE,
        NOW,
        NOW + 100_000_000,
        CLOCK,
        role,
        1,
        authorization,
    )
    return runner.begin(
        role,
        deadline,
        window,
        {"obs": "fake"},
        episode_id="run.forward.0001",
        policy_epoch=1,
    )


def test_actual_original_kernel_serializes_three_owned_roles(execution):
    runner, workers, deadline = execution
    request = begin(runner, deadline)
    with pytest.raises(ValueError, match="unresolved"):
        begin(runner, deadline, "forward", 2)
    with pytest.raises(TimeoutError):
        runner.collect(request, timeout_s=0.001)
    assert runner.authority.snapshot(now_ns=NOW)["request"] == request
    workers["judge"]._connection.finish({"success": False})
    assert runner.collect(request, timeout_s=0.001) == {"success": False}
    forward = begin(runner, deadline, "forward", 2)
    assert forward.request_epoch == request.request_epoch + 1
    assert workers["reset"]._connection.sent == []


def test_revoked_sdk_result_drains_but_cannot_publish(execution):
    runner, workers, deadline = execution
    request = begin(runner, deadline)
    runner.revoke(request)
    assert runner.authority.snapshot(now_ns=NOW)["request"] == request
    workers["judge"]._connection.finish("late-success")
    assert runner.collect(request, timeout_s=0.001) is None
    assert runner.authority.snapshot(now_ns=NOW)["request"] is None
    assert begin(runner, deadline, "forward", 2).role == "forward"


def test_fabricated_or_stale_token_cannot_reach_owned_model(execution):
    runner, workers, deadline = execution
    request = begin(runner, deadline)
    with pytest.raises(ValueError, match="original role authority"):
        workers["forward"].submit(replace(request, role="forward"), {})
    assert workers["forward"]._connection.sent == []
    with pytest.raises(ValueError):
        runner.revoke(replace(request, request_epoch=99))
    assert not runner._closed.is_set()


def test_close_is_nonblocking_while_request_worker_mutex_is_busy(execution):
    runner, workers, deadline = execution
    request = begin(runner, deadline)
    workers["judge"]._lock.acquire()
    try:
        runner.close()
        assert all(worker._closed.is_set() for worker in workers.values())
        assert runner.authority.snapshot(now_ns=NOW)["request"] == request
    finally:
        workers["judge"]._lock.release()


def test_adapter_renew_uses_original_atomic_runtime_and_lease_epochs(execution):
    runner, _workers, deadline = execution
    old_runtime, old_lease = runner.runtime, runner.lease
    new_window = replace(runner._test_window, expires_ns=NOW + 700_000_000)
    new_deadline = deadline.model_copy(
        update={"due_ns": NOW + 700_000_000, "budget_ms": 700}
    )
    token = runner.renew(new_deadline, runner._test_pressure, new_window)
    assert token.runtime_epoch == old_runtime.runtime_epoch + 1
    assert runner.lease.lease_epoch == old_lease.lease_epoch + 1
    assert runner.lease.lease_id == old_lease.lease_id
    with pytest.raises(ValueError, match="stale"):
        runner.authority.renew_resident(
            old_runtime,
            new_deadline,
            runner._test_pressure,
            new_window,
            runner.observer.observe(runner.lease),
            now_ns=NOW,
        )
    assert begin(runner, deadline).runtime == token


def test_unknown_probe_withdraws_result_and_cached_ack_can_later_drain(
    execution, monkeypatch
):
    runner, workers, deadline = execution
    request = begin(runner, deadline)
    workers["judge"]._connection.finish("must-not-publish")
    original_observe = runner.observer.observe

    def unknown(lease):
        raise ValueError("unreadable complete inventory")

    monkeypatch.setattr(runner.observer, "observe", unknown)
    with pytest.raises(ValueError, match="unreadable"):
        runner.collect(request, timeout_s=0.001)
    assert runner.authority.snapshot(now_ns=NOW)["request"] == request
    monkeypatch.setattr(runner.observer, "observe", original_observe)
    assert runner.collect(request, timeout_s=0.001) is None
    assert runner.authority.snapshot(now_ns=NOW)["request"] is None


def test_unknown_descendant_cannot_release_original_owner_or_request(
    execution, monkeypatch
):
    runner, workers, deadline = execution
    request = begin(runner, deadline)
    descendant = WorkerIdentity(999, 2, "fake")
    for worker in workers.values():
        worker.scope = SimpleNamespace(members=lambda: ())
        worker._known_members = set()
    workers["judge"]._known_members = {descendant}
    runner.observer.group_scope.members = lambda: ()
    states = {999: "unknown"}
    monkeypatch.setattr(
        owned.gpumgr, "process_state", lambda pid, recorded: states.get(pid, "gone")
    )
    proof = runner.observer.exit_proof(runner.authority.broker, runner.lease)
    assert not proof.descendants_exited and not proof.owned_gpu_released
    with pytest.raises(ValueError, match="complete exact"):
        runner.authority.release_after_exit(runner.runtime, proof, now_ns=NOW)
    assert runner.authority.snapshot(now_ns=NOW)["request"] == request
    assert runner.authority.snapshot(now_ns=NOW)["lease"] == runner.lease
    states[999] = "gone"
    proof = runner.observer.exit_proof(runner.authority.broker, runner.lease)
    runner.authority.release_after_exit(runner.runtime, proof, now_ns=NOW)
    assert runner.authority.snapshot(now_ns=NOW)["runtime"] is None
