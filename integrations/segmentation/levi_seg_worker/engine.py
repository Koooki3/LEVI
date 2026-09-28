"""Inference + tracking engines shared by live sessions and labelling jobs.

``StudentEngine`` runs the distilled RF-DETR-Seg student with one ByteTrack
per camera. ``FakeEngine`` needs only NumPy: deterministic discs that move
with the frame index, so LEVI's CPU tests exercise the same protocol, timing
and sidecar paths without Torch or a GPU.
"""

from __future__ import annotations

from typing import Any

import numpy as np


class Engine:
    provider = "base"
    precision = "none"

    def reset(self, camera: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def process(self, items: list[tuple[str, int, np.ndarray]]) -> list[list[dict[str, Any]]]:
        """``(camera, frame_index, rgb)`` per item -> instances per item."""
        raise NotImplementedError  # pragma: no cover

    def describe(self) -> dict[str, Any]:
        return {"provider": self.provider, "precision": self.precision}


class StudentEngine(Engine):
    provider = "student"

    def __init__(self, model: dict[str, Any], cameras: list[str], fps: float, *, batch: int | None = None):
        from .student import Student, TrackerSettings

        self.model_info = model
        self.student = Student(
            model["weights"],
            architecture=model.get("architecture", "rf-detr-seg-small"),
            concepts=model.get("class_names") or model.get("concepts"),
            confidence=float(model.get("confidence", 0.4)),
            fp16=bool(model.get("fp16", True)),
            batch=batch or max(1, len(cameras)),
        )
        self.precision = self.student.precision
        self.settings = TrackerSettings.from_plan(model.get("tracker"))
        self.fps = fps
        self.trackers: dict[str, Any] = {}
        for camera in cameras:
            self.reset(camera)

    def reset(self, camera: str) -> None:
        from .student import Tracker

        previous = self.trackers.get(camera)
        if previous is None:
            self.trackers[camera] = Tracker(self.fps, self.settings)
        else:
            previous.reset()

    def process(self, items):
        detections = self.student.predict([rgb for _, _, rgb in items])
        return [
            self.trackers[camera].update(det, self.student.label)
            for (camera, _, _), det in zip(items, detections, strict=True)
        ]

    def describe(self):
        return {
            "provider": self.provider,
            "precision": self.precision,
            "model": self.model_info.get("name"),
            "architecture": self.model_info.get("architecture"),
            "concepts": list(self.student.names.values()),
        }


class FakeEngine(Engine):
    """Two discs per camera: a moving "robot arm" and a still "cup"."""

    provider = "fake"
    precision = "cpu"

    def __init__(self, cameras: list[str], concepts: list[str] | None = None, delay_ms: float = 0.0):
        self.concepts = (concepts or ["robot arm", "cup"])[:2]
        if len(self.concepts) == 1:
            self.concepts.append("object")
        self.delay = delay_ms / 1000.0
        self.offsets: dict[str, int] = {camera: 0 for camera in cameras}

    def reset(self, camera: str) -> None:
        self.offsets[camera] = self.offsets.get(camera, 0) + 2

    def process(self, items):
        import time

        if self.delay:
            time.sleep(self.delay)
        out = []
        for camera, frame_index, rgb in items:
            h, w = rgb.shape[:2]
            yy, xx = np.ogrid[:h, :w]
            cx = int((0.2 + 0.6 * ((frame_index % 50) / 50.0)) * w)
            discs = [
                (cx, h // 2, max(4, h // 8), self.concepts[0]),
                (w // 4, (3 * h) // 4, max(4, h // 10), self.concepts[1]),
            ]
            found = []
            for k, (x, y, r, concept) in enumerate(discs):
                mask = (xx - x) ** 2 + (yy - y) ** 2 <= r * r
                found.append(
                    {
                        "track_id": self.offsets.get(camera, 0) + k + 1,
                        "concept": concept,
                        "score": 0.9,
                        "bbox": [float(max(0, x - r)), float(max(0, y - r)), float(min(w, x + r + 1)), float(min(h, y + r + 1))],
                        "mask": np.asarray(mask, dtype=bool),
                    }
                )
            out.append(found)
        return out


def build(plan: dict[str, Any], cameras: list[str], fps: float, *, batch: int | None = None) -> Engine:
    model = plan["model"]
    if plan.get("provider") == "fake" or model.get("provider") == "fake":
        return FakeEngine(cameras, model.get("concepts"), float(model.get("fake_delay_ms", 0.0)))
    return StudentEngine(model, cameras, fps, batch=batch)
