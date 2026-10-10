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
import hashlib
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

from . import scene_assessment as sa
from .adapters import legacy_live
from .journal import fsync_dir

# What put the scene back before a forward episode (``eval.aeri.preceded_by``
# in its metadata, ``preceded_by`` in the manifest; design X2 §1.2):
# nothing since the previous forward episode, the reset policy, or a person
# (a resume after the run waited for one; the most recent of the two wins).
PRECEDED_BY = ("none", "reset_policy", "human_reset")
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
    # Forward episodes: what put the scene back before it (PRECEDED_BY).
    preceded_by: str | None = None
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
        # Forward episode id -> what preceded it (``before_forward``).
        self._preceding: dict[str, dict] = {}
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

    def before_forward(
        self, episode_id: str, *, preceded_by: str, human_resets: list
    ) -> None:
        """What put the scene back before the forward episode about to open
        (the orchestrator derives it from the journal, the source of truth;
        ``human_resets``: ``[{wait_seq, resume_seq, wait_ms, principal_id,
        reason}]``). Kept until ``open`` writes it into the manifest
        (``after_human_resets``) and the rollout's metadata."""
        if preceded_by not in PRECEDED_BY:
            raise RecorderError(f"preceded_by {preceded_by!r}")
        self._preceding[episode_id] = {
            "preceded_by": preceded_by,
            "after_human_resets": [dict(item) for item in human_resets][-64:],
        }

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
        preceding = None
        if role == "reset":
            entry["after_forward"] = self.manifest.get("last_forward")
        else:
            entry["after_resets"] = list(self.manifest.get("pending_resets", []))
            preceding = self._preceding.pop(episode_id, None) or {
                "preceded_by": "reset_policy" if entry["after_resets"] else "none",
                "after_human_resets": [],
            }
            entry.update(preceding)
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
            task_folder,
            demo,
            episode_id,
            role,
            number,
            path,
            started_wall=now,
            preceded_by=preceding["preceded_by"] if preceding else None,
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
        if rollout.preceded_by is not None:
            meta["eval"]["aeri"]["preceded_by"] = rollout.preceded_by
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
            return self._seal(rollout, meta or {})
        except OSError as exc:
            rollout.errors.append(f"seal: {exc}")
            raise RecorderError(f"seal {rollout.demo}: {exc}") from None

    def _seal(self, rollout: Rollout, meta: dict) -> dict:
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
        self._set(
            rollout.episode_id,
            state="complete",
            steps=rollout.steps,
            last_frames=_last_frames(rollout),
            # The control group (pipeline §5.5): no early stop was allowed;
            # where the detector would have stopped, if it would have.
            control=bool(meta.get("control", False)),
            would_stop_step=meta.get("would_stop_step"),
        )
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
        self._set(
            rollout.episode_id,
            state="incomplete",
            abort_reason=reason,
            last_frames=_last_frames(rollout),
        )
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


def _last_frames(rollout: Rollout) -> dict:
    """The last frame each camera gave (as the observation named it), for
    the waiting card."""
    return {str(c): str(v)[:200] for c, (v, _) in sorted(rollout.frames.items())}


def _abort_reason(reason: str) -> str:
    """The live service flags an ``incomplete_*`` with ``fr3_fault`` as an
    FR3 fault; a safety stop is one."""
    return "fr3_fault" if reason == "safety_stop" else str(reason)[:64]


# --- sessions (C2) -------------------------------------------------------------------------------


class SessionFiles:
    """The session files of one run, one per role in ``folders``, written
    on every state change through ``legacy_live.legacy_sessions``. Use it as
    the orchestrator's ``listener``.

    A run with a reset policy has two (``forward`` and ``reset``); a
    human-assisted run ("policy evaluation only") runs no reset policy and
    passes ``forward`` alone: no reset session file is ever written, so the
    live service never lists a reset session that stays ``standby`` (design
    X2 G2)."""

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
        if "forward" not in self.folders or set(self.folders) - {"forward", "reset"}:
            raise ValueError("folders: forward, and reset when a reset policy runs")
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
            if role not in self.folders:
                continue  # no reset policy: no reset session file
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


# --- scene evidence (T-CL-04) ------------------------------------------------------------------

EVIDENCE = "evidence"
EVIDENCE_SCHEMA = "levi.aeri.evidence.v1"
FRAMES = "frames"
# The Initial State Contract the run checks against (for the waiting card).
INITIAL_STATE = "initial_state.json"
MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_EVIDENCE_BYTES = 256 * 1024 * 1024
# Frames may use this share of the budget; the rest is kept for the
# assessment records (a refused assessment is kept too).
FRAME_SHARE = 0.9
_MAGIC = ((b"\xff\xd8\xff", ".jpg"), (b"\x89PNG\r\n\x1a\n", ".png"))


