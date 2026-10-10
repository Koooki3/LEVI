"""CPU presentation-timestamp index; explicit mapping, no FPS-derived seeking."""

import bisect
import json
import os
import subprocess
import threading
from collections import OrderedDict
from itertools import pairwise

from .store import file_hash

SCAN_SETTING = "LEVI_PTS_SCAN"
SCANS = ("frame", "packet")
# Content-hash keyed, so one shared video (a v3 file holds many episodes) is
# scanned once per process however many episodes read it. Small and bounded.
_MEMO: OrderedDict = OrderedDict()
_MEMO_LOCK = threading.Lock()
MEMO_ENTRIES = 16


def scan_mode(value=None):
    """``frame`` (default: decode-order frame timestamps) or ``packet``
    (container packet timestamps, much faster). Anything else is refused."""
    mode = (value or os.environ.get(SCAN_SETTING) or "frame").strip().lower()
    if mode not in SCANS:
        raise ValueError(f"{SCAN_SETTING} must be one of {', '.join(SCANS)}: {mode!r}")
    return mode


def _ffprobe(path, entries, fmt):
    return subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            entries,
            "-of",
            fmt,
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    ).stdout


def scan_frames(path):
    """Presentation times in decode-output order, strictly increasing."""
    # Frame presentation order is authoritative for VFR and reordered codecs.
    frames = json.loads(_ffprobe(path, "frame=best_effort_timestamp_time", "json"))[
        "frames"
    ]
    if any("best_effort_timestamp_time" not in f for f in frames):
        raise ValueError("Video has frames without presentation timestamps")
    times = [float(f["best_effort_timestamp_time"]) for f in frames]
    if not times or any(b <= a for a, b in pairwise(times)):
        raise ValueError("Video presentation timestamps are not strictly increasing")
    return times


def scan_packets(path):
    """Presentation times from the container's packets, sorted; ``None`` when
    they cannot stand in for the frame scan (a packet without a timestamp, no
    packets, or timestamps that are not strictly increasing). The caller then
    runs the frame scan, which decides and reports."""
    words = _ffprobe(path, "packet=pts_time", "csv=p=0").split()
    if not words or "N/A" in words:
        return None
    try:
        times = sorted(float(word) for word in words)
    except ValueError:
        return None
    if any(b <= a for a, b in pairwise(times)):
        return None
    return times


def scan_times(path, mode):
    """The times by the chosen scan; ``packet`` falls back to ``frame``."""
    if mode == "packet":
        times = scan_packets(path)
        if times is not None:
            return times
    return scan_frames(path)


def frame_index(path, cache, checksum=None, mode=None):
    """The video's presentation times, cached next to the evidence.

    ``checksum`` is the file's SHA-256 when the caller has just computed it (a
    second pass over the file would only repeat it). The cache file records the
    scan only for ``packet`` (the default keeps the original layout) and is
    used only for the scan asked for."""
    checksum = checksum or file_hash(path)
    mode = scan_mode(mode)
    if cache.is_file():
        saved = json.loads(cache.read_text())
        if saved["source_sha256"] == checksum and saved.get("scan", "frame") == mode:
            return saved["timestamps"]
    key = (checksum, mode)
    with _MEMO_LOCK:
        times = _MEMO.get(key)
        if times is not None:
            _MEMO.move_to_end(key)
    if times is None:
        times = scan_times(path, mode)
        with _MEMO_LOCK:
            _MEMO[key] = times
            while len(_MEMO) > MEMO_ENTRIES:
                _MEMO.popitem(last=False)
    saved = {"source_sha256": checksum, "timestamps": times}
    if mode != "frame":
        saved["scan"] = mode
    cache.write_text(json.dumps(saved))
    return list(times)


def locate(times, requested, tolerance):
    position = bisect.bisect_left(times, requested)
    options = [i for i in (position - 1, position) if 0 <= i < len(times)]
    index = min(options, key=lambda i: abs(times[i] - requested))
    if abs(times[index] - requested) > tolerance:
        raise ValueError("Source table/video time mismatch exceeds approved tolerance")
    return index, times[index]
