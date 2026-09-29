"""Live overlay sessions: one student worker following one player.

The browser owns the clock. It posts play / pause / seek / rate changes (and a
heartbeat while playing) to the session; the worker decodes the frame that is
due, runs the student and tracker and writes one JSON line per camera frame.
This module relays those lines to the page as server-sent events. A slow page
only ever receives the newest result of each camera (older ones are dropped
here, as the page drops stale frames on its side), so the relay can never
hold back playback.

When a session stops (the page or an idle timeout), the worker writes the
first result of every processed frame as sidecar rows; the backend
publishes them as one revision that replaces only this episode's cameras,
so they appear in Objects & Tracking and in exports. A service shutdown
stops sessions without saving (``stop_all``).
"""

from __future__ import annotations

import collections
import contextlib
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .. import catalog, children, naming, paths
from . import jobs, models

MAX_EVENTS = 600
_SESSIONS: dict[str, LiveSession] = {}
_LOCK = threading.RLock()


def _max_sessions() -> int:
    return max(1, int(os.getenv("LEVI_SEG_LIVE_MAX_SESSIONS", "2")))


def _idle_seconds() -> float:
    return float(os.getenv("LEVI_SEG_LIVE_IDLE_SECONDS", "120"))


class LiveSession:
    def __init__(self, session_id: str, dataset: str, plan: dict[str, Any], folder: Path, provider: str):
        self.id = session_id
        self.dataset = dataset
        self.plan = plan
        self.folder = folder
        self.provider = provider
        self.episode = int(plan["episode_index"])
        self.cameras = [c["camera_key"] for c in plan["cameras"]]
        self.model = plan["model"].get("name")
        self.save = bool(plan.get("save", True))
        self.state = "starting"
        self.error: str | None = None
        self.ready: dict[str, Any] | None = None
        self.stats: dict[str, Any] | None = None
        self.summary: dict[str, Any] | None = None
        self.revision_id: str | None = None
        self.annotation_count: int | None = None
        self.created_at = time.time()
        self.stopped_at: float | None = None
        self.seq = 0
        self.events: collections.deque[tuple[int, str, str | None, str]] = collections.deque(maxlen=MAX_EVENTS)
        self.cv = threading.Condition()
        self.process: subprocess.Popen | None = None
        self._stdin_lock = threading.Lock()
        self.published = False

    # ---------------------------------------------------------- process io

    def start(self, env: dict[str, str]) -> None:
        plan_path = self.folder / "plan.json"
        catalog.atomic(plan_path, self.plan)
        log = (self.folder / "worker.log").open("ab")
        try:
            self.process = subprocess.Popen(
                [*jobs.command(self.provider), "live", "--plan", str(plan_path)],
                cwd=jobs.WORKER_PROJECT,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=log,
                start_new_session=(os.name == "posix"),
                bufsize=0,
            )
        finally:
            log.close()
        children.track(self.process, "segmentation-live", self.id)
        threading.Thread(target=self._read, name=f"seg-live-{self.id}", daemon=True).start()

    def _push(self, kind: str, camera: str | None, raw: str) -> None:
        with self.cv:
            self.seq += 1
            self.events.append((self.seq, kind, camera, raw))
            self.cv.notify_all()

    def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for raw in iter(self.process.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            kind = message.get("type")
            if kind == "result":
                self._push("result", message.get("camera_key"), line)
                continue
            if kind == "ready":
                self.ready = message
                self.state = "running"
            elif kind == "stats":
                self.stats = message
            elif kind == "error":
                self.error = message.get("message")
            elif kind == "idle":
                # The worker waits for its stdin to end; tell it to save and exit.
                self.state = "stopping"
                threading.Thread(target=self.send, args=({"op": "stop"},), daemon=True).start()
            elif kind == "stopped":
                self.summary = message.get("summary")
            self._push(kind or "message", None, line)
        code = self.process.wait()
        children.untrack(self.process.pid)
        if self.state != "failed":
            self.state = "stopped" if code == 0 else "failed"
        if code != 0 and not self.error:
            self.error = f"live worker exited with code {code}"
        self.stopped_at = time.time()
        self._push("closed", None, json.dumps({"type": "closed", "state": self.state, "error": self.error}))

    def send(self, message: dict[str, Any]) -> bool:
        process = self.process
        if process is None or process.poll() is not None or process.stdin is None:
            return False
        data = (json.dumps(message) + "\n").encode()
        with self._stdin_lock:
            try:
                process.stdin.write(data)
                process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                return False
        return True

    def stop(self, timeout: float = 30.0) -> None:
        """Ask the worker to save and exit; force it after ``timeout``."""
        process = self.process
        if process is None:
            return
        if process.poll() is None:
            self.state = "stopping"
            self.send({"op": "stop"})
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5)
                if process.poll() is None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        with contextlib.suppress(OSError, ValueError):
            if process.stdin:
                process.stdin.close()
        # The reader thread records the final state; give it a moment.
        deadline = time.time() + 5
        while self.stopped_at is None and time.time() < deadline:
            time.sleep(0.05)

    # ---------------------------------------------------------- relay

    def stream(self, after: int = 0, keepalive: float = 10.0) -> Iterator[str]:
        """SSE frames after ``after``. A subscriber that fell behind gets only
        the newest result per camera: stale frames are dropped, not queued."""
        last = after
        while True:
            with self.cv:
                if self.seq <= last:
                    self.cv.wait(keepalive)
                pending = [e for e in self.events if e[0] > last]
            if not pending:
                if self.stopped_at is not None and self.seq <= last:
                    return
                yield ": keepalive\n\n"
                continue
            newest: dict[str | None, int] = {}
            for seq, kind, camera, _ in pending:
                if kind == "result":
                    newest[camera] = seq
            for seq, kind, camera, raw in pending:
                if kind == "result" and newest.get(camera) != seq:
                    continue
                yield f"id: {seq}\nevent: {kind}\ndata: {raw}\n\n"
                if kind == "closed":
                    return
            last = pending[-1][0]

    def result(self) -> dict[str, Any] | None:
        return catalog.read(Path(self.plan["result_path"]), None)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "dataset": self.dataset,
            "episode_index": self.episode,
            "cameras": self.cameras,
            "model": self.model,
            "provider": self.provider,
            "save": self.save,
            "state": self.state,
            "error": self.error,
            "ready": self.ready,
            "stats": self.stats,
            "summary": self.summary,
            "revision_id": self.revision_id,
            "annotation_count": self.annotation_count,
            "created_at": self.created_at,
            "stopped_at": self.stopped_at,
        }


