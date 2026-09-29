"""Live overlay: follow the player's clock, decode -> infer+track -> publish.

Threads are decoupled so a slow stage never blocks the player:

* one **decoder** per camera turns the player clock into the frame that is
  due now (plus ``lead_frames``), decodes it and drops it into a 2-slot ring;
  frames the decoder could not reach in time are counted as skipped;
* the **inference** thread takes only the newest frame of each camera
  (older ones are dropped and counted), runs the student on all cameras in
  one batch and updates each camera's tracker;
* the **publisher** encodes masks (COCO RLE), writes one JSON line per
  camera frame to stdout and keeps the first result of every frame for the
  sidecar;
* the browser shows, for the frame on screen, that frame's result or the
  newest earlier one within its staleness window, and nothing older.

The clock is fed on stdin (``{"op": "clock", "playing": .., "time": ..,
"rate": ..}``); ``{"op": "stop"}`` ends the session. ``bench`` drives the
same pipeline with a synthetic clock and a simulated display.
"""

from __future__ import annotations

import collections
import json
import math
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import engine as engines
from .common import (
    Nvml,
    VideoReader,
    annotation,
    atomic_json,
    gpu_lock,
    percentile,
    rle_encode,
    write_rows,
)

# ------------------------------------------------------------------ clock


class Clock:
    """Episode-local playback position, updated from the player."""

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self.playing = False
        self.time = 0.0
        self.rate = 1.0
        self.at = time.monotonic()
        self.version = 0
        self.last_message = time.monotonic()
        self.offsets: dict[str, float] = {}

    def set(
        self,
        *,
        playing: bool,
        position: float,
        rate: float = 1.0,
        sent_at: float | None = None,
        cameras: dict[str, float] | None = None,
    ) -> None:
        """``sent_at`` (Unix seconds, when the player read ``position``)
        takes the message's transport delay out of the clock; a value in the
        future or more than 2 s old (clocks on different machines) is ignored."""
        now = time.monotonic()
        delay = 0.0
        if sent_at is not None and math.isfinite(sent_at):
            late = time.time() - float(sent_at)
            if 0.0 <= late <= 2.0:
                delay = late
        with self._cv:
            self.playing = bool(playing)
            self.time = max(0.0, float(position))
            self.rate = float(rate) if rate and math.isfinite(rate) and rate > 0 else 1.0
            self.at = now - delay
            # Per-camera offsets from ``position``: the player's cameras drift
            # apart, and each decoder follows the frame its own video shows.
            self.offsets = {
                str(k): float(v) - self.time
                for k, v in (cameras or {}).items()
                if isinstance(v, (int, float)) and math.isfinite(v) and abs(float(v) - self.time) < 5.0
            }
            self.version += 1
            self.last_message = self.at
            self._cv.notify_all()

    def heartbeat(self) -> None:
        with self._cv:
            self.last_message = time.monotonic()

    def position(self, now: float | None = None, camera: str | None = None) -> float:
        with self._cv:
            offset = self.offsets.get(camera, 0.0) if camera else 0.0
            if not self.playing:
                return max(0.0, self.time + offset)
            return max(0.0, self.time + offset + ((now or time.monotonic()) - self.at) * self.rate)

    def state(self) -> tuple[bool, float, float, int]:
        with self._cv:
            return self.playing, self.rate, self.time, self.version

    def wait_change(self, version: int, timeout: float) -> None:
        with self._cv:
            if self.version == version:
                self._cv.wait(timeout)

    def wake(self) -> None:
        with self._cv:
            self.version += 1
            self._cv.notify_all()


# ------------------------------------------------------------------ ring


@dataclass
class Ring:
    """Latest-N buffer; overwriting the oldest counts as a drop."""

    maxlen: int = 2
    buf: collections.deque = field(default_factory=collections.deque)
    dropped: int = 0

    def put(self, item: Any) -> None:
        if len(self.buf) >= self.maxlen:
            self.buf.popleft()
            self.dropped += 1
        self.buf.append(item)

    def take_latest(self) -> Any:
        if not self.buf:
            return None
        item = self.buf.pop()
        self.dropped += len(self.buf)
        self.buf.clear()
        return item


# ------------------------------------------------------------------ session


@dataclass
class CameraState:
    key: str
    item: dict[str, Any]
    ring: Ring = field(default_factory=Ring)
    decoded: int = 0
    skipped: int = 0
    processed: int = 0
    last_decoded: int = -1
    window: collections.deque = field(default_factory=lambda: collections.deque(maxlen=4096))
    latencies: list[float] = field(default_factory=list)
    ready: dict[int, float] = field(default_factory=dict)


