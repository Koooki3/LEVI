"""One validated options schema shared by CLI, service, plans and worker."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

STAGES = (
    "pipeline",
    "summary",
    "images",
    "fps-preview",
    "fps",
    "frozen",
    "stage-preview",
    "stage",
    "static",
    "filter-preview",
    "filter",
    "convert",
    "timestamps",
    "tasks-preview",
    "tasks",
    "validate",
)


class Options(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    fps: float = Field(10, ge=1, le=240)
    source_fps: float = Field(30, ge=1, le=240)
    xyz_threshold: float = Field(0.005, ge=0, le=1)
    rotation_threshold: float = Field(0.01, ge=0, le=3.141593)
    gripper_margin: int = Field(2, ge=0, le=100)
    frozen_threshold: float = Field(0.5, ge=0, le=255)
    stale_run: int = Field(3, ge=1, le=10000)
    require_complete: bool = False
    filter_static: bool = True
    orientation: Literal["euler", "quaternion"] = "euler"
    action_mode: Literal["next_state", "state"] = "next_state"
    robot_type: str = Field("generic_arm", min_length=1, max_length=100)
    cameras: dict[str, str] = Field(
        default_factory=lambda: {
            "wrist_camera": "observation.images.hand",
            "side_camera": "observation.images.view1",
        }
    )
    task_map: dict[str, str] = Field(default_factory=dict)
    exclude_demos: list[str] = Field(default_factory=list)

    @field_validator("cameras")
    @classmethod
    def camera_names(cls, value):
        import re

        if not value or len(value) > 16 or len(set(value.values())) != len(value):
            raise ValueError("Choose 1–16 distinct camera feature names")
        for source, feature in value.items():
            if not re.fullmatch(r"[A-Za-z0-9_-]+", source) or not re.fullmatch(
                r"observation\.images\.[A-Za-z0-9_.-]+", feature
            ):
                raise ValueError(
                    "Camera names must be simple names; feature must start observation.images."
                )
        return value

    @field_validator("task_map")
    @classmethod
    def task_names(cls, value):
        if len(value) > 10000 or any(
            not k.strip() or not v.strip() or len(k) > 20000 or len(v) > 20000
            for k, v in value.items()
        ):
            raise ValueError("Task mapping must contain nonempty text")
        return value

    @field_validator("exclude_demos")
    @classmethod
    def exclusions(cls, value):
        from pathlib import PurePosixPath

        if any(
            not v
            or PurePosixPath(v).is_absolute()
            or ".." in PurePosixPath(v).parts
            or "\\" in v
            for v in value
        ):
            raise ValueError("Exclude demos using relative capture paths")
        return value
