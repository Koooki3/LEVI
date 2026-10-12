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
    InterventionUnproven,
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
        "levi.live._joint_workload_broker", root / "levi/live/workload_broker.py"
    )
    broker.aeri = schema  # exact dependency domain, no mocked kernel decisions
    runtime = source_module(
        "levi.live._joint_runtime_ownership", root / "levi/live/runtime_ownership.py"
    )
    runtime.aeri = schema
    # The isolated runtime must use its matching broker classes, while
    # production imports keep their canonical module and class identities.
    for name in (
        "ExitProof",
        "SafeWindow",
        "WorkerIdentity",
        "WorkloadBroker",
        "_copy",
    ):
        setattr(runtime, name, getattr(broker, name))
    return schema, broker, runtime


KERNEL = load_kernel()
# The fixture's forward event is at step 8. A candidate pauses at once (no
# min_steps/settle_steps/cooldown_steps delay), so each episode executes
# exactly 8 actions before the pause (JOINT-INFERENCE-PROPOSAL: no new action
# after a candidate).
CANDIDATE_STEP = 8
# A rejected candidate is spent: the next pause needs a newer event, so
# not-goal chains get one at step 14 (never delivered once step 8 is confirmed).
SECOND_CANDIDATE_STEP = 14
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
                "forward": [
                    {"step": step, "event_type": "object_settled"}
                    for step in (CANDIDATE_STEP, SECOND_CANDIDATE_STEP)
                ],
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
        self.assertEqual(r.robot.step_calls, 2 * CANDIDATE_STEP)
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
        # one chunk before the pause at step 8; the buffer is discarded and
        # exactly one new chunk is requested for the pause at step 14
        self.assertEqual(r.policy.requests, 2)
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

    def test_every_proof_hook_exception_latches_cycle_across_human_resume(self):
        for hook in (
            "observe_hold",
            "role_window",
            "observe_runtime",
            "stop_proof",
            "stop_admission",
            "resume_admission",
            "prepare_evidence",
        ):
            with self.subTest(hook=hook):
                r = self.make()
                original = r.cycle.hooks

                def unproven(*args, hook=hook):
                    raise RuntimeError("trusted hook failed: " + hook)

                r.cycle.hooks = replace(original, **{hook: unproven})
                try:
                    r.orch.run()
                except (RuntimeError, InterventionUnproven):
                    pass  # caller may propagate, but permission must be latched
                self.assertEqual(r.orch.state, "FAULT_LOCKED")
                self.assertTrue(r.cycle.failed)
                self.assertIsNone(r.orch.fence.current)
                prior_steps, prior_grants = r.robot.step_calls, len(self.grants)
                snapshot = r.owner.snapshot(now_ns=r.clock.now())
                self.assertEqual(snapshot["runtime"], r.cycle.runtime)
                if hook == "stop_proof":
                    self.assertIsNotNone(snapshot["request"])
                    self.assertTrue(snapshot["request_revoked"])
                r.cycle.hooks = original
                self.resume(r, "resume-after-" + hook)
                self.assertEqual(r.orch.run(), "FAULT_LOCKED")
                self.assertTrue(r.cycle.failed)
                self.assertEqual(r.robot.step_calls, prior_steps)
                self.assertEqual(len(self.grants), prior_grants)
                self.assertEqual(
                    r.owner.snapshot(now_ns=r.clock.now())["request"],
                    snapshot["request"],
                )
                self.assertEqual(sm.check_journal(r.orch.journal.events), [])

    def test_runtime_observation_exception_after_grant_retains_revoked_slot(self):
        r = self.make()
        original = r.cycle.hooks
        observations = []

        def runtime_observation():
            observations.append(1)
            if len(observations) == 2:
                raise RuntimeError("fresh backend observation failed")
            return original.observe_runtime()

        r.cycle.hooks = replace(original, observe_runtime=runtime_observation)
        self.assertEqual(r.orch.run(), "FAULT_LOCKED")
        self.assertTrue(r.cycle.failed)
        snapshot = r.owner.snapshot(now_ns=r.clock.now())
        self.assertIsNotNone(snapshot["request"])
        self.assertTrue(snapshot["request_revoked"])
        r.cycle.hooks = original
        self.resume(r, "resume-after-barrier-observation")
        self.assertEqual(r.orch.run(), "FAULT_LOCKED")
        self.assertEqual(r.robot.step_calls, 0)
        self.assertEqual(len(self.grants), 1)
        self.assertEqual(
            r.owner.snapshot(now_ns=r.clock.now())["request"], snapshot["request"]
        )

    def test_orchestrator_containment_interrupts_existing_slot_before_backend_wait(
        self,
    ):
        r = self.make()
        captured = []

        def escaped_failure():
            r.orch._contain(RuntimeError("outside model callback"))
            captured.append((r.cycle.failed, r.owner.snapshot(now_ns=r.clock.now())))
            return "must not publish"

        with self.assertRaises(InterventionUnproven):
            r.cycle.invoke(
                "forward",
                f"{r.orch.config.run_id}.forward.0001",
                1,
                deadline_ns=r.clock.now() + 100_000_000,
                call=escaped_failure,
                interrupted=lambda: False,
            )
        self.assertEqual(r.orch.state, "FAULT_LOCKED")
        self.assertTrue(captured[0][0])
        self.assertEqual(captured[0][1]["runtime"], r.cycle.runtime)
        self.assertIsNotNone(captured[0][1]["request"])
        self.assertTrue(captured[0][1]["request_revoked"])
        self.assertEqual(
            r.owner.snapshot(now_ns=r.clock.now())["request"], captured[0][1]["request"]
        )
        self.assertEqual(r.robot.step_calls, 0)

    def test_real_session_listener_keeps_background_gate_closed_during_judging(self):
        import json

        from levi.automatic.recorder import SessionFiles
        from levi.live import config as live_config
        from levi.live import gpumgr, sessions

        for role in ("forward", "reset"):
            with self.subTest(role=role):
                r = self.make(
                    strategy="human_assisted"
                    if role == "forward"
                    else "single_reset_policy",
                    scene_script=[
                        {"decision": "ready" if role == "forward" else "reset_required"}
                    ],
                )
                root = r.directory / "fake-sessions"
                listener = SessionFiles(
                    root,
                    run_id=r.orch.config.run_id,
                    group="fake",
                    folders={"forward": "forward-task", "reset": "reset-task"},
                    pid=0,
                    wall=lambda: 1_800_000_000.0,
                )
                r.orch.listener = listener
                submit = r.verifier.submit
                checked = []
                config = live_config.Config()
                config.gpu.mode = "timeshare"

                def verify_with_real_session_gate(
                    request,
                    *,
                    listener=listener,
                    role=role,
                    root=root,
                    config=config,
                    checked=checked,
                    submit=submit,
                ):
                    active = json.loads(listener.path(role).read_text())
                    self.assertEqual(active["aeri"]["state"], role.upper() + "_JUDGING")
                    self.assertEqual(active["state"], "running")
                    self.assertEqual(active["episode_role"], role)
                    self.assertEqual(
                        json.loads(
                            listener.path(
                                "reset" if role == "forward" else "forward"
                            ).read_text()
                        )["state"],
                        "standby",
                    )
                    observed = sessions.read_sessions([root], now=1_800_000_001.0)
                    gate = gpumgr.gate(config, "timeshare", observed, True)
                    self.assertFalse(gate.open)
                    self.assertEqual(gate.code, "policy_inferring")
                    checked.append(True)
                    return submit(request)

                r.verifier.submit = verify_with_real_session_gate
                self.assertEqual(r.orch.run(), "WAIT_HUMAN")
                self.assertTrue(checked)
                self.assertFalse(
                    any(
                        e.record == "note" and e.note.code == "session_write_failed"
                        for e in r.orch.journal.events
                    )
                )
                # No process was probed: the real reader receives an explicit
                # fake pid=0 and current session timestamps.
                for path in tuple(sessions._CACHE):
                    if str(root) in path:
                        del sessions._CACHE[path]

    def test_backend_worker_exit_unproven_retains_real_kernel_slot(self):
        r = self.make()
        self.bad_barrier = True
        self.assertEqual(r.orch.run(), "FAULT_LOCKED")
        self.assertIsNotNone(r.owner.snapshot(now_ns=r.clock.now())["request"])
        self.assertEqual(r.robot.step_calls, 0)


if __name__ == "__main__":
    unittest.main()


def test_external_kernel_overlay_preserves_canonical_module_identities(monkeypatch):
    from levi.automatic import intervention, orchestrator
    from levi.live import runtime_ownership, workload_broker

    canonical = {
        "levi.automatic.intervention": intervention,
        "levi.live.runtime_ownership": runtime_ownership,
        "levi.live.workload_broker": workload_broker,
    }
    assert orchestrator.InferenceCycle is intervention.InferenceCycle
    monkeypatch.setenv("LEVI_JOINT_KERNEL_ROOT", str(HERE.parents[2]))
    schema, broker, runtime = load_kernel()
    assert schema is not aeri and broker is not workload_broker
    assert runtime is not runtime_ownership
    assert runtime.WorkerIdentity is broker.WorkerIdentity
    assert runtime.WorkloadBroker is broker.WorkloadBroker
    assert runtime.aeri is broker.aeri is schema
    for name, module in canonical.items():
        assert sys.modules[name] is module
    assert orchestrator.InferenceCycle is intervention.InferenceCycle
