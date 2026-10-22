"""The ``serve`` loop: one JSON header line plus exactly N raw bytes in,
one JSON line out per event (design record 5.2).

A request that fails but leaves the stream in sync (bad WAV, too long, a
provider error) is answered with an ``error`` event and the loop goes on. A
request that leaves the stream in doubt (malformed or oversized header, short
read) is answered once and the worker exits: it never guesses where the next
request starts. Nothing here prints a transcript; the log lines carry request
id, sizes and a hash prefix only.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Protocol

from .common import (
    EXIT_PROTOCOL,
    EXIT_STARTUP,
    MAX_HEADER_BYTES,
    MAX_REQUEST_BYTES,
    REQUEST_ID,
    log,
)
from .errors import ProtocolError, RequestError, StartupError, WorkerError
from .manifest import verify_manifest
from .wav import WavClip, parse_wav

THREAD_VARIABLES = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS")


class Provider(Protocol):
    name: str

    def load(self) -> dict[str, Any]:
        """Become ready; return extra fields for the ready event."""

    def transcribe(self, request_id: str, clip: WavClip) -> dict[str, Any]: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class Options:
    provider: str
    model_dir: Path | None
    model: str
    device: str
    max_seconds: float
    cpu_affinity: str


# ------------------------------------------------------------------ process


def parse_cpus(spec: str) -> set[int]:
    """ "8-15", "8,9,12-13" -> a set of CPU numbers; ValueError when malformed."""
    cpus: set[int] = set()
    for part in spec.split(","):
        low, dash, high = part.strip().partition("-")
        first = int(low)
        last = int(high) if dash else first
        if first < 0 or last < first:
            raise ValueError(spec)
        cpus.update(range(first, last + 1))
    return cpus


def harden_process(cpu_affinity: str) -> None:
    """Lowest scheduling priority, the configured CPU set, capped threads and
    offline model loading. Failures are logged, never fatal: a restricted
    sandbox must still let the worker run (the manager sets the same
    environment before it starts us)."""
    try:
        os.setpriority(os.PRIO_PROCESS, 0, 19)
    except OSError:
        log("could not lower the process priority")
    if cpu_affinity.strip():
        try:
            os.sched_setaffinity(0, parse_cpus(cpu_affinity))
        except (ValueError, OSError):
            log("the CPU affinity was not applied")
    for name in THREAD_VARIABLES:
        os.environ.setdefault(name, "2")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"


def protocol_writer() -> BinaryIO:
    """A private copy of stdout for events; fd 1 itself is pointed at stderr,
    so a library that prints cannot corrupt the protocol."""
    writer = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    return writer


# ----------------------------------------------------------------- protocol


class Emitter:
    def __init__(self, writer: BinaryIO) -> None:
        self._writer = writer

    def __call__(self, event: dict[str, Any]) -> None:
        line = (
            json.dumps(event, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
            + "\n"
        )
        self._writer.write(line.encode("ascii"))

    def error(self, error: WorkerError, request_id: str | None = None) -> None:
        event: dict[str, Any] = {
            "event": "error",
            "code": error.code,
            "message": error.message,
        }
        if request_id is not None:
            event["id"] = request_id
        self(event)


def read_header(reader: BinaryIO) -> dict[str, Any] | None:
    """The next request header, or None at a clean EOF."""
    line = reader.readline(MAX_HEADER_BYTES + 1)
    if not line:
        return None
    if not line.endswith(b"\n"):
        raise ProtocolError("internal", "The request header is incomplete or too long.")
    try:
        header = json.loads(line.decode("utf-8"))
    except ValueError:
        raise ProtocolError("internal", "The request header is not JSON.") from None
    if not isinstance(header, dict):
        raise ProtocolError("internal", "The request header is not an object.")
    op = header.get("op")
    if op == "shutdown" and set(header) == {"op"}:
        return header
    request_id, size = header.get("id"), header.get("bytes")
    if op != "transcribe" or set(header) != {"op", "id", "bytes"}:
        raise ProtocolError(
            "internal", "The request header has unknown or missing fields."
        )
    if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
        raise ProtocolError("internal", "The request id is not valid.")
    if type(size) is not int or size < 0:
        raise ProtocolError("internal", "The byte count is not a non-negative integer.")
    if size > MAX_REQUEST_BYTES:
        raise ProtocolError(
            "too_long", "The announced audio is larger than the allowed size."
        )
    return header


def read_exact(reader: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = reader.read(remaining)
        if not chunk:
            raise ProtocolError(
                "audio_invalid", "The audio ended before the announced byte count."
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def serve_loop(
    reader: BinaryIO, emit: Emitter, provider: Provider, max_seconds: float
) -> int:
    while True:
        request_id: str | None = None
        try:
            header = read_header(reader)
            if header is None or header["op"] == "shutdown":
                return 0
            request_id = header["id"]
            payload = read_exact(reader, header["bytes"])
        except ProtocolError as error:
            emit.error(error, request_id)
            return EXIT_PROTOCOL
        started = time.perf_counter()
        digest = hashlib.sha256(payload).hexdigest()[:12]
        try:
            body = provider.transcribe(request_id, parse_wav(payload, max_seconds))
        except RequestError as error:
            emit.error(error, request_id)
            outcome = error.code
        except Exception as error:  # noqa: BLE001 - one bad request must not take the worker down
            emit.error(
                RequestError(
                    "internal", f"The recognition failed ({type(error).__name__})."
                ),
                request_id,
            )
            outcome = "internal"
        else:
            emit({"event": "result", "id": request_id, **body})
            outcome = (
                "heard" if body.get("heard") else f"not_heard:{body.get('reason')}"
            )
        log(
            f"request id={request_id} bytes={len(payload)} sha256={digest} outcome={outcome} "
            f"ms={round((time.perf_counter() - started) * 1000)}"
        )


# ------------------------------------------------------------------ startup


def make_provider(options: Options) -> Provider:
    if options.provider == "fake":
        from .fake import FakeProvider

        return FakeProvider()
    folder = (options.model_dir or Path()) / options.model
    started = time.perf_counter()
    verified = verify_manifest(folder)
    log(
        f"manifest verified files={len(verified)} s={time.perf_counter() - started:.1f}"
    )
    try:
        from .qwen import QwenAsrProvider
    except ImportError:
        raise StartupError(
            "internal", "The speech worker environment is incomplete.", EXIT_STARTUP
        ) from None
    return QwenAsrProvider(folder, options.device)


def serve(options: Options) -> int:
    harden_process(options.cpu_affinity)
    writer = protocol_writer()
    emit = Emitter(writer)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    started = time.perf_counter()
    provider: Provider | None = None
    try:
        provider = make_provider(options)
        extra = provider.load()
    except StartupError as error:
        emit.error(error)
        if provider is not None:
            provider.close()
        return error.exit_code
    ready = {
        "event": "ready",
        "model": options.model,
        "device": options.device,
        "load_s": round(time.perf_counter() - started, 2),
        "vram_mib": 0,
    }
    ready.update(extra)
    ready["event"] = "ready"
    try:
        emit(ready)
        return serve_loop(sys.stdin.buffer, emit, provider, options.max_seconds)
    except BrokenPipeError:
        log("the output pipe was closed")
        return 1
    finally:
        provider.close()
