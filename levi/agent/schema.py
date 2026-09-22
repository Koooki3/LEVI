"""Versioned control-plane contracts. No model/accelerator imports."""

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class RunStatus(StrEnum):
    PLANNED = "planned"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting_for_review"
    BLOCKED = "blocked"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    PARTIAL = "partially_succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class Budget(Contract):
    max_calls: int = Field(default=8, ge=1, le=1000)
    max_tokens: int = Field(default=16000, ge=256, le=1000000)
    max_seconds: int = Field(default=300, ge=10, le=86400)
    max_snapshot_bytes: int = Field(default=512 * 1024 * 1024, ge=1)
    max_artifact_bytes: int = Field(default=256 * 1024 * 1024, ge=1)


class ProviderConfig(Contract):
    name: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
    enabled: bool = True
    kind: Literal["openai-compatible", "ollama"] = "openai-compatible"
    model_digest: str | None = Field(default=None, pattern=r"^(sha256:)?[a-f0-9]{64}$")
    context_tokens: int = Field(default=8192, ge=1024, le=131072)
    base_url: str
    model: str = Field(min_length=1, max_length=200)
    key_env: str = Field(default="LEVI_MODEL_API_KEY", pattern=r"^[A-Z][A-Z0-9_]*$")
    vision: bool = False
    tools: bool = False
    structured_output: bool = False
    allow_localhost: bool = False


class TaskContext(Contract):
    supervision: Literal["none", "shadow", "supervised"] = "none"
    teacher_grant: str | None = Field(default=None, max_length=100)
    pilot_runtime: Literal["codex", "claude"] | None = None
    workflow: dict[str, Any] = Field(default_factory=dict)
    dataset_adapter: str = "lerobot-and-raw-view"
    repo_id: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    revision: str | None = None
    episodes: list[int] = Field(min_length=1, max_length=1000)
    cameras: list[str] = Field(default_factory=list, max_length=16)
    instruction: str = Field(min_length=1, max_length=12000)
    provider: str = Field(min_length=1, max_length=64)
    mode: Literal["read_only", "draft"] = "draft"
    allow_media_egress: bool = False
    budget: Budget = Field(default_factory=Budget)
    samples_per_episode: int = Field(default=3, ge=1, le=16)

    @model_validator(mode="after")
    def scope(self):
        from .planning import Workflow

        self.workflow = Workflow.model_validate(self.workflow).model_dump()
        from .formats import DATASETS

        if self.dataset_adapter not in DATASETS:
            raise ValueError("Unknown dataset adapter")
        if any(ep < 0 for ep in self.episodes) or len(set(self.episodes)) != len(
            self.episodes
        ):
            raise ValueError("Episodes must be distinct non-negative indices")
        if len(set(self.cameras)) != len(self.cameras):
            raise ValueError("Duplicate cameras")
        return self


class EvidenceRef(Contract):
    id: str
    episode_index: int = Field(ge=0)
    camera_key: str | None = None
    frame_index: int = Field(ge=0)
    timestamp: float = Field(ge=0)
    video_timestamp: float | None = Field(default=None, ge=0)
    decoded_video_timestamp: float | None = None
    decoded_video_frame: int | None = None
    temporal_error_seconds: float | None = None
    source_timestamp: float | None = None
    source_frame_id: int | None = None
    artifact: str | None = None
    sha256: str
    # Native full-frame evidence has identity mapping. Crops must retain this.
    crop_xyxy: list[int] | None = None
    source_size: list[int] | None = None


class Proposal(Contract):
    """Suggestions only: model output can never claim human review."""

    episode_index: int = Field(ge=0)
    kind: str = Field(
        description="Registered annotation kind: segment, event, issue or outcome"
    )
    content: str = Field(min_length=1, max_length=8000)
    start: float = Field(ge=0)
    end: float | None = Field(default=None, ge=0)
    style: Literal["subtask", "plan", "memory", "task_aug", "interjection"] = "subtask"
    evidence_ids: list[str] = Field(min_length=1, max_length=32)
    outcome: Literal["success", "failure", "unknown"] | None = None
    subtask_id: str | None = None
    attempt: int = Field(default=1, ge=1)
    layer: str = Field(default="activity", max_length=64)
    uncertainty: str = Field(default="", max_length=2000)
    boundary_candidates: list[float] = Field(default_factory=list, max_length=8)
    evidence_note: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def interval(self):
        from .formats import ANNOTATIONS

        if self.kind not in ANNOTATIONS:
            raise ValueError("Unknown annotation kind")
        ANNOTATIONS[self.kind].validate(self)
        return self


class ModelOutput(Contract):
    summary: str = Field(max_length=8000)
    proposals: list[Proposal] = Field(default_factory=list, max_length=500)
    warnings: list[str] = Field(default_factory=list, max_length=50)


class ChangeSet(Contract):
    schema_version: Literal["levi.agent.change.v1"] = "levi.agent.change.v1"
    id: str
    run_id: str
    base_revision: str
    proposals: list[Proposal]
    provenance: dict[str, Any]
    object_jobs: list[str] = Field(default_factory=list)
    undo_of: str | None = None
    inverse: list[dict[str, Any]] = Field(default_factory=list)
    published_revision: str | None = None
    decisions: dict[str, Literal["accepted", "rejected"]] = Field(default_factory=dict)
    status: Literal["draft", "validated", "approved", "committed", "rejected"] = "draft"
    revision: int = 0
    # Earlier agent atoms a commit replaced, per episode (see supersede.py).
    replaced: dict[str, int] = Field(default_factory=dict)


class ToolCall(Contract):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=128)
