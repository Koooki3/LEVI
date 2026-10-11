"""Complete Orchestrator + original group RuntimeOwnership, fake observations.

LEVI_JOINT_KERNEL_ROOT optionally points at a separately frozen kernel checkout.
Its exact source files are imported read-only, never replaced with FakeAuthority.
The test keeps AERI episode/journal schemas from this branch and loads resource
schemas in an isolated module; the real kernels use their own matching schemas.
In an integrated tree the ordinary production modules are used directly.
"""

import importlib.util
import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path

# The standalone entry also resolves the existing in-process AERI fixture.
HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent))
from aeri_harness import config, results, rig

from levi.automatic import state_machine as sm
from levi.automatic.adapters import events as ev
from levi.automatic.intervention import (
    InferenceCycle,
    InterventionHooks,
    PhysicalHoldProof,
    hold_contract_sha256,
)
from levi.automatic.orchestrator import Orchestrator
from levi.automatic.reset_manager import SettleConfig
from levi.automatic.termination import TerminationConfig
from levi.domain import aeri


def source_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_kernel():
    selected = os.environ.get("LEVI_JOINT_KERNEL_ROOT")
    if not selected:
        if not hasattr(aeri, "ResidentGroupSpec"):
            return None
        from levi.live import runtime_ownership, workload_broker

        return aeri, workload_broker, runtime_ownership
    root = Path(selected).resolve()
    schema = source_module(
        "levi.domain._joint_resource_schema", root / "levi/domain/aeri.py"
    )
    broker = source_module(
        "levi.live.workload_broker", root / "levi/live/workload_broker.py"
    )
    broker.aeri = schema  # exact dependency domain, no mocked kernel decisions
    runtime = source_module(
        "levi.live.runtime_ownership", root / "levi/live/runtime_ownership.py"
    )
    runtime.aeri = schema
    return schema, broker, runtime


KERNEL = load_kernel()
HOLD = SettleConfig(1_000_000, 2_000_000, 100_000_000, 0.001, 0.01, 0.01)


