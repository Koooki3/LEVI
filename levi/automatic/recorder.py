"""The recorder compatibility layer (T-C-10): AERI episodes written as the
rollout folders and session files the live service already reads
(interfaces C1 and C2), plus the run manifest that ties forward and reset
rollouts together (pipeline §9.3, §9.5).

**Rollouts (C1).** One folder per episode,
``<root>/<group>/<task_folder>/demo_NNNN`` with ``NNNN`` the episode number
(``<run_id>.<role>.<NNNN>``); forward and reset episodes go to their own task
folders. Sealing keeps the live service's rule (``levi.live.criteria``):

1. the steps file is flushed and synced, the media sink finishes;
2. ``events.csv`` gets its ``episode_end`` row, ``metadata.json`` its
   ``stopped_at``, the media facts (``media_storage.video_frames_match_csv``,
   ``cameras.stall_detection.stalled``), each written whole and synced;
3. the folder is checked (both files read back, no ``*_raw.avi``);
4. **``.complete`` is created last**, atomically (temporary file, fsync,
   rename, fsync of the folder).

Any write that fails raises ``RecorderError`` before step 4: no marker is
ever left on a rollout that is not whole. ``abort`` removes a marker if one
is there and renames the folder ``incomplete_NNNN`` (``eval.abort_reason``
in its metadata when it can still be written). Sealing an episode twice
returns the first result.

The automatic verdict is never written as an operator label:
``eval.outcome`` stays ``unlabeled``, ``eval.verdict_by`` is ``aeri``,
there is no ``eval.agent_label``, and ``eval.aeri`` names the run and the
episode (design X1 §8 item 2: the verdict lives in the run journal only).

**Run manifest.** ``<run_dir>/manifest.json`` (``levi.aeri.manifest.v1``)
lists every episode the run opened, its folder and its state, and links a
reset to the forward episode before it (``after_forward``) and a forward
episode to the resets since the previous one (``after_resets``). It is a
derived view written durably after each change; the journal stays the
source of truth. An entry is written *before* its folder is created, so a
crash never leaves a folder the manifest does not know.

**After a restart** ``recover(sealed)`` renames to ``incomplete_*`` every
rollout of this run whose seal the journal did not commit, a marker
included (a crash between the marker and the commit: the journal records
the rollout as incomplete, so the folder must not stay usable). Folders of
other runs are never touched.

**Sessions (C2).** ``SessionFiles`` writes one session file per role
(``<root>/.eval_sessions/<group>__<task_folder>.json``) with the client's
seven states only (``adapters.legacy_live``), ISO times, ``episode_role``
and ``aeri{run_id, state, control_epoch}``. The reset file says
``levi.enabled: false`` by default; exclude the reset folder from the live
service with ``watch.exclude`` so the forward spec never labels a reset.

**Media.** A ``MediaSink`` writes the capture format (videos, pose and
gripper CSVs) and reports its facts; the default ``NullMedia`` writes no
video (an honest ``video_frames_match_csv: true`` with no videos). The
AERI layer itself writes ``aeri_steps.csv`` (one row per committed step)
and marks a camera stalled when its frame did not change for
``stall_limit`` observations.
"""

import csv
import io
import json
import os
import re
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from levi.domain import aeri

from .adapters import legacy_live
from .journal import fsync_dir

MANIFEST = "manifest.json"
MANIFEST_SCHEMA = "levi.aeri.manifest.v1"
SESSION_SCHEMA = "levi.eval.session.v1"
SESSION_DIR = ".eval_sessions"
MARKER = ".complete"
STEPS = "aeri_steps.csv"
_NUMBERED = re.compile(r"^(demo|incomplete|discarded)_(\d+)")


class RecorderError(Exception):
    pass


def iso(stamp: float) -> str:
    """Seconds since the epoch as the evaluation client writes them."""
    return datetime.fromtimestamp(stamp).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


# --- media -----------------------------------------------------------------------------------


