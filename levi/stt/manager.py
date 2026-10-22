"""The one speech worker process: started on demand, used one request at a time,
gone after two minutes of quiet.

The manager spawns ``python -m levi_stt_worker.cli serve`` the way the live
segmentation worker is spawned (own session, tracked in ``levi/children.py``)
and talks to it over pipes: a JSON header line plus exactly ``bytes`` raw bytes
in, one JSON line out (the protocol is in docs/SPEECH.md). Rules it keeps:

- **Lazy.** Nothing runs until the first ``transcribe``; ``status`` never starts
  a process.
- **One request at a time, no queue.** A second caller is refused at once.
- **Every wait has a deadline.** A worker that does not answer in time, answers
  out of turn, sends a line that is too long or too short, or exits, is killed
  with its whole process group; the next request starts a clean one.
- **Idle exit.** After ``idle_seconds`` without a request the worker is asked to
  shut down (killed if it does not), which frees its GPU memory. ``release``
  does the same on demand and, if another thread is already ending the worker,
  waits (bounded) until its process is really gone.
- **Held off.** ``held_off()`` is for a policy deploy: it releases the worker,
  waits until it is gone and refuses to start another for as long as the block
  runs, so the deploy's memory checks never count a worker that came back.
- **Gate guard.** The HTTP layer may register ``set_guard``: the idle watcher
  asks it every ``guard_every`` seconds and releases a loaded worker (never one
  with a request in flight) when it says the gate has closed, so a run that
  starts after a recording does not find 4.9 GB held by a worker nobody uses.
- **Nothing is kept.** Audio goes from the caller's memory into a pipe. The
  worker's stderr goes to a file in the workspace, which the worker keeps free
  of transcripts; the manager reads only ``event`` and ``code`` words from the
  worker and never copies its free text (``message``, ``text``) into a log, an
  exception or a file.
"""

import atexit
import contextlib
import json
import logging
import os
import select
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .. import children, paths
from . import settings
from .errors import SttError

log = logging.getLogger("levi.stt")

KIND = "stt"
GRACE_S = 2.0  # a worker asked to shut down gets this long before the kill
# How long a caller waits for a worker that another thread is already ending
# (the polite shutdown, the kill, the closing of its pipes and the join of its
# watcher each take up to about GRACE_S).
GONE_WAIT_S = 2 * GRACE_S + 2
GUARD_EVERY_S = 2.0  # the idle watcher asks the gate guard this often
MAX_LINE = 64 * 1024  # a longer line without a newline is a broken worker
MAX_TEXT = 1000  # characters of a transcript the worker may return
CHUNK = 64 * 1024
# Only these reach the worker from the service's environment: no UI token, no
# hub token, nothing that is not needed to find the libraries and the GPU.
PASS_ENV = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TZ",
    "LD_LIBRARY_PATH",
    "CUDA_HOME",
    "CUDA_PATH",
    "CUDA_VISIBLE_DEVICES",
    "NVIDIA_VISIBLE_DEVICES",
    "NVIDIA_DRIVER_CAPABILITIES",
    "LEVI_STT_FAKE_SCRIPT",
)
HEARD_NOT = ("no_speech", "language", "low_confidence", "empty")
LANGUAGES = ("English", "Chinese")
# What the worker may refuse a request with, and what it may refuse to start with.
REQUEST_ERRORS = ("audio_invalid", "too_long", "internal")
START_ERRORS = {
    "manifest_mismatch": "manifest_mismatch",
    "model_missing": "model_missing",
}

# failure code -> English sentence for the page (no worker text, no path)
REASONS = {
    "start_failed": "The speech worker could not be started",
    "start_timeout": "The speech worker did not finish loading in time",
    "request_timeout": "The speech worker did not answer in time",
    "worker_exited": "The speech worker stopped unexpectedly",
    "protocol_error": "The speech worker answered out of turn and was stopped",
    "device_mismatch": "The speech worker is not using the GPU and was stopped",
    "manifest_mismatch": "The speech model files do not match their manifest",
    "model_missing": "The speech model could not be loaded",
    "worker_error": "The speech worker reported an internal error and was stopped",
    "released": "The speech worker was released",
}
# failure code -> the SttError code the HTTP layer words
ERROR_CODES = {
    "start_timeout": "timeout",
    "request_timeout": "timeout",
    "manifest_mismatch": "manifest_mismatch",
    "model_missing": "model_missing",
}


