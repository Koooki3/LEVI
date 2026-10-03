"""Keeping the live service light.

The machine also runs the robot's real-time control, the cameras and the
policy server; the annotation service must never compete with them. So every
process it starts lowers its own priority and bounds its threads, idle costs
next to nothing, and everything it writes is bounded:

- ``apply``: nice 19, ionice class idle, and thread caps for OpenMP, MKL,
  OpenBLAS, NumExpr, OpenCV and Arrow (set *before* those libraries load);
- ``Meter``: resident memory, thread count and CPU percentage of a process,
  from ``/proc`` (for ``levi live doctor`` and the status file);
- ``rotating_logger``: a size-capped log with a few backups;
- ``trim_cache``: a cap on the regenerable run files (frozen inputs and
  evidence), deleted oldest first, never a batch in progress.

Standard library only.
"""

import contextlib
import logging
import logging.handlers
import os
import shutil
import subprocess
import time
from pathlib import Path

THREAD_VARS = (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "OPENCV_FOR_THREADS_NUM",
    "ARROW_NUM_THREADS",
)


# What levi.paths.configure() derives from the workspace: inherited from a
# shell that ran another workspace these would point outside this one.
WORKSPACE_VARS = (
    "UV_CACHE_DIR",
    "HF_HOME",
    "HF_HUB_CACHE",
    "BUN_INSTALL_CACHE_DIR",
    "PLAYWRIGHT_BROWSERS_PATH",
    "TMPDIR",
    "LEROBOT_ANNOTATE_CACHE",
    "LEROBOT_ANNOTATE_EXPORT",
    "LEVI_SAM3_CHECKPOINT_DIR",
    "LEVI_RECAP_VALUE_CHECKPOINT_DIR",
)


def service_env(config) -> dict:
    """The environment of everything the service starts (the UI, the core,
    the worker): the live workspace, bounded threads, no other workspace's
    derived paths, and no approver switch (only the worker gets that)."""
    env = {k: v for k, v in os.environ.items() if k not in WORKSPACE_VARS}
    env.update(thread_env(config.resources.threads))
    env.update(
        LEVI_WORKSPACE=str(config.workspace),
        LEVI_LIVE_HOME=str(config.home),
        LEVI_DROID_SAMPLE="off",
        LEVI_GPU_SHARING="allow",
        LEVI_SYNC_INTERVAL="30",
        LEVI_SYNC_DISCOVER="off",
        LEVI_AGENT=config.gpu.lock_agent,
    )
    env.pop("LEVI_LIVE_AUTO_APPROVE", None)
    # Only the worker process sets this (worker.configure_process): the core
    # must not inherit an exemption from the gate from somebody's shell.
    env.pop("LEVI_LIVE_WORKER", None)
    return env


def thread_env(threads: int) -> dict:
    return {name: str(int(threads)) for name in THREAD_VARS}


def apply(config, *, pid: int | None = None) -> dict:
    """Lower this process's priority and cap its libraries' threads.

    Returns what took effect; a refusal (no ionice, a container) is not an
    error, only reported."""
    r = config.resources
    done = {"nice": None, "ionice": None, "threads": r.threads}
    os.environ.update(thread_env(r.threads))
    pid = pid or os.getpid()
    try:
        os.setpriority(os.PRIO_PROCESS, pid, r.nice)
        done["nice"] = r.nice
    except OSError:
        pass
    try:
        args = ["ionice", "-c", str(r.ionice_class), "-p", str(pid)]
        if r.ionice_class in (1, 2):
            args[3:3] = ["-n", "7"]
        code = subprocess.run(
            args, capture_output=True, timeout=5, check=False
        ).returncode
        done["ionice"] = r.ionice_class if code == 0 else None
    except (OSError, subprocess.SubprocessError):
        pass
    return done


def limit_cv2(threads: int):
    """OpenCV decodes with its own thread pool; cap it once it is imported."""
    with contextlib.suppress(Exception):
        import cv2

        cv2.setNumThreads(int(threads))


# --- measuring ------------------------------------------------------------------


def _stat_fields(pid):
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()


def rss_mb(pid) -> float | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024, 1)
    except (OSError, ValueError, IndexError):
        pass
    return None


def thread_count(pid) -> int | None:
    try:
        return int(_stat_fields(pid)[17])
    except (OSError, ValueError, IndexError):
        return None


def fd_count(pid) -> int | None:
    """Open file descriptors of the process (a read of /proc/<pid>/fd)."""
    try:
        return len(os.listdir(f"/proc/{pid}/fd"))
    except OSError:
        return None