class LiveRunner:
    def __init__(
        self,
        plan: dict[str, Any],
        clock: Clock,
        emit: Any,
        *,
        eng: engines.Engine | None = None,
        keep_rows: bool | None = None,
    ):
        self.plan = plan
        self.clock = clock
        self.emit = emit
        self.cameras = {c["camera_key"]: CameraState(c["camera_key"], c) for c in plan["cameras"]}
        self.fps = float(plan["cameras"][0]["fps"])
        self.length = min(int(c.get("length") or 0) or 10**9 for c in plan["cameras"])
        self.lead = int(plan.get("lead_frames", 1))
        self.episode = int(plan["episode_index"])
        self.source = "fake" if plan.get("provider") == "fake" else "student"
        self.keep_rows = bool(plan.get("save", True)) if keep_rows is None else keep_rows
        self.rows: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.lock = threading.Lock()
        self.work = threading.Condition(self.lock)
        self.post: queue.Queue = queue.Queue(maxsize=256)
        self.stop = threading.Event()
        self.eng = eng
        self.nvml = Nvml()
        self.vram_peak = 0.0
        self.started = time.monotonic()
        self.errors: list[str] = []
        self.inferences = 0
        self.infer_ms: list[float] = []

    # ---------------------------------------------------------- threads

    def _decoder(self, cam: CameraState) -> None:
        item = cam.item
        try:
            reader = VideoReader(item["video"], int(item.get("start_frame", 0)), int(item.get("length") or 0) or None)
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"{cam.key}: {exc}")
            return
        # A jump further than this (forwards) or any step backwards beyond the
        # lead is a seek: the tracker restarts (new ids after the old ones).
        max_gap = 3 * max(1, self.lead) + 2
        try:
            while not self.stop.is_set():
                playing, rate, _, version = self.clock.state()
                now = time.monotonic()
                position = self.clock.position(now, cam.key)
                due = math.floor(position * self.fps + 1e-6)
                target = min(max(0, due + (self.lead if playing else 0)), max(0, reader.length - 1))
                last = cam.last_decoded
                # Pausing lands up to ``lead`` frames behind the newest decoded
                # frame, which was already processed: nothing to do.
                behind_by_lead = last >= 0 and last - (self.lead + 1) <= target < last
                if target != last and not (not playing and behind_by_lead):
                    reset = last >= 0 and (target < last - (self.lead + 1) or target > last + max_gap)
                    if not reset and last >= 0 and target > last + 1:
                        cam.skipped += target - last - 1
                    rgb = reader.read(target)
                    if rgb is None:
                        self.clock.wait_change(version, 0.05)
                        continue
                    cam.decoded += 1
                    cam.last_decoded = target
                    with self.work:
                        cam.ring.put((target, rgb, time.monotonic(), reset))
                        self.work.notify_all()
                if not playing:
                    self.clock.wait_change(version, 0.25)
                    continue
                # Sleep until the next frame becomes due (clock may change meanwhile).
                next_due = (math.floor(position * self.fps + 1e-6) + 1) / self.fps
                wait = (next_due - position) / max(rate, 1e-3)
                self.clock.wait_change(version, max(0.001, min(wait, 0.25)))
        finally:
            reader.close()

    def _infer(self) -> None:
        while not self.stop.is_set():
            with self.work:
                batch = []
                while not self.stop.is_set():
                    batch = [(cam, got) for cam in self.cameras.values() if (got := cam.ring.take_latest()) is not None]
                    if batch:
                        break
                    self.work.wait(0.1)
            if not batch:
                continue
            for cam, (_, _, _, reset) in batch:
                if reset:
                    self.eng.reset(cam.key)
            t0 = time.monotonic()
            try:
                results = self.eng.process([(cam.key, index, rgb) for cam, (index, rgb, _, _) in batch])
            except Exception as exc:  # noqa: BLE001
                self.errors.append(str(exc))
                self.emit({"type": "error", "message": f"inference failed: {exc}"})
                self.stop.set()
                return
            t1 = time.monotonic()
            self.inferences += 1
            self.infer_ms.append((t1 - t0) * 1000)
            for (cam, (index, _, captured, _)), instances in zip(batch, results, strict=True):
                cam.processed += 1
                cam.window.append(t1)
                try:
                    self.post.put((cam, index, captured, instances), timeout=1.0)
                except queue.Full:
                    pass
            used = self.nvml.process_used_mib() or 0.0
            self.vram_peak = max(self.vram_peak, used)

    def _publish(self) -> None:
        while not (self.stop.is_set() and self.post.empty()):
            try:
                cam, index, captured, instances = self.post.get(timeout=0.1)
            except queue.Empty:
                continue
            objects = []
            rows = []
            for inst in instances:
                rle = rle_encode(inst["mask"])
                objects.append(
                    {
                        "track_id": inst["track_id"],
                        "concept": inst["concept"],
                        "score": round(float(inst["score"]), 4),
                        "bbox_xyxy": [round(float(v), 1) for v in inst["bbox"]],
                        "mask_rle": rle,
                    }
                )
                if self.keep_rows and (cam.key, index) not in self.rows:
                    row = annotation(
                        episode_index=self.episode,
                        frame_index=index,
                        fps=self.fps,
                        camera_key=cam.key,
                        object_prefix=self.source,
                        track_id=inst["track_id"],
                        concept=inst["concept"],
                        score=inst["score"],
                        mask=inst["mask"],
                        source=self.source,
                        bbox=inst["bbox"],
                    )
                    if row:
                        rows.append(row)
            if self.keep_rows and (cam.key, index) not in self.rows:
                self.rows[(cam.key, index)] = rows
            ready = time.monotonic()
            latency = (ready - captured) * 1000
            cam.latencies.append(latency)
            cam.ready.setdefault(index, ready)
            self.emit(
                {
                    "type": "result",
                    "camera_key": cam.key,
                    "frame_index": index,
                    "timestamp": index / self.fps,
                    "latency_ms": round(latency, 2),
                    "image_size": list(instances[0]["mask"].shape) if instances else None,
                    "objects": objects,
                }
            )

    def stats(self) -> dict[str, Any]:
        now = time.monotonic()
        cameras = {}
        for cam in self.cameras.values():
            recent = [t for t in cam.window if now - t <= 2.0]
            cameras[cam.key] = {
                "fps": round(len(recent) / 2.0, 2),
                "decoded": cam.decoded,
                "processed": cam.processed,
                "skipped": cam.skipped,
                "dropped": cam.ring.dropped,
                "latency_ms_p50": percentile(cam.latencies[-300:], 50),
                "latency_ms_p95": percentile(cam.latencies[-300:], 95),
                "last_frame": cam.last_decoded,
            }
        return {
            "type": "stats",
            "uptime_s": round(now - self.started, 2),
            "cameras": cameras,
            "infer_ms_p50": percentile(self.infer_ms[-300:], 50),
            "vram_process_mib": self.nvml.process_used_mib(),
            "vram_peak_mib": self.vram_peak or None,
        }

    def summary(self) -> dict[str, Any]:
        cams = {}
        for cam in self.cameras.values():
            cams[cam.key] = {
                "decoded": cam.decoded,
                "processed": cam.processed,
                "skipped": cam.skipped,
                "dropped": cam.ring.dropped,
                "latency_ms_p50": percentile(cam.latencies, 50),
                "latency_ms_p95": percentile(cam.latencies, 95),
                "latency_ms_max": max(cam.latencies) if cam.latencies else None,
            }
        return {
            "cameras": cams,
            "inferences": self.inferences,
            "infer_ms_p50": percentile(self.infer_ms, 50),
            "infer_ms_p95": percentile(self.infer_ms, 95),
            "vram_peak_mib": self.vram_peak or None,
            "errors": self.errors,
            "saved_frames": len(self.rows),
        }

    # ---------------------------------------------------------- control

    def start(self) -> list[threading.Thread]:
        threads = [threading.Thread(target=self._decoder, args=(cam,), daemon=True, name=f"decode-{cam.key}") for cam in self.cameras.values()]
        threads.append(threading.Thread(target=self._infer, daemon=True, name="infer"))
        threads.append(threading.Thread(target=self._publish, daemon=True, name="publish"))
        for thread in threads:
            thread.start()
        self.threads = threads
        return threads

    def shutdown(self) -> None:
        self.stop.set()
        self.clock.wake()
        with self.work:
            self.work.notify_all()
        for thread in getattr(self, "threads", []):
            thread.join(timeout=5)

    def save(self, output_dir: Path) -> list[dict[str, Any]]:
        files = []
        by_camera: dict[str, list[dict[str, Any]]] = {}
        frames: dict[str, int] = {}
        for (camera, _), rows in self.rows.items():
            by_camera.setdefault(camera, []).extend(rows)
            frames[camera] = frames.get(camera, 0) + 1
        for camera, rows in by_camera.items():
            path = output_dir / f"episode-{self.episode:06d}--{camera.replace('/', '_')}.parquet"
            write_rows(rows, path)
            files.append({"episode_index": self.episode, "camera_key": camera, "path": str(path), "rows": len(rows), "frames": frames[camera]})
        return files


