"""What a pool job leaves behind for a person who has to find out what
happened: one timestamped log, a structured error, per-episode failures, and
plain-language hints for exit codes and exceptions.

Files next to the job record ``<id>.json`` (``<workspace>/pool/jobs/``):

- ``<id>.log``: timestamped lines from the worker (stages, warnings,
  tracebacks), rotated at ``LOG_CAP`` bytes into ``<id>.log.1``;
  ``<id>.stdio`` holds what the process printed outside the log (a crash
  the log never saw).
- ``<id>.error.json``: the fatal error (type, message, stage, hint).
- ``<id>.errors.jsonl``: one line per episode that failed and was left out.
- ``<id>.beat.json``: the worker's heartbeat.
"""

import errno
import json
import os
import signal
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

# Set the moment this process is told to stop (SIGTERM, SIGHUP): errors that
# follow are the stop's doing, not the job's.
TERMINATING = threading.Event()
LOG_CAP = 4 * 1024 * 1024
BEAT_SECONDS = 2.0

ERRNO_HINTS = {
    errno.ENOSPC: (
        "Disk full",
        (
            "The volume ran out of space. Free space (`levi pool clean`, or delete old "
            "exports), then resume: finished units are kept."
        ),
    ),
    errno.EDQUOT: (
        "Disk quota exceeded",
        "Free space under the export folder, then resume.",
    ),
    errno.EACCES: (
        "Permission denied",
        (
            "Check that the user running LEVI can read the sources and write the "
            "export folder."
        ),
    ),
    errno.EPERM: (
        "Operation not permitted",
        "Check ownership of the sources and the export folder.",
    ),
    errno.ENOENT: (
        "A source file is missing",
        (
            "A source moved or was deleted after the plan was made: scan the pool "
            "again and re-run."
        ),
    ),
    errno.EIO: (
        "Input/output error",
        (
            "The disk or network mount reported an I/O error; check its health, "
            "then resume."
        ),
    ),
    errno.ENOMEM: (
        "Out of memory",
        "Lower `workers`, close other programs, then resume.",
    ),
}
SIGNAL_HINTS = {
    signal.SIGTERM: (
        "Stopped by the service or the user (SIGTERM, exit 143)",
        (
            "The service was stopped or the process was terminated. The partial "
            "output is kept: press Resume."
        ),
    ),
    signal.SIGINT: (
        "Interrupted from the terminal (Ctrl+C, exit 130)",
        "The partial output is kept: resume it with `levi pool export --resume <job>`.",
    ),
    signal.SIGHUP: (
        "The terminal or session closed (SIGHUP, exit 129)",
        (
            "Run long exports from the page, or under `tmux`/`nohup`. The partial "
            "output is kept: resume it."
        ),
    ),
    signal.SIGKILL: (
        "Killed (SIGKILL, exit 137)",
        (
            "The process was killed, most often by the kernel because the machine "
            "ran out of memory. Lower `workers`, then resume: finished units are kept."
        ),
    ),
}


HINT_HELDOUT = (
    "Held-out (frozen test) episodes are never exported. Take them out "
    "of the selection; the composition preview never picks them, so a "
    "hand-edited plan or a stale scan is the usual cause: scan again."
)
HINT_ROOTS = (
    "Choose an export folder inside LEVI_EXPORT_ROOTS and outside every source."
)
HINT_SCAN = "Run `levi pool scan`, then Re-run the export from the saved recipe."
HINT_SPACE = "Free space on the export volume (or choose another), then start again."
HINT_SCHEMA = "Take the differing sources out of the selection, or map their cameras."
HINT_FPS = "Lower the export FPS as suggested, or choose the Retime timing."
HINT_EXISTS = "Choose another dataset name, or delete the old folder."
HINT_POOL = (
    "A conversion worker died, most often out of memory. Lower `workers`, then resume."
)
HINT_FATAL = "Fix the cause named above, then press Resume: what finished is kept."
HINT_DEFAULT = "See the job log for the traceback."
MSG_GONE = (
    "The worker is gone: the service or the machine restarted, or the process "
    "was killed"
)
HINT_GONE = "What finished is kept; resume continues."
HINT_SIGNAL = "Check `dmesg` and the job log; resume keeps finished units."
HINT_EXIT = "See the job log for the last lines it wrote."


