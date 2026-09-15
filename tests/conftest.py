import json

import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def dataset(tmp_path):
    root = tmp_path / "fixture"
    (root / "meta").mkdir(parents=True)
    (root / "data/chunk-000").mkdir(parents=True)
    info = {
        "codebase_version": "v2.1",
        "robot_type": "so100_follower",
        "fps": 10,
        "total_episodes": 2,
        "total_frames": 40,
        "total_tasks": 1,
        "chunks_size": 1000,
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
        "features": {
            "action": {
                "dtype": "float32",
                "shape": [2],
                "names": ["shoulder_pan.pos", "gripper.pos"],
            },
            "observation.state": {
                "dtype": "float32",
                "shape": [2],
                "names": ["shoulder_pan.pos", "gripper.pos"],
            },
            "timestamp": {"dtype": "float32", "shape": [1], "names": None},
            "episode_index": {"dtype": "int64", "shape": [1], "names": None},
            "frame_index": {"dtype": "int64", "shape": [1], "names": None},
            "index": {"dtype": "int64", "shape": [1], "names": None},
            "task_index": {"dtype": "int64", "shape": [1], "names": None},
        },
    }
    (root / "meta/info.json").write_text(json.dumps(info))
    (root / "meta/tasks.jsonl").write_text(
        json.dumps({"task_index": 0, "task": "Move the gripper"}) + "\n"
    )
    (root / "meta/episodes.jsonl").write_text(
        "\n".join(
            json.dumps(
                {"episode_index": ep, "length": 20, "tasks": ["Move the gripper"]}
            )
            for ep in range(2)
        )
        + "\n"
    )
    for ep in range(2):
        t = np.arange(20, dtype=float) / 10
        pd.DataFrame(
            {
                "action": [[float(np.sin(x)), float(x)] for x in t],
                "observation.state": [
                    [float(np.sin(x - 0.1)), float(x - 0.1)] for x in t
                ],
                "timestamp": t,
                "episode_index": [ep] * 20,
                "frame_index": range(20),
                "index": range(ep * 20, (ep + 1) * 20),
                "task_index": [0] * 20,
            }
        ).to_parquet(root / f"data/chunk-000/episode_{ep:06d}.parquet")
    return root


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import backend.app as annotations
    from levi import catalog, jobs, service

    state = tmp_path / "outputs"
    monkeypatch.setattr(catalog, "STATE", state)
    monkeypatch.setattr(service, "STATE", state)
    monkeypatch.setattr(jobs, "STATE", state)
    # Auto-named conversion job outputs land directly under this module's
    # own ROOT (see jobs.plan's explicit base=ROOT) — without this, a plain
    # "/api/levi/jobs/plan" call with no custom output writes into the real,
    # live LEVI_WORKSPACE (.state/levi_<job id>/) instead of the test's own
    # tmp_path, leaking real directories on every test run.
    monkeypatch.setattr(jobs, "ROOT", tmp_path)
    monkeypatch.setattr(annotations, "STATE", state)
    monkeypatch.setattr(annotations, "EXPORT_ROOT", tmp_path / "exports")
    # Keep boundary validation enabled; test temp dirs live under the workspace.
    annotations._states.clear()
    return TestClient(service.app)
