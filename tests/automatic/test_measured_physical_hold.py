"""Measured hold software contract; no device, SDK, motion or service calls."""

import math
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from levi.automatic.adapters.physical_hold import (
    AdmissionClosedProof,
    MeasuredPhysicalHold,
)
from levi.automatic.intervention import InterventionUnproven, hold_contract_sha256
from levi.automatic.reset_manager import SettleConfig

RUN, EP, PLAN = "hold", "hold.forward.0001", "ab" * 32
CFG = SettleConfig(1_000_000, 2_000_000, 10_000_000, 0.001, 0.01, 0.01)


class Clock:
    domain = "host-mono:hold-fake"

    def __init__(self):
        self.ns = 1_000_000_000
        self.extra = 0

    def now(self):
        return self.ns

    def advance(self, ns):
        self.ns += ns + self.extra


class Robot:
    held = True  # deliberately irrelevant to the observer

    def __init__(self, clock):
        self.clock = clock
        self.calls = self.generation = 0
        self.adjust = lambda value: value

    def observe(self):
        began = self.clock.now()
        self.clock.advance(1)
        self.calls += 1
        self.generation += 1
        pose = [0.0] * 6 + [0.9]  # command differs from actual measured gripper
        observed = {
            "observed_ns": self.clock.now(),
            "state_fresh": True,
            "pose": pose,
            "robot_state": {"cartesian_position": list(pose), "gripper_position": 0.1},
            "gripper_measurement": {
                "route": "getstate",
                "field": "gripper_pos",
                "generation": self.generation,
                "read_from_ns": began,
                "read_through_ns": self.clock.now(),
                "clock_domain": self.clock.domain,
                "value": 0.1,
            },
        }
        return self.adjust(observed)


