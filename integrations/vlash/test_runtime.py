"""Deterministic ordering, reset and protocol checks; no network or robot."""

import importlib.util
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

spec = importlib.util.spec_from_file_location(
    "vlash_runtime_tested", Path(__file__).with_name("runtime.py")
)
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def meta():
    return {
        "config": "pi05_fr3_all_state",
        "action_dim": 7,
        "action_horizon": 4,
        "discrete_state_input": True,
        "ee_frame": "franka_hand_tcp",
        "gripper_convention": "1.0=open, 0.0=closed, commanded not measured",
    }


def chunk(start):
    result = np.zeros((4, 7), dtype=np.float32)
    result[:, 0] = np.arange(start, start + 4)
    return result


class RuntimeTests(unittest.TestCase):
    def test_early_reply_waits_for_end_and_keeps_first_row(self):
        seen = []
        replied = threading.Event()

        def request(obs, future):
            seen.append((obs, future))

            def infer():
                if future is not None:
                    replied.set()
                return chunk(0 if future is None else 4), {}

            return infer

        scheduler = runtime.VlashChunker(request, overlap_steps=2, horizon=4)
        try:
            values = [scheduler.next_action({"tick": i})[0] for i in range(3)]
            self.assertTrue(replied.wait(2))
            values.extend(scheduler.next_action({"tick": i})[0] for i in range(3, 6))
            self.assertEqual(values, list(range(6)))
            self.assertEqual(seen[1][0], {"tick": 2})
            self.assertEqual(seen[1][1][0], 3)
        finally:
            scheduler.close()

    def test_reset_drains_old_reply_before_new_request(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        active = 0
        lock = threading.Lock()

        def request(obs, future):
            def infer():
                nonlocal active
                with lock:
                    active += 1
                    self.assertEqual(active, 1)
                try:
                    if future is not None:
                        entered.set()
                        self.assertTrue(release.wait(2))
                    calls.append(future is not None)
                    return chunk(40 if future is not None else 0), {}
                finally:
                    with lock:
                        active -= 1

            return infer

        scheduler = runtime.VlashChunker(request, overlap_steps=2, horizon=4)
        try:
            for _ in range(3):
                scheduler.next_action({})
            self.assertTrue(entered.wait(2))
            scheduler.reset()
            release.set()
            self.assertEqual(scheduler.next_action({})[0], 0)
            self.assertEqual(calls, [False, True, False])
            self.assertEqual(scheduler.stats["discarded_replies"], 1)
        finally:
            release.set()
            scheduler.close()

    def test_clamp_discards_predicted_state(self):
        scheduler = runtime.VlashChunker(
            lambda o, f: lambda: (chunk(0), {}), overlap_steps=2, horizon=4
        )
        try:
            raw = scheduler.next_action({})
            actual = raw.copy()
            actual[0] += 0.1
            scheduler.action_adjusted(raw, actual)
            self.assertIsNone(scheduler.chunk)
            self.assertEqual(scheduler.stats["adjusted_targets"], 1)
        finally:
            scheduler.close()

    def test_protocol_refuses_plain_rtc_or_mismatched_overlap(self):
        args = SimpleNamespace(rtc=False, vlash_overlap_steps=2)
        with self.assertRaises(ValueError):
            runtime.check_server(meta(), args)
        server = meta()
        server["vlash"] = runtime.capabilities(server, 2)
        runtime.check_server(server, args)
        args.rtc = True
        with self.assertRaises(ValueError):
            runtime.check_server(server, args)
        args.rtc = False
        args.vlash_overlap_steps = 1
        with self.assertRaises(ValueError):
            runtime.check_server(server, args)

    def test_future_state_is_used_before_original_policy_transforms(self):
        class Policy:
            metadata = meta()

            def infer(self, observation):
                self.seen = observation
                return {"actions": chunk(0)}

        original = Policy()
        policy = runtime.FutureStatePolicy(original, 2)
        observation = {
            "observation/state": np.zeros(7),
            "vlash_future_state": np.ones(7),
            "vlash_protocol_version": 1,
        }
        result = policy.infer(observation)
        np.testing.assert_equal(original.seen["observation/state"], np.ones(7))
        np.testing.assert_equal(observation["observation/state"], np.zeros(7))
        self.assertNotIn("vlash_future_state", original.seen)
        self.assertTrue(result["vlash"]["future_state_used"])
        self.assertFalse(policy.metadata["vlash"]["delay_training_verified"])
        with self.assertRaises(ValueError):
            policy.infer({**observation, "rtc_prev_actions": []})

    def test_unmarked_plain_request_is_refused(self):
        class Policy:
            metadata = meta()

            def infer(self, observation):
                raise AssertionError("must refuse before model inference")

        policy = runtime.FutureStatePolicy(Policy(), 2)
        for marker in (None, True, 2):
            with self.assertRaises(ValueError):
                policy.infer(
                    {"observation/state": np.zeros(7), "vlash_protocol_version": marker}
                )

    def test_binarization_preserves_prefetch_and_future_command_state(self):
        futures = []

        def request(obs, future):
            if future is not None:
                futures.append(future.copy())
            result = chunk(0)
            result[:, 6] = 0.7
            return lambda: (result, {})

        scheduler = runtime.VlashChunker(
            request, overlap_steps=2, horizon=4, binarize_gripper=True
        )
        try:
            for _ in range(4):
                action = scheduler.next_action({})
                self.assertEqual(action[6], 1)
                scheduler.action_adjusted(action, action.copy())
            self.assertEqual(scheduler.stats["prefetches"], 1)
            self.assertEqual(scheduler.stats["adjusted_targets"], 0)
            self.assertEqual(futures[0][6], 1)
        finally:
            scheduler.close()

    def test_state_blind_and_invalid_overlap_are_refused(self):
        for overlap in (0, 4, True, 1.5):
            with self.assertRaises(ValueError):
                runtime.capabilities(meta(), overlap)
        source = meta()
        source["discrete_state_input"] = False
        with self.assertRaises(ValueError):
            runtime.capabilities(source, 2)

    def test_invalid_reply_is_refused(self):
        s = runtime.VlashChunker(
            lambda o, f: lambda: (np.zeros((4, 6)), {}), overlap_steps=2, horizon=4
        )
        try:
            with self.assertRaises(ValueError):
                s.next_action({})
        finally:
            s.close()

    def test_installer_is_idempotent_and_refuses_overwrite(self):
        spec = importlib.util.spec_from_file_location(
            "isolated_installer", Path(__file__).with_name("install.py")
        )
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            hooks = root / "examples/fr3_local"
            hooks.mkdir(parents=True)
            (hooks / "run_robotiq_client.py").write_text(
                "def _run_episode(*, scheduler_factory=None): pass"
            )
            (hooks / "run_robotiq_client_collect_data.py").write_text(
                "def main(*, server_validator=None): pass"
            )
            self.assertFalse(installer.install(root)["applied"])
            self.assertFalse((root / "lab/vlash").exists())
            installer.install(root, True)
            installer.install(root, True)
            target = root / "lab/vlash/runtime.py"
            target.write_text("# changed by owner")
            with self.assertRaises(ValueError):
                installer.install(root, True)
            self.assertEqual(target.read_text(), "# changed by owner")

    def test_installer_refuses_missing_client_extension(self):
        spec = importlib.util.spec_from_file_location(
            "isolated_installer", Path(__file__).with_name("install.py")
        )
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            hooks = root / "examples/fr3_local"
            hooks.mkdir(parents=True)
            (hooks / "run_robotiq_client.py").write_text("def _run_episode(): pass")
            with self.assertRaises(ValueError):
                installer.install(root, True)
            self.assertFalse((root / "lab/vlash").exists())

    def test_installer_refuses_symlink_root(self):
        spec = importlib.util.spec_from_file_location(
            "isolated_installer", Path(__file__).with_name("install.py")
        )
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            real = root / "real"
            real.mkdir()
            link = root / "link"
            link.symlink_to(real)
            with self.assertRaises(ValueError):
                installer.install(link, True)


if __name__ == "__main__":
    unittest.main()
