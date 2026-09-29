"""The distilled student: RF-DETR-Seg (Apache-2.0) plus ByteTrack (supervision, MIT).

Torch and rfdetr are imported only when a model is built. ``Tracker`` keeps
one ByteTrack per camera stream and a per-track concept vote, so an instance
keeps both its id and its label when a single frame's class flips.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

ARCHITECTURES = {
    "rf-detr-seg-nano": "RFDETRSegNano",
    "rf-detr-seg-small": "RFDETRSegSmall",
    "rf-detr-seg-medium": "RFDETRSegMedium",
}
DEFAULT_ARCHITECTURE = "rf-detr-seg-small"


def _names(model: Any) -> dict[int, str]:
    names = getattr(model, "class_names", None)
    if isinstance(names, dict):
        return {int(k): str(v) for k, v in names.items()}
    if isinstance(names, (list, tuple)):
        return {i: str(v) for i, v in enumerate(names)}
    return {}


class Student:
    """Batched inference; ``predict`` returns one supervision Detections per image."""

    def __init__(
        self,
        weights: str | Path,
        *,
        architecture: str = DEFAULT_ARCHITECTURE,
        concepts: list[str] | None = None,
        confidence: float = 0.4,
        fp16: bool = True,
        batch: int = 1,
    ):
        import rfdetr
        import torch

        cls = getattr(rfdetr, ARCHITECTURES.get(architecture, "RFDETRSegSmall"))
        self.model = cls(pretrain_weights=str(weights))
        self.confidence = float(confidence)
        self.batch = max(1, int(batch))
        self.precision = "fp32"
        names = _names(self.model)
        # A manifest's concept list is authoritative for display names (the
        # checkpoint may store the COCO dataset's category names instead).
        if concepts:
            names = {i: concepts[i] if i < len(concepts) else names.get(i, str(i)) for i in range(max(len(concepts), len(names)))}
        self.names = {i: v.replace("_", " ") for i, v in names.items()}
        # rfdetr >= 1.10 names it ``inference``; older releases
        # ``optimize_for_inference``. Both trace an FP16 copy for ``batch``.
        optimize = getattr(self.model, "inference", None) or getattr(self.model, "optimize_for_inference", None)
        self.optimize_error = None
        if fp16 and torch.cuda.is_available() and optimize is not None:
            try:
                optimize(compile=True, batch_size=self.batch, dtype=torch.float16)
                self.precision = "fp16"
            except Exception as exc:  # noqa: BLE001 - fall back to the eager model
                self.optimize_error = str(exc)
                try:
                    optimize(compile=False, dtype=torch.float16)
                    self.precision = "fp16-eager"
                except Exception:  # noqa: BLE001
                    self.precision = "fp32"

    def predict(self, images: list[np.ndarray]) -> list[Any]:
        """RGB uint8 images (any count); padded to the compiled batch size."""
        if not images:
            return []
        out: list[Any] = []
        for start in range(0, len(images), self.batch):
            chunk = list(images[start : start + self.batch])
            real = len(chunk)
            if self.precision == "fp16" and real < self.batch:
                chunk += [chunk[-1]] * (self.batch - real)
            result = self.model.predict(chunk if len(chunk) > 1 else chunk[0], threshold=self.confidence)
            if not isinstance(result, list):
                result = [result]
            out.extend(result[:real])
        return out

    def label(self, class_id: int) -> str:
        return self.names.get(int(class_id), str(class_id))


@dataclass
class TrackerSettings:
    lost_buffer: int = 300
    activation: float = 0.5
    matching: float = 0.8
    min_frames: int = 1

    @classmethod
    def from_plan(cls, value: dict[str, Any] | None) -> TrackerSettings:
        value = value or {}
        return cls(
            lost_buffer=int(value.get("lost_buffer", 300)),
            activation=float(value.get("activation", 0.5)),
            matching=float(value.get("matching", 0.8)),
            min_frames=int(value.get("min_frames", 1)),
        )


@dataclass
class Tracker:
    """ByteTrack for one camera stream, with ids kept unique across resets."""

    fps: float
    settings: TrackerSettings = field(default_factory=TrackerSettings)
    offset: int = 0
    _bt: Any = None
    _votes: dict[int, dict[str, float]] = field(default_factory=dict)
    _max_id: int = 0

    def __post_init__(self) -> None:
        self._new()

    def _new(self) -> None:
        import supervision as sv

        # supervision scales lost_track_buffer by frame_rate / 30.
        self._bt = sv.ByteTrack(
            frame_rate=max(1, round(self.fps)),
            lost_track_buffer=self.settings.lost_buffer,
            track_activation_threshold=self.settings.activation,
            minimum_matching_threshold=self.settings.matching,
            minimum_consecutive_frames=self.settings.min_frames,
        )

    def reset(self) -> None:
        """A seek breaks temporal continuity: start fresh ids after the last."""
        self.offset = self._max_id
        self._votes.clear()
        self._new()

    def update(self, detections: Any, label_of: Any) -> list[dict[str, Any]]:
        tracked = self._bt.update_with_detections(detections)
        out = []
        if tracked is None or len(tracked) == 0 or tracked.mask is None:
            return out
        for j in range(len(tracked)):
            raw = int(tracked.tracker_id[j])
            track_id = self.offset + raw
            self._max_id = max(self._max_id, track_id)
            concept = label_of(int(tracked.class_id[j]))
            score = float(tracked.confidence[j]) if tracked.confidence is not None else 1.0
            votes = self._votes.setdefault(track_id, {})
            votes[concept] = votes.get(concept, 0.0) + score
            out.append(
                {
                    "track_id": track_id,
                    "concept": max(votes, key=votes.get),
                    "score": score,
                    "bbox": [float(v) for v in tracked.xyxy[j].tolist()],
                    "mask": np.asarray(tracked.mask[j], dtype=bool),
                }
            )
        return out


def environment() -> dict[str, Any]:
    """Versions for ``--check`` (imports torch but never initialises CUDA)."""
    import importlib.metadata as md

    value: dict[str, Any] = {"python_ok": True}
    for name in ("torch", "torchvision", "rfdetr", "supervision", "pycocotools"):
        try:
            value[name] = md.version(name)
        except md.PackageNotFoundError:
            value[name] = None
    value["cuda_probe_performed"] = False
    value["rf_home"] = os.environ.get("RF_HOME")
    return value
