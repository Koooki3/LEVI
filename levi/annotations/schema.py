"""Public, model-neutral schemas for LEVI object annotations."""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .rle import validate_rle


class ReviewStatus(StrEnum):
    SUGGESTED = "suggested"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NEEDS_REVIEW = "needs_review"


class Sam3Plan(BaseModel):
    """An immutable request description stored before a worker is launched."""

    model_config = ConfigDict(extra="forbid")

    repo_id: str | None = None
    local_path: str | None = None
    revision: str | None = None
    episode_indices: list[int] = Field(default_factory=list, min_length=1)
    camera_keys: list[str] = Field(default_factory=list, min_length=1)
    prompts: list[str] = Field(default_factory=list, min_length=1)
    start_frame: int = Field(default=0, ge=0)
    max_frames: int | None = Field(default=None, ge=1)
    review_threshold: float = Field(default=0.60, ge=0, le=1)
    accept_threshold: float = Field(default=0.90, ge=0, le=1)
    provider: Literal["sam3", "fake"] = "sam3"

    @field_validator("episode_indices")
    @classmethod
    def validate_episode_indices(cls, value: list[int]) -> list[int]:
        if any(index < 0 for index in value):
            raise ValueError("episode indices must be non-negative")
        if len(set(value)) != len(value):
            raise ValueError("episode indices must be unique")
        return sorted(value)

    @field_validator("camera_keys", "prompts")
    @classmethod
    def validate_non_empty_strings(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if not cleaned or any(not item for item in cleaned):
            raise ValueError("values must contain non-empty strings")
        return list(dict.fromkeys(cleaned))

    @model_validator(mode="after")
    def validate_threshold_order(self) -> Sam3Plan:
        if self.accept_threshold < self.review_threshold:
            raise ValueError("accept_threshold must be >= review_threshold")
        return self


class ObjectAnnotation(BaseModel):
    """One lossless instance mask at one source frame."""

    model_config = ConfigDict(extra="forbid")

    episode_index: int = Field(ge=0)
    frame_index: int = Field(ge=0)
    timestamp: float = Field(ge=0)
    camera_key: str = Field(min_length=1)
    object_id: str = Field(min_length=1)
    track_id: int = Field(ge=0)
    concept: str = Field(min_length=1)
    category: str | None = None
    bbox_xyxy: list[float] = Field(min_length=4, max_length=4)
    image_size: list[int] = Field(min_length=2, max_length=2)
    mask_rle: dict[str, Any]
    score: float = Field(ge=0, le=1)
    visible: bool = True
    occluded: bool = False
    status: ReviewStatus = ReviewStatus.SUGGESTED
    source: Literal["sam3", "human", "fake", "import"] = "sam3"
    prompt: str | None = None

    @field_validator("bbox_xyxy")
    @classmethod
    def validate_bbox(cls, value: list[float]) -> list[float]:
        if any(not math.isfinite(number) for number in value):
            raise ValueError("bbox must contain finite numbers")
        x1, y1, x2, y2 = value
        if x1 < 0 or y1 < 0 or x2 < x1 or y2 < y1:
            raise ValueError("bbox must be ordered and non-negative")
        return [float(number) for number in value]

    @field_validator("image_size")
    @classmethod
    def validate_image_size(cls, value: list[int]) -> list[int]:
        if any(number <= 0 for number in value):
            raise ValueError("image dimensions must be positive")
        return value

    @field_validator("mask_rle")
    @classmethod
    def validate_mask_rle(cls, value: dict[str, Any]) -> dict[str, Any]:
        validate_rle(value)
        return value


class ObjectTrack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    episode_index: int = Field(ge=0)
    camera_key: str = Field(min_length=1)
    track_id: int = Field(ge=0)
    object_id: str = Field(min_length=1)
    concept: str = Field(min_length=1)
    category: str | None = None
    start_frame: int = Field(ge=0)
    end_frame: int = Field(ge=0)
    mean_score: float = Field(ge=0, le=1)
    min_score: float = Field(ge=0, le=1)
    status: ReviewStatus = ReviewStatus.SUGGESTED
    lineage: list[str] = Field(default_factory=list)


class ObjectEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    episode_index: int = Field(ge=0)
    camera_key: str = Field(min_length=1)
    operation: Literal[
        "accept",
        "reject",
        "relabel",
        "occlusion",
        "delete",
        "split",
        "merge",
        "refine",
    ]
    object_id: str | None = None
    track_id: int | None = Field(default=None, ge=0)
    frame_index: int | None = Field(default=None, ge=0)
    concept: str | None = None
    visible: bool | None = None
    occluded: bool | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    base_revision: str | None = None
