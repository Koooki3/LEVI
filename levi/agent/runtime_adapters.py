"""Runtime boundary for local Pilot integrations.

The ACP implementation in pilot.py uses the same Capability Registry and explicit LEVI
principal; protocol handshakes never grant filesystem, terminal or commit rights.
"""

from typing import Protocol


class RuntimeTransport(Protocol):
    """Synchronous adapter boundary used by the local ACP session supervisor."""

    def call(self, method: str, params: dict, timeout: float | None = None) -> dict: ...
    def send(self, value: dict) -> None: ...
    def close(self) -> None: ...
