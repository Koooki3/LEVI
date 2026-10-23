import threading
import unittest
from types import SimpleNamespace

from integrations.fr3_manual.protocol import AsyncGoal, Protocol, Refused

LIMITS = {
    "box_min_m": [-1, -1, -1],
    "box_max_m": [1, 1, 1],
    "max_delta_m": 0.2,
    "max_step_m": 0.01,
    "max_velocity_m_s": 0.1,
    "period_s": 0.1,
    "max_reading_age_s": 0.2,
    "max_anchor_age_s": 10,
    "deadman_timeout_s": 0.5,
    "target_timeout_s": 0.5,
    "max_orientation_drift_rad": 0.1,
    "quaternion_norm_tolerance": 0.001,
    "max_settle_age_s": 1,
}
STOP = {
    "settle_window_s": 0.2,
    "settle_position_m": 0.001,
    "settle_angle_rad": 0.001,
    "gripper_settle_rad": 0.001,
}


class Fake:
    def __init__(self):
        self.t = 1.0
        self.seq = 1
        self.sent = []
        self.stale = False
        self.terminal = True
        self.theta = 0.0
        self.busy = False
        self.fail = False

    def sample(self):
        return {
            "pose7": [0, 0, 0, 0, 0, 0, 1],
            "joints": [0] * 7,
            "frame": "base",
            "seq": self.seq,
            "joint_seq": self.seq,
            "monotonic_time": self.t - (1 if self.stale else 0),
            "joint_monotonic_time": self.t - (1 if self.stale else 0),
        }

    def gripper_state(self):
        return {
            "seq": self.seq,
            "theta": self.theta,
            "monotonic_time": self.t,
            "terminal": self.terminal,
        }

    def legacy_idle(self):
        return not self.busy

    def gripper_supported(self):
        return True

    def gripper_terminal(self):
        return self.terminal

    def begin_gripper(self, ratio):
        self.terminal = False
        self.sent.append(("gripper", ratio))

    def cancel_gripper(self):
        pass

    def publish(self, pose):
        if self.fail:
            raise RuntimeError("failure")
        self.sent.append(pose)

    def advance(self, dt=0.1):
        self.t += dt
        self.seq += 1


