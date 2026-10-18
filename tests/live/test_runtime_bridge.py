"""CPU/memory fake evidence only; no IPC, real flock, GPU or service calls."""

import threading
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from levi.domain import aeri
from levi.live.runtime_bridge import (
    AuthorityObservation,
    BackendAck,
    BackendCapability,
    DriverIdentity,
    DriverObservation,
    RuntimeBridge,
)
from levi.live.workload_broker import (
    ExitProof,
    IdleProof,
    SafeWindow,
    WorkerIdentity,
    WorkloadBroker,
)

NOW = 1_000_000_000
MS = 1_000_000
CLOCK = "host-mono:fake-boot"
PROFILE = "ab" * 32


class FakeObserver:
    def __init__(self, bridge):
        self.bridge = bridge
        self.over = {}
        self.exit_over = {}
        self.exited = False

    def driver(self, now):
        b = self.bridge
        return DriverObservation(b.driver, b.clock(), CLOCK, True, False, True, True)

    def observe(self, lease, worker):
        b = self.bridge
        return replace(
            AuthorityObservation(
                b.driver,
                worker,
                b.clock(),
                CLOCK,
                True,
                False,
                True,
                True,
                True,
                b.capability,
                worker,
            ),
            **self.over,
        )

    def window(self, episode, now):
        b = self.bridge
        return SafeWindow(
            b.broker.broker_id, "run", episode, PROFILE, now, NOW + 500 * MS, CLOCK
        )

    def exit_proof(self, lease, worker):
        return replace(self._exit_proof(lease, worker), **self.exit_over)

    def _exit_proof(self, lease, worker):
        return ExitProof(
            self.bridge.broker.broker_id,
            lease.lease_id,
            lease.lease_epoch,
            worker,
            self.bridge.clock(),
            CLOCK,
            self.exited,
            self.exited,
            self.exited,
        )


class FakeBackend:
    def __init__(self, capability):
        self.capability = capability
        self.release = threading.Event()
        self.release.set()
        self.entered = threading.Event()
        self.fail = False
        self.epochs = []

    def pause(self, worker, epoch, mode, timeout):
        self.entered.set()
        if self.fail or not self.release.wait(timeout):
            raise TimeoutError("fake backend remains computing")
        self.epochs.append((epoch, mode))
        return BackendAck(worker, epoch, self.capability, mode)

    def resume(self, worker, epoch, timeout):
        pass


def assembled_fake(*, bind=True, other_judge=False, admit=True):
    now = [NOW]
    broker = WorkloadBroker(run_id="run", clock_domain=CLOCK, profile_sha256=PROFILE)
    capability = BackendCapability("fake", "cd" * 32, True, True, True)
    driver = DriverIdentity("driver", 1, "session", WorkerIdentity(10, 20, "boot"))
    backend = FakeBackend(capability)
    bridge = RuntimeBridge(
        broker,
        observer=None,
        backend=backend,
        driver=driver,
        capability=capability,
        clock=lambda: now[0],
        barrier_timeout_s=0.02,
    )
    bridge.observer = FakeObserver(bridge)
    envelope = {
        "schema": aeri.SCHEMAS["runtime"],
        "minor": aeri.MINOR,
        "run_id": "run",
        "emitted_wall_ns": 0,
    }
    spec = aeri.WorkloadSpec(
        **envelope,
        kind="workload_spec",
        workload_id="resident",
        workload_class="background_annotation",
        requester="live",
        device_preference="gpu",
        memory_estimate_mib=1000,
        memory_source="measured",
        duration_p99_ms=100,
        duration_source="measured",
        preemptibility="request_boundary",
    )
    deadline = aeri.Deadline(
        due_ns=NOW + 500 * MS, clock_domain=CLOCK, budget_ms=500, hardness="hard"
    )
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
    if other_judge:
        queued = broker.submit(
            spec.model_copy(
                update={
                    "workload_id": "other-judge",
                    "workload_class": "judge_critical",
                }
            ),
            deadline,
            profile_sha256=PROFILE,
            now_ns=NOW,
        )
        assert queued is None
    admission = (
        spec,
        deadline,
        pressure,
        bridge.observer.window(None, NOW),
        IdleProof(broker.broker_id, NOW, CLOCK, True, True, True),
    )
    if not admit:
        return bridge, admission
    lease = bridge.admit_runtime(*admission)
    worker = WorkerIdentity(100, 123, "boot")
    if bind:
        bridge.bind_runtime(worker)
    return bridge, backend, now, lease, worker


