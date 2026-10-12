"""Original kernel/Cycle/Orchestrator + actual Media stage, CPU observations only."""

import sys
import unittest
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_event_pause_orchestrator_runtime import CANDIDATE_STEP, KERNEL
from test_paused_frames import sampling

from levi.automatic import event_window as W
from levi.automatic import state_machine as sm
from levi.automatic.adapters.paused_frames import PausedFrames
from levi.automatic.fr3_media import Media
from levi.events.stream import StreamConfig


@unittest.skipUnless(KERNEL is not None, "needs original frozen resident kernel")
class PausedFramesOrchestratorTests(unittest.TestCase):
    def make(
        self,
        *,
        rejected=False,
        scene_script=None,
        strategy="human_assisted",
        episodes=1,
    ):
        from test_event_pause_orchestrator_runtime import OrchestratorRuntimeTests

        fixture = OrchestratorRuntimeTests()
        self.addCleanup(fixture.doCleanups)
        r = fixture.make(
            strategy=strategy,
            scene_script=scene_script,
            episodes=episodes,
            goal_script=[{"decision": "rejected"}, {"decision": "confirmed"}]
            if rejected
            else [],
        )
        observe = r.robot.observe

        def pixels():
            obs = observe()
            r.clock.advance(1)
            obs["observed_ns"] = r.clock.now()
            image = np.full((8, 8, 3), observer.serial % 200, np.uint8)
            obs["images_bgr"] = {"side": image, "wrist": image}
            return obs

        r.robot.observe = pixels
        context = lambda: r.orch.ctx
        observer = W.EventObserver(
            r.robot, r.clock, step_of=lambda: context().last_step if context() else 0
        )
        cached_observe = observer.observe

        def current_observe():
            ctx = context()
            if (
                ctx is not None
                and ctx.stream is not None
                and observer.identity != ctx.stream
            ):
                observer.begin(ctx.stream)
            return cached_observe()

        observer.observe = current_observe
        r.orch.robot = observer
        media = Media(cv2, None, None, None, 10)
        self.sources, self.payloads = [], []
        self.stage_fault = self.prepare_fault = None

        def stage(ledger, folder, obs):
            if self.stage_fault is not None:
                self.stage_fault(r, ledger, obs)
            PausedFrames(ledger, r.orch.journal, context, r.fence).stage(folder, obs)
            self.sources.append((r.orch.state, context().policy_epoch, ledger.rows[-1]))

        media.use_event_source(
            observer, context, StreamConfig(), r.config.run_id, stage_adapter=stage
        )
        original_stage = r.recorder.stage

        def record(rollout, obs):
            original_stage(rollout, obs)
            rollout.path = r.directory / rollout.episode_id
            media.active.setdefault(rollout.path, {"pending": None})
            media.frame(rollout.path, context().last_step, obs)

        r.recorder.stage = record
        module, judge = sampling()

        def prepare(request, obs):
            ledger = media.event_ledger(request["episode_id"])
            adapter = PausedFrames(ledger, r.orch.journal, context, r.fence)
            if self.prepare_fault is not None:
                self.prepare_fault(r, ledger, request, obs)
            self.payloads.append(adapter.freeze(request, obs, module, judge))
            return True

        r.cycle.hooks = replace(r.cycle.hooks, prepare_evidence=prepare)
        r.observer, r.media = observer, media
        self.grants = fixture.grants
        return r

    def test_complete_not_goal_chain_uses_actual_journal_kernel_and_media(self):
        r = self.make(rejected=True)
        self.assertEqual(r.orch.run(), "WAIT_HUMAN")
        self.assertEqual(r.robot.step_calls, 16)
        self.assertEqual(len(self.payloads), 2)
        paused = [
            (state, epoch, s) for state, epoch, s in self.sources if s.phase == "paused"
        ]
        # First judge and fresh pre-resume capture share a step but have new sources.
        first = paused[0]
        before_resume = paused[1]
        self.assertEqual(first[2].control_step, before_resume[2].control_step)
        self.assertEqual(first[1], before_resume[1])
        self.assertGreater(before_resume[2].serial, first[2].serial)
        self.assertGreater(before_resume[2].observed_ns, first[2].observed_ns)
        refreshed = next(
            (epoch, s)
            for state, epoch, s in self.sources
            if state == "FORWARD_ACTIVE"
            and s.phase == "pre_action"
            and s.serial > before_resume[2].serial
        )
        self.assertEqual(refreshed[1].control_step, before_resume[2].control_step)
        self.assertGreater(refreshed[0], first[1])
        self.assertGreater(refreshed[1].observed_ns, before_resume[2].observed_ns)
        commits = [e for e in r.orch.journal.events if e.record == "committed"]
        resumes = [e for e in commits if e.reason == "inference_resumed"]
        self.assertEqual(len(resumes), 1)
        self.assertEqual(resumes[0].policy_epoch, refreshed[0])
        self.assertTrue(
            any(
                t.role == "forward" and t.policy_epoch == refreshed[0]
                for t in self.grants
            )
        )
        self.assertEqual(self.payloads[0].binding.source_serial, first[2].serial)
        self.assertEqual(self.payloads[0].binding.policy_epoch, first[1])
        self.assertNotEqual(
            self.payloads[0].binding.source_serial,
            self.payloads[1].binding.source_serial,
        )
        self.assertEqual(sm.check_journal(r.orch.journal.events), [])
        self.assertIsNone(r.owner.snapshot(now_ns=r.clock.now())["request"])
        self.assertIsNone(r.fence.current)
        self.assertTrue(
            all(
                ledger.retained_bytes <= W.MAX_FRAME_BYTES
                for ledger in r.media.event_ledgers.values()
            )
        )

    def test_confirmed_seals_actual_paused_endpoint_without_reset_motion(self):
        r = self.make()
        self.assertEqual(r.orch.run(), "WAIT_HUMAN")
        self.assertEqual(r.robot.step_calls, CANDIDATE_STEP)
        self.assertEqual(len(self.payloads), 1)
        self.assertEqual(self.payloads[0].binding.role, "forward")
        self.assertIsNone(r.fence.current)
        self.assertEqual(sm.check_journal(r.orch.journal.events), [])

    def test_initial_reset_required_uses_actual_reset_judging_window(self):
        r = self.make(
            strategy="single_reset_policy",
            scene_script=[{"decision": "reset_required"}],
        )
        self.assertEqual(r.orch.run(), "WAIT_HUMAN")
        self.assertEqual(len(self.payloads), 1)
        self.assertEqual(self.payloads[0].binding.role, "reset")
        self.assertTrue(
            any(
                state == "RESET_JUDGING" and s.role == "reset"
                for state, _, s in self.sources
            )
        )
        reset_grants = [t for t in self.grants if t.role == "reset"]
        self.assertTrue(reset_grants)
        self.assertGreater(reset_grants[0].policy_epoch, self.grants[0].policy_epoch)
        self.assertEqual(sm.check_journal(r.orch.journal.events), [])
        self.assertIsNone(r.fence.current)

    def test_multiple_forward_episodes_never_reuse_old_stream_or_payload(self):
        r = self.make(episodes=2)
        for number in (1, 2):
            self.assertEqual(r.orch.run(), "WAIT_HUMAN")
            self.assertEqual(r.orch.completed_forward, number)
            resume = r.orch.resume(
                "frame-episode-" + str(number),
                expected_seq=r.orch.journal.next_seq,
                environment_handled=True,
                health_rechecked=True,
            )
            self.assertTrue(resume.ok)
        self.assertEqual(r.orch.run(), "COMPLETED")
        self.assertEqual(len(self.payloads), 2)
        first, second = self.payloads
        self.assertNotEqual(first.binding.episode_id, second.binding.episode_id)
        self.assertGreater(second.binding.policy_epoch, first.binding.policy_epoch)
        self.assertNotEqual(
            first.proof.picks[-1].source.epoch, second.proof.picks[-1].source.epoch
        )
        self.assertEqual(r.robot.step_calls, 2 * CANDIDATE_STEP)
        self.assertFalse(any(t.role == "reset" for t in self.grants))
        self.assertEqual(sm.check_journal(r.orch.journal.events), [])

    def test_media_stage_observer_error_closes_cycle_and_resume_cannot_reuse_it(self):
        r = self.make()

        def fault(rig, ledger, obs):
            if rig.orch.state == "FORWARD_JUDGING":
                raise RuntimeError("fake paused observer failed")

        self.stage_fault = fault
        with self.assertRaises(RuntimeError):
            r.orch.run()
        self.assertEqual(r.orch.state, "FAULT_LOCKED")
        self.assertEqual(r.robot.step_calls, CANDIDATE_STEP)
        self.assertTrue(r.cycle.failed)
        self.assertIsNone(r.fence.current)
        result = r.orch.resume(
            "human-frame-rearranged",
            expected_seq=r.orch.journal.next_seq,
            environment_handled=True,
            health_rechecked=True,
        )
        self.assertTrue(result.ok)
        self.stage_fault = None
        self.assertEqual(r.orch.run(), "FAULT_LOCKED")
        self.assertEqual(r.robot.step_calls, CANDIDATE_STEP)
        self.assertEqual(sm.check_journal(r.orch.journal.events), [])

    def test_stale_frame_and_corrupt_payload_rejected_before_judge_grant(self):
        for fault in ("old_stamp", "role", "bytes"):
            with self.subTest(fault=fault):
                r = self.make()

                def corrupt(rig, ledger, request, obs, fault=fault):
                    if fault == "old_stamp":
                        request["observed_through_ns"] -= 1
                    elif fault == "role":
                        request["episode_role"] = "reset"
                    else:
                        ledger.rows[-1] = replace(
                            ledger.rows[-1], images=(("side", b"corrupt", "00" * 32),)
                        )

                self.prepare_fault = corrupt
                self.assertEqual(r.orch.run(), "FAULT_LOCKED")
                self.assertEqual(r.robot.step_calls, CANDIDATE_STEP)
                self.assertTrue(r.cycle.failed)
                # Scene judge only; event judge never acquired a request slot.
                self.assertEqual(sum(t.role == "judge" for t in self.grants), 1)
                self.assertIsNone(r.fence.current)
                self.assertEqual(sm.check_journal(r.orch.journal.events), [])


if __name__ == "__main__":
    unittest.main()
