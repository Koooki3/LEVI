"""CPU presentation-timestamp index; explicit mapping, no FPS-derived seeking."""

import bisect
import json
import subprocess
from itertools import pairwise

from .store import file_hash


def frame_index(path, cache):
    checksum = file_hash(path)
    if cache.is_file():
        saved = json.loads(cache.read_text())
        if saved["source_sha256"] == checksum:
            return saved["timestamps"]
    # Frame presentation order is authoritative for VFR and reordered codecs.
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "frame=best_effort_timestamp_time",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    frames = json.loads(result.stdout)["frames"]
    if any("best_effort_timestamp_time" not in f for f in frames):
        raise ValueError("Video has frames without presentation timestamps")
    times = [float(f["best_effort_timestamp_time"]) for f in frames]
    if not times or any(b <= a for a, b in pairwise(times)):
        raise ValueError("Video presentation timestamps are not strictly increasing")
    cache.write_text(json.dumps({"source_sha256": checksum, "timestamps": times}))
    return times


def locate(times, requested, tolerance):
    position = bisect.bisect_left(times, requested)
    options = [i for i in (position - 1, position) if 0 <= i < len(times)]
    index = min(options, key=lambda i: abs(times[i] - requested))
    if abs(times[index] - requested) > tolerance:
        raise ValueError("Source table/video time mismatch exceeds approved tolerance")
    return index, times[index]
