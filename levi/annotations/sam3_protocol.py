"""SAM3 worker protocol and CPU-safe fake provider.

The real adapter is intentionally loaded only in the isolated integrations
environment.  These structures are shared with the control plane and can be
tested without importing torch or initializing CUDA.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .rle import encode_rle, validate_rle
from .schema import ObjectAnnotation, Sam3Plan


class WorkerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    session_id: str | None = None
    resource_path: str | None = None
    frame_index: int | None = Field(default=None, ge=0)
    text: str | None = None
    points: list[list[float]] | None = None
    point_labels: list[int] | None = None
    bounding_boxes: list[list[float]] | None = None


class WorkerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    session_id: str | None = None
    frame_index: int | None = None
    outputs: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


def validate_worker_output(output: dict[str, Any]) -> None:
    """Validate the small output contract before it reaches a sidecar."""

    for key in ("frame_index", "timestamp", "camera_key", "objects"):
        if key not in output:
            raise ValueError(f"worker output missing {key!r}")
    if not isinstance(output["objects"], list):
        raise TypeError("worker output objects must be a list")
    for item in output["objects"]:
        if not isinstance(item, dict):
            raise TypeError("worker object must be an object")
        rle = item.get("mask_rle")
        if not isinstance(rle, dict):
            raise TypeError("worker object mask_rle must be an object")
        validate_rle(rle)


def validate_annotations_for_plan(
    plan: Sam3Plan, annotations: list[ObjectAnnotation]
) -> None:
    """Reject worker rows that escape the requested episode/camera/frame scope."""
    episodes = set(plan.episode_indices)
    cameras = set(plan.camera_keys)
    last_frame = (
        plan.start_frame + plan.max_frames if plan.max_frames is not None else None
    )
    for row in annotations:
        if row.episode_index not in episodes:
            raise ValueError(
                f"worker episode {row.episode_index} is outside the requested plan"
            )
        if row.camera_key not in cameras:
            raise ValueError(
                f"worker camera {row.camera_key!r} is outside the requested plan"
            )
        if row.frame_index < plan.start_frame:
            raise ValueError(
                f"worker frame {row.frame_index} precedes the requested start frame"
            )
        if last_frame is not None and row.frame_index > last_frame:
            raise ValueError(
                f"worker frame {row.frame_index} exceeds the requested frame range"
            )


def fake_annotations(plan: Sam3Plan) -> list[ObjectAnnotation]:
    """Create deterministic CPU fixtures for UI and protocol tests.

    The fake provider produces two moving rectangles per prompt.  It is never
    presented as a model result and makes the complete review flow testable on
    machines where SAM3 is unavailable.
    """

    rows: list[ObjectAnnotation] = []
    frame_count = plan.max_frames or 4
    for episode_index in plan.episode_indices:
        for camera_no, camera_key in enumerate(plan.camera_keys):
            for prompt_no, prompt in enumerate(plan.prompts):
                object_id = f"fake-{episode_index}-{camera_no}-{prompt_no}"
                for frame_index in range(plan.start_frame, plan.start_frame + frame_count):
                    x1 = 4 + frame_index + prompt_no * 10
                    y1 = 5 + camera_no * 8
                    x2 = x1 + 12
                    y2 = y1 + 10
                    mask = [
                        [x1 <= x < x2 and y1 <= y < y2 for x in range(64)]
                        for y in range(48)
                    ]
                    rows.append(
                        ObjectAnnotation(
                            episode_index=episode_index,
                            frame_index=frame_index,
                            timestamp=frame_index / 10,
                            camera_key=camera_key,
                            object_id=object_id,
                            track_id=prompt_no,
                            concept=prompt,
                            bbox_xyxy=[x1, y1, x2, y2],
                            image_size=[48, 64],
                            mask_rle=encode_rle(mask),
                            score=0.95,
                            source="fake",
                            prompt=prompt,
                        )
                    )
    return rows
