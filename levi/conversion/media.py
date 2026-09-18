"""Sequential decode and bounded-memory H.264 encoding with measured statistics."""

import functools
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np


def probe(path: Path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise ValueError("Install ffmpeg and ffprobe to use the conversion pipeline")
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,avg_frame_rate,r_frame_rate,codec_name,pix_fmt,nb_frames",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    streams = json.loads(result.stdout).get("streams", [])
    if not streams:
        raise ValueError(f"No video stream: {path.name}")
    s = streams[0]
    a, b = s.get("avg_frame_rate", "0/1").split("/")
    return {
        "width": int(s["width"]),
        "height": int(s["height"]),
        "fps": float(a) / float(b) if float(b) else 0,
        # Nominal (container) rate as an exact fraction, e.g. "19/2".
        "rate": s.get("r_frame_rate", "0/1"),
        "codec": s.get("codec_name"),
        "pixel_format": s.get("pix_fmt"),
        "declared_frames": int(s["nb_frames"])
        if s.get("nb_frames", "").isdigit()
        else None,
    }


@functools.lru_cache(maxsize=8192)
def _probe_cached(path: str, size: int, mtime: int):
    return probe(Path(path))


def probe_cached(path: Path):
    """``probe`` memoized on (path, size, mtime): inspection, planning and
    the worker all ask about the same files."""
    st = Path(path).stat()
    return dict(_probe_cached(str(path), st.st_size, st.st_mtime_ns))


def decode(path: Path):
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            raise ValueError(f"Cannot open video: {path.name}")
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            yield frame
    finally:
        cap.release()


def inspect(path: Path, pixels=False):
    info = probe(path)
    first = None
    count = 0
    span = 0.0
    lo, hi, total, squares = np.ones(3), np.zeros(3), np.zeros(3), np.zeros(3)
    pixel_count = 0
    for frame in decode(path):
        if frame.shape[:2] != (info["height"], info["width"]):
            raise ValueError(f"Video changes dimensions: {path.name}")
        thumb = cv2.resize(frame, (32, 32)).astype(float)
        if first is None:
            first = thumb
            info["first_hash"] = hashlib.sha256(frame.tobytes()).hexdigest()
        span = max(span, float(np.abs(thumb - first).mean()))
        count += 1
        if pixels:
            rgb = frame[:, :, ::-1].reshape(-1, 3).astype(np.float64) / 255
            lo = np.minimum(lo, rgb.min(0))
            hi = np.maximum(hi, rgb.max(0))
            total += rgb.sum(0)
            squares += np.square(rgb).sum(0)
            pixel_count += len(rgb)
    if count < 2:
        raise ValueError(f"Video has fewer than 2 decoded frames: {path.name}")
    if info["declared_frames"] is not None and count != info["declared_frames"]:
        raise ValueError(
            f"Incomplete video decode: {path.name}, decoded={count}, declared={info['declared_frames']}"
        )
    info.update(frames=count, span=span)
    if pixels:
        mean = total / pixel_count
        shape = lambda x: np.asarray(x).reshape(3, 1, 1).tolist()
        info["stats"] = {
            "min": shape(lo),
            "max": shape(hi),
            "mean": shape(mean),
            "std": shape(np.sqrt(np.maximum(0, squares / pixel_count - mean**2))),
            "count": [count],
        }
    return info


def encode(frames, destination: Path, fps: float, expected: int):
    """Stream frames to ffmpeg; output is new and is published only on success."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError(f"Output already exists: {destination}")
    iterator = iter(frames)
    first = next(iterator, None)
    if first is None:
        raise ValueError("No video frames selected")
    height, width = first.shape[:2]
    if width % 2 or height % 2:
        raise ValueError(
            "H.264 yuv420p requires even image dimensions; resize inputs explicitly"
        )
    temporary = destination.with_suffix(".partial.mp4")
    with tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-n",
                "-f",
                "rawvideo",
                "-pixel_format",
                "bgr24",
                "-video_size",
                f"{width}x{height}",
                "-framerate",
                str(fps),
                "-i",
                "pipe:0",
                "-an",
                "-c:v",
                "libx264",
                "-threads",
                "2",
                "-preset",
                "fast",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(temporary),
            ],
            stdin=subprocess.PIPE,
            stderr=errors,
        )
        count = 0
        try:
            import itertools

            for frame in itertools.chain([first], iterator):
                if frame.shape != first.shape:
                    raise ValueError("Inconsistent image dimensions")
                proc.stdin.write(np.ascontiguousarray(frame).tobytes())
                count += 1
            proc.stdin.close()
            code = proc.wait(timeout=300)
            if code or count != expected:
                errors.seek(0)
                raise ValueError(
                    f"Encoder failed or count mismatch: {count}/{expected}; {errors.read(4000).decode(errors='replace')}"
                )
            temporary.replace(destination)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
            if temporary.exists():
                temporary.unlink()


def selected_video(path: Path, positions):
    wanted = set(map(int, positions))
    for index, frame in enumerate(decode(path)):
        if index in wanted:
            yield frame


class FrameScan:
    """Preflight statistics gathered while frames stream past once: decoded
    count, first-frame hash and frozen-camera span (same measures as
    ``inspect``), so the source is never decoded a second time just to audit
    it."""

    def __init__(self):
        self.count = 0
        self.span = 0.0
        self.first = None
        self.first_hash = None

    def see(self, frame):
        thumb = cv2.resize(frame, (32, 32)).astype(float)
        if self.first is None:
            self.first = thumb
            self.first_hash = hashlib.sha256(frame.tobytes()).hexdigest()
        self.span = max(self.span, float(np.abs(thumb - self.first).mean()))
        self.count += 1

    def result(self):
        return {"frames": self.count, "span": self.span, "first_hash": self.first_hash}


def scanned_selection(frames, positions, scan: FrameScan):
    """Yield the frames at ``positions`` while scanning every frame."""
    wanted = set(map(int, positions))
    for index, frame in enumerate(frames):
        scan.see(frame)
        if index in wanted:
            yield frame


def remuxable(info) -> bool:
    """Stream copy is only safe for browser-playable H.264 with even sizes."""
    return (
        info.get("codec") == "h264"
        and info.get("pixel_format") == "yuv420p"
        and not info["width"] % 2
        and not info["height"] % 2
    )


def packet_times(path: Path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "packet=pts_time",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return sorted(float(x) for x in result.stdout.split() if x not in ("", "N/A"))


def frame_interval(path: Path) -> float | None:
    """Median presentation interval from packet timestamps (no decode): the
    real frame rate, unaffected by how a muxer rounds the stream duration."""
    times = np.diff(packet_times(path))
    return float(np.median(times)) if len(times) else None


def remux(source: Path, destination: Path, rate: str, fps: float, expected: int):
    """Declare every frame of ``source`` at exactly ``fps`` without decoding
    or re-encoding (pixels stay bit-identical).

    ``-itsscale`` multiplies timestamps in the *input* time base and
    truncates, so a direct rescale drifts. First stream-copy into a time
    base ``T = lcm(numerator(rate), numerator(fps))`` where both the source
    frame duration and the rescaled one are whole ticks, then rescale — every
    frame lands on exactly ``i / fps``. Verified from the packet timestamps
    afterwards; a source that is not constant-frame-rate raises (the caller
    re-encodes instead)."""
    import math
    from fractions import Fraction

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError(f"Output already exists: {destination}")
    source_rate = Fraction(rate)
    target = Fraction(str(fps)).limit_denominator(1001)
    if source_rate <= 0:
        raise ValueError("Unknown source frame rate")
    scale = math.lcm(source_rate.numerator, target.numerator)
    if scale > 2**31 - 1:
        raise ValueError("Frame rates have no common time base")
    middle = destination.with_suffix(".rebase.mp4")
    temporary = destination.with_suffix(".partial.mp4")
    common = [
        "-map",
        "0:v:0",
        "-c",
        "copy",
        "-an",
        "-video_track_timescale",
        str(scale),
    ]
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-n",
                "-i",
                str(source),
                *common,
                str(middle),
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        )
        # A hair above the exact ratio: ffmpeg truncates the product.
        factor = float(source_rate / target) * (1 + 1e-9)
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-n",
                "-itsscale",
                repr(factor),
                "-i",
                str(middle),
                *common,
                "-movflags",
                "+faststart",
                str(temporary),
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        )
        times = np.array(packet_times(temporary))
        if len(times) != expected:
            raise ValueError(f"Remux frame count {len(times)} != {expected}")
        ideal = times[0] + np.arange(expected) / fps
        if np.abs(times - ideal).max() > 1e-3:
            raise ValueError("Source frame timing is not uniform; re-encode instead")
        temporary.replace(destination)
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"Remux failed: {exc.stderr[-2000:]}") from exc
    finally:
        middle.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)