class Tests(unittest.TestCase):
    def setUp(self):
        self.a = Fake()
        self.p = Protocol(self.a, lambda: self.a.t)
        self.p.start({"session_id": "s", "limits": LIMITS, "stop_limits": STOP})
        self.p.heartbeat(
            {"session_id": "s", "sequence": 0, "expires_at_mono": self.a.t + 0.5}
        )

    def cmd(self, **extra):
        return dict(session_id="s", sequence=1, expires_at_mono=self.a.t + 0.3, **extra)

    def test_start_has_no_motion(self):
        self.assertEqual(self.a.sent, [])

    def test_start_waits_without_moving_until_first_held_control(self):
        a = Fake()
        p = Protocol(a, lambda: a.t)
        p.start({"session_id": "waiting", "limits": LIMITS, "stop_limits": STOP})
        a.advance(1)
        p.tick()
        self.assertTrue(p.open)
        self.assertEqual(a.sent, [])
        with self.assertRaisesRegex(Refused, "deadman_required"):
            p.pose(
                {
                    "session_id": "waiting",
                    "sequence": 0,
                    "expires_at_mono": a.t + 0.1,
                    "pose7": [0.001, 0, 0, 0, 0, 0, 1],
                }
            )
        self.assertEqual(a.sent, [])

    def test_expiry_without_http(self):
        self.a.advance(0.6)
        self.p.tick()
        self.assertFalse(self.p.open)
        self.assertEqual(len(self.a.sent), 1)

    def test_replay(self):
        data = self.cmd()
        self.p.heartbeat(data)
        with self.assertRaisesRegex(Refused, "sequence_replay"):
            self.p.heartbeat(data)

    def test_nonfinite_schema(self):
        for target in ([float("nan"), 0, 0, 0, 0, 0, 1], [True, 0, 0, 0, 0, 0, 1]):
            with self.assertRaises(Refused):
                self.p.pose(self.cmd(pose7=target))
        with self.assertRaises(Refused):
            self.p.stop({"session_id": "s", "extra": 1})

    def test_expired_before_dispatch(self):
        original = self.a.sample
        calls = []

        def delayed():
            calls.append(1)
            if len(calls) == 2:
                self.a.t += 0.4
            return original()

        self.a.sample = delayed
        with self.assertRaisesRegex(Refused, "expired_before_dispatch"):
            self.p.pose(self.cmd(pose7=[0.001, 0, 0, 0, 0, 0, 1]))
        self.assertFalse(self.p.open)

    def test_pose_bounds(self):
        for xyz, code in (
            ([2, 0, 0], "outside_box"),
            ([0.02, 0, 0], "measured_step_limit"),
        ):
            with self.assertRaisesRegex(Refused, code):
                self.p.pose(self.cmd(pose7=xyz + [0, 0, 0, 1]))
            self.p.sequence = -1

    def test_target_deadline_independent_of_heartbeat(self):
        data = self.cmd(pose7=[0.001, 0, 0, 0, 0, 0, 1])
        data["expires_at_mono"] = self.a.t + 0.1
        self.p.pose(data)
        self.a.advance(0.11)
        self.p.tick()
        self.assertEqual(self.p.reason, "target_deadline_expired")

    def test_watchdog_detects_stale_feedback(self):
        self.a.stale = True
        self.p.tick()
        self.assertFalse(self.p.open)
        self.assertEqual(self.p.stop_evidence, "hold_failed_unproven")

    def test_quaternion_rotation(self):
        with self.assertRaisesRegex(Refused, "rotation_unsupported"):
            self.p.pose(self.cmd(pose7=[0, 0, 0, 0, 0, 0.1, 0.995]))

    def test_rate_limit(self):
        self.p.pose(self.cmd(pose7=[0.001, 0, 0, 0, 0, 0, 1]))
        data = self.cmd(pose7=[0.002, 0, 0, 0, 0, 0, 1])
        data["sequence"] = 2
        with self.assertRaisesRegex(Refused, "rate_limit"):
            self.p.pose(data)

    def test_legacy_exclusion_retained_after_stop(self):
        self.p.stop({"session_id": "s"})
        with self.assertRaisesRegex(Refused, "manual_owns_robot"):
            self.p.legacy(lambda: None)

    def test_hold_failure_retains(self):
        self.a.fail = True
        self.p.stop({"session_id": "s"})
        self.assertEqual(self.p.stop_evidence, "hold_failed_unproven")
        self.a.advance()
        with self.assertRaises(Refused):
            self.p.end({"session_id": "s"})

    def test_stale_hold_retains(self):
        self.a.stale = True
        self.p.stop({"session_id": "s"})
        self.assertEqual(self.p.stop_evidence, "hold_failed_unproven")

    def test_end_requires_continuous_post_stop_window(self):
        self.p.stop({"session_id": "s"})
        with self.assertRaises(Refused):
            self.p.end({"session_id": "s"})
        for _ in range(4):
            self.a.advance()
            self.p.tick()
        self.p.end({"session_id": "s"})
        self.assertIsNone(self.p.session_id)

    def test_old_settle_proof_is_revoked_after_feedback_gap(self):
        self.p.stop({"session_id": "s"})
        for _ in range(4):
            self.a.advance()
            self.p.tick()
        self.assertEqual(self.p.stop_evidence, "measured_settled")
        self.a.stale = True
        self.p.tick()
        self.assertEqual(self.p.stop_evidence, "hold_published_unproven")
        self.a.stale = False
        self.a.advance()
        self.p.tick()
        # Repeated observations of the same fresh sample are not a new window.
        with self.assertRaisesRegex(Refused, "physical_stop_unproven"):
            self.p.end({"session_id": "s"})
        for _ in range(4):
            self.a.advance()
            self.p.tick()
        self.p.end({"session_id": "s"})

    def test_long_stopped_session_retains_bounded_evidence(self):
        self.p.stop({"session_id": "s"})
        for _ in range(1000):
            self.a.advance(0.01)
            self.p.tick()
        self.assertLessEqual(len(self.p.settle_samples), 2)
        self.assertEqual(self.p.stop_evidence, "measured_settled")

    def test_gripper_expiry_before_dispatch(self):
        original = self.a.gripper_terminal

        def delayed():
            self.a.t += 0.4
            return original()

        self.a.gripper_terminal = delayed
        with self.assertRaisesRegex(Refused, "expired_before_dispatch"):
            self.p.gripper(self.cmd(opening_ratio=0.3))
        self.assertFalse(any(isinstance(x, tuple) for x in self.a.sent))

    def test_gripper_no_reversal_queue(self):
        self.p.gripper(self.cmd(opening_ratio=0.3))
        data = self.cmd(opening_ratio=0.8)
        data["sequence"] = 2
        with self.assertRaisesRegex(Refused, "gripper_busy"):
            self.p.gripper(data)
        self.assertEqual(self.a.sent, [("gripper", 0.3)])

    def test_terminal_ack_not_stop(self):
        self.p.gripper(self.cmd(opening_ratio=0.3))
        self.p.stop({"session_id": "s"})
        for _ in range(5):
            self.a.advance()
            self.p.tick()
        with self.assertRaisesRegex(Refused, "physical_stop_unproven"):
            self.p.end({"session_id": "s"})

    def test_stop_send_serialized(self):
        entered = threading.Event()
        release = threading.Event()
        original = self.a.publish

        def publish(pose):
            entered.set()
            release.wait(2)
            original(pose)

        self.a.publish = publish
        thread = threading.Thread(
            target=lambda: self.p.pose(self.cmd(pose7=[0.001, 0, 0, 0, 0, 0, 1]))
        )
        thread.start()
        self.assertTrue(entered.wait(2))
        stopped = threading.Event()
        stopthread = threading.Thread(
            target=lambda: (self.p.stop({"session_id": "s"}), stopped.set())
        )
        stopthread.start()
        self.assertFalse(stopped.wait(0.02))
        release.set()
        thread.join(2)
        stopthread.join(2)
        self.assertEqual(self.a.sent[-1], [0, 0, 0, 0, 0, 0, 1])
        self.assertFalse(self.p.open)

    def test_legacy_dispatch_owns_serialization(self):
        entered = threading.Event()
        release = threading.Event()
        self.p.stop({"session_id": "s"})
        for _ in range(4):
            self.a.advance()
            self.p.tick()
        self.p.end({"session_id": "s"})
        t = threading.Thread(
            target=lambda: self.p.legacy(lambda: (entered.set(), release.wait(2)))
        )
        t.start()
        self.assertTrue(entered.wait(2))
        started = threading.Event()
        u = threading.Thread(
            target=lambda: (
                self.p.start(
                    {"session_id": "t", "limits": LIMITS, "stop_limits": STOP}
                ),
                started.set(),
            )
        )
        u.start()
        self.assertFalse(started.wait(0.02))
        release.set()
        t.join(2)
        u.join(2)
        self.assertTrue(started.is_set())


