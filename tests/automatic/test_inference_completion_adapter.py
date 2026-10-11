"""Explicit completion receipts over the original role authority."""

import unittest
from dataclasses import replace

import test_inference_request_callback as callbacks
from test_event_pause_orchestrator_runtime import KERNEL

from levi.automatic import intervention as iv


@unittest.skipUnless(KERNEL is not None, "requires original frozen group kernel")
class CompletionTests(unittest.TestCase):
    make = callbacks.RequestCallbackTests.make
    invoke = callbacks.RequestCallbackTests.invoke

    def receipt(self, r, request, result):
        return iv.CompletionEvidence(
            request,
            result,
            r.cycle.hooks.stop_proof(request, r.clock.now() + 100_000_000),
            r.cycle.hooks.observe_runtime(),
        )

    def test_exact_completion_identity_and_only_original_finish_all_roles(self):
        for role in ("forward", "reset", "judge"):
            with self.subTest(role=role):
                r = self.make()
                marker = object()
                receipts = []

                def call(request, r=r, marker=marker, receipts=receipts):
                    receipt = self.receipt(r, request, marker)
                    receipts.append(receipt)
                    return receipt

                def adapt(request, returned):
                    self.assertIs(request, returned.request)
                    return returned

                self.assertIs(
                    self.invoke(
                        r, role=role, request_call=call, completion_adapter=adapt
                    ),
                    marker,
                )
                self.assertEqual(len(self.begin_attempts), 1)
                self.assertEqual(len(self.finished), 1)
                self.assertIs(self.finished[0][1], receipts[0].stop_proof)
                self.assertIsNone(r.owner.snapshot(now_ns=r.clock.now())["request"])

    def test_invalid_adapter_entry_refuses_before_grant(self):
        for kwargs in (
            {"call": lambda: None, "completion_adapter": lambda a, b: None},
            {"completion_adapter": lambda a, b: None},
            {"request_call": lambda req: None, "completion_adapter": True},
        ):
            r = self.make()
            with self.assertRaises(iv.InterventionUnproven):
                self.invoke(r, **kwargs)
            self.assertEqual(self.begin_attempts, [])
            self.assertEqual(self.finished, [])
            self.assertTrue(r.cycle.failed)

    def test_no_implicit_receipt_detection_old_callback_compatibility(self):
        r = self.make()
        value = object()
        self.assertIs(self.invoke(r, request_call=lambda req: value), value)
        self.assertEqual(len(self.finished), 1)
        r = self.make()
        self.assertIs(self.invoke(r, call=lambda: value), value)

    def test_wrong_type_equal_but_not_same_token_and_proof_are_contained(self):
        mutations = (
            lambda receipt: (receipt.stop_proof, receipt.observation),
            lambda receipt: True,
            lambda receipt: replace(receipt, request=replace(receipt.request)),
            lambda receipt: replace(
                receipt,
                stop_proof=replace(
                    receipt.stop_proof, request=replace(receipt.request)
                ),
            ),
        )
        for mutate in mutations:
            r = self.make()
            seen = []

            def call(request, r=r, seen=seen):
                seen.append(request)
                return self.receipt(r, request, "result")

            with self.assertRaises(iv.InterventionUnproven):
                self.invoke(
                    r,
                    request_call=call,
                    completion_adapter=lambda req, result, mutate=mutate: mutate(result),
                )
            snap = r.owner.snapshot(now_ns=r.clock.now())
            self.assertIs(snap["request"], seen[0])
            self.assertTrue(snap["request_revoked"])
            self.assertEqual(self.finished, [])
            self.assertEqual(len(self.begin_attempts), 1)
            self.assertTrue(r.cycle.failed)

    def test_callback_and_adapter_exception_no_retry_slot_retained(self):
        for where in ("callback", "adapter"):
            r = self.make()
            calls = []

            def call(request, r=r, where=where, calls=calls):
                calls.append(request)
                if where == "callback":
                    raise TypeError("no fallback")
                return self.receipt(r, request, "result")

            def adapt(request, result):
                raise RuntimeError("no proof fallback")

            with self.assertRaises(iv.InterventionUnproven):
                self.invoke(r, request_call=call, completion_adapter=adapt)
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(self.begin_attempts), 1)
            self.assertEqual(self.finished, [])
            self.assertIs(r.owner.snapshot(now_ns=r.clock.now())["request"], calls[0])
            self.assertTrue(r.cycle.failed)
            with self.assertRaises(iv.InterventionUnproven):
                self.invoke(
                    r,
                    request_call=lambda req: self.fail("no reuse"),
                    completion_adapter=adapt,
                )
            self.assertEqual(len(self.begin_attempts), 1)

    def test_original_kernel_rejects_bad_stop_group_clock_and_observation(self):
        mutations = (
            lambda receipt: replace(
                receipt,
                stop_proof=replace(receipt.stop_proof, backend_request_stopped=False),
            ),
            lambda receipt: replace(
                receipt,
                stop_proof=replace(receipt.stop_proof, clock_domain="foreign-clock"),
            ),
            lambda receipt: replace(
                receipt, observation=replace(receipt.observation, observed_ns=0)
            ),
            lambda receipt: replace(
                receipt, observation=replace(receipt.observation, group=None)
            ),
        )
        for mutate in mutations:
            r = self.make()
            with self.assertRaises(iv.InterventionUnproven):
                self.invoke(
                    r,
                    request_call=lambda req, r=r: self.receipt(r, req, "result"),
                    completion_adapter=lambda req, result, mutate=mutate: mutate(result),
                )
            self.assertTrue(r.cycle.failed)
            self.assertEqual(len(self.begin_attempts), 1)
            self.assertEqual(len(self.finished), 1)


if __name__ == "__main__":
    unittest.main()
