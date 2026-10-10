"""Opt-in built-in pipeline integration check; generates only fixture data.

It talks to the running core directly, over the core's socket with the
person's credential (the way the `levi` CLI does), not through the web
page's port: the web bridge takes writes only from the LEVI page itself (see
docs/API.md, "Trust boundary of the web bridge"). Start `uv run levi serve`
first.
"""

import json
import subprocess
import time

import numpy as np
import pandas as pd

from levi.agent import core
from levi.paths import ROOT, inside


def make_fixture(run):
    """A one-demo raw capture with two cameras under ``run / "raw"``."""
    raw = run / "raw"
    demo = raw / "task_001/demo_001"
    demo.mkdir(parents=True, exist_ok=True)
    (demo.parent / "task_description.txt").write_text("Move the gripper to the target.")
    n = 30
    t = np.arange(n) / 10
    pd.DataFrame(
        {
            "timestamp_sec": t,
            "frame_index": range(n),
            "success_flag": 1,
            "source_stamp_sec": t,
            "px": 0.1 + t * 0.01,
            "py": 0.2,
            "pz": 0.3,
            "qx": 0.0,
            "qy": 0.0,
            "qz": 0.0,
            "qw": 1.0,
        }
    ).to_csv(demo / "end_effector_pose.csv", index=False)
    pd.DataFrame(
        {
            "timestamp_sec": t,
            "frame_index": range(n),
            "success_flag": 1,
            "source_stamp_sec": t,
            "finger_left": 0.01,
            "finger_right": 0.01,
            "gripper_width": 0.02,
            "last_gripper_command": ["open" if i < 15 else "close" for i in range(n)],
        }
    ).to_csv(demo / "gripper_state.csv", index=False)
    for camera in ["side", "wrist"]:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=640x480:rate=10:duration=3",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(demo / f"{camera}_camera.mp4"),
            ],
            check=True,
        )
    return raw


def exercise(raw, *, request=core.request, sleep=time.sleep):
    """Plan, run and diagnose a conversion of ``raw`` through the core."""
    job = request(
        "/api/levi/jobs/plan",
        {"stage": "pipeline", "source": str(raw), "fps": 10},
        human=True,
    )
    request(f"/api/levi/jobs/{job['id']}/run", {}, human=True)
    for _ in range(60):
        sleep(0.5)
        data = next(
            j for j in request("/api/levi/jobs", human=True) if j["id"] == job["id"]
        )
        if data["status"] in ("succeeded", "failed"):
            break
    assert data["status"] == "succeeded", data
    assert data.get("dataset"), data
    report = request(
        "/api/levi/diagnostics",
        {"repo_id": data["dataset"], "decode_video": True},
        human=True,
    )
    return {"job": data, "diagnostics": report}


def main():
    run = inside(ROOT / "tmp/build/levi-conversion-fixture")
    result = exercise(make_fixture(run))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    (run / "result.json").write_text(json.dumps(result["job"], indent=2))


if __name__ == "__main__":
    main()
