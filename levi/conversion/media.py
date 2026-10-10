"""Sequential decode and bounded-memory H.264 encoding with measured statistics."""

import functools
import hashlib
import json
import os
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


STATS_SETTING = "LEVI_PIXEL_STATS"
STATS_METHODS = ("float", "histogram")
# cv2.calcHist counts in float32, exact up to 2**24 pixels per channel.
EXACT_HIST_PIXELS = 1 << 24


def stats_method(value=None) -> str:
    """``float`` (default: the original per-frame float64 accumulation) or
    ``histogram`` (integer counts, exact sums). Anything else is refused."""
    method = (value or os.environ.get(STATS_SETTING) or "float").strip().lower()
    if method not in STATS_METHODS:
        raise ValueError(
            f"{STATS_SETTING} must be one of {', '.join(STATS_METHODS)}: {method!r}"
        )
    return method


class PixelHistogram:
    """Per-channel counts of the 8-bit values of every decoded frame.

    The statistics ``inspect`` reports are functions of these 3 x 256 counts,
    so they are computed once, from integers: the sums are exact and the
    variance has no cancellation (the float method subtracts two nearly equal
    numbers). Channels are kept in the frame's BGR order and reported as RGB."""

    def __init__(self):
        self.counts = np.zeros((3, 256), dtype=np.int64)

    def add(self, frame):
        if frame.shape[0] * frame.shape[1] <= EXACT_HIST_PIXELS:
            for c in range(3):
                hist = cv2.calcHist([frame], [c], None, [256], [0, 256])
                self.counts[c] += np.rint(hist.ravel()).astype(np.int64)
        else:
            for c in range(3):
                self.counts[c] += np.bincount(frame[:, :, c].ravel(), minlength=256)

    def result(self):
        """``(lo, hi, mean, std, pixels)``: per RGB channel, values in [0, 1]."""
        lo, hi, mean, std = [], [], [], []
        for counts in self.counts[::-1].tolist():
            n = sum(counts)
            s1 = sum(v * c for v, c in enumerate(counts))
            s2 = sum(v * v * c for v, c in enumerate(counts))
            seen = [v for v, c in enumerate(counts) if c]
            lo.append(seen[0] / 255)
            hi.append(seen[-1] / 255)
            mean.append(s1 / (255 * n))
            # Exact integer numerator: n * sum(v^2) - (sum v)^2 >= 0.
            std.append(((s2 * n - s1 * s1) / (n * n * 255 * 255)) ** 0.5)
        return np.array(lo), np.array(hi), np.array(mean), np.array(std), n


def inspect(path: Path, pixels=False):
    info = probe(path)
    method = stats_method() if pixels else None
    histogram = None
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
            if method == "histogram" and frame.dtype == np.uint8:
                histogram = PixelHistogram()
        span = max(span, float(np.abs(thumb - first).mean()))
        count += 1
        if histogram is not None:
            histogram.add(frame)
        elif pixels:
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
        shape = lambda x: np.asarray(x).reshape(3, 1, 1).tolist()
        if histogram is not None:
            lo, hi, mean, std, _ = histogram.result()
        else:
            mean = total / pixel_count
            std = np.sqrt(np.maximum(0, squares / pixel_count - mean**2))
        info["stats"] = {
            "min": shape(lo),
            "max": shape(hi),
            "mean": shape(mean),
            "std": shape(std),
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


class FrameStore:
    """A video decoded once into an uncompressed scratch file, so frames can be
    read in any order, any number of times, with bounded memory. ``selected_video``
    cannot do this: it walks the stream once and yields frames in source order,
    so asking it for ``[5, 4, 3]`` gives 3, 4, 5, and a repeated index appears
    once. Close it (or use ``with``) to delete the scratch file."""

    def __init__(self, path: Path, scratch: Path):
        scratch.mkdir(parents=True, exist_ok=True)
        self.path = Path(path)
        self.file = (
            scratch
            / f".frames-{hashlib.sha256(str(path).encode()).hexdigest()[:16]}-{id(self)}.raw"
        )
        self.shape: tuple[int, int, int] | None = None
        count = 0
        try:
            with self.file.open("wb") as out:
                for frame in decode(self.path):
                    if self.shape is None:
                        self.shape = frame.shape
                    elif frame.shape != self.shape:
                        raise ValueError(f"Video changes dimensions: {self.path.name}")
                    out.write(np.ascontiguousarray(frame).tobytes())
                    count += 1
        except BaseException:
            self.file.unlink(missing_ok=True)
            raise
        if count == 0 or self.shape is None:
            self.file.unlink(missing_ok=True)
            raise ValueError(f"No decodable frames: {self.path.name}")
        self.count = count
        self._map = np.memmap(
            self.file, dtype=np.uint8, mode="r", shape=(count, *self.shape)
        )

    def __len__(self) -> int:
        return self.count

    def __getitem__(self, index: int):
        if not 0 <= index < self.count:
            raise IndexError(f"frame {index} of {self.count} in {self.path.name}")
        return np.array(self._map[index])

    def close(self) -> None:
        self._map = None
        self.file.unlink(missing_ok=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def ordered_frames(parts):
    """Frames in the order given: ``parts`` is a list of ``(store, rows)``;
    every row of every part is yielded in turn, repeats included. The inputs
    of a reversed or spliced video."""
    for store, rows in parts:
        for row in rows:
            yield store[int(row)]


def frames_at(path: Path, rows) -> dict[int, "np.ndarray"]:
    """The frames at ``rows`` of a video, from one pass over the stream."""
    wanted = {int(r) for r in rows}
    if not wanted:
        return {}
    last = max(wanted)
    found = {}
    for index, frame in enumerate(decode(path)):
        if index in wanted:
            found[index] = frame
        if index >= last:
            break
    return found


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
