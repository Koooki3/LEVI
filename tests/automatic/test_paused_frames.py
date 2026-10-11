"""Paused frame window reuse; CPU fakes, no hardware/SDK/network."""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from levi.automatic import event_window as W
from levi.automatic.adapters.paused_frames import PausedFrames
from levi.automatic.intervention import InterventionUnproven
from levi.events.stream import StreamConfig


def sampling():
    def picks(frames, spec):
        return [
            {
                "role": role,
                "offset_s": offset,
                "step": frame.step,
                "image": frame.images[key],
            }
            for role, key in (("side", "view1"), ("wrist", "hand"))
            for frame, offset in ((frames[0], -0.2), (frames[-1], 0.0))
        ]

    module = NS(
        pick_frames=picks,
        pick_start_frames=lambda *args: [],
        encode_jpeg=lambda pixels, max_side: cv2.imencode(".jpg", pixels)[1].tobytes(),
    )
    judge = NS(
        spec=NS(
            max_side=1280,
            views=(("side", (-0.2, 0.0)), ("wrist", (-0.2, 0.0))),
            start_roles=(),
        )
    )
    return module, judge


class PausedFrameTests(unittest.TestCase):
    def make(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.clock = NS(domain="host-mono:paused-fake", ns=1_000_000_000)
        self.clock.now = lambda: self.clock.ns
        self.ctx = NS(
            stream="stream-1",
            episode_id="paused.forward.0001",
            role="forward",
            policy_epoch=3,
            last_step=0,
            rollout=NS(path=self.folder),
        )
        pixels = np.zeros((8, 8, 3), np.uint8)

        def observe():
            pixels.fill(self.observer.serial + 10)
            return {
                "observed_ns": self.clock.ns,
                "images_bgr": {"side": pixels, "wrist": pixels},
                "pose": [0.0] * 7,
                "robot_state": {"gripper_position": 0.1},
                "state_fresh": True,
            }

        self.observer = W.EventObserver(
            NS(observe=observe), self.clock, step_of=lambda: self.ctx.last_step
        )
        self.observer.begin(self.ctx.stream)
        self.ledger = W.Ledger(
            self.observer, lambda: self.ctx, StreamConfig(), "paused", cv2
        )
        self.journal = NS(
            state="FORWARD_ACTIVE",
            open_transaction=None,
            control_epoch=7,
            run_id="paused",
            events=[],
        )
        self.fence = NS(current=None)
        self.adapter = PausedFrames(
            self.ledger, self.journal, lambda: self.ctx, self.fence
        )
        for step in range(3):
            self.ctx.last_step = step
            self.observe()
            self.adapter.stage(self.folder, self.obs)
        self.previous = self.ledger.rows[-1]
        self.clock.ns += 1_000_000
        self.pause = NS(
            record="committed",
            to_state="FORWARD_JUDGING",
            reason="inference_paused",
            run_id="paused",
            episode_id=self.ctx.episode_id,
            episode_role="forward",
            policy_epoch=3,
            control_epoch=7,
            mono_ns=self.clock.ns,
            clock_domain=self.clock.domain,
            sequence_no=11,
            transaction_id="pause-1",
        )
        self.journal.state = "FORWARD_JUDGING"
        self.journal.events = [self.pause]
        self.observe()

    def observe(self):
        self.clock.ns += 1_000_000
        self.obs = self.observer.observe()
        return self.obs

    def request(self):
        return {
            "run_id": "paused",
            "episode_id": self.ctx.episode_id,
            "episode_role": self.ctx.role,
            "request_id": "candidate-1",
            "target": "forward_goal",
            "event_ids": ["settled-1"],
            "observed_from_step": 0,
            "observed_through_step": self.ctx.last_step,
            "observed_through_ns": self.obs["observed_ns"],
        }

    def test_same_step_paused_replaces_current_tail_without_action_count(self):
        self.make()
        self.adapter.stage(self.folder, self.obs)
        self.assertEqual([s.control_step for s in self.ledger.rows], [0, 1, 2])
        self.assertEqual(self.ledger.rows[-1].phase, "paused")
        self.assertGreater(self.ledger.rows[-1].serial, self.previous.serial)
        self.assertGreater(self.ledger.rows[-1].observed_ns, self.pause.mono_ns)
        self.assertTrue(self.adapter.prepare_evidence(self.request(), self.obs))
        self.assertLessEqual(self.ledger.retained_bytes, W.MAX_FRAME_BYTES)
        self.assertEqual(self.ctx.last_step, 2)

    def test_trigger_observation_and_duplicate_paused_sample_rejected(self):
        self.make()
        with self.assertRaises(InterventionUnproven):
            self.adapter.prepare_evidence(self.request(), self.obs)
        self.adapter.stage(self.folder, self.obs)
        with self.assertRaises(InterventionUnproven):
            self.adapter.stage(self.folder, self.obs)

    def test_journal_identity_epoch_clock_and_permission_fail_closed(self):
        for field, value in [
            ("episode_id", "paused.forward.9999"),
            ("episode_role", "reset"),
            ("policy_epoch", 2),
            ("policy_epoch", True),
            ("clock_domain", "old-clock"),
            ("run_id", "other"),
            ("control_epoch", 6),
            ("reason", "unproven"),
            ("mono_ns", 0),
            ("mono_ns", True),
        ]:
            with self.subTest(field=field):
                self.make()
                setattr(self.pause, field, value)
                with self.assertRaises(InterventionUnproven):
                    self.adapter.stage(self.folder, self.obs)
        for fault in ("open", "motion", "fault", "old_observation", "clock", "missing"):
            with self.subTest(fault=fault):
                self.make()
                if fault == "open":
                    self.journal.open_transaction = NS()
                if fault == "motion":
                    self.fence.current = NS()
                if fault == "fault":
                    self.journal.state = "FAULT_LOCKED"
                if fault == "old_observation":
                    self.pause.mono_ns = self.clock.ns + 1
                if fault == "clock":
                    self.clock.domain = "other"
                if fault == "missing":
                    self.journal.events = []
                with self.assertRaises(InterventionUnproven):
                    self.adapter.stage(self.folder, self.obs)

    def test_current_source_request_identities_and_bounds_rejected(self):
        self.make()
        self.adapter.stage(self.folder, self.obs)
        for key, value in [
            ("run_id", "other"),
            ("episode_id", "paused.forward.9999"),
            ("episode_role", "reset"),
            ("observed_through_step", 1),
            ("observed_through_step", True),
            ("observed_through_ns", self.previous.observed_ns),
        ]:
            with self.subTest(key=key):
                request = {**self.request(), key: value}
                with self.assertRaises(InterventionUnproven):
                    self.adapter.prepare_evidence(request, self.obs)
        with self.assertRaises(InterventionUnproven):
            self.adapter.prepare_evidence(self.request(), dict(self.obs))

    def test_not_goal_resume_observation_and_next_active_step_refresh(self):
        self.make()
        self.adapter.stage(self.folder, self.obs)
        judge_source = self.ledger.rows[-1]
        self.observe()
        self.adapter.stage(
            self.folder, self.obs
        )  # fresh resume observation, still paused
        self.ctx.policy_epoch += 1
        self.clock.ns += 1
        resumed = NS(
            **{
                **vars(self.pause),
                "to_state": "FORWARD_ACTIVE",
                "reason": "inference_resumed",
                "policy_epoch": 4,
                "control_epoch": 8,
                "mono_ns": self.clock.ns,
                "sequence_no": 15,
                "transaction_id": "resume-1",
            }
        )
        self.journal.state = "FORWARD_ACTIVE"
        self.journal.control_epoch = 8
        self.journal.events.append(resumed)
        self.fence.current = NS()  # original transaction supplied motion token
        self.observe()
        self.adapter.stage(self.folder, self.obs)
        self.assertEqual(self.ledger.rows[-1].phase, "pre_action")
        self.assertGreater(self.ledger.rows[-1].serial, judge_source.serial)
        self.assertEqual([s.control_step for s in self.ledger.rows], [0, 1, 2])
        self.ctx.last_step += 1
        self.observe()
        self.adapter.stage(self.folder, self.obs)
        self.assertEqual([s.control_step for s in self.ledger.rows], [0, 1, 2, 3])

    def test_resume_requires_a_new_policy_epoch(self):
        self.make()
        self.adapter.stage(self.folder, self.obs)
        self.journal.state = "FORWARD_ACTIVE"
        self.journal.events.append(
            NS(
                **{
                    **vars(self.pause),
                    "to_state": "FORWARD_ACTIVE",
                    "reason": "inference_resumed",
                }
            )
        )
        self.observe()
        with self.assertRaises(InterventionUnproven):
            self.adapter.stage(self.folder, self.obs)

    def test_image_budget_and_new_serial_failure_never_reuse_previous_source(self):
        self.make()
        with patch.object(W, "MAX_FRAME_BYTES", 4), self.assertRaises(InterventionUnproven):
            self.adapter.stage(self.folder, self.obs)
        self.assertEqual(self.ledger.rows[-1], self.previous)
        with self.assertRaises(InterventionUnproven):
            self.adapter.prepare_evidence(self.request(), self.obs)

    def test_freeze_reuses_original_window_and_keeps_sent_payload(self):
        self.make()
        self.adapter.stage(self.folder, self.obs)
        module, judge = sampling()
        frozen = self.adapter.freeze(self.request(), self.obs, module, judge)
        payload = tuple(dict(image) for image in frozen.images)
        provenance = frozen.proof
        self.assertEqual(frozen.binding.transaction_id, "pause-1")
        self.assertEqual(frozen.binding.source_serial, self.ledger.rows[-1].serial)
        self.assertTrue(
            all(not data for p in frozen.proof.picks for _, data, _ in p.source.images)
        )
        self.observe()
        self.adapter.stage(self.folder, self.obs)
        self.assertEqual(frozen.images, payload)
        self.assertEqual(frozen.proof, provenance)
        self.assertNotEqual(frozen.binding.source_serial, self.ledger.rows[-1].serial)
        self.assertLessEqual(self.ledger.retained_bytes, W.MAX_FRAME_BYTES)

    def test_freeze_rejects_missing_endpoint_gap_and_encoded_byte_limit(self):
        for fault in ("endpoint", "gap", "bytes"):
            with self.subTest(fault=fault):
                self.make()
                self.adapter.stage(self.folder, self.obs)
                module, judge = sampling()
                if fault == "endpoint":
                    original = module.pick_frames
                    module.pick_frames = lambda frames, spec, picker=original: picker(
                        frames[:-1], spec
                    )
                if fault == "gap":
                    del self.ledger.rows[1]
                if fault == "bytes":
                    module.encode_jpeg = lambda *args: b"x" * (W.MAX_FRAME_BYTES + 1)
                with self.assertRaises(InterventionUnproven):
                    self.adapter.freeze(self.request(), self.obs, module, judge)

    def test_explicit_media_stage_uses_original_ledger_and_propagates_failure(self):
        from levi.automatic.fr3_media import Media

        self.make()
        media = Media(cv2, None, None, None, 10)
        media.active[self.folder] = {"pending": None}
        media.event_ledgers[self.folder] = self.ledger
        media.use_event_source(
            self.observer,
            lambda: self.ctx,
            self.ledger.config,
            "paused",
            stage_adapter=lambda ledger, folder, obs: self.adapter.stage(folder, obs),
        )
        media.frame(self.folder, 2, self.obs)
        self.assertIs(media.event_ledger(self.ctx.episode_id), self.ledger)
        self.assertEqual(self.ledger.rows[-1].phase, "paused")
        self.observe()
        self.fence.current = NS()
        with self.assertRaises(InterventionUnproven):
            media.frame(self.folder, 2, self.obs)

    def test_default_media_stage_preserves_legacy_duplicate_behavior(self):
        from levi.automatic.fr3_media import Media

        self.make()
        media = Media(cv2, None, None, None, 10)
        media.active[self.folder] = {"pending": None}
        media.event_ledgers[self.folder] = self.ledger
        media.use_event_source(
            self.observer, lambda: self.ctx, self.ledger.config, "paused"
        )
        media.frame(self.folder, 2, self.obs)  # legacy duplicate silently omitted
        self.assertEqual(self.ledger.rows[-1], self.previous)

    def test_direct_pause_refresh_cannot_skip_to_a_new_or_old_action_step(self):
        for step in (1, 3):
            with self.subTest(step=step):
                self.make()
                self.ctx.last_step = step
                self.observe()
                with self.assertRaises(ValueError):
                    self.ledger.stage_paused(self.folder, self.obs, self.pause.mono_ns)
                self.assertEqual(self.ledger.rows[-1], self.previous)


if __name__ == "__main__":
    unittest.main()