class MediaSink(Protocol):
    def open(self, folder: Path) -> None: ...
    def frame(self, folder: Path, step: int, obs: dict) -> None: ...
    def finish(self, folder: Path, frames: int) -> dict:
        """``{"video_frames_match_csv": bool, "stalled": [...], "videos": [...]}``"""
        ...

    def abort(self, folder: Path) -> None: ...


class NullMedia:
    """No video: nothing can disagree with the CSV, nothing stalls."""

    def open(self, folder):
        pass

    def frame(self, folder, step, obs):
        pass

    def finish(self, folder, frames):
        return {"video_frames_match_csv": True, "stalled": [], "videos": []}

    def abort(self, folder):
        pass


# --- the rollout ---------------------------------------------------------------------------------


@dataclass
class Rollout:
    task_folder: str
    demo: str
    episode_id: str
    role: str
    number: int
    path: Path
    state: str = "open"  # open | complete | incomplete
    steps: int = 0
    staged: dict | None = None
    sealed: dict | None = None
    errors: list = field(default_factory=list)
    started_wall: float = 0.0
    handle: object = field(default=None, repr=False)
    # Per camera: (last frame value, observations it has not changed).
    frames: dict = field(default_factory=dict, repr=False)
    stalled: set = field(default_factory=set)

    @property
    def complete_marker(self) -> bool:
        return (self.path / MARKER).exists()


def sealed_episodes(events) -> set:
    """Episodes whose ``recorder_seal`` transaction the journal committed."""
    committed = {e.transaction_id for e in events if e.record == "committed"}
    return {
        e.episode_id
        for e in events
        if e.record == "prepared"
        and e.action is not None
        and e.action.kind == "recorder_seal"
        and e.transaction_id in committed
    }