def warm_up(eng: engines.Engine, plan: dict[str, Any]) -> None:
    """Pay compilation/cuDNN autotuning before the first real frame."""
    import numpy as np

    items = []
    for cam in plan["cameras"]:
        try:
            reader = VideoReader(cam["video"], int(cam.get("start_frame", 0)), int(cam.get("length") or 0) or None)
            rgb = reader.read(0)
            reader.close()
        except Exception:  # noqa: BLE001
            rgb = None
        items.append((cam["camera_key"], 0, rgb if rgb is not None else np.zeros((480, 640, 3), np.uint8)))
    for _ in range(3):
        eng.process(items)
    for cam in plan["cameras"]:
        eng.reset(cam["camera_key"])


# ------------------------------------------------------------------ serve


def serve(plan_path: Path) -> int:
    """Live session over stdin/stdout (spawned by LEVI's core)."""
    plan = json.loads(plan_path.read_text())
    out_lock = threading.Lock()
    stdout = sys.stdout

    def emit(message: dict[str, Any]) -> None:
        line = json.dumps(message, separators=(",", ":"))
        with out_lock:
            try:
                stdout.write(line + "\n")
                stdout.flush()
            except (BrokenPipeError, ValueError):
                pass

    clock = Clock()
    idle = float(plan.get("idle_seconds", 120))
    started = time.monotonic()
    try:
        with gpu_lock(None, wait=False):
            cameras = [c["camera_key"] for c in plan["cameras"]]
            eng = engines.build(plan, cameras, float(plan["cameras"][0]["fps"]))
            warm_up(eng, plan)
            runner = LiveRunner(plan, clock, emit, eng=eng)
            emit(
                {
                    "type": "ready",
                    "engine": eng.describe(),
                    "fps": runner.fps,
                    "frames": runner.length,
                    "cameras": cameras,
                    "load_s": round(time.monotonic() - started, 3),
                    "pid": os.getpid(),
                }
            )
            runner.start()

            def stats_loop() -> None:
                while not runner.stop.wait(1.0):
                    emit(runner.stats())
                    if time.monotonic() - clock.last_message > idle:
                        emit({"type": "idle", "message": f"no player clock for {idle:.0f} s"})
                        runner.stop.set()
                        clock.wake()

            threading.Thread(target=stats_loop, daemon=True, name="stats").start()
            reason = "stop"
            for line in sys.stdin:
                if runner.stop.is_set():
                    reason = "idle" if not runner.errors else "error"
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                op = message.get("op")
                if op == "clock":
                    clock.set(
                        playing=bool(message.get("playing")),
                        position=float(message.get("time", 0.0)),
                        rate=float(message.get("rate", 1.0)),
                        sent_at=message.get("sent_at"),
                        cameras=message.get("cameras") if isinstance(message.get("cameras"), dict) else None,
                    )
                elif op == "ping":
                    clock.heartbeat()
                elif op == "stop":
                    break
            else:
                reason = "stdin closed"
            runner.shutdown()
            files = runner.save(Path(plan["output_dir"])) if plan.get("save", True) else []
            result = {
                "status": "succeeded",
                "reason": reason,
                "files": files,
                "engine": eng.describe(),
                "summary": runner.summary(),
            }
            atomic_json(Path(plan["result_path"]), result)
            emit({"type": "stopped", "reason": reason, "files": len(files), "summary": result["summary"]})
            return 0
    except Exception as exc:  # noqa: BLE001
        emit({"type": "error", "message": str(exc)})
        atomic_json(Path(plan["result_path"]), {"status": "failed", "error": str(exc)})
        return 1


