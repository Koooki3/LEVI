"""Runtime boundary for local Pilot integrations.

The ACP implementation in pilot.py uses the same Capability Registry and explicit LEVI
principal; protocol handshakes never grant filesystem, terminal or commit rights.
"""

from collections.abc import AsyncIterator
from typing import Protocol

from .schema import Contract, TaskContext


class RuntimeCapabilities(Contract):
    protocol: str
    resumable: bool = False
    cancellable: bool = False
    media_input: bool = False


class AgentRuntimeAdapter(Protocol):
    async def initialize(self) -> RuntimeCapabilities: ...
    async def start(self, context: TaskContext) -> str: ...
    def events(self, session_id: str, after: int) -> AsyncIterator[dict]: ...
    async def cancel(self, session_id: str) -> None: ...
    async def resume(self, session_id: str) -> None: ...


class RuntimeTransport(Protocol):
    """Synchronous adapter boundary used by the local ACP session supervisor."""

    def call(self, method: str, params: dict, timeout: float | None = None) -> dict: ...
    def send(self, value: dict) -> None: ...
    def close(self) -> None: ...
