"""CPU-only synthetic stage cost; stdout is a self-contained JSON record.

Run with the active worktree on PYTHONPATH. No files, sockets, robot SDK, GPU,
video writers, policy inference or model calls are used by the benchmark.
"""

import json
import platform
import time
from pathlib import Path
from types import SimpleNamespace as NS

import cv2
import numpy as np

from levi.automatic.event_window import EventObserver, Ledger
from levi.events.stream import StreamConfig
from levi.live.online import MAX_SIDE


def measure(width, height):
    rng = np.random.default_rng(42)
    pixels = {
        view: rng.integers(0, 256, (height, width, 3), dtype=np.uint8)
        for view in ("side", "wrist")
    }
    clock = NS(domain="host-mono:cpu-benchmark", now=time.monotonic_ns)
    ctx = NS(
        stream="cpu-only-epoch",
        episode_id="cpu-only.forward.0000",
        role="forward",
        last_step=0,
        rollout=NS(path=Path("/tmp/cpu-only-event-ledger")),
    )

    def observe():
        return {
            "observed_ns": clock.now(),
            "images_bgr": pixels,
            "pose": [0.0] * 7,
            "robot_state": {},
            "state_fresh": True,
        }

    observer = EventObserver(NS(observe=observe), clock, step_of=lambda: ctx.last_step)
    observer.begin(ctx.stream)
    ledger = Ledger(
        observer, lambda: ctx, StreamConfig(max_history=128), "cpu-only", cv2
    )
    samples, observation, peak = [], [], 0
    for step in range(220):
        ctx.last_step = step
        started = time.perf_counter_ns()
        obs = observer.observe()
        staged = time.perf_counter_ns()
        ledger.stage(ctx.rollout.path, obs)
        ended = time.perf_counter_ns()
        peak = max(peak, ledger.retained_bytes)
        if step >= 20:
            samples.append((ended - staged) / 1e6)
            observation.append((staged - started) / 1e6)
    return {
        "shape": [height, width, 3],
        "views": 2,
        "samples": len(samples),
        "warmup": 20,
        "entropy": "uniform uint8 noise seed42",
        "max_history": 128,
        "stage_mean_ms": float(np.mean(samples)),
        "stage_p50_ms": float(np.percentile(samples, 50)),
        "stage_p95_ms": float(np.percentile(samples, 95)),
        "stage_max_ms": float(np.max(samples)),
        "observer_mean_ms": float(np.mean(observation)),
        "observer_p95_ms": float(np.percentile(observation, 95)),
        "peak_retained_jpeg_bytes": peak,
        "final_recent_count": len(ledger.rows),
    }


if __name__ == "__main__":
    print(
        json.dumps(
            {
                "schema": "levi.event_window.synthetic_cpu_measurement.v1",
                "measured_wall_ns": time.time_ns(),
                "python": platform.python_version(),
                "opencv": cv2.__version__,
                "numpy": np.__version__,
                "platform": platform.platform(),
                "opencv_threads": cv2.getNumThreads(),
                "spec_max_side": MAX_SIDE,
                "scope": "shared-host synthetic CPU only; no video writer/action/model/control deadline measurement",
                "measurements": [
                    measure(640, 480),
                    measure(MAX_SIDE, MAX_SIDE * 3 // 4),
                ],
            },
            indent=2,
        )
    )