class MeasuredPhysicalHoldTests(unittest.TestCase):
    def make(self):
        self.clock = Clock()
        self.robot = Robot(self.clock)
        self.closed_from = self.clock.now()
        self.stopped = False
        self.adjust_admission = lambda proof: proof

        def closed(episode, epoch, deadline):
            return self.adjust_admission(
                AdmissionClosedProof(
                    RUN,
                    episode,
                    epoch,
                    PLAN,
                    self.clock.domain,
                    self.closed_from,
                    self.clock.now(),
                    deadline,
                    True,
                    True,
                )
            )

        self.observer = MeasuredPhysicalHold(
            self.robot,
            self.clock,
            CFG,
            run_id=RUN,
            plan_sha256=PLAN,
            observe_admission=closed,
            interrupted=lambda: self.stopped,
        )
        self.deadline = self.clock.now() + 100_000_000

    def invoke(self):
        return self.observer(EP, 3, self.deadline)

    def rejected(self):
        with self.assertRaises(InterventionUnproven):
            self.invoke()
        self.assertTrue(self.observer.failed)
        calls = self.robot.calls
        self.robot.adjust = lambda value: value
        self.adjust_admission = lambda proof: proof
        self.stopped = False
        with self.assertRaises(InterventionUnproven):
            self.invoke()
        self.assertEqual(self.robot.calls, calls)

    def test_complete_frozen_window_and_digest(self):
        self.make()
        self.robot.held = False
        proof = self.invoke()
        self.assertEqual(self.robot.calls, 3)
        self.assertEqual(proof.policy_epoch, 3)
        self.assertGreaterEqual(
            proof.observed_through_ns - proof.observed_from_ns, CFG.stable_window_ns
        )
        self.assertEqual(proof.condition_sha256, hold_contract_sha256(CFG))
        self.assertEqual(proof.plan_sha256, PLAN)
        self.assertTrue(
            proof.admission_closed and proof.no_new_commands and proof.physically_stable
        )

    def test_every_admission_identity_and_closed_window_is_required(self):
        attacks = {
            "run_id": "other",
            "episode_id": "hold.reset.0001",
            "policy_epoch": 2,
            "plan_sha256": "cd" * 32,
            "clock_domain": "other",
            "closed_from_ns": 2_000_000_000,
            "observed_through_ns": 0,
            "expires_ns": 0,
            "admission_closed": None,
            "no_new_commands": False,
        }
        for key, value in attacks.items():
            with self.subTest(key=key):
                self.make()
                self.adjust_admission = lambda proof, k=key, v=value: replace(
                    proof, **{k: v}
                )
                self.rejected()

    def test_measured_route_generation_time_and_gripper_binding(self):
        attacks = {
            "route": "pose",
            "field": "command",
            "generation": True,
            "read_from_ns": 0,
            "read_through_ns": 0,
            "clock_domain": "old",
            "value": 0.9,
        }
        for key, value in attacks.items():
            with self.subTest(key=key):
                self.make()

                def adjust(obs, k=key, v=value):
                    obs["gripper_measurement"][k] = v
                    return obs

                self.robot.adjust = adjust
                self.rejected()

    def test_finite_dimensions_and_measured_pose_are_required(self):
        for value in (
            [0.0] * 6,
            [0.0] * 8,
            [True] + [0.0] * 6,
            [math.nan] + [0.0] * 6,
            [math.inf] + [0.0] * 6,
        ):
            with self.subTest(pose=value):
                self.make()

                def adjust(obs, pose=value):
                    obs["pose"] = pose
                    return obs

                self.robot.adjust = adjust
                self.rejected()
        self.make()

        def changed_source(obs):
            obs["robot_state"]["cartesian_position"][0] = 0.2
            return obs

        self.robot.adjust = changed_source
        self.rejected()

    def test_stale_unknown_and_absent_measurements_refuse(self):
        for field, value in (
            ("state_fresh", None),
            ("state_fresh", False),
            ("observed_ns", 0),
            ("gripper_measurement", None),
            ("robot_state", {}),
        ):
            with self.subTest(field=field, value=value):
                self.make()

                def adjust(obs, k=field, v=value):
                    obs[k] = v
                    return obs

                self.robot.adjust = adjust
                self.rejected()

    def test_measured_gripper_not_pose_command_controls_stability(self):
        self.make()

        def changing_measured(obs):
            value = 0.1 if self.robot.calls % 2 else 0.5
            obs["robot_state"]["gripper_position"] = value
            obs["gripper_measurement"]["value"] = value
            return obs

        self.robot.adjust = changing_measured
        self.rejected()
        self.assertGreater(self.robot.calls, 2)

    def test_pairwise_window_prevents_slow_position_drift(self):
        self.make()

        def drifting(obs):
            value = self.robot.calls * 0.0007
            obs["pose"][0] = value
            obs["robot_state"]["cartesian_position"][0] = value
            return obs

        self.robot.adjust = drifting
        self.rejected()

    def test_euler_wrap_across_pi_uses_same_frozen_metric(self):
        self.make()

        def wrapped(obs):
            value = math.pi - 0.001 if self.robot.calls % 2 else -math.pi + 0.001
            obs["pose"][3] = value
            obs["robot_state"]["cartesian_position"][3] = value
            return obs

        self.robot.adjust = wrapped
        self.assertTrue(self.invoke().physically_stable)

    def test_reused_measurement_generation_across_calls_refuses(self):
        self.make()
        self.invoke()
        self.robot.generation = 0
        self.rejected()

    def test_frozen_maximum_sample_interval_must_not_be_exceeded(self):
        self.make()
        self.clock.extra = 1
        self.rejected()

    def test_interruption_never_publishes_or_retries(self):
        self.make()

        def interrupted(obs):
            self.stopped = True
            return obs

        self.robot.adjust = interrupted
        self.rejected()

    def test_observer_exception_never_publishes_or_retries(self):
        self.make()

        def broken(obs):
            raise RuntimeError("measurement unavailable")

        self.robot.adjust = broken
        self.rejected()

    def test_final_admission_check_cannot_reopen_gate(self):
        self.make()
        checks = []

        def final_closure(proof):
            checks.append(1)
            return replace(proof, no_new_commands=False) if len(checks) == 7 else proof

        self.adjust_admission = final_closure
        self.rejected()
        self.assertEqual(len(checks), 7)

    def test_clock_identity_or_time_reversal_refuses(self):
        for attack in ("domain", "time"):
            with self.subTest(attack=attack):
                self.make()

                def adjust(obs, attack=attack):
                    if attack == "domain":
                        self.clock.domain = "old-clock"
                    else:
                        self.clock.ns -= 2
                    return obs

                self.robot.adjust = adjust
                self.rejected()

    def test_invalid_measured_gripper_refuses(self):
        for value in (True, math.nan, math.inf, -0.1, 1.1):
            with self.subTest(value=value):
                self.make()

                def adjust(obs, value=value):
                    obs["robot_state"]["gripper_position"] = value
                    obs["gripper_measurement"]["value"] = value
                    return obs

                self.robot.adjust = adjust
                self.rejected()

    def test_sampler_in_original_cycle_orchestrator_composition(self):
        from test_event_pause_orchestrator_runtime import (
            KERNEL,
            OrchestratorRuntimeTests,
        )

        if KERNEL is None:
            self.skipTest("needs original resident kernel")
        fixture = OrchestratorRuntimeTests()
        self.addCleanup(fixture.doCleanups)
        r = fixture.make()
        original = r.robot.observe
        generation = [0]

        def measured_observation():
            before = r.clock.now()
            observed = original()
            r.clock.advance(1)
            generation[0] += 1
            observed["observed_ns"] = r.clock.now()
            observed["state_fresh"] = True
            # Explicit fake measured getstate route. No hardware is contacted.
            observed["robot_state"] = {
                "cartesian_position": list(observed["pose"]),
                "gripper_position": 0.1,
            }
            observed["gripper_measurement"] = {
                "route": "getstate",
                "field": "gripper_pos",
                "generation": generation[0],
                "read_from_ns": before,
                "read_through_ns": r.clock.now(),
                "clock_domain": r.clock.domain,
                "value": 0.1,
            }
            return observed

        r.robot.observe = measured_observation
        closures = {}

        def admission(episode, epoch, deadline):
            began = closures.setdefault((episode, epoch), r.clock.now())
            return AdmissionClosedProof(
                r.config.run_id,
                episode,
                epoch,
                r.config.plan_sha256,
                r.clock.domain,
                began,
                r.clock.now(),
                deadline,
                True,
                True,
            )

        sampler = MeasuredPhysicalHold(
            r.robot,
            r.clock,
            r.config.termination.physical_hold,
            run_id=r.config.run_id,
            plan_sha256=r.config.plan_sha256,
            observe_admission=admission,
            interrupted=lambda: False,
        )
        r.cycle.hooks = replace(r.cycle.hooks, observe_hold=sampler)
        self.assertEqual(r.orch.run(), "WAIT_HUMAN")
        self.assertEqual(r.robot.step_calls, 11)
        self.assertFalse(sampler.failed)
        self.assertIsNone(r.owner.snapshot(now_ns=r.clock.now())["request"])

    def test_nonpositive_deadline_and_wrong_episode_refuse_before_sampling(self):
        self.make()
        with self.assertRaises(InterventionUnproven):
            self.observer("other.forward.0001", 3, self.deadline)
        self.assertEqual(self.robot.calls, 0)
        self.make()
        self.deadline = self.clock.now()
        self.rejected()


if __name__ == "__main__":
    unittest.main()