class RolloutRecorder:
    """Writes AERI episodes as live-compatible rollout folders.

    ``io_hook(op, path)`` is called before every write, rename and marker
    (tests raise ``OSError`` there to stand in for a full disk)."""

    def __init__(
        self,
        root,
        *,
        run_id: str,
        run_dir,
        group: str = "aeri",
        texts: dict | None = None,
        media: MediaSink | None = None,
        wall: Callable[[], float] = time.time,
        io_hook: Callable[[str, Path], None] | None = None,
        stall_limit: int = 10,
    ):
        self.root = Path(root)
        # A camera whose frame did not change for this many observations is
        # sealed as stalled (``cameras.stall_detection``): the live service
        # then rejects the rollout, as it does the client's.
        self.stall_limit = stall_limit
        self.run_id = run_id
        self.run_dir = Path(run_dir)
        self.group = group
        self.texts = dict(texts or {})
        self.media = media or NullMedia()
        self.wall = wall
        self.io_hook = io_hook
        self.rollouts: dict[str, Rollout] = {}
        self.manifest = self._load_manifest()

    def close(self) -> None:
        for rollout in self.rollouts.values():
            if rollout.handle is not None:
                rollout.handle.close()
                rollout.handle = None

    # ---------------------------------------------------------- low level

    def _io(self, op: str, path: Path) -> None:
        if self.io_hook is not None:
            self.io_hook(op, path)

    def _durable(self, path: Path, data: bytes) -> None:
        self._io("write", path)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            fsync_dir(path.parent)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _json(self, path: Path, value: dict) -> None:
        self._durable(
            path, (json.dumps(value, indent=1, sort_keys=True) + "\n").encode()
        )

    # ---------------------------------------------------------- manifest

    def _load_manifest(self) -> dict:
        path = self.run_dir / MANIFEST
        try:
            found = json.loads(path.read_text())
        except FileNotFoundError:
            found = None
        except (OSError, ValueError) as exc:
            raise RecorderError(f"{path}: unreadable manifest ({exc})") from None
        if found is None:
            return {
                "schema": MANIFEST_SCHEMA,
                "run_id": self.run_id,
                "rollout_root": str(self.root),
                "group": self.group,
                "episodes": [],
            }
        if found.get("schema") != MANIFEST_SCHEMA or found.get("run_id") != self.run_id:
            raise RecorderError(f"{path} belongs to another run or schema")
        return found

    def _entry(self, episode_id: str) -> dict | None:
        return next(
            (e for e in self.manifest["episodes"] if e["episode_id"] == episode_id),
            None,
        )

    def _save_manifest(self) -> None:
        self.manifest["updated_wall_ns"] = time.time_ns()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._json(self.run_dir / MANIFEST, self.manifest)

    def _set(self, episode_id: str, **fields) -> None:
        entry = self._entry(episode_id)
        if entry is not None:
            entry.update(fields)
            self._save_manifest()

    # ---------------------------------------------------------- the episode

    def open(self, *, task_folder: str, episode_id: str, number: int) -> Rollout:
        # Only what this call created is cleaned up after a failure.
        fresh = self._entry(episode_id) is None
        try:
            return self._open(task_folder, episode_id, number)
        except (OSError, RecorderError) as exc:
            if fresh:
                self._open_failed(episode_id, number)
            if isinstance(exc, OSError):
                raise RecorderError(f"open {episode_id}: {exc}") from None
            raise

    def _open_failed(self, episode_id: str, number: int) -> None:
        """An episode that could not open: what it left is ``incomplete``
        (best effort; a restart's ``recover`` finishes the job)."""
        entry = self._entry(episode_id)
        if entry is None or entry["state"] not in ("opening", "open"):
            return
        try:
            path = self.root / self.group / entry["task_folder"] / entry["demo"]
            if path.is_dir():
                entry["demo"] = self._abandon(path, number, "open_failed").name
            self.rollouts.pop(f"{entry['task_folder']}/demo_{number:04d}", None)
            entry.update(state="incomplete", abort_reason="open_failed")
            self._save_manifest()
        except (OSError, RecorderError):
            pass

    def _open(self, task_folder, episode_id, number) -> Rollout:
        run_id, role, found = aeri.episode_parts(episode_id)
        if run_id != self.run_id or found != number:
            raise RecorderError(
                f"{episode_id} is not episode {number} of {self.run_id}"
            )
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", task_folder):
            raise RecorderError(f"bad task folder {task_folder!r}")
        folder = self.root / self.group / task_folder
        demo = f"demo_{number:04d}"
        if folder.is_dir():
            taken = [
                p.name
                for p in folder.iterdir()
                if (m := _NUMBERED.match(p.name)) and int(m.group(2)) == number
            ]
            if taken:
                # Another run's rollout (or an earlier attempt): never reused.
                raise RecorderError(f"{folder.name}/{taken[0]} exists")
        if self._entry(episode_id) is not None:
            raise RecorderError(f"{episode_id} was opened before")
        now = self.wall()
        entry = {
            "episode_id": episode_id,
            "role": role,
            "task_folder": task_folder,
            "demo": demo,
            "state": "opening",
            "steps": 0,
            "opened_at": iso(now),
        }
        if role == "reset":
            entry["after_forward"] = self.manifest.get("last_forward")
        else:
            entry["after_resets"] = list(self.manifest.get("pending_resets", []))
        # The manifest knows the folder before it exists.
        self.manifest["episodes"].append(entry)
        if role == "reset":
            self.manifest.setdefault("pending_resets", []).append(episode_id)
        else:
            self.manifest["last_forward"] = episode_id
            self.manifest["pending_resets"] = []
        self.manifest.setdefault("folders", {})[role] = task_folder
        self._save_manifest()
        folder.mkdir(parents=True, exist_ok=True)
        text = self.texts.get(role, task_folder)
        description = folder / "task_description.txt"
        if not description.exists():
            self._durable(description, (text + "\n").encode())
        path = folder / demo
        self._io("mkdir", path)
        path.mkdir()
        fsync_dir(folder)
        rollout = Rollout(
            task_folder, demo, episode_id, role, number, path, started_wall=now
        )
        self._json(path / "metadata.json", self._metadata(rollout, finished=False))
        self._durable(
            path / "events.csv",
            f"timestamp_sec,frame_index,event\n{now:.3f},0,start_demo\n".encode(),
        )
        self._io("write", path / STEPS)
        handle = (path / STEPS).open("w", newline="")
        handle.write("step,observed_ns,executed,detail,pose\n")
        rollout.handle = handle
        self.media.open(path)
        self.rollouts[f"{task_folder}/{demo}"] = rollout
        self._set(episode_id, state="open")
        return rollout

    def _metadata(self, rollout: Rollout, *, finished: bool, media=None, reason=None):
        meta = {
            "data_source": "policy_rollout",
            "task_description": self.texts.get(rollout.role, rollout.task_folder),
            "demo_index": rollout.number,
            "created_at": iso(rollout.started_wall),
            "frame_count": rollout.steps,
            "eval": {
                # The automatic verdict is not an operator label (X1 §8.2).
                "outcome": "aborted" if reason else "unlabeled",
                "verdict_by": "aeri",
                "counted": False,
                "run_id": self.run_id,
                "aeri": {
                    "run_id": self.run_id,
                    "episode_id": rollout.episode_id,
                    "episode_role": rollout.role,
                },
            },
        }
        if reason:
            meta["eval"]["abort_reason"] = reason
        if finished:
            meta["stopped_at"] = iso(self.wall())
            media = media or {}
            meta["media_storage"] = {
                "video_frames_match_csv": media.get("video_frames_match_csv") is True,
                "videos": list(media.get("videos", [])),
            }
            meta["cameras"] = {
                "stall_detection": {"stalled": list(media.get("stalled", []))}
            }
        return meta

    def _check_open(self, rollout: Rollout) -> None:
        if rollout.state != "open":
            raise RecorderError(f"{rollout.demo} is {rollout.state}")
        if rollout.errors:
            raise RecorderError(rollout.errors[0])

    def stage(self, rollout: Rollout, obs) -> None:
        self._check_open(rollout)
        rollout.staged = obs
        for camera, value in sorted((obs.get("frames") or {}).items()):
            last, same = rollout.frames.get(camera, (None, 0))
            same = same + 1 if value == last else 0
            rollout.frames[camera] = (value, same)
            if same >= self.stall_limit:
                rollout.stalled.add(camera)
        try:
            self.media.frame(rollout.path, rollout.steps, obs)
        except OSError as exc:
            rollout.errors.append(f"media: {exc}")
            raise RecorderError(rollout.errors[0]) from None

    def commit(self, rollout: Rollout, result) -> None:
        self._check_open(rollout)
        obs = rollout.staged or {}
        row = io.StringIO()
        csv.writer(row).writerow(
            [
                rollout.steps,
                obs.get("observed_ns", ""),
                getattr(result, "executed", ""),
                getattr(result, "detail", ""),
                json.dumps(obs.get("pose", [])),
            ]
        )
        try:
            self._io("append", rollout.path / STEPS)
            rollout.handle.write(row.getvalue())
        except OSError as exc:
            rollout.errors.append(f"steps: {exc}")
            raise RecorderError(rollout.errors[0]) from None
        rollout.steps += 1
        rollout.staged = None

    def seal(self, rollout: Rollout, meta: dict) -> dict:
        if rollout.state == "complete":
            return dict(rollout.sealed)  # idempotent per episode
        self._check_open(rollout)
        try:
            return self._seal(rollout)
        except OSError as exc:
            rollout.errors.append(f"seal: {exc}")
            raise RecorderError(f"seal {rollout.demo}: {exc}") from None

    def _seal(self, rollout: Rollout) -> dict:
        handle = rollout.handle
        self._io("sync", rollout.path / STEPS)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        rollout.handle = None
        media = dict(self.media.finish(rollout.path, rollout.steps))
        media["stalled"] = sorted(set(media.get("stalled", [])) | rollout.stalled)
        now = self.wall()
        self._durable(
            rollout.path / "events.csv",
            (
                "timestamp_sec,frame_index,event\n"
                f"{rollout.started_wall:.3f},0,start_demo\n"
                f"{now:.3f},{rollout.steps},episode_end\n"
            ).encode(),
        )
        self._json(
            rollout.path / "metadata.json",
            self._metadata(rollout, finished=True, media=media),
        )
        self._verify(rollout)
        marker = rollout.path / MARKER
        self._io("marker", marker)
        self._durable(marker, b"")  # last: temporary file, fsync, rename, fsync
        rollout.state = "complete"
        rollout.sealed = {"sealed": "complete", "demo": rollout.demo}
        self._set(rollout.episode_id, state="complete", steps=rollout.steps)
        return dict(rollout.sealed)

    def _verify(self, rollout: Rollout) -> None:
        meta = json.loads((rollout.path / "metadata.json").read_text())
        if not meta.get("stopped_at"):
            raise RecorderError("metadata.json has no stopped_at")
        events = (rollout.path / "events.csv").read_text()
        if ",episode_end" not in events:
            raise RecorderError("events.csv has no episode_end")
        raw = list(rollout.path.glob("*_raw.avi"))
        if raw:
            raise RecorderError(f"unmuxed capture {raw[0].name}")

    def abort(self, rollout: Rollout, reason: str) -> dict:
        if rollout.state == "incomplete":
            return {"sealed": "incomplete", "demo": rollout.demo}
        try:
            renamed = self._abandon(rollout.path, rollout.number, reason, rollout)
        except OSError as exc:
            raise RecorderError(f"abort {rollout.demo}: {exc}") from None
        rollout.state = "incomplete"
        rollout.demo = renamed.name
        rollout.path = renamed
        self._set(rollout.episode_id, state="incomplete", abort_reason=reason)
        return {"sealed": "incomplete", "demo": rollout.demo}

    def _abandon(self, path: Path, number: int, reason: str, rollout=None) -> Path:
        handle = rollout.handle if rollout else None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
            rollout.handle = None
        try:
            self.media.abort(path)
        except OSError:
            pass
        marker = path / MARKER
        if marker.exists():
            self._io("unlink", marker)
            marker.unlink()
        try:
            meta = json.loads((path / "metadata.json").read_text())
            if not isinstance(meta, dict):
                meta = {}
        except (OSError, ValueError):
            meta = {}
        meta.setdefault("eval", {})
        if isinstance(meta["eval"], dict):
            meta["eval"].update(
                outcome="aborted", abort_reason=_abort_reason(reason), counted=False
            )
            meta.setdefault("stopped_at", iso(self.wall()))
            try:
                self._json(path / "metadata.json", meta)
            except OSError:
                pass  # the rename below is what keeps the live service away
        target = path.with_name(f"incomplete_{number:04d}")
        if target.exists():
            target = path.with_name(f"incomplete_{number:04d}_{time.time_ns()}")
        self._io("rename", path)
        os.rename(path, target)
        fsync_dir(path.parent)
        return target

    # ---------------------------------------------------------- after a restart

    def recover(self, sealed: set) -> list:
        """Rename every rollout of this run whose seal the journal did not
        commit to ``incomplete_*`` (``orchestrator_crash``); returns the
        renamed folders."""
        renamed = []
        for entry in self.manifest["episodes"]:
            if entry["state"] == "incomplete":
                continue
            folder = self.root / self.group / entry["task_folder"]
            path = folder / entry["demo"]
            if entry["episode_id"] in sealed and (path / MARKER).exists():
                entry["state"] = "complete"
                continue
            number = aeri.episode_parts(entry["episode_id"])[2]
            if path.is_dir():
                target = self._abandon(path, number, "orchestrator_crash")
                renamed.append(str(target))
                entry["demo"] = target.name
            else:
                # Renamed by an abort the crash kept out of the manifest.
                found = sorted(folder.glob(f"incomplete_{number:04d}*"))
                if found:
                    entry["demo"] = found[-1].name
            entry["state"] = "incomplete"
            entry["abort_reason"] = "orchestrator_crash"
        self._save_manifest()
        return renamed


