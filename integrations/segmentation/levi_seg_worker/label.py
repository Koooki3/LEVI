"""Offline labelling: every frame of the planned (episode, camera) items.

Decoding runs in a thread ahead of inference; frames are predicted in
batches and tracked in order, so ids stay continuous within an item. Each
item is written as one parquet in LEVI's mask schema; LEVI publishes them as
one sidecar revision after the job succeeds.
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Any

from . import engine as engines
from .common import (
    Heartbeat,
    Progress,
    VideoReader,
    annotation,
    atomic_json,
    gpu_lock,
    write_rows,
)


def _decode(reader: VideoReader, out: queue.Queue, stop: threading.Event) -> None:
    try:
        for index, frame in reader.frames():
            while not stop.is_set():
                try:
                    out.put((index, frame), timeout=0.5)
                    break
                except queue.Full:
                    continue
            if stop.is_set():
                return
    finally:
        out.put(None)


def label_item(eng: engines.Engine, item: dict[str, Any], batch: int, source: str) -> tuple[list[dict[str, Any]], int]:
    camera = item["camera_key"]
    fps = float(item["fps"])
    reader = VideoReader(item["video"], int(item.get("start_frame", 0)), int(item.get("length") or 0) or None)
    eng.reset(camera)
    frames: queue.Queue = queue.Queue(maxsize=64)
    stop = threading.Event()
    thread = threading.Thread(target=_decode, args=(reader, frames, stop), daemon=True)
    thread.start()
    rows: list[dict[str, Any]] = []
    count = 0
    pending: list[tuple[int, Any]] = []

    def flush() -> None:
        nonlocal count
        results = eng.process([(camera, index, rgb) for index, rgb in pending])
        for (index, _), instances in zip(pending, results, strict=True):
            for inst in instances:
                row = annotation(
                    episode_index=int(item["episode_index"]),
                    frame_index=index,
                    fps=fps,
                    camera_key=camera,
                    object_prefix=source,
                    track_id=inst["track_id"],
                    concept=inst["concept"],
                    score=inst["score"],
                    mask=inst["mask"],
                    source=source,
                    bbox=inst.get("bbox"),
                )
                if row:
                    rows.append(row)
        count += len(pending)
        pending.clear()

    try:
        while True:
            got = frames.get()
            if got is None:
                break
            pending.append(got)
            if len(pending) >= batch:
                flush()
        if pending:
            flush()
    finally:
        stop.set()
        thread.join(timeout=5)
        reader.close()
    return rows, count


def run(plan_path: Path, output: Path, progress_path: Path | None) -> int:
    import json

    plan = json.loads(plan_path.read_text())
    progress = Progress(progress_path)
    items = plan["items"]
    total_frames = int(sum(int(i.get("length") or 0) for i in items))
    progress.set("loading", 0, total_frames)
    out_dir = Path(plan["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    source = "student" if plan.get("provider") != "fake" else "fake"
    files: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    started = time.time()
    frames_done = 0
    with gpu_lock(progress):
        cameras = sorted({i["camera_key"] for i in items})
        fps = float(items[0]["fps"]) if items else 10.0
        batch = int(plan.get("batch", 8))
        with Heartbeat(progress):
            eng = engines.build(plan, cameras, fps, batch=batch)
        load_s = time.time() - started
        progress.set("labelling", 0, total_frames)
        infer_s = 0.0
        for number, item in enumerate(items):
            try:
                t0 = time.time()
                rows, count = label_item(eng, item, batch, source)
                infer_s += time.time() - t0
                name = f"episode-{int(item['episode_index']):06d}--{item['camera_key'].replace('/', '_')}.parquet"
                write_rows(rows, out_dir / name)
                files.append(
                    {
                        "episode_index": int(item["episode_index"]),
                        "camera_key": item["camera_key"],
                        "path": str(out_dir / name),
                        "rows": len(rows),
                        "frames": count,
                    }
                )
                frames_done += count
            except Exception as exc:  # noqa: BLE001 - one bad item must not sink the batch
                errors.append(
                    {"episode_index": item.get("episode_index"), "camera_key": item.get("camera_key"), "error": str(exc)}
                )
            progress.set(
                "labelling",
                frames_done,
                total_frames,
                items_done=number + 1,
                items_total=len(items),
                current_episode=item.get("episode_index"),
                current_camera=item.get("camera_key"),
            )
    seconds = time.time() - started
    result: dict[str, Any] = {
        "status": "succeeded" if files else "failed",
        "files": files,
        "item_errors": errors,
        "engine": eng.describe(),
        "timing": {
            "frames": frames_done,
            "seconds": round(seconds, 3),
            "load_seconds": round(load_s, 3),
            "fps": round(frames_done / infer_s, 2) if infer_s else None,
        },
    }
    if not files:
        first = errors[0] if errors else {"error": "no items"}
        result["error"] = f"All {len(errors)} item(s) failed; first: {first.get('error')}"
    atomic_json(output, result)
    return 0 if files else 1
