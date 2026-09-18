"""Shared result models for input inspection and output compatibility.

Every input format produces an ``InputReport`` (what was found, and which of
its requirements pass); every output format turns that report into a
``TargetCompatibility`` (can this be exported, why not, how to fix it). The
web UI renders both directly, so new formats get a checklist and target card
for free.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

Status = Literal["pass", "warn", "fail", "info"]


class Requirement(BaseModel):
    id: str
    label: str
    status: Status
    # "during_scan": needs a full video decode, so it is only verified once a
    # conversion runs — shown as pending, never as passed.
    verified: Literal["now", "during_scan"] = "now"
    detail: str = ""
    fix: str = ""


class EpisodeFinding(BaseModel):
    source_id: str
    frames: int | None = None
    outcome: str | None = None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class Solution(BaseModel):
    id: str
    label: str
    # Option overrides applied when the user picks this solution.
    options: dict[str, Any] = Field(default_factory=dict)
    # Where the UI should send the user instead (e.g. to label outcomes).
    link: str | None = None


class TargetCompatibility(BaseModel):
    target: str
    label: str
    status: Literal["supported", "warnings", "unsupported"]
    reasons: list[str] = Field(default_factory=list)
    solutions: list[Solution] = Field(default_factory=list)
    # Pipeline options this target changes by default (e.g. RECAP keeps
    # every executed step: timing=retime, filter_static=False).
    defaults: dict[str, Any] = Field(default_factory=dict)


class InputReport(BaseModel):
    source: str
    format: str | None = None
    label: str = "Unrecognized"
    variant: str | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    requirements: list[Requirement] = Field(default_factory=list)
    episodes: list[EpisodeFinding] = Field(default_factory=list)
    targets: list[TargetCompatibility] = Field(default_factory=list)

    @property
    def failed(self) -> list[Requirement]:
        return [r for r in self.requirements if r.status == "fail"]

    @property
    def warned(self) -> list[Requirement]:
        return [r for r in self.requirements if r.status == "warn"]
