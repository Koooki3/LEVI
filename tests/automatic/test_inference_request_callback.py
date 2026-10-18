"""Explicit borrowed-token callback over original kernel and Orchestrator fakes."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_event_pause_orchestrator_runtime import KERNEL

from levi.automatic import state_machine as sm
from levi.automatic.intervention import InterventionUnproven


@unittest.skipUnless(KERNEL is not None, "requires original frozen group kernel")
class RequestCallbackTests(unittest.TestCase):
    def make(self, **kwargs):
        from test_event_pause_orchestrator_runtime import OrchestratorRuntimeTests

        fixture = OrchestratorRuntimeTests()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        r = fixture.make(**kwargs)
        self.finished, self.begin_attempts = [], []
        begin = r.owner.begin_role_request

        def begun(*args, **kwargs):
            self.begin_attempts.append((args, kwargs))
            return begin(*args, **kwargs)  # exact original kernel decision

        r.owner.begin_role_request = begun
        finish = r.owner.finish_request

        def finished(request, proof, observation, **kw):
            self.finished.append((request, proof))
            return finish(request, proof, observation, **kw)

        r.owner.finish_request = finished
        return r

    def invoke(self, r, *, role="judge", epoch=1, **kwargs):
        episode_role = "reset" if role == "reset" else "forward"
        return r.cycle.invoke(
            role,
            f"{r.config.run_id}.{episode_role}.0001",
            epoch,
            deadline_ns=r.clock.now() + 100_000_000,
            interrupted=lambda: False,
            **kwargs,
        )

    def test_precise_original_request_token_identity_for_every_role(self):
        for role in ("forward", "reset", "judge"):
            with self.subTest(role=role):
                r = self.make()
                received = []

                def borrowed(
                    request, role=role, received=received, run_id=r.config.run_id
                ):
                    received.append(request)
                    self.assertIs(request, self.fixture.grants[-1])
                    self.assertEqual(request.role, role)
                    self.assertEqual(
                        request.episode_id,
                        f"{run_id}.{'reset' if role == 'reset' else 'forward'}.0001",
                    )
                    self.assertEqual(request.policy_epoch, 7)
                    self.assertEqual(request.request_epoch, 1)
                    return "bounded-fake-result"

                self.assertEqual(
                    self.invoke(r, role=role, epoch=7, request_call=borrowed),
                    "bounded-fake-result",
                )
                self.assertEqual(len(received), 1)
                self.assertEqual(len(self.fixture.grants), 1)
                self.assertEqual(len(self.begin_attempts), 1)
                self.assertEqual(len(self.finished), 1)
                self.assertIs(self.finished[0][0], received[0])
                self.assertIs(self.finished[0][1].request, received[0])
                self.assertIsNone(r.owner.snapshot(now_ns=r.clock.now())["request"])
                self.assertEqual(r.cycle.authorization_epoch, 1)
                self.assertFalse(r.cycle.failed)

    def test_consecutive_callbacks_have_current_policy_and_request_epochs(self):
        r = self.make()
        received = []
        for epoch in (3, 4, 9):

            def borrowed(request, epoch=epoch):
                self.assertIs(request, self.fixture.grants[-1])
                self.assertEqual(request.policy_epoch, epoch)
                received.append(request)
                return epoch

            self.assertEqual(self.invoke(r, epoch=epoch, request_call=borrowed), epoch)
        self.assertEqual([r.request_epoch for r in received], [1, 2, 3])
        self.assertEqual([r.policy_epoch for r in received], [3, 4, 9])
        self.assertEqual(len(self.fixture.grants), len(self.finished))
        self.assertEqual(r.cycle.authorization_epoch, 3)
        self.assertEqual(len(self.begin_attempts), 3)

    def test_legacy_zero_argument_callback_remains_compatible(self):
        r = self.make()
        calls = []

        def legacy():
            calls.append("zero-arguments")
            return object()

        result = self.invoke(r, call=legacy)
        self.assertIsNotNone(result)
        self.assertEqual(calls, ["zero-arguments"])
        self.assertEqual(len(self.fixture.grants), 1)
        self.assertEqual(len(self.finished), 1)
        self.assertIs(self.finished[0][0], self.fixture.grants[0])

    def test_missing_dual_and_noncallable_callbacks_refuse_before_grant(self):
        for callbacks in (
            {},
            {"call": lambda: None, "request_call": lambda req: None},
            {"call": object()},
            {"request_call": object()},
        ):
            with self.subTest(callbacks=tuple(callbacks)):
                r = self.make()
                with self.assertRaises(InterventionUnproven):
                    self.invoke(r, **callbacks)
                self.assertTrue(r.cycle.failed)
                self.assertEqual(self.fixture.grants, [])
                self.assertEqual(self.begin_attempts, [])
                self.assertEqual(self.finished, [])
                self.assertEqual(r.cycle.authorization_epoch, 0)
                self.assertIsNone(r.owner.snapshot(now_ns=r.clock.now())["request"])

    def test_callback_exception_has_no_retry_and_retains_original_owned_slot(self):
        for error in (RuntimeError, TypeError):
            with self.subTest(error=error.__name__):
                r = self.make()
                calls = []

                def borrowed(request, error=error, calls=calls):
                    calls.append(request)
                    raise error("fake SDK callback failed")

                with self.assertRaises(InterventionUnproven) as raised:
                    self.invoke(r, request_call=borrowed)
                self.assertIsInstance(raised.exception.__cause__, error)
                self.assertEqual(len(calls), 1)
                self.assertEqual(len(self.fixture.grants), 1)
                self.assertEqual(self.finished, [])
                self.assertEqual(len(self.begin_attempts), 1)
                observation = r.owner.snapshot(now_ns=r.clock.now())
                self.assertIs(observation["request"], calls[0])
                self.assertEqual(observation["runtime"], r.cycle.runtime)
                self.assertTrue(observation["request_revoked"])
                self.assertTrue(r.cycle.failed)
                with self.assertRaises(InterventionUnproven):
                    self.invoke(
                        r, request_call=lambda req: self.fail("no retry allowed")
                    )
                self.assertEqual(len(self.fixture.grants), 1)
                self.assertIs(
                    r.owner.snapshot(now_ns=r.clock.now())["request"], calls[0]
                )

    def token_entry(self, r, *, fail=False):
        original = r.cycle.invoke
        self.received = []

        def invoke(role, episode, epoch, *, call, **kwargs):
            # Explicit test transport, never infer a callback signature or grant.
            def borrowed(request):
                self.assertIs(request, self.fixture.grants[-1])
                self.assertEqual(
                    (request.role, request.episode_id, request.policy_epoch),
                    (role, episode, epoch),
                )
                self.received.append(request)
                if fail:
                    raise TypeError("callback failure is never a retry signal")
                return call()

            return original(role, episode, epoch, request_call=borrowed, **kwargs)

        r.cycle.invoke = invoke

    def test_actual_orchestrator_success_and_fault_reuse_original_containment(self):
        r = self.make(goal_script=[{"decision": "rejected"}, {"decision": "confirmed"}])
        self.token_entry(r)
        self.assertEqual(r.orch.run(), "COMPLETED")
        # rejected at step 8, then the newer candidate at step 14 pauses again
        self.assertEqual(r.robot.step_calls, 14)
        self.assertEqual(len(self.received), len(self.fixture.grants))
        self.assertEqual(len(self.finished), len(self.fixture.grants))
        self.assertEqual(len(self.begin_attempts), len(self.received))
        self.assertIsNone(r.owner.snapshot(now_ns=r.clock.now())["request"])
        self.assertEqual(sm.check_journal(r.orch.journal.events), [])
        r = self.make()
        self.token_entry(r, fail=True)
        self.assertEqual(r.orch.run(), "FAULT_LOCKED")
        self.assertEqual(r.robot.step_calls, 0)
        self.assertEqual(len(self.received), 1)
        before = r.owner.snapshot(now_ns=r.clock.now())
        self.assertTrue(before["request_revoked"])
        self.fixture.resume(r, "human-after-borrowed-failure")
        self.assertEqual(r.orch.run(), "FAULT_LOCKED")
        self.assertEqual(r.robot.step_calls, 0)
        self.assertEqual(len(self.fixture.grants), 1)
        self.assertEqual(len(self.received), 1)
        self.assertEqual(len(self.begin_attempts), 1)
        self.assertIs(
            r.owner.snapshot(now_ns=r.clock.now())["request"], before["request"]
        )
        self.assertEqual(sm.check_journal(r.orch.journal.events), [])


if __name__ == "__main__":
    unittest.main()
