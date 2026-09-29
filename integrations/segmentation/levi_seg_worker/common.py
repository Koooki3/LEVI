"""Shared helpers: plans, masks, video frames, progress and the GPU lock.

Only the standard library and NumPy/OpenCV/pyarrow are imported here, so the
fake provider (LEVI's own Python, no Torch) can use the same code paths as the
real student. Masks leave the worker as COCO *uncompressed*, column-major RLE,
exactly the sidecar format LEVI stores (``levi.annotations.sidecar``).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Self

import numpy as np

# ------------------------------------------------------------------ json io


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=1, allow_nan=False))
    os.replace(temp, path)


class Progress:
    """Progress record the LEVI watchdog reads (a stalled file stops the job)."""

    def __init__(self, path: Path | None):
        self.path = path
        self.value: dict[str, Any] = {}

    def set(self, stage: str, done: int = 0, total: int = 0, **extra: Any) -> None:
        self.value = {
            "stage": stage,
            "done": int(done),
            "total": int(total),
            "updated_at": time.time(),
            **extra,
        }
        if self.path is None:
            return
        try:
            atomic_json(self.path, self.value)
        except OSError:
            # Telemetry must never fail an otherwise healthy job.
            pass

    def touch(self) -> None:
        if self.value:
            self.set(**{k: v for k, v in self.value.items() if k != "updated_at"})


# ------------------------------------------------------------------ gpu lock


@contextlib.contextmanager
def gpu_lock(progress: Progress | None = None, *, wait: bool = True) -> Iterator[None]:
    """Hold ``LEVI_GPU_LOCK_FILE`` (an flock file shared with other GPU users
    on this machine) for the lifetime of the block; no file set, no lock.

    Waiting keeps the progress file fresh so LEVI's stall watchdog does not
    mistake a queued job for a hung one. ``wait=False`` (live sessions) fails
    at once instead of queueing behind a long job."""
    path = os.environ.get("LEVI_GPU_LOCK_FILE", "").strip()
    if not path:
        yield
        return
    timeout = float(os.environ.get("LEVI_GPU_LOCK_TIMEOUT_SECONDS", "14400"))
    handle = open(path, "a+")  # noqa: SIM115 - held for the block
    try:
        started = time.time()
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not wait:
                    raise RuntimeError(
                        f"The GPU lock {path} is held by another job; try again when it is free"
                    ) from None
                if time.time() - started > timeout:
                    raise RuntimeError(
                        f"Waited {timeout:.0f} s for the GPU lock {path} (LEVI_GPU_LOCK_TIMEOUT_SECONDS)"
                    ) from None
                if progress is not None:
                    progress.set("waiting_gpu_lock", 0, 0, lock=path)
                time.sleep(2.0)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


# ------------------------------------------------------------------ masks


def rle_encode(mask: np.ndarray) -> dict[str, Any]:
    """COCO uncompressed RLE (column-major); the first run counts zeros."""
    h, w = mask.shape
    flat = np.asarray(mask, dtype=bool).T.reshape(-1)
    if flat.size == 0:
        return {"size": [int(h), int(w)], "counts": [0]}
    change = np.flatnonzero(flat[1:] != flat[:-1]) + 1
    bounds = np.concatenate(([0], change, [flat.size]))
    runs = np.diff(bounds).tolist()
    if flat[0]:
        runs = [0, *runs]
    return {"size": [int(h), int(w)], "counts": [int(v) for v in runs]}


def rle_decode(rle: dict[str, Any]) -> np.ndarray:
    h, w = (int(v) for v in rle["size"])
    counts = np.asarray(rle["counts"], dtype=np.int64)
    values = np.zeros(len(counts), dtype=bool)
    values[1::2] = True
    flat = np.repeat(values, counts)
    if flat.size < h * w:
        flat = np.concatenate([flat, np.zeros(h * w - flat.size, bool)])
    return flat[: h * w].reshape(w, h).T


def mask_bbox(mask: np.ndarray) -> list[float] | None:
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return None
    return [float(xs.min()), float(ys.min()), float(xs.max() + 1), float(ys.max() + 1)]


def mask_iou(a: np.ndarray, b: np.ndarray) -> float:
    inter = np.logical_and(a, b).sum()
    if inter == 0:
        return 0.0
    return float(inter / np.logical_or(a, b).sum())


def annotation(
    *,
    episode_index: int,
    frame_index: int,
    fps: float,
    camera_key: str,
    object_prefix: str,
    track_id: int,
    concept: str,
    score: float,
    mask: np.ndarray,
    source: str,
    bbox: list[float] | None = None,
) -> dict[str, Any] | None:
    """One sidecar row in the ``ObjectAnnotation`` shape (``mask_rle``)."""
    box = bbox or mask_bbox(mask)
    if box is None or not mask.any():
        return None
    h, w = mask.shape
    box = [
        max(0.0, min(float(w), float(box[0]))),
        max(0.0, min(float(h), float(box[1]))),
        max(0.0, min(float(w), float(box[2]))),
        max(0.0, min(float(h), float(box[3]))),
    ]
    if box[2] < box[0] or box[3] < box[1]:
        return None
    return {
        "episode_index": int(episode_index),
        "frame_index": int(frame_index),
        "timestamp": frame_index / float(fps),
        "camera_key": camera_key,
        "object_id": f"{object_prefix}-{episode_index}-{camera_key.replace('.', '_')}-{int(track_id)}",
        "track_id": int(track_id),
        "concept": concept,
        "category": concept,
        "bbox_xyxy": box,
        "image_size": [int(h), int(w)],
        "mask_rle": rle_encode(mask),
        "score": max(0.0, min(1.0, float(score))),
        "visible": True,
        "occluded": False,
        # Model output is a suggestion until a person reviews it.
        "status": "suggested",
        "source": source,
        "prompt": concept,
    }


MASK_COLUMNS = (
    ("episode_index", "int64"),
    ("frame_index", "int64"),
    ("timestamp", "float64"),
    ("camera_key", "string"),
    ("object_id", "string"),
    ("track_id", "int64"),
    ("concept", "string"),
    ("category", "string"),
    ("bbox_xyxy", "list<float32>"),
    ("image_size", "list<int32>"),
    ("rle_size", "list<int32>"),
    ("rle_counts", "list<int64>"),
    ("score", "float32"),
    ("visible", "bool"),
    ("occluded", "bool"),
    ("status", "string"),
    ("source", "string"),
    ("prompt", "string"),
)


def _mask_schema():
    import pyarrow as pa

    types = {
        "int64": pa.int64(),
        "float64": pa.float64(),
        "float32": pa.float32(),
        "string": pa.string(),
        "bool": pa.bool_(),
        "list<float32>": pa.list_(pa.float32()),
        "list<int32>": pa.list_(pa.int32()),
        "list<int64>": pa.list_(pa.int64()),
    }
    return pa.schema([(name, types[kind]) for name, kind in MASK_COLUMNS])


def write_rows(rows: list[dict[str, Any]], path: Path) -> None:
    """Sidecar rows -> one parquet in LEVI's MASK_SCHEMA (flat RLE columns)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    flat = []
    for row in rows:
        value = dict(row)
        rle = value.pop("mask_rle")
        value["rle_size"] = rle["size"]
        value["rle_counts"] = rle["counts"]
        flat.append(value)
    flat.sort(key=lambda r: (r["frame_index"], r["track_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    pq.write_table(pa.Table.from_pylist(flat, schema=_mask_schema()), temp, compression="zstd")
    os.replace(temp, path)


# ------------------------------------------------------------------ video


class VideoReader:
    """Sequential RGB frames of one episode inside a (possibly shared) video.

    ``start_frame`` is the episode's first frame in the file (v3 shards hold
    several episodes). Reading forward decodes; jumping backwards or far
    ahead seeks. Frame ``i`` is episode-local.
    """

    def __init__(self, path: str | Path, start_frame: int = 0, length: int | None = None):
        import cv2

        self._cv2 = cv2
        self.path = str(path)
        self.cap = cv2.VideoCapture(self.path)
        if not self.cap.isOpened():
            raise FileNotFoundError(f"Cannot open video {self.path}")
        self.start = int(start_frame)
        total = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        available = max(0, total - self.start) if total else 0
        self.length = int(length) if length else available
        if total and available:
            self.length = min(self.length, available) if self.length else available
        self.width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.next = 0
        if self.start:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, self.start)

    def read(self, index: int) -> np.ndarray | None:
        """Frame ``index`` (episode-local) as RGB, or None past the end."""
        if index < 0 or (self.length and index >= self.length):
            return None
        if index < self.next or index > self.next + 15:
            self.cap.set(self._cv2.CAP_PROP_POS_FRAMES, self.start + index)
            self.next = index
        while self.next < index:
            if not self.cap.grab():
                return None
            self.next += 1
        ok, frame = self.cap.read()
        if not ok:
            return None
        self.next = index + 1
        return self._cv2.cvtColor(frame, self._cv2.COLOR_BGR2RGB)

    def frames(self, start: int = 0, stop: int | None = None, step: int = 1) -> Iterator[tuple[int, np.ndarray]]:
        stop = self.length if stop is None else min(stop, self.length or stop)
        for index in range(start, stop, step):
            frame = self.read(index)
            if frame is None:
                return
            yield index, frame

    def close(self) -> None:
        self.cap.release()


# ------------------------------------------------------------------ misc


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    return float(np.percentile(np.asarray(values, dtype=float), q))


class Nvml:
    """Device memory in use (MiB), whole device and this process. Optional."""

    def __init__(self) -> None:
        self.handle = None
        try:
            import pynvml

            pynvml.nvmlInit()
            self._nv = pynvml
            index = int(os.environ.get("CUDA_VISIBLE_DEVICES", "0").split(",")[0] or 0)
            self.handle = pynvml.nvmlDeviceGetHandleByIndex(index)
        except Exception:  # noqa: BLE001 - NVML is telemetry only
            self.handle = None

    def device_used_mib(self) -> float | None:
        if self.handle is None:
            return None
        try:
            return self._nv.nvmlDeviceGetMemoryInfo(self.handle).used / 2**20
        except Exception:  # noqa: BLE001
            return None

    def process_used_mib(self) -> float | None:
        if self.handle is None:
            return None
        try:
            pid = os.getpid()
            for proc in self._nv.nvmlDeviceGetComputeRunningProcesses(self.handle):
                if proc.pid == pid and proc.usedGpuMemory:
                    return proc.usedGpuMemory / 2**20
        except Exception:  # noqa: BLE001
            return None
        return None


class Heartbeat:
    """Touch the progress file every ``period`` s from a background thread,
    for stages that make progress without a natural per-item callback
    (model loading, a training epoch)."""

    def __init__(self, progress: Progress, period: float = 20.0):
        self.progress = progress
        self.period = period
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="seg-heartbeat")

    def _run(self) -> None:
        while not self._stop.wait(self.period):
            self.progress.touch()

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