def _frame_suffix(data: bytes) -> str:
    return next((suffix for magic, suffix in _MAGIC if data.startswith(magic)), ".bin")


class EvidenceStore:
    """``<run_dir>/evidence/``: one record per scene assessment (accepted
    or not; ``<assessment_id>.json``, or the request id when the provider
    gave none) and the frames it was decided on (``frames/<sha256>.<ext>``,
    content-addressed, written once).

    Every file is written whole (temporary file, fsync, rename, fsync of
    the folder): a writer killed half-way leaves only a hidden temporary
    file, removed when the next store opens the folder (one writer per run:
    the journal's lock holder). Frames over ``max_frame_bytes`` are not
    kept, and frames stop being kept at ``FRAME_SHARE`` of
    ``max_total_bytes`` (both say so in the record); records stop at the
    total. Nothing here ever decides anything: the journal stays the
    source of truth."""

    def __init__(
        self,
        run_dir,
        *,
        max_frame_bytes: int = MAX_FRAME_BYTES,
        max_total_bytes: int = MAX_EVIDENCE_BYTES,
        io_hook: Callable[[str, Path], None] | None = None,
    ):
        self.folder = Path(run_dir) / EVIDENCE
        self.frames = self.folder / FRAMES
        self.max_frame_bytes = max_frame_bytes
        self.max_total_bytes = max_total_bytes
        self.io_hook = io_hook
        self.used = 0
        for folder in (self.folder, self.frames):
            if not folder.is_dir():
                continue
            for path in folder.iterdir():
                if path.name.startswith(".") and path.name.endswith(".tmp"):
                    path.unlink(missing_ok=True)  # cut short by a crash
                elif path.is_file():
                    self.used += path.stat().st_size

    def _whole(self, path: Path, data: bytes) -> None:
        if self.io_hook is not None:
            self.io_hook("write", path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if self.io_hook is not None:
                self.io_hook("rename", path)
            os.replace(temporary, path)
            fsync_dir(path.parent)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        self.used += len(data)

    def _frame(self, ref: str, data: bytes) -> dict:
        digest = hashlib.sha256(data).hexdigest()
        name = f"{digest}{_frame_suffix(data)}"
        found = {
            "ref": ref,
            "view": ref.partition(":")[0] if ":" in ref else None,
            "sha256": digest,
            "bytes": len(data),
            "file": None,
            "skipped": None,
        }
        path = self.frames / name
        if path.is_file():
            found["file"] = f"{FRAMES}/{name}"  # the same frame, kept once
        elif len(data) > self.max_frame_bytes:
            found["skipped"] = "frame_too_large"
        elif self.used + len(data) > self.max_total_bytes * FRAME_SHARE:
            found["skipped"] = "evidence_budget_exhausted"
        else:
            self._whole(path, data)
            found["file"] = f"{FRAMES}/{name}"
        return found

    def write(self, record: dict, frames: dict | None = None) -> dict | None:
        """Keep one assessment record (and its frames, ``{ref: bytes}``);
        returns what was written, None when the budget is spent. A record
        is never overwritten: a second one under the same id gets a
        numbered name."""
        kept = [self._frame(ref, data) for ref, data in sorted((frames or {}).items())]
        body = {"schema": EVIDENCE_SCHEMA, **record, "frames": kept}
        data = (json.dumps(body, sort_keys=True, indent=1) + "\n").encode()
        if self.used + len(data) > self.max_total_bytes:
            return None
        name = str(record.get("assessment_id") or record["request_id"])
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$", name):
            raise RecorderError(f"bad evidence id {name[:60]!r}")
        path = self.folder / f"{name}.json"
        number = 1
        while path.exists():
            number += 1
            path = self.folder / f"{name}.{number}.json"
        self._whole(path, data)
        return body

    def keep_contract(self, described: dict) -> None:
        """The run's Initial State Contract (``describe()``), for the
        waiting card; rewritten only when it changed."""
        path = self.folder / INITIAL_STATE
        data = (json.dumps(described, sort_keys=True, indent=1) + "\n").encode()
        try:
            if path.read_bytes() == data:
                return
        except OSError:
            pass
        self._whole(path, data)


def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def evidence_records(run_dir) -> list:
    """Every whole evidence record of a run, in journal order."""
    folder = Path(run_dir) / EVIDENCE
    if not folder.is_dir():
        return []
    out = []
    for path in folder.glob("*.json"):
        if path.name.startswith(".") or path.name == INITIAL_STATE:
            continue
        found = _read_json(path)
        if isinstance(found, dict) and found.get("schema") == EVIDENCE_SCHEMA:
            out.append(found)
    return sorted(out, key=lambda r: (r.get("journal_seq", 0), r.get("request_id", "")))


DRAFT_NOTICE = "not confirmed by the user (HA-23) / 未经用户确认，HA-23"


def pending_card(run_dir, *, now_wall_ns: int | None = None) -> dict:
    """What a person needs when the run waits for them (design X2 §1.2,
    "to-do card"), read only from the run's folder (journal, manifest,
    evidence); nothing is written. ``waiting`` is False when no person is
    waited for (the other fields still describe the last episode)."""
    from .journal import Journal

    run_dir = Path(run_dir)
    scan = Journal.read(run_dir)
    events = scan.events
    state = scan.effective_state
    committed = [e for e in events if e.record == "committed"]
    waiting = state in ("WAIT_HUMAN", "FAULT_LOCKED")
    wait = next(
        (e for e in reversed(committed) if waiting and e.to_state == state), None
    )
    last_forward = max(
        (e.sequence_no for e in committed if e.to_state == "FORWARD_ACTIVE"),
        default=-1,
    )
    upto = wait.sequence_no if wait is not None else len(events)
    waits = [
        e for e in committed if e.to_state == "WAIT_HUMAN" and e.sequence_no <= upto
    ]
    now = time.time_ns() if now_wall_ns is None else now_wall_ns
    card = {
        "run_id": events[0].run_id if events else None,
        "state": state,
        "corrupt": scan.corrupt,
        "waiting": waiting,
        "reason": wait.reason if wait is not None else None,
        "since_sequence_no": wait.sequence_no if wait is not None else None,
        # The sequence a resume must name (``expected_seq``).
        "expected_seq": len(events),
        "waited_ms": max(0, (now - wait.emitted_wall_ns) // 1_000_000)
        if wait is not None
        else None,
        "human_wait_number": len(waits) if waiting and state == "WAIT_HUMAN" else None,
        "human_waits_since_forward": sum(
            1 for e in waits if e.sequence_no > last_forward
        ),
    }
    result = next((e for e in reversed(committed) if e.episode_result), None)
    manifest = _read_json(run_dir / MANIFEST) or {}
    episode = None
    if result is not None:
        found = result.episode_result
        entry = next(
            (
                e
                for e in manifest.get("episodes", [])
                if e.get("episode_id") == result.episode_id
            ),
            {},
        )
        root = manifest.get("rollout_root")
        path = None
        if root and entry.get("task_folder") and entry.get("demo"):
            path = str(
                Path(root)
                / manifest.get("group", "")
                / entry["task_folder"]
                / entry["demo"]
            )
        episode = {
            "episode_id": result.episode_id,
            "role": result.episode_role,
            "task_outcome": found.task_outcome,
            "stop_reason": found.stop_reason,
            "goal_verification": found.goal_verification,
            "robot_home": found.robot_home,
            "scene_reset": found.scene_reset,
            "rollout": found.rollout.model_dump(),
            "rollout_path": path,
            "last_frames": entry.get("last_frames"),
        }
    card["last_episode"] = episode
    described = _read_json(run_dir / EVIDENCE / INITIAL_STATE)
    contract = None
    if isinstance(described, dict) and described.get("id"):
        draft = described.get("status") != "confirmed"
        contract = {
            "key": f"{described['id']}@{described.get('version')}",
            "status": described.get("status"),
            "unconfirmed": draft,
            "notice": DRAFT_NOTICE if draft else None,
            "predicates": [
                {"name": n, "text": _predicate_text(n), "required": True}
                for n in described.get("required", [])
            ]
            + [
                {"name": n, "text": _predicate_text(n), "required": False}
                for n in described.get("optional", [])
            ],
        }
    card["contract"] = contract
    records = [r for r in evidence_records(run_dir) if r.get("journal_seq", 0) <= upto]
    last = records[-1] if records else None
    card["evidence"] = (
        {
            key: last.get(key)
            for key in (
                "assessment_id",
                "request_id",
                "target",
                "provider",
                "provider_decision",
                "decision",
                "reason",
                "failed_predicates",
                "unknown_predicates",
                "frames",
            )
        }
        if last
        else None
    )
    return card


def _predicate_text(name) -> str:
    return sa.predicate_text(str(name))