class _Timeout(Exception):
    pass


class _Closed(Exception):
    """The worker's pipe ended (it exited, or was killed)."""


class _Violation(Exception):
    """The worker said something the protocol does not allow."""


class _Refused(Exception):
    """The worker reported that it cannot start; ``failure`` says why."""

    def __init__(self, failure: str):
        super().__init__(failure)
        self.failure = failure


@dataclass(frozen=True)
class Transcript:
    """One answer of the worker. ``text`` and ``language`` are empty when
    ``heard`` is false; ``reason`` says why nothing was heard."""

    heard: bool
    reason: str | None
    text: str
    language: str | None
    loaded_now: bool
    load_ms: int
    infer_ms: int


class _Worker:
    """One spawned process and what the manager knows about it."""

    def __init__(self, proc: subprocess.Popen, now: float):
        self.proc = proc
        self.out_fd = proc.stdout.fileno()
        self.in_fd = proc.stdin.fileno()
        self.buf = bytearray()
        self.last_used = now
        self.phase = "loading"  # loading | ready | busy
        self.stop = threading.Event()  # tells the idle watcher to end
        self.gone = threading.Event()  # set once the process is cleaned up
        self.io = threading.RLock()  # held while the request thread uses the pipes
        self.watcher: threading.Thread | None = None
        self.retired = False
        self.cancelled = False  # a release killed it under a running request


def _wait(fd: int, events: int, timeout: float) -> bool:
    """Whether ``fd`` has ``events`` (or has ended or failed) within ``timeout``
    seconds. ``poll``, not ``select``: a long-running service can hold more
    descriptors than ``select`` can address."""
    poller = select.poll()
    poller.register(fd, events)
    return bool(poller.poll(max(0.0, timeout) * 1000))


def _terminate(proc: subprocess.Popen) -> None:
    """Kill the process group (only while the leader is still unreaped, so a
    reused pid is never signalled) and reap the leader."""
    if proc.poll() is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=GRACE_S)


def _environment(extra: dict[str, str] | None) -> dict[str, str]:
    env = {k: os.environ[k] for k in PASS_ENV if k in os.environ}
    env.update(
        {
            "PYTHONUNBUFFERED": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "OMP_NUM_THREADS": str(settings.OMP_THREADS),
            # The worker never takes the physical GPU lock: it would block while a
            # policy server runs and make a deploy fail with "foreign holder".
            "LEVI_GPU_LOCK_FILE": "",
        }
    )
    env.update(extra or {})
    return env


