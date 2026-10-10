"""CPU micro-benchmark of the steps that run on every episode before a model
is asked: file hashing, presentation-timestamp (PTS) scan, conversion pixel
statistics. Synthetic video, made with ffmpeg in a scratch directory that is
removed afterwards. No GPU, no network, no LEVI workspace.

The result is one JSON document (``levi.performance.bench.v1``): parameters,
machine, versions, and per case the number of runs, median and p95 seconds.
It exists so a speed-up can be shown as numbers from the same command before
and after, not so that absolute figures travel between machines.
"""

import contextlib
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCHEMA = "levi.performance.bench.v1"
MAX_CORES = 4  # a real robot may be running on this machine


def limit_cores(cores=MAX_CORES) -> list:
    """Pin this process to at most ``cores`` of the CPUs it may use and lower
    its priority. Returns the CPU list (empty where affinity is unsupported)."""
    cores = max(1, min(int(cores), MAX_CORES))
    try:
        allowed = sorted(os.sched_getaffinity(0))
        chosen = allowed[-cores:]
        os.sched_setaffinity(0, chosen)
    except (AttributeError, OSError):
        chosen = []
    with contextlib.suppress(OSError):
        os.nice(15)
    return chosen


def quantile(values, q):
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low, high = int(position), min(int(position) + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def timed(function, repeat) -> dict:
    """Run ``function`` ``repeat`` times; seconds as median, p95, min."""
    seconds = []
    for _ in range(repeat):
        start = time.perf_counter()
        function()
        seconds.append(time.perf_counter() - start)
    return {
        "runs": repeat,
        "median_s": round(quantile(seconds, 0.5), 6),
        "p95_s": round(quantile(seconds, 0.95), 6),
        "min_s": round(min(seconds), 6),
    }


def make_video(path, *, size="640x480", fps=10, frames=182, extra=()):
    """A synthetic H.264 file (testsrc2) of exactly ``frames`` frames."""
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi",
            "-i", f"testsrc2=size={size}:rate={fps}:duration={frames / fps}",
            "-frames:v", str(frames), "-c:v", "libx264", "-threads", "1",
            "-pix_fmt", "yuv420p", *extra, str(path),
        ],
        check=True,
        timeout=300,
    )  # fmt: skip
    return Path(path)


def machine() -> dict:
    info = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "affinity": len(os.sched_getaffinity(0))
        if hasattr(os, "sched_getaffinity")
        else None,
    }
    for name in ("numpy", "cv2"):
        try:
            module = __import__(name)
            info[name] = module.__version__
        except ImportError:
            info[name] = None
    try:
        out = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.splitlines()
        info["ffmpeg"] = out[0].split()[2] if out else None
    except (OSError, subprocess.SubprocessError, IndexError):
        info["ffmpeg"] = None
    return info


# --- cases ---------------------------------------------------------------------


def case_hash(video, repeat):
    def run():
        with Path(video).open("rb") as handle:
            hashlib.file_digest(handle, "sha256").hexdigest()

    return {"bytes": Path(video).stat().st_size, **timed(run, repeat)}


def case_pts(video, repeat):
    """Both ways of listing presentation times (``levi.agent.video_evidence``),
    and whether they agree."""
    from levi.agent import video_evidence as ve

    scans = {
        "frame": timed(lambda: ve.scan_frames(video), repeat),
        "packet": timed(lambda: ve.scan_packets(video), repeat),
    }
    return {
        "scans": scans,
        "identical": ve.scan_frames(video) == ve.scan_packets(video),
    }


def case_pixels(video, repeat):
    from levi.conversion import media

    return timed(lambda: media.inspect(Path(video), pixels=True), repeat)


def compare_scans(paths) -> dict:
    """Read-only: for each video, whether the packet scan returns exactly the
    frame scan's list (PRF-04: required on the real videos before ``packet`` may
    become the default). A file that cannot be scanned is reported, not raised."""
    from levi.agent import video_evidence as ve

    files = []
    for entry in map(Path, paths):
        files += sorted(entry.rglob("*.mp4")) if entry.is_dir() else [entry]
    rows = []
    for path in files:
        row = {"file": path.name, "identical": False, "frames": None, "error": None}
        try:
            frames = ve.scan_frames(path)
            row.update(frames=len(frames), identical=ve.scan_packets(path) == frames)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            row["error"] = type(error).__name__
        rows.append(row)
    return {
        "schema": "levi.performance.pts_compare.v1",
        "files": len(rows),
        "all_identical": bool(rows) and all(r["identical"] for r in rows),
        "different": [r["file"] for r in rows if not r["identical"]],
        "rows": rows,
    }


CASES = {"hash": case_hash, "pts": case_pts, "pixels": case_pixels}


def run(
    cases=None, repeat=3, frames=182, size="640x480", fps=10, scratch=None, cores=None
):
    """Run the chosen cases and return the JSON-able result. ``cores`` pins
    this process to that many CPUs (at most ``MAX_CORES``) and lowers its
    priority; ``None`` leaves the process alone (tests)."""
    names = list(cases or CASES)
    unknown = [n for n in names if n not in CASES]
    if unknown:
        raise ValueError(f"Unknown case: {', '.join(unknown)}")
    chosen = limit_cores(cores) if cores else []
    base = Path(scratch) if scratch else None
    if base:
        base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="levi-bench-", dir=base))
    try:
        started = time.time()
        video = make_video(work / "synthetic.mp4", size=size, fps=fps, frames=frames)
        out = {
            "schema": SCHEMA,
            "params": {
                "repeat": repeat, "frames": frames, "size": size, "fps": fps,
                "cores": len(chosen) or None,
            },
            "machine": machine(),
            "cases": {name: CASES[name](video, repeat) for name in names},
        }  # fmt: skip
        out["wall_s"] = round(time.time() - started, 2)
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


def dumps(value) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)


if __name__ == "__main__":  # pragma: no cover
    sys.exit("use: python -m levi.performance bench")
