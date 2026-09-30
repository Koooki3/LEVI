"""A fake evaluation client for the live-service tests: writes rollout
directories the way the robot side does (interface C1), half-written ones
included, and the session files (interface C2). No robot, no GPU."""

import csv
import json
import shutil
import time
from pathlib import Path

import pandas as pd
from test_formats import make_demo

FRAMES = 30
_TEMPLATE: dict = {}


def template(tmp_factory) -> Path:
    """One demo built once per test session (ffmpeg is the slow part)."""
    if "demo" not in _TEMPLATE:
        root = Path(tmp_factory.mktemp("live-template"))
        demo = root / "demo_template"
        make_demo(demo, n=FRAMES, fps=10.0, outcome="failure")
        # close -> open -> close: one release for the anchored review.
        rows = pd.read_csv(demo / "gripper_state.csv")
        rows["last_gripper_command"] = (
            ["open"] * 6 + ["close"] * 12 + ["open"] * 6 + ["close"] * (FRAMES - 24)
        )
        rows.to_csv(demo / "gripper_state.csv", index=False)
        (demo / "events.csv").write_text(
            "timestamp_sec,frame_index,event\n"
            "1.0,1,start_demo\n2.0,18,gripper\n3.0,30,episode_end\n"
        )
        _TEMPLATE["demo"] = demo
    return _TEMPLATE["demo"]


def metadata(n, *, text, finished=True, outcome="unlabeled", stalled=(), match=True):
    meta = {
        "data_source": "policy_rollout",
        "task_description": text,
        "demo_index": n,
        "created_at": "2026-10-01T10:00:00",
        "frame_count": FRAMES,
        "success_flag_final": 0,
        "media_storage": {"video_frames_match_csv": bool(match)},
        "cameras": {"stall_detection": {"stalled": list(stalled)}},
        "eval": {"outcome": outcome, "counted": True, "verdict_by": "pending-levi"},
    }
    if finished:
        meta["stopped_at"] = "2026-10-01T10:01:00"
    return meta


class Rollouts:
    """``<root>/<group>/<task>/demo_NNNN`` writer."""

    def __init__(
        self,
        root,
        source,
        group="pi05_fake",
        task="stack_the_plates",
        text="stack the plates of same color together",
    ):
        self.root = Path(root)
        self.source = Path(source)
        self.group, self.task, self.text = group, task, text
        self.dir = self.root / group / task
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "task_description.txt").write_text(text + "\n")

    def demo(self, n) -> Path:
        return self.dir / f"demo_{n:04d}"

    def begin(self, n):
        """A demo the client is still writing: files present, no stopped_at,
        no completion marker."""
        demo = self.demo(n)
        shutil.copytree(self.source, demo)
        (demo / "metadata.json").write_text(
            json.dumps(metadata(n, text=self.text, finished=False))
        )
        (demo / "events.csv").write_text(
            "timestamp_sec,frame_index,event\n1.0,1,start_demo\n"
        )
        (demo / "side_camera_raw.avi").write_bytes(b"")
        return demo

    def finish(self, n, **kwargs):
        demo = self.demo(n)
        (demo / "side_camera_raw.avi").unlink(missing_ok=True)
        shutil.copy2(self.source / "events.csv", demo / "events.csv")
        (demo / "metadata.json").write_text(
            json.dumps(metadata(n, text=self.text, **kwargs))
        )
        (demo / ".complete").touch()
        return demo

    def write(self, n, **kwargs):
        self.begin(n)
        return self.finish(n, **kwargs)

    def legacy(self, n, age_s=3600):
        """A demo from before the marker existed: no .complete."""
        demo = self.write(n)
        (demo / ".complete").unlink()
        import os

        old = time.time() - age_s
        for path in [demo, *demo.iterdir()]:
            os.utime(path, (old, old))
        return demo

    def incomplete(self, n, reason="fr3_fault"):
        demo = self.demo(n)
        if demo.exists():
            demo.rename(self.dir / f"incomplete_{n:04d}")
        else:
            shutil.copytree(self.source, self.dir / f"incomplete_{n:04d}")
        target = self.dir / f"incomplete_{n:04d}"
        (target / "metadata.json").write_text(
            json.dumps(
                {
                    "eval": {
                        "outcome": "aborted",
                        "abort_reason": reason,
                        "counted": False,
                    },
                    "stopped_at": "2026-10-01T10:02:00",
                }
            )
        )
        return target

    def discard(self, n):
        self.demo(n).rename(self.dir / f"discarded_{n:04d}")

    def session(
        self, state, *, started_at=None, levi=True, pid=None, updated_at=None, reason=""
    ):
        folder = self.root / ".eval_sessions"
        folder.mkdir(exist_ok=True)
        import os

        now = time.time()
        (folder / f"{self.group}__{self.task}.json").write_text(
            json.dumps(
                {
                    "schema": "levi.eval.session.v1",
                    "session_id": "s1",
                    "pid": pid if pid is not None else os.getpid(),
                    "host": __import__("socket").gethostname(),
                    "started_at": started_at if started_at is not None else now - 30,
                    "updated_at": updated_at if updated_at is not None else now,
                    "state": state,
                    "reason": reason,
                    "prompt": self.text,
                    "group": self.group,
                    "task_folder": self.task,
                    "policy": {
                        "config": "pi05_fr3_all_state",
                        "checkpoint_dir": "/ckpt/pi05_fr3_all_step49999/",
                    },
                    "levi": {"enabled": levi, "reset_wait_s": 10},
                    "episode": {"no": 1, "target": 5},
                }
            )
        )


def read_events(path):
    with Path(path).open(newline="") as handle:
        return list(csv.DictReader(handle))