# ---------------------------------------------------------------- registry


def get(session_id: str) -> LiveSession:
    with _LOCK:
        session = _SESSIONS.get(session_id)
    if session is None:
        raise jobs.SegError(404, "Live segmentation session not found")
    return session


def sessions(dataset: str | None = None) -> list[LiveSession]:
    with _LOCK:
        rows = list(_SESSIONS.values())
    return [s for s in rows if dataset is None or s.dataset == dataset]


def start(
    dataset: str,
    *,
    episode_videos: list[dict[str, Any]],
    episode_index: int,
    model: str | None,
    provider: str = "student",
    save: bool = True,
    lead_frames: int = 1,
    concepts: list[str] | None = None,
) -> LiveSession:
    if provider not in ("student", "fake"):
        raise jobs.SegError(400, "provider is student or fake")
    if provider == "student":
        if not model:
            raise jobs.SegError(400, "Choose a student model")
        worker = jobs.worker_state()
        if not worker["ready"]:
            raise jobs.SegError(503, "Fast segmentation worker is not ready: " + worker["reason"])
        try:
            spec = models.worker_spec(model)
        except KeyError as exc:
            raise jobs.SegError(404, str(exc.args[0])) from exc
        except ValueError as exc:
            raise jobs.SegError(400, str(exc)) from exc
        jobs.check_gpu("live")
    else:
        spec = {"name": model or "fake", "provider": "fake", "concepts": concepts or ["robot arm", "cup"]}
    with _LOCK:
        running = [s for s in _SESSIONS.values() if s.stopped_at is None]
        # One live view per episode: a second start replaces the first.
        for other in running:
            if other.dataset == dataset and other.episode == episode_index:
                other.stop()
        running = [s for s in _SESSIONS.values() if s.stopped_at is None]
        if len(running) >= _max_sessions():
            raise jobs.SegError(
                409,
                f"{len(running)} live segmentation session(s) already running "
                "(LEVI_SEG_LIVE_MAX_SESSIONS); stop one first",
            )
        base = jobs.root(dataset) / "live"
        session_id = naming.timestamp_id(base, create_dir=True)
        folder = base / session_id
        plan = {
            "schema": "levi.segmentation.live.v1",
            "session_id": session_id,
            "provider": provider,
            "model": spec,
            "episode_index": int(episode_index),
            "cameras": episode_videos,
            "save": bool(save),
            "lead_frames": max(0, int(lead_frames)),
            "idle_seconds": _idle_seconds(),
            "output_dir": str(folder / "rows"),
            "result_path": str(folder / "result.json"),
        }
        session = LiveSession(session_id, dataset, plan, folder, provider)
        session.start(jobs._env())
        _SESSIONS[session_id] = session
    return session


