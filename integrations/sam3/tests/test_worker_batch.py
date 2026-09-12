"""CPU-only tests for the per-item batch loop in ``worker.py``.

``levi_sam3_worker.worker`` only imports torch/sam3 lazily inside
``run_plan``/``_ensure_checkpoint``, so this module is importable — and
``_run_batch`` is callable — without a CUDA host, torch, or the sam3 package.
Run with: integrations/sam3/.venv/bin/python -m unittest discover tests
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from levi_sam3_worker import worker


def _plan(episode_indices: list[int], camera_keys: list[str]) -> dict:
    return {"episode_indices": episode_indices, "camera_keys": camera_keys}


class RunBatchTests(unittest.TestCase):
    def test_all_pairs_succeed(self) -> None:
        calls: list[tuple[int, str]] = []

        def fake_run_episode_camera(_predictor, _root, _info, *, plan, episode_index, camera_key, np):
            calls.append((episode_index, camera_key))
            return [{"episode_index": episode_index, "camera_key": camera_key}]

        with mock.patch.object(worker, "_run_episode_camera", fake_run_episode_camera):
            annotations, item_errors = worker._run_batch(
                predictor=object(),
                root=Path("."),
                info={},
                plan=_plan([0, 1], ["cam_a", "cam_b"]),
                np=None,
                progress_path=None,
            )

        self.assertEqual(len(annotations), 4)
        self.assertEqual(item_errors, [])
        self.assertEqual(
            sorted(calls),
            [(0, "cam_a"), (0, "cam_b"), (1, "cam_a"), (1, "cam_b")],
        )

    def test_partial_failure_continues_and_reports_item_errors(self) -> None:
        def fake_run_episode_camera(_predictor, _root, _info, *, plan, episode_index, camera_key, np):
            if episode_index == 1:
                raise FileNotFoundError(f"no video for episode {episode_index}")
            return [{"episode_index": episode_index, "camera_key": camera_key}]

        with mock.patch.object(worker, "_run_episode_camera", fake_run_episode_camera):
            annotations, item_errors = worker._run_batch(
                predictor=object(),
                root=Path("."),
                info={},
                plan=_plan([0, 1, 2], ["cam_a"]),
                np=None,
                progress_path=None,
            )

        self.assertEqual(len(annotations), 2)
        self.assertEqual(len(item_errors), 1)
        self.assertEqual(item_errors[0]["episode_index"], 1)
        self.assertEqual(item_errors[0]["camera_key"], "cam_a")
        self.assertIn("no video for episode 1", item_errors[0]["error"])

    def test_all_pairs_fail_reports_every_item_error(self) -> None:
        def fake_run_episode_camera(*_args, **_kwargs):
            raise RuntimeError("boom")

        with mock.patch.object(worker, "_run_episode_camera", fake_run_episode_camera):
            annotations, item_errors = worker._run_batch(
                predictor=object(),
                root=Path("."),
                info={},
                plan=_plan([0, 1], ["cam_a"]),
                np=None,
                progress_path=None,
            )

        self.assertEqual(annotations, [])
        self.assertEqual(len(item_errors), 2)

    def test_progress_file_written_after_each_pair(self) -> None:
        def fake_run_episode_camera(_predictor, _root, _info, *, plan, episode_index, camera_key, np):
            return [{"episode_index": episode_index, "camera_key": camera_key}]

        with TemporaryDirectory() as tmp:
            progress_path = Path(tmp) / "progress.json"
            with mock.patch.object(worker, "_run_episode_camera", fake_run_episode_camera):
                worker._run_batch(
                    predictor=object(),
                    root=Path("."),
                    info={},
                    plan=_plan([0, 1], ["cam_a"]),
                    np=None,
                    progress_path=progress_path,
                )
            payload = json.loads(progress_path.read_text())
        self.assertEqual(payload["done"], 2)
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["current_episode"], 1)
        self.assertEqual(payload["current_camera"], "cam_a")


if __name__ == "__main__":
    unittest.main()