def fd_soft_limit(pid) -> int | None:
    """The process's soft limit on open files, from /proc/<pid>/limits."""
    try:
        for line in Path(f"/proc/{pid}/limits").read_text().splitlines():
            if line.startswith("Max open files"):
                word = line.split()[3]
                return None if word == "unlimited" else int(word)
    except (OSError, ValueError, IndexError):
        pass
    return None


def cpu_seconds(pid) -> float | None:
    """User + system CPU seconds the process has used."""
    try:
        fields = _stat_fields(pid)
        ticks = int(fields[11]) + int(fields[12])
        return ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None


def tree(pid) -> list:
    """``pid`` and its descendants (from /proc/*/stat parents)."""
    children: dict = {}
    try:
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            try:
                parent = int(_stat_fields(entry.name)[1])
            except (OSError, ValueError, IndexError):
                continue
            children.setdefault(parent, []).append(int(entry.name))
    except OSError:
        return [pid]
    out, stack = [], [pid]
    while stack:
        current = stack.pop()
        out.append(current)
        stack.extend(children.get(current, []))
    return out


class Meter:
    """CPU percentage of a process between two calls (100 = one full core)."""

    def __init__(self, pid=None):
        self.pid = pid or os.getpid()
        self._last = (time.monotonic(), cpu_seconds(self.pid))

    def percent(self) -> float | None:
        now, cpu = time.monotonic(), cpu_seconds(self.pid)
        before, cpu_before = self._last
        self._last = (now, cpu)
        if cpu is None or cpu_before is None or now <= before:
            return None
        return round(100.0 * (cpu - cpu_before) / (now - before), 2)


def dir_size(path, limit=None) -> int:
    """Bytes under ``path`` (files only, symlinks not followed); stops early
    once ``limit`` is exceeded."""
    total = 0
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
        if limit is not None and total > limit:
            return total
    return total


def disk_free_gib(path) -> float | None:
    target = Path(path)
    while not target.exists() and target != target.parent:
        target = target.parent
    try:
        return round(shutil.disk_usage(target).free / 1024**3, 1)
    except OSError:
        return None


# --- logs ---------------------------------------------------------------------------


def rotating_logger(name, path, max_mb=5, backups=3) -> logging.Logger:
    """A logger writing to ``path`` that never exceeds ``max_mb`` x (backups+1)."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=int(max_mb * 1024 * 1024), backupCount=backups
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def rotate_file(path, max_mb=5, backups=3):
    """Rotate a log a child process appends to (done before it starts)."""
    path = Path(path)
    try:
        if path.stat().st_size <= max_mb * 1024 * 1024:
            return
    except OSError:
        return
    for n in range(backups - 1, 0, -1):
        older = path.with_name(f"{path.name}.{n}")
        if older.exists():
            os.replace(older, path.with_name(f"{path.name}.{n + 1}"))
    os.replace(path, path.with_name(path.name + ".1"))


# --- the cache cap ---------------------------------------------------------------------


def run_files(workbench_state: Path):
    """(path, mtime, bytes) of every regenerable run folder (frozen input,
    evidence) in a workbench state directory."""
    base = Path(workbench_state) / "agent" / "datasets"
    found = []
    try:
        for dataset in os.scandir(base):
            runs = Path(dataset.path) / "runs"
            try:
                run_dirs = list(os.scandir(runs))
            except OSError:
                continue
            for run in run_dirs:
                for name in ("input", "evidence"):
                    target = Path(run.path) / name
                    try:
                        mtime = target.stat().st_mtime
                    except OSError:
                        continue
                    found.append((target, mtime, dir_size(target)))
    except OSError:
        pass
    return found


def trim_cache(
    workbench_state, cap_gib, *, keep=(), min_age_s=6 * 3600, now=None
) -> dict:
    """Delete the oldest regenerable run folders until the total is under
    ``cap_gib``. ``keep``: path fragments (run ids) that must stay (a batch in
    progress). Returns ``{"before", "after", "removed"}`` in bytes / count."""
    now = time.time() if now is None else now
    rows = run_files(workbench_state)
    total = sum(r[2] for r in rows)
    before, removed = total, 0
    cap = int(cap_gib * 1024**3)
    for target, mtime, size in sorted(rows, key=lambda r: r[1]):
        if total <= cap:
            break
        if now - mtime < min_age_s or any(k and k in str(target) for k in keep):
            continue
        shutil.rmtree(target, ignore_errors=True)
        total -= size
        removed += 1
    return {"before": before, "after": total, "removed": removed}
