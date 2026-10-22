"""Injected pose-only sink for offline manual-session verification.

No socket, URL, environment switch, robot import or production transport lives
here. Closing admission forbids later releases; it cannot retract an in-flight
command or establish that a physical robot stopped.
"""

import threading
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Ack:
    acknowledged: bool
    detail: str


class PoseSink(Protocol):
    def send(self, pose) -> Ack: ...

    def close_admission(self) -> None: ...


class WorkerRefused(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class PoseWorker:
    """One in-flight call; stop never waits for send's lock.

    The injected sink's close_admission must be nonblocking, must atomically
    reject future sends, and must never initiate motion. The worker also closes
    its own release gate first. Counts describe local releases, not delivery.
    """

    def __init__(self, sink: PoseSink):
        if not callable(getattr(sink, "send", None)) or not callable(
            getattr(sink, "close_admission", None)
        ):
            raise TypeError("an explicit pose-only sink is required")
        self._sink = sink
        self._lock = threading.Lock()
        self._closed = False
        self._close_confirmed = False
        self._in_flight = False
        self._sent = 0

    def snapshot(self):
        with self._lock:
            return {
                "closed": self._closed,
                "close_confirmed": self._close_confirmed,
                "in_flight": self._in_flight,
                "sent": self._sent,
            }

    def close_admission(self):
        with self._lock:
            if self._closed:
                if not self._close_confirmed:
                    raise WorkerRefused("close_unproven")
                return
            self._closed = True
        # Never hold the release lock across a sink callback or network-like
        # fake wait. Failure still leaves this worker permanently closed.
        try:
            self._sink.close_admission()
        except Exception as exc:
            raise WorkerRefused("close_unproven") from exc
        with self._lock:
            self._close_confirmed = True

    def send(self, pose) -> Ack:
        with self._lock:
            if self._closed:
                raise WorkerRefused("admission_closed")
            if self._in_flight:
                raise WorkerRefused("worker_busy")
            self._in_flight = True
            self._sent += 1
        try:
            # A concurrent close may overtake this release. The sink contract
            # above must reject a call that reaches it after its gate closed.
            try:
                answer = self._sink.send(pose)
            except Exception:  # noqa: BLE001 - injected callback failures must close admission
                answer = Ack(False, "delivery_unknown")
            if not isinstance(answer, Ack) or type(answer.acknowledged) is not bool:
                answer = Ack(False, "ack_invalid")
            if answer.acknowledged is not True:
                try:
                    self.close_admission()
                except WorkerRefused:
                    pass  # the release gate stays shut even if sink closure failed
            return answer
        finally:
            with self._lock:
                self._in_flight = False


def production_factory(*args, **kwargs):
    """The production entry stays closed regardless of configuration or env."""
    from .manual import ManualRefused

    raise ManualRefused("stop_unproven")