def _abort_reason(reason: str) -> str:
    """The live service flags an ``incomplete_*`` with ``fr3_fault`` as an
    FR3 fault; a safety stop is one."""
    return "fr3_fault" if reason == "safety_stop" else str(reason)[:64]


# --- sessions (C2) -------------------------------------------------------------------------------


class SessionFiles:
    """The two session files (forward and reset role) of one run, written on
    every state change through ``legacy_live.legacy_sessions``. Use it as the
    orchestrator's ``listener``."""

    def __init__(
        self,
        root,
        *,
        run_id: str,
        group: str,
        folders: dict,
        texts: dict | None = None,
        policies: dict | None = None,
        reset_levi_enabled: bool = False,
        wall: Callable[[], float] = time.time,
        pid: int | None = None,
    ):
        self.root = Path(root)
        self.run_id = run_id
        self.group = group
        self.folders = dict(folders)
        self.texts = dict(texts or {})
        self.policies = dict(policies or {})
        self.reset_levi_enabled = reset_levi_enabled
        self.wall = wall
        self.pid = os.getpid() if pid is None else pid
        self.started = wall()
        self._waiting: dict = {}
        self.last: dict = {}
        # ``write`` (the orchestrator, under its state lock) and
        # ``heartbeat`` (any thread) never interleave.
        self._lock = threading.Lock()

    def path(self, role: str) -> Path:
        return self.root / SESSION_DIR / f"{self.group}__{self.folders[role]}.json"

    def __call__(self, state: str, info: dict) -> None:
        self.write(state, **info)

    def write(
        self,
        state: str,
        *,
        control_epoch: int = 0,
        stopped: bool = False,
        reason: str = "",
        episode_id: str | None = None,
    ) -> dict:
        with self._lock:
            return self._write(state, control_epoch, stopped, reason, episode_id)

    def _write(self, state, control_epoch, stopped, reason, episode_id) -> dict:
        files = legacy_live.legacy_sessions(state, stopped=stopped)
        now = self.wall()
        written = {}
        for role, value in files.items():
            if value == "waiting_reset":
                self._waiting.setdefault(role, round(now, 3))
            else:
                self._waiting.pop(role, None)
            body = {
                "schema": SESSION_SCHEMA,
                "session_id": f"{self.run_id}-{role}",
                "run_id": self.run_id,
                "pid": self.pid,
                "host": socket.gethostname(),
                "started_at": iso(self.started),
                "updated_at": iso(now),
                "state": value,
                "reason": reason[:300],
                "waiting_reset_since": self._waiting.get(role),
                "prompt": self.texts.get(role, self.folders[role]),
                "group": self.group,
                "task_folder": self.folders[role],
                "rollout_dir": str(self.root / self.group / self.folders[role]),
                "policy": dict(self.policies.get(role, {})),
                "episode": {"id": episode_id} if episode_id else {},
                "levi": {
                    "enabled": True if role == "forward" else self.reset_levi_enabled,
                    "reset_wait_s": None,
                    **({"mode": "unattended"} if role == "forward" else {}),
                },
                "episode_role": role,
                "aeri": {
                    "run_id": self.run_id,
                    "state": state,
                    "control_epoch": control_epoch,
                },
            }
            assert body["state"] in legacy_live.LEGACY_STATES
            path = self.path(role)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            with temporary.open("w") as handle:
                json.dump(body, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            written[role] = body
        self.last = {"state": state, "control_epoch": control_epoch, "stopped": stopped}
        return written

    def heartbeat(self) -> None:
        """Rewrite the last state with a fresh ``updated_at`` (the live
        reader calls a session stale after 10 s without one and a dead pid)."""
        with self._lock:
            if self.last:
                self._write(
                    self.last["state"],
                    self.last["control_epoch"],
                    self.last["stopped"],
                    "",
                    None,
                )
