"""Pilot control contracts; runtime and model providers remain separate."""

from typing import Literal

from pydantic import Field

from .schema import Contract


class GrantRequest(Contract):
    client: Literal["codex", "claude"]
    datasets: list[str] = Field(min_length=1, max_length=1000)
    # None means "until it is disconnected": a local connection the operator
    # opened themselves should not stop working mid-task because a clock ran
    # out. A bounded grant is still available by giving a number.
    hours: int | None = Field(default=None, ge=1, le=720)
    run_id: str | None = None
    max_tool_calls: int | None = Field(default=None, ge=1, le=1000000)


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
