"""Chunk acceleration: the rule (accel.py) and the client wrapper (run_collect_accel.py) without openpi, robot or GPU."""

import importlib
import sys
import types
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from accel import ChunkAccel


def chunk(step=0.005, n=10, g=1.0, curve=0.0, x0=0.45, rot=0.0):
    k = np.arange(1, n + 1)
    p = np.column_stack(
        [x0 + step * k, curve * np.sin(np.pi * k / n), np.full(n, 0.25)]
    )
    e = np.column_stack(
        [np.full(n, 3.1) + rot * k / n, np.full(n, 0.01), np.full(n, -0.8)]
    )
    return np.column_stack([p, e, np.full(n, g)]).astype(np.float32)


def on_segment(points, a, b, tol=1e-6):
    ab = b - a
    s = (points - a) @ ab / max(float(ab @ ab), 1e-12)
    return (
        np.all(s >= -tol)
        and np.all(s <= 1 + tol)
        and np.allclose(a + np.outer(s, ab), points, atol=1e-5)
    )


class RuleTests(unittest.TestCase):
    def test_straight_chunk_becomes_a_short_ramp_from_its_first_to_its_last_action(
        self,
    ):
        a, c = ChunkAccel(6, 10.0), chunk()
        out = a.plan(c)
        self.assertLess(len(out), 6)
        np.testing.assert_allclose(out[0], c[0])
        np.testing.assert_allclose(out[-1], c[5])
        self.assertTrue(
            on_segment(
                out[:, :3].astype(float), c[0, :3].astype(float), c[5, :3].astype(float)
            )
        )
        self.assertTrue(np.all(out[:, 6] == 1.0))
        self.assertEqual(a.stats["hurried"], 1)
        self.assertEqual(a.stats["ticks_saved"], 6 - len(out))

    def test_a_gripper_change_anywhere_in_the_chunk_keeps_it_as_it_is(self):
        a = ChunkAccel(6, 10.0)
        c = chunk()
        c[8, 6] = 0.0  # beyond the executed part: an approach to a grasp
        np.testing.assert_array_equal(a.plan(c), c[:6])
        self.assertEqual(a.stats["why_not"]["gripper"], 1)

    def test_no_hurry_for_a_second_after_a_gripper_change(self):
        a = ChunkAccel(6, 10.0)
        a.plan(chunk(g=1.0))
        closed = chunk(g=0.0, step=0.0)
        self.assertEqual(len(a.plan(closed)), 6)  # the change itself
        self.assertEqual(
            len(a.plan(closed)), 6
        )  # 6 actions since it: still holding (10 needed)
        self.assertEqual(a.stats["why_not"]["after_gripper"], 1)
        self.assertLess(len(a.plan(closed)), 6)  # 12 since it

    def test_curved_or_turning_chunks_are_not_hurried(self):
        a = ChunkAccel(6, 10.0)
        np.testing.assert_array_equal(a.plan(chunk(curve=0.03)), chunk(curve=0.03)[:6])
        a = ChunkAccel(6, 10.0)
        np.testing.assert_array_equal(a.plan(chunk(rot=0.5)), chunk(rot=0.5)[:6])
        self.assertEqual(a.stats["why_not"]["rotation"], 1)

    def test_consecutive_targets_never_jump_more_than_max_jump(self):
        a = ChunkAccel(8, 10.0)
        out = a.plan(chunk(step=0.012))
        self.assertLessEqual(
            float(np.max(np.linalg.norm(np.diff(out[:, :3], axis=0), axis=1))),
            a.max_jump + 1e-9,
        )

    def test_after_a_ramp_actions_behind_its_end_are_dropped_only_when_the_motion_goes_on(
        self,
    ):
        a = ChunkAccel(6, 10.0)
        first = a.plan(chunk())
        ahead = chunk(
            x0=float(first[-1, 0]) - 0.012
        )  # observed 12 mm behind the ramp's end, same direction
        out = a.plan(ahead)
        self.assertGreater(a.stats["dropped_behind"], 0)
        self.assertGreaterEqual(float(out[0, 0]), float(first[-1, 0]) - 1e-6)
        back = chunk(
            step=-0.005, x0=float(out[-1, 0])
        )  # the policy turns back: nothing is dropped
        before = a.stats["dropped_behind"]
        a.plan(back)
        self.assertEqual(a.stats["dropped_behind"], before)

    def test_a_plan_the_arm_never_finished_is_forgotten(self):
        a = ChunkAccel(6, 10.0)
        first = a.plan(chunk())
        far = np.asarray(first[-1], np.float64).copy()
        far[0] -= (
            0.05  # the arm is 5 cm short of the last target: the plan was abandoned (rejected jump)
        )
        a.plan(chunk(x0=float(first[-1, 0]) - 0.012), far)
        self.assertEqual(a.stats["dropped_behind"], 0)

    def test_a_sideways_turn_after_a_ramp_keeps_all_its_actions(self):
        # review 2026-10-31: the old "keeps going" test was only the sign of the drift
        a = ChunkAccel(6, 10.0)
        first = a.plan(chunk())
        end = np.asarray(first[-1], np.float64)
        k = np.arange(1, 11)
        side = chunk(step=0.0001, x0=float(end[0]) - 0.012)
        side[:, 1] = 0.012 * k
        a.plan(side, np.r_[end[:3] - [0.015, 0, 0], end[3:]])
        self.assertEqual(a.stats["dropped_behind"], 0)

    def test_dropping_never_runs_past_the_open_loop_horizon(self):
        a = ChunkAccel(8, 10.0)
        first = a.plan(chunk())
        c = chunk(
            x0=float(first[-1, 0]) - 0.012, curve=0.03
        )  # not hurried, but continues
        out = a.plan(c)
        self.assertGreater(a.stats["dropped_behind"], 0)
        np.testing.assert_allclose(out[-1], c[7])  # the client's own last action

    def test_a_gripper_chunk_after_a_ramp_is_left_exactly_as_the_policy_gave_it(self):
        a = ChunkAccel(6, 10.0)
        first = a.plan(chunk())
        c = chunk(x0=float(first[-1, 0]) - 0.012)
        c[2:, 6] = 0.0  # closes at index 2
        np.testing.assert_array_equal(a.plan(c), c[:6])
        self.assertEqual(a.stats["dropped_behind"], 0)

    def test_ramp_rotation_steps_are_bounded(self):
        a = ChunkAccel(8, 10.0)
        c = chunk(rot=0.14)  # 0.14 rad over the chunk, straight
        out = a.plan(c)
        steps = np.abs(np.diff(out[:, 3].astype(np.float64)))
        self.assertLessEqual(float(steps.max()), a.max_rot_jump + 1e-9)

    def test_episode_start_compares_the_gripper_with_the_measured_state(self):
        a = ChunkAccel(6, 10.0)
        closed = chunk(g=0.0)
        state = np.r_[closed[0, :6], 1.0]  # the arm's gripper is open
        np.testing.assert_array_equal(a.plan(closed, state), closed[:6])
        self.assertEqual(a.stats["why_not"]["gripper"], 1)

    def test_a_press_and_retract_along_the_chord_is_not_straight(self):
        # final review 2026-10-31: distance to the infinite line called it straight and the press was never sent
        a = ChunkAccel(10, 10.0)
        c = chunk(step=0.0)
        c[:, 2] = [0.24, 0.23, 0.22, 0.21, 0.20, 0.21, 0.22, 0.23, 0.24, 0.25]
        np.testing.assert_array_equal(a.plan(c), c[:10])
        self.assertEqual(a.stats["why_not"]["curved"], 1)

    def test_the_issued_gripper_command_in_the_state_restarts_the_hold(self):
        # the client may throw a planned close away (rejected jump) or send it late (rate limit): the measured
        # state's gripper is what was issued
        a = ChunkAccel(6, 10.0)
        closed = chunk(g=0.0, step=0.0)
        open_state = np.r_[closed[0, :6], 1.0]
        closed_state = np.r_[closed[0, :6], 0.0]
        a.plan(chunk(), open_state)
        self.assertEqual(a.stats["hurried"], 1)
        np.testing.assert_array_equal(
            a.plan(closed, open_state), closed[:6]
        )  # planned close
        self.assertEqual(
            len(a.plan(closed, closed_state)), 6
        )  # issued only now: the hold starts
        self.assertEqual(a.stats["why_not"]["after_gripper"], 1)

    def test_a_nan_gripper_counts_as_closed_like_the_client_binarizes_it(self):
        a = ChunkAccel(6, 10.0)
        c = chunk()
        c[:, 6] = np.nan
        np.testing.assert_array_equal(a.plan(c, np.r_[c[0, :6], 1.0]), c[:6])
        self.assertEqual(a.stats["why_not"]["gripper"], 1)

    def test_the_drop_rule_never_makes_a_rotation_jump(self):
        a = ChunkAccel(6, 10.0)
        first = a.plan(chunk())
        c = chunk(x0=float(first[-1, 0]) - 0.012)
        c[:, 3] = float(first[-1, 3]) + 0.12 * np.arange(10)
        out = a.plan(c)
        self.assertLessEqual(
            abs(float(out[0, 3]) - float(first[-1, 3])), a.max_rot_jump + 1e-9
        )

    def test_reset_forgets_the_episode(self):
        a = ChunkAccel(6, 10.0)
        a.plan(chunk())
        a.reset()
        self.assertEqual(a.stats["chunks"], 0)
        self.assertIsNone(a.last)


