"""Structured progress for conversion and inspection jobs.

Written only by the parent process (workers report back through futures),
throttled, and replaced atomically so a reader never sees a torn file.
"""

import json
import os
import time
from pathlib import Path


class Progress:
    def __init__(self, path: Path | None, stages: list[str], interval: float = 0.5):
        self.path = Path(path) if path else None
        self.stages = stages
        self.interval = interval
        self.started = time.time()
        self.stage_started = self.started
        self.state = {
            "stages": stages,
            "stage": stages[0] if stages else "",
            "stage_index": 0,
            "done": 0,
            "total": 0,
            "current": "",
            "warnings": [],
            "started_at": self.started,
        }
        self._last = 0.0

    def stage(self, name: str, total: int = 0):
        self.state.update(
            stage=name,
            stage_index=self.stages.index(name) if name in self.stages else 0,
            done=0,
            total=total,
            current="",
        )
        self.stage_started = time.time()
        self._write(force=True)

    def advance(self, current: str = "", step: int = 1):
        self.state["done"] += step
        self.state["current"] = current
        self._write()

    def warn(self, message: str):
        self.state["warnings"].append(message)
        self._write(force=True)

    def finish(self, status: str = "done"):
        self.state.update(stage=status, stage_index=len(self.stages))
        self._write(force=True)

    def _write(self, force: bool = False):
        now = time.time()
        if not self.path or (not force and now - self._last < self.interval):
            return
        self._last = now
        done, total = self.state["done"], self.state["total"]
        elapsed = now - self.stage_started
        eta = elapsed / done * (total - done) if done and total else None
        value = {
            **self.state,
            "updated_at": now,
            "elapsed_seconds": now - self.started,
            "eta_seconds": eta,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
        temp.write_text(json.dumps(value))
        os.replace(temp, self.path)