# ------------------------------------------------------------------ bench


def bench(plan_path: Path, output: Path) -> int:
    """Real-time simulation over several episodes: both cameras at once,
    playback at ``rate`` x the video frame rate, a simulated display at the
    playback frame rate that shows the newest result not older than
    ``stale_frames``. Nothing is saved to a sidecar."""
    import psutil

    plan = json.loads(plan_path.read_text())
    rate = float(plan.get("rate", 3.0))
    stale = int(plan.get("stale_frames", 2))
    reports = []
    process = psutil.Process()
    with gpu_lock(None):
        first = plan["episodes"][0]
        cameras = [c["camera_key"] for c in first["cameras"]]
        t0 = time.monotonic()
        eng = engines.build({**plan, **first}, cameras, float(first["cameras"][0]["fps"]))
        load_s = time.monotonic() - t0
        import torch

        for episode in plan["episodes"]:
            sub = {**plan, **episode, "save": False}
            warm_up(eng, sub)
            if torch.cuda.is_available():
                torch.cuda.reset_peak_memory_stats()
            clock = Clock()
            runner = LiveRunner(sub, clock, lambda m: None, eng=eng, keep_rows=False)
            process.cpu_percent(None)
            cpu = []
            display = {"ticks": 0, "exact": 0, "stale_shown": 0, "empty": 0}
            clock.set(playing=True, position=0.0, rate=rate)
            wall0 = time.monotonic()
            runner.start()
            duration = runner.length / runner.fps / rate
            period = 1.0 / (runner.fps * rate)
            tick = wall0
            while True:
                tick += period
                time.sleep(max(0.0, tick - time.monotonic()))
                now = time.monotonic()
                if now - wall0 > duration + 0.2:
                    break
                shown = math.floor(clock.position(now) * runner.fps + 1e-6)
                if shown >= runner.length:
                    break
                display["ticks"] += 1
                for cam in runner.cameras.values():
                    ok = cam.ready.get(shown)
                    if ok is not None and ok <= now:
                        display["exact"] += 1
                        continue
                    older = [f for f in range(shown - stale, shown) if (t := cam.ready.get(f)) is not None and t <= now]
                    if older:
                        display["stale_shown"] += 1
                    else:
                        display["empty"] += 1
                if display["ticks"] % 10 == 0:
                    cpu.append(process.cpu_percent(None))
            wall = time.monotonic() - wall0
            runner.shutdown()
            summary = runner.summary()
            n_cam = len(runner.cameras)
            reports.append(
                {
                    "episode_index": episode["episode_index"],
                    "frames": runner.length,
                    "cameras": n_cam,
                    "rate": rate,
                    "source_fps": runner.fps * rate,
                    "wall_s": round(wall, 3),
                    "sustained_fps_per_camera": {
                        k: round(v["processed"] / wall, 2) for k, v in summary["cameras"].items()
                    },
                    "processed_fraction": {
                        k: round(v["processed"] / runner.length, 4) for k, v in summary["cameras"].items()
                    },
                    "display": {
                        "ticks": display["ticks"],
                        "exact_fraction": round(display["exact"] / max(1, display["ticks"] * n_cam), 4),
                        "stale_fraction": round(display["stale_shown"] / max(1, display["ticks"] * n_cam), 4),
                        "empty_fraction": round(display["empty"] / max(1, display["ticks"] * n_cam), 4),
                    },
                    "cpu_percent_mean": round(sum(cpu) / len(cpu), 1) if cpu else None,
                    "torch_peak_allocated_mib": round(torch.cuda.max_memory_allocated() / 2**20, 1)
                    if torch.cuda.is_available()
                    else None,
                    **{k: v for k, v in summary.items() if k != "cameras"},
                    "per_camera": summary["cameras"],
                }
            )
            print(json.dumps(reports[-1]), flush=True)
    lat = [c for r in reports for c in r["per_camera"].values()]
    total = {
        "episodes": len(reports),
        "engine": eng.describe(),
        "load_s": round(load_s, 2),
        "rate": rate,
        "stale_frames": stale,
        "min_processed_fraction": min(min(r["processed_fraction"].values()) for r in reports),
        "latency_ms_p50_median": percentile([c["latency_ms_p50"] for c in lat if c["latency_ms_p50"] is not None], 50),
        "latency_ms_p95_max": max((c["latency_ms_p95"] for c in lat if c["latency_ms_p95"] is not None), default=None),
        "vram_peak_mib": max((r["vram_peak_mib"] or 0) for r in reports),
        "exact_fraction_mean": round(sum(r["display"]["exact_fraction"] for r in reports) / len(reports), 4),
        "reports": reports,
    }
    atomic_json(output, total)
    return 0
