"""Pilot control contracts; runtime and model providers remain separate."""

from typing import Literal

from pydantic import Field

from .schema import Contract


class GrantRequest(Contract):
    client: Literal["codex", "claude"]
    datasets: list[str] = Field(min_length=1, max_length=1000)
    hours: int = Field(default=24, ge=1, le=720)
    run_id: str | None = None
    max_tool_calls: int = Field(default=300, ge=1, le=10000)


class PilotStart(Contract):
    run_id: str
    runtime: Literal["codex", "claude"]
    max_turns: int = Field(default=12, ge=1, le=100)
    max_seconds: int = Field(default=1800, ge=10, le=86400)


class PilotMessage(Contract):
    text: str = Field(min_length=1, max_length=12000)


class RuntimeProfile(Contract):
    id: Literal["codex", "claude"]
    executable: str | None = None
    enabled: bool = True
    authentication: str = "unknown"


class PermissionDecision(Contract):
    option_id: str