def wait_ready(session: LiveSession, timeout: float = 120.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if session.state in ("running", "stopped", "failed"):
            return
        time.sleep(0.05)


def _files(session: LiveSession) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    result = session.result() or {}
    return result, list(result.get("files") or [])


def awaiting_publish(session: LiveSession) -> bool:
    """Stopped, not yet finished, and has rows to save into the sidecar."""
    if session.stopped_at is None or session.published or not session.save:
        return False
    return bool(_files(session)[1])


def finish(session: LiveSession, publish: jobs.Publisher | None) -> LiveSession:
    """Finish a stopped session once: publish its rows (needs ``publish``;
    without it a session with rows to save waits for a caller that has one)
    and drop the rows folder. No-op until the session has stopped."""
    if session.stopped_at is None or session.published:
        return session
    result, files = _files(session)
    wanted = session.save and bool(files)
    if wanted and publish is None:
        return session
    session.published = True
    if wanted:
        assert publish is not None
        rows: list[dict[str, Any]] = []
        pairs: set[tuple[int, str]] = set()
        for item in files:
            pairs.add((int(item["episode_index"]), str(item["camera_key"])))
            rows.extend(jobs._read_rows(Path(item["path"])))
        engine = result.get("engine") or {}
        try:
            revision = publish(
                rows,
                pairs,
                {
                    "provider": "student" if session.provider != "fake" else "fake",
                    "model_version": engine.get("model") or session.model,
                    "precision": engine.get("precision"),
                    "live_session": session.id,
                },
            )
            session.revision_id = revision["revision_id"]
            session.annotation_count = len(rows)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            session.error = f"Saving the live results failed: {exc}"
    if result.get("summary"):
        session.summary = result["summary"]
    # Rows are in the sidecar now (or were not wanted); the worker folder
    # keeps only its plan, log and result summary.
    shutil.rmtree(session.folder / "rows", ignore_errors=True)
    return session


def stop_all(timeout: float = 10.0) -> None:
    """Service shutdown: stop every session and discard what it had not
    saved yet. Publishing needs the dataset's annotation transaction, which
    a shutting-down service does not open, and sessions live only in this
    process, so rows left on disk would never be published. The live view
    saves on stop from the page or on idle; a shutdown is neither."""
    for session in sessions():
        if session.stopped_at is None:
            session.stop(timeout)
        if not session.published:
            session.published = True
            shutil.rmtree(session.folder / "rows", ignore_errors=True)
    discard_orphans()


def discard_orphans() -> None:
    """Remove ``rows/`` folders of sessions this process does not know (a
    service that was killed before it could clean up)."""
    known = {s.folder.resolve() for s in sessions()}
    base = live_root()
    if not base.is_dir():
        return
    for rows in base.glob("*/live/*/rows"):
        if rows.parent.resolve() not in known:
            shutil.rmtree(rows, ignore_errors=True)


def forget_finished(max_age: float = 3600.0) -> None:
    now = time.time()
    with _LOCK:
        for key, session in list(_SESSIONS.items()):
            if session.stopped_at and session.published and now - session.stopped_at > max_age:
                _SESSIONS.pop(key, None)


def live_root() -> Path:
    return paths.STATE / "segmentation"