@unittest.skipUnless(KERNEL is not None, "needs frozen resident-group kernel")
class OrchestratorRuntimeTests(unittest.TestCase):
    def make(
        self,
        *,
        strategy="human_assisted",
        episodes=1,
        scene_script=None,
        goal_script=None,
        events=True,
        duration_ms=500_000,
    ):
        import tempfile

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        cfg = config(
            episodes=episodes,
            reset_strategy=strategy,
            termination=TerminationConfig(
                inference_mode="event_pause_judge",
                physical_hold=HOLD,
                min_steps=5,
                settle_steps=3,
                cooldown_steps=5,
            ),
        )
        r = rig(Path(self.temporary.name) / "run", cfg=cfg, create=False)
        r.scene = ev.FakeSceneAssessor(scene_script or [], r.clock, cfg.run_id)
        r.verifier = ev.FakeGoalVerifier(
            goal_script or [], r.clock, cfg.run_id, default={"decision": "confirmed"}
        )
        r.events = ev.FakeEventStream(
            {
                "forward": [{"step": 8, "event_type": "object_settled"}],
                "reset": [{"step": 4, "event_type": "object_settled"}],
            }
            if events
            else {},
            r.clock,
            cfg.run_id,
        )
        domain, b, rt = KERNEL
        now, clock, profile = r.clock.now(), r.clock.domain, "ab" * 32
        broker = b.WorkloadBroker(
            run_id=cfg.run_id, clock_domain=clock, profile_sha256=profile
        )
        broker.prove_idle(
            b.IdleProof(broker.broker_id, now, clock, True, True, True), now_ns=now
        )
        members = tuple(
            rt.OwnedMember(role, b.WorkerIdentity(100 + i, 1, "fake"), role + ":owned")
            for i, role in enumerate(("forward", "judge", "reset"))
        )
        group = rt.OwnedGroup("test-owned", b.WorkerIdentity(99, 1, "fake"), members)
        envelope = {
            "schema": domain.SCHEMAS["runtime"],
            "minor": domain.MINOR,
            "run_id": cfg.run_id,
            "emitted_wall_ns": 0,
        }
        workload = domain.WorkloadSpec(
            **envelope,
            kind="workload_spec",
            workload_id="models",
            workload_class="resident_group",
            requester="c",
            device_preference="gpu",
            memory_estimate_mib=1000,
            memory_source="declared",
            duration_p99_ms=10,
            duration_source="declared",
            preemptibility="request_boundary",
            resident_group=domain.ResidentGroupSpec(
                group_id=group.group_id,
                members=[
                    {"role": m.role, "capability_id": m.capability_id} for m in members
                ],
            ),
        )
        due = domain.Deadline(
            due_ns=now + duration_ms * 1_000_000,
            clock_domain=clock,
            budget_ms=duration_ms,
            hardness="hard",
        )
        pressure = domain.ResourcePressure(
            **envelope,
            kind="pressure",
            observed_ns=now,
            clock_domain=clock,
            source="fake",
            free_mib=5000,
            vllm_state="awake",
            gate_open=True,
            gate_code="open",
            policy_server_seen=False,
            age_ms=0,
        )
        resident_window = b.SafeWindow(
            broker.broker_id, cfg.run_id, None, profile, now, due.due_ns, clock
        )
        self.assertIsNone(
            broker.submit(workload, due, profile_sha256=profile, now_ns=now)
        )
        lease = broker.admit(pressure, resident_window, now_ns=now)
        group_observation = rt.GroupObservation(
            group,
            tuple(rt.MemberObservation(m, True, True, True, True) for m in members),
            "fake-driver",
            1,
            "fake-session",
            True,
            True,
            True,
        )
        observed = rt.RuntimeObservation(
            lease.lease_id,
            lease.lease_epoch,
            group.supervisor,
            now,
            clock,
            True,
            True,
            True,
            True,
            group_observation,
        )
        owner = rt.RuntimeOwnership(broker)
        runtime = owner.bind_group(lease, group, observed, now_ns=now)
        self.grants, self.calls = [], []
        original_begin = owner.begin_role_request

        def begin(*args, **kwargs):
            token = original_begin(*args, **kwargs)
            self.grants.append(token)
            return token

        owner.begin_role_request = begin
        self.bad_window = self.bad_barrier = self.expire = False

        def observe_runtime():
            if self.expire:
                self.expire = False
                r.clock.advance(max(1, lease.expires_ns - r.clock.now() + 1))
            return replace(observed, observed_ns=r.clock.now())

        def physical(episode, epoch, deadline):
            start = r.clock.now()
            r.clock.advance(HOLD.stable_window_ns)
            return PhysicalHoldProof(
                cfg.run_id,
                episode,
                epoch,
                cfg.plan_sha256,
                hold_contract_sha256(HOLD),
                clock,
                start,
                r.clock.now(),
                deadline,
                True,
                True,
                True,
            )

        def role_window(role, episode, epoch, authorization, deadline):
            return rt.RoleWindow(
                broker.broker_id,
                cfg.run_id,
                episode,
                profile,
                r.clock.now(),
                deadline,
                clock,
                role,
                epoch - 1 if self.bad_window and role != "judge" else epoch,
                authorization,
            )

        def stop_proof(request, deadline):
            return rt.RequestStopProof(
                request, r.clock.now(), clock, True, not self.bad_barrier
            )

        def close(episode, old_epoch, new_epoch):
            r.policy.quiesce(policy_epoch=old_epoch)
            return True

        cycle = InferenceCycle(
            authority=owner,
            runtime=runtime,
            clock=r.clock,
            run_id=cfg.run_id,
            plan_sha256=cfg.plan_sha256,
            condition_sha256=hold_contract_sha256(HOLD),
            hooks=InterventionHooks(
                close,
                lambda *args: True,
                physical,
                observe_runtime,
                role_window,
                stop_proof,
                lambda *args: True,
            ),
        )

        def require_slot(provider, method, role):
            original = getattr(provider, method)

            def wrapped(request):
                token = owner.snapshot(now_ns=r.clock.now())["request"]
                self.assertIsNotNone(token)
                self.assertEqual(
                    token.role,
                    role if role else aeri.episode_parts(request["episode_id"])[1],
                )
                self.assertEqual(token.episode_id, request["episode_id"])
                self.calls.append((token.role, token.episode_id, token.policy_epoch))
                return original(request)

            setattr(provider, method, wrapped)

        require_slot(r.scene, "submit", "judge")
        require_slot(r.verifier, "submit", "judge")
        require_slot(r.policy, "request", None)
        r.orch = Orchestrator.create(r.directory, cfg, intervention=cycle, **r.parts())
        self.addCleanup(r.orch.close)
        r.owner, r.lease, r.cycle = owner, lease, cycle
        return r

    def resume(self, r, command):
        result = r.orch.resume(
            command,
            expected_seq=r.orch.journal.next_seq,
            environment_handled=True,
            health_rechecked=True,
        )
        self.assertTrue(result.ok)

    def test_scene_epoch_is_durable_before_any_policy_episode_starts(self):
        r = self.make()
        first = f"{r.orch.config.run_id}.forward.0001"
        second = f"{r.orch.config.run_id}.reset.0001"
        self.assertEqual(r.orch._assess("initial_state", first), "ready")
        reserved = self.grants[-1].policy_epoch
        notes = [
            e
            for e in r.orch.journal.events
            if e.record == "note" and e.note.code == "inference_epoch_reserved"
        ]
        self.assertEqual(notes[-1].policy_epoch, reserved)
        self.assertEqual(notes[-1].note.detail, first)
        r.orch.policy_epoch = 0
        r.orch._read_back()
        self.assertEqual(r.orch.policy_epoch, reserved)
        self.assertEqual(r.orch._assess("initial_state", second), "ready")
        self.assertGreater(self.grants[-1].policy_epoch, reserved)
        self.assertEqual(r.robot.step_calls, 0)
        self.assertEqual(r.policy.requests, 0)
        self.assertEqual(sm.check_journal(r.orch.journal.events), [])

    def test_initial_reset_required_changes_episode_with_new_epoch(self):
        r = self.make(
            strategy="single_reset_policy",
            scene_script=[{"decision": "reset_required"}],
        )
        self.assertEqual(r.orch.run(), "WAIT_HUMAN")
        self.assertGreater(r.robot.step_calls, 0)
        reset_grants = [g for g in self.grants if g.role == "reset"]
        self.assertTrue(reset_grants)
        self.assertGreater(reset_grants[0].policy_epoch, self.grants[0].policy_epoch)
        self.assertEqual(results(r.orch)[0].task_outcome, "success")
        self.assertEqual(sm.check_journal(r.orch.journal.events), [])

    def test_no_reset_multiple_episodes_use_current_scene_and_policy_epochs(self):
        r = self.make(episodes=2)
        for number in (1, 2):
            self.assertEqual(r.orch.run(), "WAIT_HUMAN")
            self.assertEqual(r.orch.completed_forward, number)
            r.orch._read_back()  # durable counter/epoch reconstruction, not fake runtime recreation
            self.assertEqual(r.orch.completed_forward, number)
            self.resume(r, f"resume-{number}")
        self.assertEqual(r.orch.run(), "COMPLETED")
        self.assertEqual(len(results(r.orch)), 2)
        self.assertEqual(r.robot.step_calls, 22)
        self.assertEqual(r.robot.home_calls, 0)
        self.assertFalse(any(g.role == "reset" for g in self.grants))
        for previous, current in zip(self.grants, self.grants[1:]):
            self.assertGreater(current.request_epoch, previous.request_epoch)
            if previous.episode_id != current.episode_id:
                self.assertGreater(current.policy_epoch, previous.policy_epoch)
        self.assertEqual(sm.check_journal(r.orch.journal.events), [])

    def test_not_goal_discards_buffer_and_resumes_new_logged_policy_epoch(self):
        r = self.make(goal_script=[{"decision": "rejected"}, {"decision": "confirmed"}])
        self.assertEqual(r.orch.run(), "WAIT_HUMAN")
        resumed = [
            e
            for e in r.orch.journal.events
            if e.record == "committed" and e.reason == "inference_resumed"
        ]
        self.assertEqual(len(resumed), 1)
        self.assertTrue(
            any(
                g.role == "forward" and g.policy_epoch == resumed[0].policy_epoch
                for g in self.grants
            )
        )
        self.assertGreater(r.policy.requests, 2)
        self.assertEqual(sm.check_journal(r.orch.journal.events), [])

    def test_stop_during_policy_revokes_actual_slot_without_clearing_ownership(self):
        r = self.make()
        collect = r.policy.collect
        stopped = []

        def stop(ticket, **kwargs):
            if not stopped:
                stopped.append(r.orch.stop("stop-owned-policy"))
            return collect(ticket, **kwargs)

        r.policy.collect = stop
        self.assertEqual(r.orch.run(), "FAULT_LOCKED")
        self.assertTrue(stopped[0].ok)
        self.assertEqual(r.robot.step_calls, 0)
        self.assertIsNone(r.orch.fence.current)
        self.assertIsNotNone(r.owner.snapshot(now_ns=r.clock.now())["request"])
        self.assertEqual(results(r.orch)[0].task_outcome, "unknown")

    def test_expired_resident_permission_never_sends_a_policy_request(self):
        r = self.make()
        initial_submit = r.scene.submit

        def expire_after_scene(request):
            result = initial_submit(request)
            self.expire = True
            return result

        # Expire at initial scene finish, not during a live backend call.
        r.scene.submit = expire_after_scene
        self.assertEqual(r.orch.run(), "FAULT_LOCKED")
        self.assertEqual(r.policy.requests, 0)
        self.assertEqual(r.robot.step_calls, 0)

    def test_old_policy_role_window_is_rejected_by_real_kernel(self):
        r = self.make()
        self.bad_window = True
        self.assertEqual(r.orch.run(), "FAULT_LOCKED")
        self.assertEqual(r.policy.requests, 0)
        self.assertEqual(r.robot.step_calls, 0)

    def test_backend_worker_exit_unproven_retains_real_kernel_slot(self):
        r = self.make()
        self.bad_barrier = True
        self.assertEqual(r.orch.run(), "FAULT_LOCKED")
        self.assertIsNotNone(r.owner.snapshot(now_ns=r.clock.now())["request"])
        self.assertEqual(r.robot.step_calls, 0)


if __name__ == "__main__":
    unittest.main()