def translatable() -> list[str]:
    """Every fixed sentence a job's error can carry to the page (the UI
    translates them; a test keeps both catalogs complete)."""
    texts = [
        t for pair in (*ERRNO_HINTS.values(), *SIGNAL_HINTS.values()) for t in pair
    ]
    texts += [
        HINT_HELDOUT,
        HINT_ROOTS,
        HINT_SCAN,
        HINT_SPACE,
        HINT_SCHEMA,
        HINT_FPS,
        HINT_EXISTS,
        HINT_POOL,
        HINT_FATAL,
        HINT_DEFAULT,
        MSG_GONE,
        HINT_GONE,
        HINT_SIGNAL,
        HINT_EXIT,
    ]
    return list(dict.fromkeys(texts))


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def paths(job_path: Path) -> dict[str, Path]:
    base = Path(job_path)
    stem = base.name.removesuffix(".json")
    return {
        "log": base.with_name(stem + ".log"),
        "stdio": base.with_name(stem + ".stdio"),
        "error": base.with_name(stem + ".error.json"),
        "errors": base.with_name(stem + ".errors.jsonl"),
        "beat": base.with_name(stem + ".beat.json"),
        "cancel": base.with_name(stem + ".cancel"),
        "stopping": base.with_name(stem + ".stopping"),
        "progress": base.with_name(stem + ".progress.json"),
        "result": base.with_name(stem + ".result.json"),
    }


def companions(job_path: Path) -> list[Path]:
    """Every file a job leaves in the jobs folder (record, log, errors …)."""
    p = paths(job_path)
    return [Path(job_path), *p.values(), p["log"].with_name(p["log"].name + ".1")]


class JobLog:
    """A line-oriented, timestamped, size-capped log file."""

    def __init__(self, path: Path, cap: int = LOG_CAP):
        self.path = Path(path)
        self.cap = cap
        self._lock = threading.Lock()

    def write(self, level: str, message: str) -> None:
        stamp = now_iso()
        lines = str(message).rstrip("\n").split("\n") or [""]
        text = "".join(f"{stamp} {level:<5} {line}\n" for line in lines)
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > self.cap:
                    os.replace(self.path, self.path.with_name(self.path.name + ".1"))
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(text)
            except OSError:
                pass  # a full disk must not hide the original error

    def info(self, message: str) -> None:
        self.write("INFO", message)

    def warning(self, message: str) -> None:
        self.write("WARN", message)

    def error(self, message: str) -> None:
        self.write("ERROR", message)

    def trace(self, exc: BaseException, stage: str = "") -> None:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self.write("ERROR", f"{stage + ': ' if stage else ''}{tb}")


def tail(job_path: Path, kilobytes: int = 64) -> str:
    """The last ``kilobytes`` KB of the job's log (and its stdio, when that
    holds something the log does not)."""
    p = paths(job_path)
    limit = max(1, min(int(kilobytes), 2048)) * 1024
    out = []
    for key in ("log", "stdio"):
        path = p[key]
        if not path.is_file():
            continue
        size = path.stat().st_size
        if key == "stdio" and size == 0:
            continue
        with path.open("rb") as handle:
            handle.seek(max(0, size - limit))
            data = handle.read().decode("utf-8", "replace")
        if size > limit:
            data = "…\n" + data.split("\n", 1)[-1]
        out.append(data if key == "log" else f"--- stdio ---\n{data}")
    return "\n".join(out)


def signal_of(exit_code: int | None) -> int | None:
    """The signal a worker died from: a negative ``Popen`` code, or the shell
    convention 128+N."""
    if exit_code is None:
        return None
    if exit_code < 0:
        return -exit_code
    if 129 <= exit_code <= 159:
        return exit_code - 128
    return None