class RuntimeBridgeTests(unittest.TestCase):
    def test_serial_requests_retain_original_physical_owner(self):
        bridge, backend, _, lease, _ = assembled_fake()
        first = bridge.begin_request(due_ns=NOW + 300 * MS)
        with self.assertRaises(ValueError):
            bridge.begin_request(due_ns=NOW + 300 * MS)
        bridge.drain(first, (), lambda: None)
        self.assertTrue(first.done.wait(0.2))
        self.assertTrue(first.permitted)
        second = bridge.begin_request(due_ns=NOW + 300 * MS)
        self.assertEqual(second.token.request_epoch, first.token.request_epoch + 1)
        self.assertEqual(
            bridge.broker.snapshot(now_ns=NOW)["active"],
            lease.model_dump(by_alias=True),
        )
        bridge.cancel(second)
        bridge.drain(second, (), lambda: None)
        self.assertTrue(second.done.wait(0.2))
        self.assertFalse(second.permitted)
        self.assertEqual(backend.epochs[-1], (2, "abort"))

    def test_client_done_keeps_busy_until_backend_ack(self):
        from levi.live.online import RequestOwnership

        bridge, backend, _, _, _ = assembled_fake()
        judge = SimpleNamespace(
            gpu=SimpleNamespace(runtime_bridge=bridge, online_done=lambda: None),
            _busy=threading.Lock(),
            _owner=None,
            _stop_reason=None,
        )
        judge._busy.acquire()
        local = RequestOwnership(judge, "caller")
        local.runtime_request = bridge.begin_request(due_ns=NOW + 300 * MS)
        local.admitted = True
        backend.fail = True
        local.cancel()
        local.seal_workers()
        local.finished()
        drainer = local.runtime_request.drainer
        if drainer is not None:
            drainer.join(0.2)
        self.assertTrue(judge._busy.locked())
        self.assertIsNotNone(bridge.ownership.snapshot(now_ns=NOW)["request"])
        backend.fail = False
        bridge.retry_drain()
        self.assertTrue(local.runtime_request.done.wait(0.2))
        local.runtime_request.drainer.join(
            0.2
        ) if local.runtime_request.drainer else None
        self.assertFalse(judge._busy.locked())
        self.assertFalse(local.runtime_request.permitted)

    def test_all_real_local_threads_must_exit_before_barrier(self):
        bridge, backend, _, _, _ = assembled_fake()
        backend.entered.clear()
        request = bridge.begin_request(due_ns=NOW + 300 * MS)
        release = threading.Event()
        worker = threading.Thread(target=lambda: release.wait(0.2))
        worker.start()
        bridge.drain(request, (worker,), lambda: None)
        self.assertFalse(backend.entered.is_set())
        release.set()
        self.assertTrue(request.done.wait(0.2))
        self.assertTrue(request.permitted)
        self.assertFalse(worker.is_alive())

    def test_physical_expiry_does_not_release_or_renew(self):
        bridge, _, now, lease, _ = assembled_fake()
        now[0] = lease.expires_ns
        self.assertFalse(bridge.available())
        with self.assertRaises(ValueError):
            bridge.begin_request(due_ns=now[0] + 100 * MS)
        self.assertIsNotNone(bridge.broker.snapshot(now_ns=now[0])["active"])
        self.assertFalse(bridge.release_after_exit())

    def test_identity_driver_flock_unknown_fail_closed(self):
        for changed in (
            {"exact_flock_held": None},
            {"driver_active": None},
            {"gpu_resource_held": False},
            {"driver_cancelled": True},
            {"process_running": None},
            {"endpoint_owner": WorkerIdentity(101, 123, "boot")},
            {
                "driver": DriverIdentity(
                    "driver", 2, "session", WorkerIdentity(10, 20, "boot")
                )
            },
            {"worker": WorkerIdentity(100, 124, "boot")},
        ):
            with self.subTest(changed=changed):
                bridge, _, _, _, _ = assembled_fake()
                bridge.observer.over = changed
                self.assertFalse(bridge.available())
                self.assertIsNotNone(bridge.broker.snapshot(now_ns=NOW)["active"])

    def test_deadline_at_backend_ack_discards_result_but_resolves_slot(self):
        bridge, backend, now, _, _ = assembled_fake()
        request = bridge.begin_request(due_ns=NOW + 100 * MS)
        original = backend.pause

        def late(*args):
            now[0] += 101 * MS
            return original(*args)

        backend.pause = late
        bridge.drain(request, (), lambda: None)
        self.assertTrue(request.done.wait(0.2))
        self.assertFalse(request.permitted)
        self.assertIsNone(bridge.ownership.snapshot(now_ns=now[0])["request"])
        self.assertIsNotNone(bridge.broker.snapshot(now_ns=now[0])["active"])

    def test_sleep_revocation_never_releases_physical_owner(self):
        bridge, _, _, _, _ = assembled_fake()
        bridge.close()
        self.assertFalse(bridge.available())
        self.assertFalse(bridge.release_after_exit())
        self.assertIsNotNone(bridge.broker.snapshot(now_ns=NOW)["active"])

    def test_strict_exit_clears_failed_backend_slot(self):
        bridge, backend, _, _, _ = assembled_fake()
        request = bridge.begin_request(due_ns=NOW + 300 * MS)
        backend.fail = True
        bridge.drain(request, (), lambda: None)
        drainer = request.drainer
        if drainer is not None:
            drainer.join(0.2)
        bridge.observer.exited = True
        self.assertTrue(bridge.release_after_exit())
        self.assertTrue(request.done.is_set())
        self.assertFalse(request.permitted)
        self.assertIsNone(bridge.broker.snapshot(now_ns=NOW)["active"])

    def test_exit_refused_while_local_request_worker_is_alive(self):
        bridge, _, _, _, _ = assembled_fake()
        request = bridge.begin_request(due_ns=NOW + 300 * MS)
        gate = threading.Event()
        local = threading.Thread(target=lambda: gate.wait(2))
        local.start()
        try:
            bridge.drain(request, (local,), lambda: None)
            bridge.observer.exited = True
            self.assertFalse(bridge.release_after_exit())
            self.assertIsNotNone(bridge.broker.snapshot(now_ns=NOW)["active"])
            self.assertFalse(request.done.is_set())
        finally:
            gate.set()
            local.join(2)
        self.assertTrue(request.done.wait(0.5))
        bridge.observer.exited = True
        self.assertTrue(bridge.release_after_exit())
        self.assertIsNone(bridge.broker.snapshot(now_ns=NOW)["active"])

    def test_exit_proof_with_unknown_or_false_flags_never_releases(self):
        for flag in ("process_exited", "descendants_exited", "owned_gpu_released"):
            for value in (None, False):
                with self.subTest(flag=flag, value=value):
                    bridge, _, _, _, _ = assembled_fake()
                    bridge.observer.exited = True
                    bridge.observer.exit_over = {flag: value}
                    self.assertFalse(bridge.release_after_exit())
                    self.assertIsNotNone(bridge.broker.snapshot(now_ns=NOW)["active"])
                    bridge.observer.exit_over = {}
                    self.assertTrue(bridge.release_after_exit())

    def test_admit_never_binds_another_workloads_lease(self):
        bridge, admission = assembled_fake(other_judge=True, admit=False)
        with self.assertRaises(ValueError):
            bridge.admit_runtime(*admission)
        self.assertIsNone(bridge._lease)
        snapshot = bridge.broker.snapshot(now_ns=NOW)
        self.assertIsNone(snapshot["active"])
        self.assertEqual(snapshot["queued"], 1)  # other-judge is not dropped
        self.assertFalse(bridge.available())
        with self.assertRaises(ValueError):
            bridge.bind_runtime(WorkerIdentity(100, 123, "boot"))
        neighbour = bridge.broker.admit(
            admission[2], admission[3], now_ns=bridge.clock()
        )
        self.assertIsInstance(neighbour, aeri.ResourceLease)
        self.assertEqual(neighbour.workload_id, "other-judge")

    def test_admit_withdraws_own_request_when_neighbour_return_proof_fails(self):
        bridge, admission = assembled_fake(other_judge=True, admit=False)
        original = bridge.broker.return_unstarted

        def reject_proof(lease, proof, *, now_ns):
            return original(
                lease, replace(proof, clock_domain="foreign-clock"), now_ns=now_ns
            )

        with (
            patch.object(bridge.broker, "return_unstarted", side_effect=reject_proof),
            patch.object(bridge.broker, "cancel", wraps=bridge.broker.cancel) as cancel,
        ):
            with self.assertRaises(ValueError):
                bridge.admit_runtime(*admission)
            cancel.assert_called_once_with("resident", now_ns=NOW)
        snapshot = bridge.broker.snapshot(now_ns=NOW)
        self.assertEqual(snapshot["queued"], 0)
        self.assertEqual(snapshot["active"]["workload_id"], "other-judge")
        self.assertIsNone(snapshot["worker"])
        self.assertIsNone(bridge._lease)
        self.assertFalse(bridge.available())

    def test_p0_close_and_exit_attempt_do_not_wait_for_shared_mutex(self):
        bridge, _, _, _, _ = assembled_fake()
        entered, release = threading.Event(), threading.Event()

        def hold():
            with bridge._mutex:
                entered.set()
                release.wait(0.2)

        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(entered.wait(0.2))
        began = time.monotonic()
        bridge.close()
        self.assertFalse(bridge.release_after_exit())
        self.assertLess(time.monotonic() - began, 0.05)
        release.set()
        thread.join(0.2)

    def test_unsupported_capability_has_no_fallback(self):
        with self.assertRaises(ValueError):
            BackendCapability("fake", "cd" * 32, False, True, True)

    def test_legacy_request_ownership_without_bridge_keeps_existing_release(self):
        from levi.live.online import RequestOwnership

        calls = []
        judge = SimpleNamespace(
            gpu=SimpleNamespace(online_done=lambda: calls.append("done")),
            _busy=threading.Lock(),
            _owner=None,
            _stop_reason=None,
        )
        judge._busy.acquire()
        local = RequestOwnership(judge, "legacy")
        local.admitted = True
        local.add_worker()
        local.finished()
        self.assertTrue(judge._busy.locked())
        local.finished()
        self.assertFalse(judge._busy.locked())
        self.assertEqual(calls, ["done"])

    def test_controller_strict_manual_and_background_paths_have_no_fallback(self):
        from levi.live.controller import Controller

        bridge, _, _, _, _ = assembled_fake()
        controller = Controller.__new__(Controller)
        controller.runtime_bridge = bridge
        controller.config = SimpleNamespace(effective_gpu_mode=lambda: "manual")
        controller.running = True
        controller.worker = controller.orphan = None
        controller._gpu_mutex = threading.Lock()
        controller.vllm = SimpleNamespace(mine=lambda: True)
        answer = controller.online_admit(
            0, ownership=SimpleNamespace(), due_ns=NOW + MS
        )
        self.assertFalse(answer[0])
        self.assertEqual(answer[1], "runtime_unavailable")
        controller._spawn("never-start")
        self.assertEqual(controller.decision.code, "runtime_unavailable")

    def test_controller_stop_never_signals_a_changed_runtime(self):
        from levi.live.controller import Controller

        bridge, _, _, _, _ = assembled_fake()
        calls = []
        controller = Controller.__new__(Controller)
        controller.runtime_bridge = bridge
        controller.vllm = SimpleNamespace(
            _record=lambda: {
                "pid": 101,
                "identity": {"start_ticks": "123", "boot": "boot"},
            },
            stop=lambda: calls.append("stop"),
        )
        controller.lock = SimpleNamespace(release=lambda: calls.append("release"))
        self.assertFalse(controller._stop_vllm())
        self.assertEqual(calls, [])
        self.assertIsNotNone(bridge.broker.snapshot(now_ns=NOW)["active"])

    def test_ordinary_judge_success_waits_for_backend_ack(self):
        from levi.live import online

        bridge, backend, _, _, _ = assembled_fake()
        judge = online.Judge.__new__(online.Judge)
        judge.config = SimpleNamespace(
            online=SimpleNamespace(timeout_s=0.03),
            vllm=SimpleNamespace(served_model="fake"),
        )

        def admit(now, *, ownership, due_ns, episode_id=None):
            ownership.runtime_request = bridge.begin_request(due_ns=due_ns)
            return True, None, None

        judge.gpu = SimpleNamespace(
            runtime_bridge=bridge, online_admit=admit, online_done=lambda: None
        )
        judge.spec = None
        judge.identity = {"id": "fake", "version": 1}
        judge._busy, judge._owner, judge._stop_reason = threading.Lock(), None, None
        judge.record = lambda *args: None
        judge.log = lambda *args: None
        judge._run = lambda *args: {"status": "ok", "answer": {}, "outcome": True}
        request = SimpleNamespace(episode={}, images=[], start_images=[])
        backend.fail = True
        with patch.object(online, "parse", return_value=request):
            _, result = judge.handle(b"fake")
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(judge._busy.locked())
        backend.fail = False
        bridge.retry_drain()
        pending = bridge._request
        if pending is not None:
            self.assertTrue(pending.done.wait(0.2))

    def test_aeri_probe_success_waits_for_same_backend_ack(self):
        from levi.live import aeri_online, online

        bridge, backend, _, _, _ = assembled_fake()
        judge = online.Judge.__new__(online.Judge)
        judge.config = SimpleNamespace(
            live_dir=__import__("pathlib").Path("unused-fake-live")
        )
        judge._busy, judge._owner, judge._stop_reason = threading.Lock(), None, None
        judge._aeri_specs = {
            "fake": SimpleNamespace(
                value={
                    "run_id": "run",
                    "provider": {},
                    "deadlines_ns": {"probe": 30 * MS},
                }
            ),
        }
        judge.provider = lambda: SimpleNamespace(
            model_dump=lambda **kw: {},
            model_digest="fake-model",
        )

        def admit(now, *, ownership, due_ns, episode_id=None):
            ownership.runtime_request = bridge.begin_request(due_ns=due_ns)
            return True, None, None

        judge.gpu = SimpleNamespace(
            runtime_bridge=bridge,
            online_admit=admit,
            online_check=lambda now: (True, None, None),
            online_done=lambda: None,
        )
        backend.fail = True
        result = aeri_online.query(judge, "fake", {}, [], probe=True)
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(judge._busy.locked())
        backend.fail = False
        previous = bridge._request
        bridge.retry_drain()
        if previous is not None:
            self.assertTrue(previous.done.wait(0.2))
            drainer = previous.drainer
            if drainer is not None:
                drainer.join(0.2)
        self.assertFalse(judge._busy.locked())
        result = aeri_online.query(judge, "fake", {}, [], probe=True)
        self.assertEqual(result["status"], "ok")

    def test_pause_adapter_only_uses_frozen_synchronized_endpoints(self):
        from levi.live.runtime_backend import VllmPauseBackend

        capability = BackendCapability("fake", "cd" * 32, True, True, True)
        calls = []

        class FakeTransport:
            def __init__(self, config, **kwargs):
                pass

            def acknowledged(self, path):
                calls.append(path)

        backend = VllmPauseBackend(None, capability, transport_factory=FakeTransport)
        worker = WorkerIdentity(100, 123, "boot")
        ack = backend.pause(worker, 7, "wait", 1)
        self.assertEqual(ack, BackendAck(worker, 7, capability, "wait"))
        backend.resume(worker, 8, 1)
        self.assertEqual(calls, ["/pause?mode=wait&clear_cache=false", "/resume"])
        with self.assertRaises(ValueError):
            backend.pause(worker, 7, "keep", 1)

    def test_gate_window_changed_during_barrier_discards_success(self):
        bridge, _, _, _, _ = assembled_fake()
        request = bridge.begin_request(due_ns=NOW + 300 * MS)
        bridge.observer.window = lambda *args: None
        bridge.drain(request, (), lambda: None)
        self.assertTrue(request.done.wait(0.2))
        self.assertFalse(request.permitted)
        self.assertIsNone(bridge.ownership.snapshot(now_ns=NOW)["request"])
        self.assertIsNotNone(bridge.broker.snapshot(now_ns=NOW)["active"])

    def test_failed_initial_backend_barrier_retains_bound_actual_worker(self):
        bridge, backend, _, _, worker = assembled_fake(bind=False)
        backend.fail = True
        with self.assertRaises(TimeoutError):
            bridge.bind_runtime(worker)
        self.assertFalse(bridge.available())
        self.assertEqual(bridge.broker.snapshot(now_ns=NOW)["worker"], worker)
        self.assertFalse(bridge.release_after_exit())
        bridge.observer.exited = True
        self.assertTrue(bridge.release_after_exit())

    def test_old_backend_ack_epoch_keeps_slot_until_valid_late_ack(self):
        bridge, backend, _, _, worker = assembled_fake()
        request = bridge.begin_request(due_ns=NOW + 300 * MS)
        original = backend.pause
        backend.pause = lambda *args: BackendAck(worker, 0, bridge.capability, "wait")
        bridge.drain(request, (), lambda: None)
        drainer = request.drainer
        if drainer is not None:
            drainer.join(0.2)
        self.assertFalse(request.done.is_set())
        self.assertIsNotNone(bridge.ownership.snapshot(now_ns=NOW)["request"])
        backend.pause = original
        bridge.retry_drain()
        self.assertTrue(request.done.wait(0.2))
        self.assertFalse(request.permitted)

    def test_worker_registration_is_sealed_before_any_early_completion_drains(self):
        from levi.live.online import RequestOwnership

        bridge, backend, _, _, _ = assembled_fake()
        judge = SimpleNamespace(
            gpu=SimpleNamespace(runtime_bridge=bridge, online_done=lambda: None),
            _busy=threading.Lock(),
            _owner=None,
            _stop_reason=None,
        )
        judge._busy.acquire()
        local = RequestOwnership(judge, "fast-worker")
        local.runtime_request = bridge.begin_request(due_ns=NOW + 300 * MS)
        local.admitted = True
        backend.entered.clear()
        first = threading.Thread(target=local.finished)
        local.add_worker(first)
        first.start()
        first.join(0.2)
        self.assertFalse(backend.entered.is_set())
        self.assertIsNotNone(bridge._request)
        second = threading.Thread(target=local.finished)
        local.add_worker(second)
        second.start()
        local.seal_workers()
        self.assertTrue(local.commit(time.monotonic_ns() + 200 * MS))
        local.finished()
        self.assertFalse(judge._busy.locked())
        self.assertFalse(second.is_alive())


if __name__ == "__main__":
    unittest.main()
