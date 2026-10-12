"""Explicit OwnedCalls + original Kernel/Orchestrator CPU-only integration."""

import hashlib
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace

import test_inference_request_callback as callbacks
from test_event_pause_orchestrator_runtime import KERNEL

from levi.automatic import state_machine as sm
from levi.automatic.adapters import events as ev
from levi.automatic.adapters.owned_calls import (
    OwnedCalls,
    PreparedCall,
    SourceBinding,
    canonical,
)
from levi.automatic.intervention import CompletionEvidence
from levi.automatic.orchestrator import ConfigError, Orchestrator


class PrivateExecution:
    """Explicit injected software fake; never claims SDK/physical support."""

    def __init__(self, r, grants):
        self.authority, self.runtime, self.lease = r.owner, r.cycle.runtime, r.lease
        self.r, self.grants = r, grants
        self.dispatches, self.active = [], None
        self.goal_decisions = ["rejected", "confirmed"]
        self.scene_decisions = ["ready"]
        self.error = None

    def dispatch_existing(self, request, payload, *, role, episode_id, policy_epoch):
        assert request is self.grants[-1]
        assert (role, episode_id, policy_epoch) == (
            request.role,
            request.episode_id,
            request.policy_epoch,
        )
        self.dispatches.append((request, json.loads(payload)))
        self.active = request
        return request

    def collect_existing(self, request, *, timeout_s):
        assert request is self.active
        if self.error:
            raise self.error("injected collection failure")
        raw = self.dispatches[-1][1]
        if raw["kind"] == "goal":
            raw = {
                "decision": self.goal_decisions.pop(0)
                if self.goal_decisions
                else "confirmed"
            }
        elif raw["kind"] == "scene":
            raw = {
                "decision": self.scene_decisions.pop(0)
                if self.scene_decisions
                else "ready"
            }
        else:
            raw = {"actions": [[0.0] * self.r.config.action_dims for _ in range(16)]}
        return SimpleNamespace(
            request=request,
            result=raw,
            sdk_ack=("explicit-fake-ack", request.request_epoch),
            stop_proof=self.r.cycle.hooks.stop_proof(request, request.expires_ns),
            observation=self.r.cycle.hooks.observe_runtime(),
        )


