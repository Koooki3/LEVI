"""Opt-in built-in pipeline integration check; generates only fixture data."""

import json
import subprocess
import time

import httpx
import numpy as np
import pandas as pd

from levi.paths import ROOT, inside

run = inside(ROOT / "tmp/build/levi-conversion-fixture")
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
client = httpx.Client(base_url="http://127.0.0.1:7860", timeout=60)
plan = client.post(
    "/api/levi/jobs/plan", json={"stage": "pipeline", "source": str(raw), "fps": 10}
)
plan.raise_for_status()
job = plan.json()
result = client.post(f"/api/levi/jobs/{job['id']}/run", json={})
result.raise_for_status()
for _ in range(60):
    time.sleep(0.5)
    data = next(j for j in client.get("/api/levi/jobs").json() if j["id"] == job["id"])
    if data["status"] in ("succeeded", "failed"):
        break
assert data["status"] == "succeeded", data
assert data.get("dataset"), data
report = client.post(
    "/api/levi/diagnostics", json={"repo_id": data["dataset"], "decode_video": True}
)
report.raise_for_status()
print(
    json.dumps(
        {"job": data, "diagnostics": report.json()}, ensure_ascii=False, indent=2
    )
)
(run / "result.json").write_text(json.dumps(data, indent=2))