class Future:
    def __init__(self):
        self.callbacks = []

    def add_done_callback(self, cb):
        self.callbacks.append(cb)

    def result(self):
        return self.value

    def finish(self, value):
        self.value = value
        for cb in self.callbacks:
            cb(self)


class ActionTests(unittest.TestCase):
    def test_late_acceptance_cancel_and_terminal(self):
        tracker = AsyncGoal()
        accept = Future()
        result = Future()
        cancelled = []
        handle = SimpleNamespace(
            accepted=True,
            get_result_async=lambda: result,
            cancel_goal_async=lambda: cancelled.append(1),
        )
        tracker.begin(lambda: accept)
        tracker.cancel()
        accept.finish(handle)
        self.assertEqual(cancelled, [1])
        self.assertFalse(tracker.terminal)
        result.finish(SimpleNamespace(status=5))
        self.assertTrue(tracker.terminal)

    def test_unknown_result_retains_ownership(self):
        tracker = AsyncGoal()
        accept = Future()
        result = Future()
        tracker.begin(lambda: accept)
        accept.finish(SimpleNamespace(accepted=True, get_result_async=lambda: result))
        result.finish(SimpleNamespace(status=2))
        self.assertFalse(tracker.terminal)


if __name__ == "__main__":
    unittest.main()
