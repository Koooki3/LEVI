"""CPU presentation-timestamp index; explicit mapping, no FPS-derived seeking."""

import bisect
import json
import logging
import os
import subprocess
import threading
from collections import OrderedDict
from itertools import pairwise
from pathlib import Path

from .store import file_hash

log = logging.getLogger(__name__)
SCAN_SETTING = "LEVI_PTS_SCAN"
SCANS = ("frame", "packet")
# Content-hash keyed, so one shared video (a v3 file holds many episodes) is
# scanned once per process however many episodes read it. Small and bounded.
_MEMO: OrderedDict = OrderedDict()
_MEMO_LOCK = threading.Lock()
MEMO_ENTRIES = 16


def scan_mode(value=None):
    """``packet`` (default: container packet timestamps, about 7 times faster;
    it steps aside to the frame scan whenever the packets are not one-to-one
    with the frames) or ``frame`` (the original decode-order frame timestamps,
    kept as the switch back). Anything else is refused."""
    mode = (value or os.environ.get(SCAN_SETTING) or "packet").strip().lower()
    if mode not in SCANS:
        raise ValueError(f"{SCAN_SETTING} must be one of {', '.join(SCANS)}: {mode!r}")
    return mode


def _ffprobe(path, entries, fmt):
    """``(stdout, stderr)`` of a one-stream ffprobe query."""
    run = subprocess.run(
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
    )
    return run.stdout, run.stderr


def scan_frames(path):
    """Presentation times in decode-output order, strictly increasing."""
    # Frame presentation order is authoritative for VFR and reordered codecs.
    out, _ = _ffprobe(path, "frame=best_effort_timestamp_time", "json")
    frames = json.loads(out)["frames"]
    if any("best_effort_timestamp_time" not in f for f in frames):
        raise ValueError("Video has frames without presentation timestamps")
    times = [float(f["best_effort_timestamp_time"]) for f in frames]
    if not times or any(b <= a for a, b in pairwise(times)):
        raise ValueError("Video presentation timestamps are not strictly increasing")
    return times


def packet_times(path):
    """``(times, reason)``: the container's packet timestamps, sorted, or
    ``(None, why)`` when they cannot stand in for the frame scan.

    Packet timestamps are the frames' only when every packet becomes exactly one
    shown frame. Not so with an edit list or a negative start (the container
    marks leading packets ``D``, discard: the decoder drops them, the packet
    list still counts them), a truncated or damaged file (a ``C`` packet, or
    ffprobe complaining on stderr while still exiting 0), a packet without a
    timestamp, or times that are not strictly increasing. Each of those means
    the frame scan must decide."""
    out, err = _ffprobe(path, "packet=pts_time,flags", "csv=p=0")
    if err.strip():
        return None, "ffprobe reported problems"
    rows = [line.split(",") for line in out.split() if line]
    if not rows:
        return None, "no packets"
    times = []
    for row in rows:
        if len(row) != 2 or row[0] in ("", "N/A"):
            return None, "a packet has no presentation timestamp"
        if "D" in row[1] or "C" in row[1]:
            return None, f"a packet is flagged {row[1]!r} (discard or corrupt)"
        try:
            value = float(row[0])
        except ValueError:
            return None, "unreadable packet timestamp"
        if value < 0:
            return None, "negative presentation timestamp"
        times.append(value)
    times.sort()
    if any(b <= a for a, b in pairwise(times)):
        return None, "timestamps are not strictly increasing"
    return times, None


def scan_packets(path):
    """The packet times, or ``None`` (logged) when the frame scan must decide."""
    times, reason = packet_times(path)
    if times is None:
        log.warning(
            "LEVI_PTS_SCAN=packet: using the frame scan for %s: %s",
            Path(path).name,
            reason,
        )
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
    scan only for ``packet`` (the ``frame`` scan keeps the original layout) and
    is used only for the scan asked for."""
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