def describe_exit(exit_code: int | None) -> dict:
    """A worker's exit code as a person reads it."""
    number = signal_of(exit_code)
    if number in SIGNAL_HINTS:
        message, hint = SIGNAL_HINTS[number]
        return {"type": "Signal", "message": message, "hint": hint}
    if exit_code is None:
        return {
            "type": "WorkerGone",
            "message": MSG_GONE,
            "hint": HINT_GONE,
        }
    if number:
        name = signal.Signals(number).name if number in signal.Signals else number
        return {
            "type": "Signal",
            "message": f"The worker died from signal {name} (exit {exit_code})",
            "hint": HINT_SIGNAL,
        }
    return {
        "type": "ExitCode",
        "message": f"The worker exited with code {exit_code} without a result",
        "hint": HINT_EXIT,
    }


def describe_exception(exc: BaseException, stage: str = "") -> dict:
    """An exception as ``{type, message, stage, hint}``."""
    hint = HINT_DEFAULT
    message = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, OSError) and exc.errno in ERRNO_HINTS:
        title, hint = ERRNO_HINTS[exc.errno]
        message = f"{title}: {exc}"
    elif isinstance(exc, MemoryError):
        title, hint = ERRNO_HINTS[errno.ENOMEM]
        message = f"{title}: {exc}"
    elif isinstance(exc, PermissionError):
        text = str(exc)
        if "held-out" in text:
            hint = HINT_HELDOUT
        elif "outside LEVI_EXPORT_ROOTS" in text or "inside the source" in text:
            hint = HINT_ROOTS
        else:
            title, hint = ERRNO_HINTS[errno.EACCES]
    elif isinstance(exc, FileNotFoundError):
        title, hint = ERRNO_HINTS[errno.ENOENT]
    elif isinstance(exc, ValueError):
        text = str(exc)
        if "changed since the pool was scanned" in text:
            hint = HINT_SCAN
        elif "free space" in text.lower():
            hint = HINT_SPACE
        elif "do not share one schema" in text:
            hint = HINT_SCHEMA
        elif "lower the export fps" in text or "timing retime" in text:
            hint = HINT_FPS
        elif "already exists" in text or "unfinished export" in text:
            hint = HINT_EXISTS
    elif type(exc).__name__ == "BrokenProcessPool":
        hint = HINT_POOL
    elif type(exc).__name__ == "FatalExport":
        hint = HINT_FATAL
    return {
        "type": type(exc).__name__,
        "message": message,
        "stage": stage,
        "hint": hint,
    }


def write_error(job_path: Path, info: dict) -> None:
    from ..catalog import atomic

    atomic(paths(job_path)["error"], {**info, "at": now_iso()})


def read_error(job_path: Path) -> dict | None:
    try:
        return json.loads(paths(job_path)["error"].read_text())
    except (OSError, ValueError):
        return None


def append_failure(job_path: Path, record: dict) -> None:
    """One episode's failure: episode, unit, stage, type, message, traceback."""
    line = json.dumps({**record, "at": now_iso()}, ensure_ascii=False)
    path = paths(job_path)["errors"]
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def read_failures(job_path: Path) -> list[dict]:
    path = paths(job_path)["errors"]
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):  # a line that is valid JSON but no record is skipped
            out.append(row)
    return out


def failure(exc: BaseException, *, episode: str, unit: str, stage: str) -> dict:
    return {
        "episode": episode,
        "unit": unit,
        "stage": stage,
        "type": type(exc).__name__,
        "message": str(exc)[:2000],
        "traceback": "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )[-4000:],
    }


def read_beat(job_path: Path) -> dict | None:
    try:
        return json.loads(paths(job_path)["beat"].read_text())
    except (OSError, ValueError):
        return None


class Heartbeat:
    """Rewrites ``<id>.beat.json`` every couple of seconds from a daemon
    thread: ``at`` says the process is alive, ``activity_at`` when its work
    last moved (a stage began, a unit finished, a warning was raised)."""

    def __init__(self, job_path: Path, activity=None, interval: float = BEAT_SECONDS):
        self.path = paths(job_path)["beat"]
        self.interval = interval
        self.activity = activity or (lambda: time.time())
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def beat(self) -> None:
        from ..catalog import atomic

        try:
            atomic(
                self.path,
                {"at": time.time(), "pid": os.getpid(), "activity_at": self.activity()},
            )
        except OSError:
            pass

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self.beat()

    def __enter__(self):
        self.beat()
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(2)
        return False