class Manager:
    """The process's speech worker (see the module docstring).

    ``command(cpu_affinity)`` builds the argv, ``installed()`` says whether the
    worker and model are there, ``clock`` times the idle period; deadlines on
    the pipes use the real monotonic clock. Tests pass fakes for all of these.
    """

    def __init__(
        self,
        *,
        command: Callable[[str], list[str]] | None = None,
        installed: Callable[[], settings.WorkerState] | None = None,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        log_dir: Path | None = None,
        children_state: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        idle_seconds: float = settings.IDLE_SECONDS,
        start_timeout: float = settings.START_TIMEOUT_S,
        request_timeout: float = settings.REQUEST_TIMEOUT_S,
        watch: bool = True,
        guard_every: float = GUARD_EVERY_S,
    ):
        self._command = command or settings.command
        self._installed = installed or settings.worker_state
        self._cwd = cwd
        self._env = env
        self._log_dir = log_dir
        self._children_state = children_state
        self._clock = clock
        self._idle_seconds = idle_seconds
        self._start_timeout = start_timeout
        self._request_timeout = request_timeout
        self._watch = watch
        self._guard_every = guard_every
        self._lock = threading.Lock()  # the fields below; never held across I/O
        self._request = threading.Lock()  # held by the one request being served
        self._worker: _Worker | None = None
        self._failure: str | None = None
        # Workers on their way out: ``_worker`` is cleared before the process is
        # gone, and its GPU memory is not free until it is.
        self._ending: list[_Worker] = []
        self._launch_lock = threading.Lock()
        self._holds = 0  # open ``held_off`` blocks: no worker may start
        self._guard: Callable[[], bool] | None = None

    # ------------------------------------------------------------ passive

    def installed(self) -> settings.WorkerState:
        return self._installed()

    def worker_pid(self) -> int | None:
        with self._lock:
            return self._worker.proc.pid if self._worker else None

    def status(self) -> dict:
        """``{"state", "reason", "model", "device", "idle_in_s"}``. Never starts
        a process; a worker found dead is cleaned up and shown as ``failed``."""
        worker = self._current()
        if worker is not None and worker.proc.poll() is not None:
            self._retire(worker, failure="worker_exited")
        with self._lock:
            worker, failure = self._worker, self._failure
            if worker is None:
                state = "failed" if failure else "off"
                idle_in = None
            else:
                state = worker.phase
                idle_in = (
                    max(0.0, self._idle_seconds - (self._clock() - worker.last_used))
                    if worker.phase == "ready"
                    else None
                )
        return {
            "state": state,
            "reason": (
                {"code": failure, "text": REASONS[failure]}
                if state == "failed" and failure
                else None
            ),
            "model": settings.model_name(),
            "device": settings.DEVICE,
            "idle_in_s": idle_in,
        }

    def _current(self) -> _Worker | None:
        with self._lock:
            return self._worker

    # ------------------------------------------------------------ transcribe

    def transcribe(
        self,
        request_id: str,
        wav: bytes,
        cpu_affinity: str = "",
        may_start: Callable[[], None] | None = None,
    ) -> Transcript:
        """Run ``wav`` (already validated) through the worker, starting it if
        needed. One at a time: a second caller gets ``SttError("busy")`` at
        once. Raises ``SttError`` for every other failure; the worker that
        caused one is already gone.

        ``may_start`` is called, under the request lock, only when a worker has
        to be started (none is loaded, or the loaded one just died); it raises
        ``SttError`` to refuse. The caller's check that was made before this
        call may have been made while a worker was loaded."""
        if not self._request.acquire(blocking=False):
            raise SttError("busy", "a transcription is already running")
        try:
            worker, loaded_now, load_ms = self._ready_worker(cpu_affinity, may_start)
            try:
                return self._exchange(worker, request_id, wav, loaded_now, load_ms)
            except SttError:
                raise
            except Exception:
                # A bug of ours, not the worker's: where its stream stands is
                # unknown, so it does not stay in service.
                self._retire(worker, failure="protocol_error")
                raise
        finally:
            self._request.release()

    def _ready_worker(
        self, cpu_affinity: str, may_start: Callable[[], None] | None = None
    ) -> tuple:
        """The live worker, or a new one: ``(worker, started_now, load_ms)``."""
        worker = self._current()
        if worker is not None:
            try:
                if worker.proc.poll() is not None:
                    self._retire(worker, failure="worker_exited")
                    worker = None
                elif not self._quiet(worker):
                    self._retire(worker, failure="protocol_error")
                    worker = None
            except _Closed:
                # Retired by another thread (a release, a crash its status
                # call noticed) between the lookup and now.
                raise self._error(worker, "worker_exited") from None
        if worker is not None:
            return worker, False, 0
        if may_start is not None:
            may_start()
        began = time.monotonic()
        worker = self._spawn(cpu_affinity)
        return worker, True, int((time.monotonic() - began) * 1000)

    def _quiet(self, worker: _Worker) -> bool:
        """Nothing is waiting in the worker's output (an idle worker is silent;
        anything there, or its end, is a worker that is not as we left it)."""
        with worker.io:
            if worker.retired:  # its pipes may be closed, its numbers reused
                raise _Closed
            return not worker.buf and not _wait(worker.out_fd, select.POLLIN, 0)

    # ------------------------------------------------------------ spawning

    def _stderr_file(self):
        folder = self._log_dir or paths.STATE / "stt"
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        # One log for the current worker; the worker keeps transcripts out of it.
        fd = os.open(
            folder / "worker.log", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        return os.fdopen(fd, "wb")

    def _launch(self, cpu_affinity: str) -> _Worker:
        with self._launch_lock:
            with self._lock:
                held = self._holds > 0
            if held:
                raise SttError("held", "a policy deploy is in progress")
            try:
                argv = self._command(cpu_affinity)
                logfile = self._stderr_file()
                try:
                    proc = subprocess.Popen(
                        argv,
                        cwd=self._cwd or settings.worker_project(),
                        env=_environment(self._env),
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=logfile,
                        start_new_session=True,
                        bufsize=0,
                    )
                finally:
                    logfile.close()
            except (OSError, ValueError):
                with self._lock:
                    self._failure = "start_failed"
                raise SttError("failed", REASONS["start_failed"]) from None
            try:
                worker = _Worker(proc, self._clock())
                os.set_blocking(worker.in_fd, False)
            except Exception:  # noqa: BLE001 - cleanup even before publication
                _terminate(proc)
                for pipe in (proc.stdin, proc.stdout):
                    with contextlib.suppress(OSError, ValueError):
                        pipe.close()
                with self._lock:
                    self._failure = "start_failed"
                raise SttError("failed", REASONS["start_failed"]) from None
            with self._lock:
                # Checked again where the worker is published: a deploy that fenced
                # starts after the check above has either not looked yet (and then
                # finds this worker and waits for it) or is seen here.
                held = self._holds > 0
                if not held:
                    self._worker, self._failure = worker, None
            if held:
                _terminate(proc)  # started a moment ago: nothing is loaded yet
                for pipe in (proc.stdin, proc.stdout):
                    with contextlib.suppress(OSError, ValueError):
                        pipe.close()
                raise SttError("held", "a policy deploy is in progress")
            return worker

    def _spawn(self, cpu_affinity: str) -> _Worker:
        worker = self._launch(cpu_affinity)
        proc = worker.proc
        log.info("speech worker started pid=%d", proc.pid)
        try:
            children.track(proc, KIND, "worker", state=self._children_state)
            self._start_watcher(worker)
            with worker.io:
                if worker.retired:  # released meanwhile; its pipes may be closed
                    raise _Closed
                self._await_ready(worker)
        except _Refused as refusal:
            raise self._error(worker, refusal.failure) from None
        except _Timeout:
            raise self._error(worker, "start_timeout") from None
        except _Closed:
            raise self._error(worker, "worker_exited") from None
        except _Violation:
            raise self._error(worker, "protocol_error") from None
        except OSError:
            raise self._error(worker, "start_failed") from None
        except Exception:  # noqa: BLE001 - e.g. no thread could be started
            raise self._error(worker, "start_failed") from None
        with self._lock:
            worker.phase, worker.last_used = "ready", self._clock()
        return worker

    def _await_ready(self, worker: _Worker) -> None:
        event = self._event(worker, time.monotonic() + self._start_timeout)
        kind = event.get("event")
        if kind == "error":
            raise _Refused(START_ERRORS.get(event.get("code"), "start_failed"))
        if kind != "ready":
            raise _Violation("expected ready")
        if settings.provider() != "fake" and event.get("device") != settings.DEVICE:
            raise _Refused("device_mismatch")

    # ------------------------------------------------------------ one request

    def _exchange(
        self,
        worker: _Worker,
        request_id: str,
        wav: bytes,
        loaded_now: bool,
        load_ms: int,
    ) -> Transcript:
        with self._lock:
            worker.phase = "busy"
        began = time.monotonic()
        deadline = began + self._request_timeout
        header = json.dumps(
            {"op": "transcribe", "id": request_id, "bytes": len(wav)},
            separators=(",", ":"),
        )
        try:
            with worker.io:
                if worker.retired:  # its pipes may be closed, its numbers reused
                    raise _Closed
                self._send(worker, header.encode() + b"\n" + wav, deadline)
                event = self._event(worker, deadline)
            answer = self._answer(event, request_id)
        except _Timeout:
            raise self._error(worker, "request_timeout") from None
        except _Closed:
            raise self._error(worker, "worker_exited") from None
        except (_Violation, OSError):
            raise self._error(worker, "protocol_error") from None
        if isinstance(answer, SttError):
            # A refusal of this audio: the worker is fine. An internal error is
            # not: its state is unknown.
            if answer.code == "internal":
                raise self._error(worker, "worker_error")
            with self._lock:
                worker.phase, worker.last_used = "ready", self._clock()
            raise answer
        with self._lock:
            worker.phase, worker.last_used = "ready", self._clock()
        measured = int((time.monotonic() - began) * 1000)
        return Transcript(
            answer["heard"],
            answer["reason"],
            answer["text"],
            answer["language"],
            loaded_now,
            load_ms,
            answer["infer_ms"] if answer["infer_ms"] is not None else measured,
        )

    @staticmethod
    def _answer(event: dict, request_id: str):
        """The checked result as a dict, or the ``SttError`` of the worker's own
        refusal of this request; ``_Violation`` for anything else."""
        kind = event.get("event")
        if event.get("id") != request_id:
            raise _Violation("answer for another request")
        if kind == "error":
            code = event.get("code")
            if code not in REQUEST_ERRORS:
                raise _Violation("unknown error code")
            if code == "audio_invalid":
                return SttError("audio_invalid", "the worker could not read the audio")
            if code == "too_long":
                return SttError("audio_too_long", "the audio is longer than the limit")
            return SttError("internal", REASONS["worker_error"])
        if kind != "result":
            raise _Violation("expected result")
        heard = event.get("heard")
        text, language, reason = "", None, None
        if heard is True:
            text, language = event.get("text"), event.get("language")
            if (
                not isinstance(text, str)
                or len(text) > MAX_TEXT
                or language not in (*LANGUAGES, None)
            ):
                raise _Violation("bad result")
            text = text.strip()
            if not text:
                heard, reason, language = False, "empty", None
        elif heard is False:
            reason = event.get("reason")
            if reason not in HEARD_NOT:
                raise _Violation("unknown reason")
        else:
            raise _Violation("heard is not a boolean")
        timing = event.get("timing_ms")
        infer = timing.get("infer") if isinstance(timing, dict) else None
        if isinstance(infer, bool) or not isinstance(infer, int) or infer < 0:
            infer = None
        return {
            "heard": heard,
            "reason": reason,
            "text": text,
            "language": language,
            "infer_ms": infer,
        }

    # ------------------------------------------------------------ pipe I/O

    def _send(self, worker: _Worker, payload: bytes, deadline: float) -> None:
        view, sent = memoryview(payload), 0
        while sent < len(view):
            left = deadline - time.monotonic()
            if left <= 0:
                raise _Timeout
            if not _wait(worker.in_fd, select.POLLOUT, min(left, 0.5)):
                if worker.retired or worker.proc.poll() is not None:
                    raise _Closed
                continue
            try:
                sent += os.write(worker.in_fd, view[sent : sent + CHUNK])
            except BlockingIOError:
                continue
            except OSError:  # broken pipe: the worker is gone
                raise _Closed from None

    def _event(self, worker: _Worker, deadline: float) -> dict:
        """The next JSON line of the worker's output, as a dict with an
        ``event`` word."""
        line = self._line(worker, deadline)
        try:
            event = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise _Violation("not JSON") from None
        if not isinstance(event, dict) or not isinstance(event.get("event"), str):
            raise _Violation("not an event")
        return event

    def _line(self, worker: _Worker, deadline: float) -> bytes:
        buf = worker.buf
        while True:
            end = buf.find(b"\n")
            if end > MAX_LINE or (end < 0 and len(buf) > MAX_LINE):
                raise _Violation("line too long")
            if end >= 0:
                line = bytes(buf[:end])
                del buf[: end + 1]
                return line
            left = deadline - time.monotonic()
            if left <= 0:
                raise _Timeout
            if not _wait(worker.out_fd, select.POLLIN, min(left, 0.5)):
                continue
            try:
                chunk = os.read(worker.out_fd, CHUNK)
            except OSError:
                raise _Closed from None
            if not chunk:  # the end of the stream, also in the middle of a line
                raise _Closed
            buf += chunk

    # ------------------------------------------------------------ ending

    def _error(self, worker: _Worker, failure: str) -> SttError:
        """Retire ``worker`` as failed and return the error to raise."""
        self._retire(worker, failure=failure)
        if worker.cancelled:
            return SttError("failed", REASONS["released"])
        return SttError(ERROR_CODES.get(failure, "failed"), REASONS[failure])

    def _retire(
        self,
        worker: _Worker,
        *,
        failure: str | None = None,
        polite: bool = False,
        cancel: bool = False,
    ) -> None:
        """End ``worker`` once: stop its watcher, ask it to shut down (when
        ``polite``) or kill its group, close its pipes, forget it in
        ``children``. A second caller waits for the first to finish. The
        manager shows ``failed`` with ``failure``, or ``off`` without."""
        with self._lock:
            first = not worker.retired
            worker.retired = True
            worker.cancelled = worker.cancelled or cancel
            if self._worker is worker:
                self._worker, self._failure = None, failure
            if first:
                self._ending.append(worker)
        if not first:
            worker.gone.wait(GONE_WAIT_S)
            return
        worker.stop.set()
        try:
            if polite:
                self._ask_shutdown(worker)
            _terminate(worker.proc)
            # The request thread may still be reading the pipe (the kill makes
            # it see the end); close the pipes only after it has left.
            if worker.io.acquire(timeout=GRACE_S + 1):
                try:
                    for pipe in (worker.proc.stdin, worker.proc.stdout):
                        with contextlib.suppress(OSError, ValueError):
                            pipe.close()
                finally:
                    worker.io.release()
            if worker.proc.poll() is not None:
                children.untrack(worker.proc.pid, 0.5, state=self._children_state)
            if worker.watcher and worker.watcher is not threading.current_thread():
                worker.watcher.join(GRACE_S)
            log.info(
                "speech worker ended pid=%d (%s)", worker.proc.pid, failure or "off"
            )
        finally:
            worker.gone.set()
            with self._lock, contextlib.suppress(ValueError):
                self._ending.remove(worker)

    @staticmethod
    def _ask_shutdown(worker: _Worker) -> None:
        if not worker.io.acquire(timeout=0.2):
            return
        try:
            with contextlib.suppress(OSError, ValueError):
                os.write(worker.in_fd, b'{"op":"shutdown"}\n')
            with contextlib.suppress(subprocess.TimeoutExpired):
                worker.proc.wait(timeout=GRACE_S)
        finally:
            worker.io.release()

    def release(self) -> bool:
        """Stop the worker if one runs and clear a shown failure; idempotent.
        A request in flight is cancelled (it fails with ``SttError``). A worker
        that another thread is already ending (idle exit, a kill) is waited for,
        at most ``GONE_WAIT_S``, so that when this returns its process is gone
        and its GPU memory is free. True when a process was stopped or waited
        for."""
        with self._launch_lock, self._lock:
            worker, self._failure = self._worker, None
            ending = [w for w in self._ending if w is not worker]
        if worker is not None:
            self._retire(worker, polite=not self._request.locked(), cancel=True)
        for other in ending:
            other.gone.wait(GONE_WAIT_S)
        return worker is not None or bool(ending)

    stop = release  # service shutdown

    @contextlib.contextmanager
    def held_off(self):
        """For a policy deploy: no worker runs, and none can start, inside the
        block. Entering releases the worker and waits until its process is gone
        (``release``); until the block ends a transcription that needs a worker
        is refused with ``SttError("held")``. Blocks may nest or overlap. A
        release failure is logged and does not stop the caller. Normal entry
        waits for launch publication and worker shutdown, not model loading."""
        # Fence Popen through publication: a deploy cannot enter while a late
        # process is still being created outside the visible worker slot.
        with self._launch_lock, self._lock:
            self._holds += 1
        try:
            try:
                self.release()
            except Exception as exc:  # noqa: BLE001 - best effort, the block goes on
                log.warning(
                    "releasing the speech worker failed (%s)", type(exc).__name__
                )
            yield
        finally:
            with self._lock:
                self._holds -= 1

    # ------------------------------------------------------------ the gate guard

    def set_guard(self, guard: Callable[[], bool] | None) -> None:
        """``guard()`` says whether a loaded worker must go (True) or may stay;
        the idle watcher asks it every ``guard_every`` seconds. It runs on the
        watcher's thread and may read slow things; an exception counts as True
        (what cannot be read is not safe to keep running next to)."""
        self._guard = guard

    def release_idle(self, worker: _Worker | None = None) -> bool:
        """Stop the worker unless a request is in flight (then leave it be).
        With ``worker``: only if it is still the current one. True when a
        process was stopped."""
        if not self._request.acquire(blocking=False):
            return False
        try:
            current = self._current()
            if current is None or (worker is not None and current is not worker):
                return False
            self._retire(current, polite=True)
            return True
        finally:
            self._request.release()

    def _run_guard(self, worker: _Worker) -> None:
        guard = self._guard
        if guard is None or self._current() is not worker:
            return
        try:
            closed = bool(guard())
        except Exception as exc:  # noqa: BLE001 - unreadable counts as closed
            log.warning("the speech gate guard failed (%s)", type(exc).__name__)
            closed = True
        if closed and self.release_idle(worker):
            log.info("speech worker released: the gate closed")

    # ------------------------------------------------------------ idle

    def _start_watcher(self, worker: _Worker) -> None:
        if not self._watch:
            return
        worker.watcher = threading.Thread(
            target=self._watch_idle,
            args=(worker,),
            name="levi-stt-idle",
            daemon=True,
        )
        worker.watcher.start()

    def _watch_idle(self, worker: _Worker) -> None:
        interval = max(0.02, min(1.0, self._idle_seconds / 4))
        next_guard = time.monotonic() + self._guard_every
        while not worker.stop.wait(interval):
            if self._current() is not worker:
                return
            self.expire_idle()
            if time.monotonic() >= next_guard:
                next_guard = time.monotonic() + self._guard_every
                self._run_guard(worker)

    def expire_idle(self) -> bool:
        """Stop the worker when it has been idle for ``idle_seconds`` (or has
        died); never while a request is in flight. True when it did."""
        if not self._request.acquire(blocking=False):
            return False
        try:
            worker = self._current()
            if worker is None:
                return False
            if worker.proc.poll() is not None:
                self._retire(worker, failure="worker_exited")
                return True
            with self._lock:
                quiet_for = self._clock() - worker.last_used
            if quiet_for < self._idle_seconds:
                return False
            self._retire(worker, polite=True)
            return True
        finally:
            self._request.release()


# ---------------------------------------------------------------- the process's one

_LOCK = threading.Lock()
_MANAGER: list = []


def get() -> Manager:
    """The process's manager (built on first use; nothing runs until a
    transcription asks for the worker)."""
    with _LOCK:
        if not _MANAGER:
            _MANAGER.append(Manager())
            atexit.register(stop_all)
        return _MANAGER[0]


def stop_all() -> None:
    """Stop the worker, if a manager was ever built (service shutdown)."""
    with _LOCK:
        manager = _MANAGER[0] if _MANAGER else None
    if manager is not None:
        manager.stop()


def held_off():
    """``Manager.held_off`` of the process's manager: the block a policy deploy
    runs in (the manager is built if need be, so that a transcription arriving
    during the deploy finds the fence; building it starts nothing)."""
    return get().held_off()
