"""Coordinator against the ORIGINAL resident-group runtime, fake observations.

Requires the resident-group kernel dependency (d82). No process or GPU probes.
"""

import importlib.util
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from levi.automatic import intervention
from levi.automatic.reset_manager import SettleConfig
from levi.domain import aeri
from levi.live.runtime_ownership import RequestStopProof


@unittest.skipUnless(
    hasattr(aeri, "ResidentGroupSpec"), "needs resident-group kernel d82"
)
class JointRuntimeTests(unittest.TestCase):
    def setUp(self):
        fixture_path = (
            Path(aeri.__file__).parents[2]
            / "tests"
            / "live"
            / "test_resident_group_authority.py"
        )
        fixture_spec = importlib.util.spec_from_file_location(
            "joint_resident_fixture", fixture_path
        )
        self.fixture = importlib.util.module_from_spec(fixture_spec)
        fixture_spec.loader.exec_module(self.fixture)
        self.owner, self.token, self.obs, *_ = self.fixture.setup()
        self.clock = SimpleNamespace(domain=self.fixture.CLOCK)
        self.current_ns = self.fixture.NOW
        self.clock.now = lambda: self.current_ns
        self.hold = SettleConfig(1_000_000, 2_000_000, 100_000_000, 0.001, 0.01, 0.01)
        self.bad_stop = False
        self.condition = intervention.hold_contract_sha256(self.hold)

        def physical(episode, epoch, deadline):
            start = self.current_ns
            self.current_ns += self.hold.stable_window_ns
            return intervention.PhysicalHoldProof(
                "run",
                episode,
                epoch,
                "ab" * 32,
                self.condition,
                self.clock.domain,
                start,
                self.current_ns,
                deadline,
                True,
                True,
                True,
            )

        def observation():
            return replace(self.obs, observed_ns=self.current_ns)

        def window(role, episode, epoch, authorization, deadline):
            return self.fixture.role_window(
                self.owner,
                role,
                epoch=epoch,
                authorization=authorization,
                episode=episode,
                now=self.current_ns,
            )

        hooks = intervention.InterventionHooks(
            lambda *args: True,
            lambda *args: True,
            physical,
            observation,
            window,
            lambda request, deadline: RequestStopProof(
                request, self.current_ns, self.clock.domain, True, not self.bad_stop
            ),
            lambda *args: True,
        )
        self.cycle = intervention.InferenceCycle(
            authority=self.owner,
            runtime=self.token,
            hooks=hooks,
            clock=self.clock,
            run_id="run",
            plan_sha256="ab" * 32,
            condition_sha256=self.condition,
        )
        self.cycle.validate(
            SimpleNamespace(
                run_id="run",
                plan_sha256="ab" * 32,
                termination=SimpleNamespace(physical_hold=self.hold),
            ),
            self.clock,
        )

    def invoke(self, role, call=lambda: "result", episode="run.forward.0001", epoch=1):
        return self.cycle.invoke(
            role,
            episode,
            epoch,
            deadline_ns=self.current_ns + 90_000_000,
            call=call,
            interrupted=lambda: False,
        )

    def test_forward_judge_reset_rotate_original_one_slot(self):
        for role in ("forward", "judge", "reset", "judge", "forward"):
            self.assertEqual(self.invoke(role), "result")
            self.assertIsNone(self.owner.snapshot(now_ns=self.current_ns)["request"])
        self.assertEqual(self.cycle.authorization_epoch, 5)
        self.assertEqual(
            self.invoke("forward", episode="run.forward.0002", epoch=2), "result"
        )

    def test_competing_role_cannot_enter_original_slot(self):
        def body():
            with self.assertRaises(ValueError):
                self.fixture.begin(
                    self.owner,
                    self.token,
                    replace(self.obs, observed_ns=self.current_ns),
                    role="reset",
                    authorization=2,
                    now=self.current_ns,
                )
            return "alone"

        self.assertEqual(self.invoke("forward", body), "alone")

    def test_missing_worker_exit_retains_original_ownership(self):
        self.bad_stop = True
        with self.assertRaises(intervention.InterventionUnproven):
            self.invoke("judge")
        self.assertIsNotNone(self.owner.snapshot(now_ns=self.current_ns)["request"])
        self.assertTrue(self.cycle.failed)

    def test_interrupt_revokes_without_clearing_request_slot(self):
        with self.assertRaises(intervention.InterventionStopped):
            self.invoke("forward", lambda: self.cycle.interrupt())
        snapshot = self.owner.snapshot(now_ns=self.current_ns)
        self.assertIsNotNone(snapshot["request"])
        self.assertTrue(self.cycle.failed)


if __name__ == "__main__":
    unittest.main()
