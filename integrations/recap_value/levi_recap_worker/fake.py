"""Deterministic stand-in values, no model and no Torch.

For tests and trying the UI without a checkpoint. V is the RLinf-style
normalised return a perfect critic would predict — -(frames to go) on a
success, a further failure penalty on a failure, over the longest episode
plus |failure_reward| — plus a small per-episode wiggle seeded by the
episode, clipped to [v_min, v_max]. Success episodes rise toward 0; failures
stay lower.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq


def _frames(plan: dict, episode: int, length: int) -> np.ndarray:
    ds = plan["dataset"]
    path = Path(ds["root"]) / ds["data_path"].format(
        episode_chunk=episode // int(ds["chunks_size"]), episode_index=episode
    )
    frames = pq.read_table(path, columns=["frame_index"]).column("frame_index")
    frames = frames.to_numpy().astype(np.int64)
    if len(frames) != length:
        raise ValueError(f"episode {episode}: {len(frames)} rows, planned {length}")
    return frames


def curve(n: int, success: bool, episode: int, scale: float, penalty: float):
    t = np.arange(n, dtype=np.float64)
    to_go = (n - 1) - t
    base = -(to_go + (0.0 if success else penalty)) / scale
    rng = np.random.default_rng(1_000_003 * (episode + 1) + n)
    freq = rng.uniform(1.0, 3.0)
    phase = rng.uniform(0.0, 2.0 * np.pi)
    wiggle = 0.04 * np.sin(2.0 * np.pi * freq * t / max(n, 1) + phase)
    return base + wiggle


def values(plan: dict, progress) -> tuple[dict, dict]:
    manifest = plan["checkpoint"]["manifest"]
    v_min, v_max = float(manifest["v_min"]), float(manifest["v_max"])
    penalty = abs(float(manifest.get("failure_reward", -300.0)))
    longest = max(int(e["length"]) for e in plan["episodes"])
    scale = float(longest) + penalty or 1.0
    delay = float(os.environ.get("LEVI_RECAP_VALUE_FAKE_DELAY_SECONDS", "0"))
    out = {}
    done = 0
    for item in plan["episodes"]:
        ep, n = int(item["episode_index"]), int(item["length"])
        frames = _frames(plan, ep, n)
        if "keep" in item:  # static-filtered: only the kept frames
            frames = frames[np.asarray(item["keep"], dtype=np.int64)]
        success = item.get("success") is not False
        value = np.clip(curve(len(frames), success, ep, scale, penalty), v_min, v_max)
        out[ep] = (frames, value.astype(np.float32))
        if delay:
            time.sleep(delay)
        done += len(frames)
        progress.values(done)
    return out, {"provider": "fake", "model": "deterministic time-to-go curve"}