@unittest.skipUnless(KERNEL is not None, "requires frozen original kernel")
class OwnedCallsTests(unittest.TestCase):
    make = callbacks.RequestCallbackTests.make

    def mount(
        self,
        r,
        *,
        mutate=None,
        bad_decode=False,
        execution=None,
        completion_adapter=None,
        decode_mutate=None,
    ):
        executor = execution or PrivateExecution(r, self.fixture.grants)
        self.executor = executor
        self.preparations, self.decodes = [], []

        def prepare(request, obs, context):
            payload = canonical(
                {
                    "kind": context.kind,
                    "request": request,
                    "observed_ns": obs["observed_ns"],
                }
            )
            binding = SourceBinding(
                request["run_id"],
                request["episode_id"],
                request["episode_id"].rsplit(".", 2)[1],
                context.policy_epoch,
                r.clock.domain,
                obs["observed_ns"],
                {"scene": "scene", "goal": "paused", "chunk": "policy"}[context.kind],
                hashlib.sha256(payload).hexdigest(),
            )
            prepared = PreparedCall(
                canonical(request),
                payload,
                binding,
                tuple(
                    (view + ":" + request["request_id"], b"\xff\xd8fake\xff\xd9")
                    for view in ("side", "wrist")
                )
                if context.kind == "scene"
                else (),
            )
            self.preparations.append((context.kind, obs, prepared))
            return mutate(prepared) if mutate else prepared

        def decode(prepared, result, now):
            # The original kernel must have finished before any CPU decode.
            self.assertIsNone(r.owner.snapshot(now_ns=now)["request"])
            self.assertEqual(len(self.finished), len(executor.dispatches))
            kind = json.loads(prepared.payload)["kind"]
            self.decodes.append(kind)
            if bad_decode:
                raise TypeError("SDK result is not a valid CPU codec answer")
            request = json.loads(prepared.request)
            ticket = ev.Ticket(
                request["request_id"],
                0,
                prepared.binding.observed_ns,
                now,
                request,
                result,
            )
            if kind == "scene":
                message = json.loads(r.scene._answer(ticket, result, now))
                refs = [
                    {
                        "kind": "frame",
                        "ref": ref,
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                    for ref, data in prepared.frames
                ]
                message["evidence_refs"] = refs
                for predicate in message["predicate_results"]:
                    predicate["evidence_refs"] = refs
                return canonical(decode_mutate(message) if decode_mutate else message)
            if kind == "goal":
                return r.verifier._answer(ticket, result, now)
            return {
                "schema": "levi.aeri.runtime.v1",
                "minor": 0,
                "run_id": request["run_id"],
                "emitted_wall_ns": 0,
                "kind": "chunk_response",
                "request_id": request["request_id"],
                "episode_id": request["episode_id"],
                "policy_epoch": request["policy_epoch"],
                "action_contract": "fr3-robotiq@1",
                "actions": result["actions"],
                "valid_from_action_index": request["action_start_index"],
                "prefix_conditioning_applied": "none",
                "clock_domain": r.clock.domain,
                "model_version": {"config": "fake", "checkpoint_dir_name": "fake"},
                "timing": {"total_ms": 0},
            }

        def completion(request, returned):
            self.assertIs(request, returned.request)
            self.assertEqual(
                returned.sdk_ack, ("explicit-fake-ack", request.request_epoch)
            )
            return CompletionEvidence(
                request, returned.result, returned.stop_proof, returned.observation
            )

        self.calls = OwnedCalls(
            executor,
            r.cycle,
            prepare_scene=prepare,
            prepare_goal=prepare,
            prepare_chunk=prepare,
            decode_scene=decode,
            decode_goal=decode,
            decode_chunk=decode,
            completion_adapter=completion_adapter or completion,
        )

        def forbidden(*args, **kwargs):
            self.fail("old transport must never be called")

        r.scene.submit = r.verifier.submit = r.policy.request = forbidden
        r.scene.collect = r.verifier.collect = r.policy.collect = forbidden
        r.orch = Orchestrator(
            r.orch.journal,
            config=r.config,
            intervention=r.cycle,
            owned_calls=self.calls,
            evidence=r.orch.evidence,
            **r.parts(),
        )
        return r

    def test_actual_three_paths_no_old_transport_and_decode_after_finish(self):
        r = self.mount(self.make())
        self.assertEqual(r.orch.run(), "WAIT_HUMAN")
        # rejected at step 8, then the newer candidate at step 14 pauses again
        self.assertEqual(r.robot.step_calls, 14)
        self.assertEqual(set(self.decodes), {"scene", "goal", "chunk"})
        self.assertEqual(len(self.begin_attempts), len(self.executor.dispatches))
        self.assertEqual(len(self.finished), len(self.executor.dispatches))
        self.assertEqual(sm.check_journal(r.orch.journal.events), [])

    def test_not_goal_has_new_epoch_and_new_observation_chunk(self):
        r = self.mount(self.make())
        self.assertEqual(r.orch.run(), "WAIT_HUMAN")
        chunks = [p for kind, obs, p in self.preparations if kind == "chunk"]
        self.assertEqual(len(chunks), 2)
        self.assertGreater(
            chunks[1].binding.policy_epoch, chunks[0].binding.policy_epoch
        )
        self.assertGreater(chunks[1].binding.observed_ns, chunks[0].binding.observed_ns)
        self.assertEqual(
            json.loads(chunks[1].request)["obs_ns"], chunks[1].binding.observed_ns
        )

    def test_explicit_invalid_mount_rejected_zero_grants(self):
        r = self.make()
        with self.assertRaises(ConfigError):
            Orchestrator(
                r.orch.journal,
                config=r.config,
                intervention=r.cycle,
                owned_calls=object(),
                **r.parts(),
            )
        self.assertEqual(self.begin_attempts, [])

    def test_wrong_epoch_clock_or_payload_hash_stops_before_dispatch(self):
        for change in ("epoch", "clock", "hash"):
            r = self.make()

            def mutate(prepared, change=change):
                field, value = {
                    "epoch": ("policy_epoch", prepared.binding.policy_epoch + 1),
                    "clock": ("clock_domain", "foreign-clock"),
                    "hash": ("payload_sha256", "00" * 32),
                }[change]
                return replace(
                    prepared, binding=replace(prepared.binding, **{field: value})
                )

            self.mount(r, mutate=mutate)
            self.assertEqual(r.orch.run(), "FAULT_LOCKED")
            self.assertEqual(self.executor.dispatches, [])
            self.assertTrue(r.cycle.failed)
            self.assertEqual(r.robot.step_calls, 0)
            self.assertTrue(r.owner.snapshot(now_ns=r.clock.now())["request_revoked"])

    def test_decoder_failure_human_resume_cannot_reuse_cycle(self):
        r = self.mount(self.make(), bad_decode=True)
        self.assertEqual(r.orch.run(), "FAULT_LOCKED")
        self.assertTrue(r.cycle.failed)
        self.assertEqual(len(self.finished), 1)
        self.fixture.resume(r, "human-after-codec-failure")
        self.assertEqual(r.orch.run(), "FAULT_LOCKED")
        self.assertEqual(len(self.begin_attempts), 1)
        self.assertEqual(r.robot.step_calls, 0)

    def test_decoder_cannot_change_request_source_time_or_retained_frame_hash(self):
        for change in ("request", "time", "frame"):
            r = self.make()

            def mutate(message, change=change):
                if change == "request":
                    message["request_id"] += ":foreign"
                elif change == "time":
                    message["observed_ns"] += 1
                else:
                    message["evidence_refs"][0]["sha256"] = "00" * 32
                return message

            self.mount(r, decode_mutate=mutate)
            self.assertEqual(r.orch.run(), "FAULT_LOCKED")
            self.assertEqual(r.robot.step_calls, 0)
            self.assertEqual(len(self.finished), 1)
            self.assertTrue(r.cycle.failed)

    def test_missing_frames_or_invalid_prepared_type_refuses_before_dispatch(self):
        for mutate in (
            lambda prepared: replace(prepared, frames=()),
            lambda prepared: True,
        ):
            r = self.make()
            self.mount(r, mutate=mutate)
            self.assertEqual(r.orch.run(), "FAULT_LOCKED")
            self.assertEqual(self.executor.dispatches, [])
            self.assertTrue(r.cycle.failed)
            self.assertTrue(r.owner.snapshot(now_ns=r.clock.now())["request_revoked"])

    def test_collection_error_preserves_original_slot_and_no_decode(self):
        r = self.mount(self.make())
        self.executor.error = TypeError
        self.assertEqual(r.orch.run(), "FAULT_LOCKED")
        self.assertEqual(self.decodes, [])
        self.assertEqual(self.finished, [])
        snapshot = r.owner.snapshot(now_ns=r.clock.now())
        self.assertIs(snapshot["request"], self.executor.dispatches[0][0])
        self.assertTrue(snapshot["request_revoked"])
        self.assertEqual(r.robot.step_calls, 0)


if __name__ == "__main__":
    unittest.main()
