"""Exceptions that map one-to-one to protocol ``error`` events."""

from __future__ import annotations


class WorkerError(Exception):
    """An error event: ``code`` is the contract code, ``message`` carries no
    path and no transcript."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class StartupError(WorkerError):
    """The worker cannot become ready; it exits non-zero after the event."""

    def __init__(self, code: str, message: str, exit_code: int) -> None:
        super().__init__(code, message)
        self.exit_code = exit_code


class RequestError(WorkerError):
    """One request failed and the stream is still in sync: the loop goes on."""


class ProtocolError(WorkerError):
    """The stream cannot be trusted any more (bad header, short read): the
    worker answers once and exits instead of guessing where the next
    request starts."""