class WrapperTests(unittest.TestCase):
    def test_install_wraps_the_query_and_records_settings_and_counts(self):
        rc = types.SimpleNamespace(
            _query_policy=lambda client, obs, state, prompt: chunk(),
            _run_episode=lambda *args, **kwargs: ("success", {}),
        )
        calls = {}

        class Recorder:
            def _session_metadata(self):
                return {"policy": {}}

            def close(
                self,
                outcome,
                verdict_by,
                counted,
                stats,
                discard_if_invalid=False,
                extra=None,
            ):
                calls["close"] = extra
                return "closed"

            def abort(self, reason, stats=None, detail=None):
                calls["abort"] = detail
                return "aborted"

        @__import__("dataclasses").dataclass
        class BaseArgs:
            open_loop_horizon: int = 6
            control_hz: float = 10.0
            rtc: bool = False
            step_by_step: bool = False
            binarize_gripper: bool = True

        sys.modules["run_robotiq_client_collect_data"] = types.SimpleNamespace(
            rc=rc, RolloutRecorder=Recorder, Args=BaseArgs, main=lambda a: None
        )
        sys.modules.setdefault("tyro", types.SimpleNamespace(cli=lambda c: c()))
        sys.modules.pop("run_collect_accel", None)
        mod = importlib.import_module("run_collect_accel")
        args = mod.Args()
        accel = mod.install(args)
        out = rc._query_policy(None, None, None, "p")
        self.assertLess(len(out), 6)
        rec = Recorder()
        self.assertEqual(
            rec._session_metadata()["policy"]["chunk_accel"]["speed_m_s"], accel.speed
        )
        self.assertEqual(rec.close("success", "key", True, {}), "closed")
        self.assertEqual(calls["close"]["chunk_accel"]["hurried"], 1)
        self.assertEqual(accel.stats["chunks"], 0)  # reset for the next episode
        rc._query_policy(None, None, None, "p")
        self.assertEqual(rc._run_episode(), ("success", {}))
        self.assertEqual(
            accel.stats["chunks"], 0
        )  # and at every episode start, recorded or not
        for bad in (
            {"rtc": True},
            {"control_hz": 20.0},
            {"step_by_step": True},
            {"binarize_gripper": False},
        ):
            with self.assertRaises(ValueError):
                mod.main(mod.Args(**bad))
        sys.modules.pop("run_robotiq_client_collect_data", None)
        sys.modules.pop("run_collect_accel", None)


if __name__ == "__main__":
    unittest.main()
