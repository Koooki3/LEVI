"""Sequential decode and bounded-memory H.264 encoding with measured statistics."""

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
            "stream=width,height,avg_frame_rate,codec_name,pix_fmt,nb_frames",
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
        "codec": s.get("codec_name"),
        "pixel_format": s.get("pix_fmt"),
        "declared_frames": int(s["nb_frames"])
        if s.get("nb_frames", "").isdigit()
        else None,
    }


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
