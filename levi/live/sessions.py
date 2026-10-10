"""What the robot side says about itself: evaluation sessions (interface C2)
and the FR3 health monitor (interface C3). Read-only, standard library only.

``<root>/.eval_sessions/<group>__<task_folder>.json`` is rewritten by the
evaluation client about every two seconds while it runs (schema
``levi.eval.session.v1``). A session whose ``updated_at`` is more than
``STALE_S`` old *and* whose process is gone crashed: it never wrote ``stopped``.

``fr3_health.json`` (schema ``levi.fr3.health.v1``) is written 2 Hz by the
health monitor in the ROS terminal. A file that is missing or older than
``fr3.stale_s`` is **not** a red light: it means the monitor is not running.
"""

import os
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import jsonio

STALE_S = 10.0
VERY_OLD_S = 3600.0
SESSION_DIR = ".eval_sessions"
DEFAULT_ACTIVE = ("running", "homing", "waiting_reset")
# How long an ended session still counts as "just ended" for the GPU settle.
_CACHE: dict = {}


def parse_time(value) -> float | None:
    """Seconds since the epoch from a number or an ISO string (naive = local)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return None
    return None


def _alive(pid, host) -> bool | None:
    """Whether the session's process exists; None when it ran elsewhere."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    if host and host != socket.gethostname():
        return None
    return os.path.exists(f"/proc/{pid}")


@dataclass
class Session:
    path: str
    group: str
    task_folder: str
    state: str
    raw_state: str
    updated_at: float | None
    started_at: float | None
    pid: int | None
    age_s: float | None
    crashed: bool = False
    levi_enabled: bool | None = None
    reason: str = ""
    episode: dict = field(default_factory=dict)
    fr3: dict = field(default_factory=dict)
    last_episode: dict = field(default_factory=dict)
    prompt: str = ""
    session_id: str = ""
    run_id: str = ""
    policy: dict = field(default_factory=dict)
    root: str = ""
    # Seconds the client waits for the operator to reset the scene after an
    # episode (``levi.reset_wait_s``): the next episode starts right after.
    reset_wait_s: float | None = None
    # Epoch seconds of the first entry into ``waiting_reset`` of the current
    # wait, written by the client (None in every other state, or by an older
    # client): the wait's true start, which the supervisor cannot see between
    # its polls or before it started.
    waiting_reset_since: float | None = None
    # How the client labels its episodes (``levi.mode``): ``dual_label`` (the
    # operator labels every episode and LEVI labels it too) or ``unattended``
    # (LEVI alone); None for a manual run, an older client or another value.
    label_mode: str | None = None

    def active(self, states=DEFAULT_ACTIVE) -> bool:
        return self.state in states and not self.crashed

    @property
    def fault(self) -> bool:
        return self.state == "fault"

    def public(self) -> dict:
        return {
            "group": self.group,
            "task_folder": self.task_folder,
            "state": self.state,
            "reported_state": self.raw_state,
            "crashed": self.crashed,
            "age_s": None if self.age_s is None else round(self.age_s, 1),
            "reason": self.reason,
            "levi_enabled": self.levi_enabled,
            "episode": self.episode,
            "last_episode": self.last_episode,
            "fr3": self.fr3,
            "prompt": self.prompt[:300],
            "session_id": self.session_id,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "policy": self.policy,
            "root": self.root,
            "reset_wait_s": self.reset_wait_s,
            "waiting_reset_since": self.waiting_reset_since,
            "label_mode": self.label_mode,
        }


LABEL_MODES = ("dual_label", "unattended")


def read_sessions(roots, now=None) -> dict:
    """``{(group, task_folder): Session}`` from every root's session folder."""
    now = time.time() if now is None else now
    found = {}
    seen = set()
    for root in roots:
        folder = Path(root).expanduser() / SESSION_DIR
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            if not entry.name.endswith(".json") or entry.name.startswith("."):
                continue
            try:
                stamp = entry.stat().st_mtime_ns
            except OSError:
                continue
            key = entry.path
            seen.add(key)
            cached = _CACHE.get(key)
            if cached and cached[0] == stamp:
                data = cached[1]
            else:
                data = jsonio.read(entry.path)
                _CACHE[key] = (stamp, data)
            if not isinstance(data, dict):
                continue
            session = _session(entry.path, entry.name, data, now)
            if session:
                session.root = str(Path(root).expanduser())
                found[(session.root, session.group, session.task_folder)] = session
    mine = {str(Path(r).expanduser()) for r in roots}
    for key in [
        k for k in _CACHE if k not in seen and str(Path(k).parent.parent) in mine
    ]:
        _CACHE.pop(key, None)
    return found


def _number(value):
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
        else None
    )


def _epoch(value, now):
    """A time from the client, in the past: anything else is not believed."""
    stamp = _number(parse_time(value))
    return None if stamp is None or stamp > now + 5 else stamp


def _session(path, name, data, now):
    stem = name[: -len(".json")]
    group = data.get("group")
    task = data.get("task_folder")
    if not (group and task):
        group, _, task = stem.partition("__")
    if not (group and task):
        return None
    raw = str(data.get("state") or "unknown")
    updated = parse_time(data.get("updated_at"))
    age = None if updated is None else max(0.0, now - updated)
    alive = _alive(data.get("pid"), data.get("host"))
    terminal = raw in ("stopped", "finished")
    # Stale for STALE_S and its process gone; or so old (an hour) that a
    # recycled pid must not keep it alive (same rule as the client's).
    crashed = bool(
        not terminal
        and age is not None
        and (
            age > VERY_OLD_S
            or (
                age > STALE_S
                and alive is not True
                and (alive is False or age > 6 * STALE_S)
            )
        )
    )
    levi = data.get("levi") if isinstance(data.get("levi"), dict) else {}
    policy = data.get("policy") if isinstance(data.get("policy"), dict) else {}
    checkpoint = str(policy.get("checkpoint_dir") or "").rstrip("/")
    enabled = levi.get("enabled") if isinstance(levi.get("enabled"), bool) else None
    return Session(
        path=path,
        group=str(group),
        task_folder=str(task),
        state="crashed" if crashed else raw,
        raw_state=raw,
        updated_at=updated,
        started_at=parse_time(data.get("started_at")),
        pid=data.get("pid") if isinstance(data.get("pid"), int) else None,
        age_s=age,
        crashed=crashed,
        levi_enabled=enabled,
        reason=str(data.get("reason") or "")[:300],
        episode=_small(data.get("episode")),
        fr3=_small(data.get("fr3")),
        last_episode=_small(data.get("last_episode")),
        prompt=str(data.get("prompt") or ""),
        session_id=str(data.get("session_id") or ""),
        run_id=str(data.get("run_id") or ""),
        # The page shows which model is being evaluated: its config name and
        # the checkpoint's folder name (never the full path).
        policy={
            "config": str(policy.get("config") or "")[:120] or None,
            "checkpoint": os.path.basename(checkpoint)[:120] or None,
        },
        reset_wait_s=_number(levi.get("reset_wait_s")),
        waiting_reset_since=_epoch(data.get("waiting_reset_since"), now),
        label_mode=levi.get("mode") if levi.get("mode") in LABEL_MODES else None,
    )


def _small(value, limit=30):
    """A bounded copy of a nested dict (keeps API bodies small)."""
    if not isinstance(value, dict):
        return {}
    out = {}
    for key, item in list(value.items())[:limit]:
        if isinstance(item, (str, int, float, bool)) or item is None:
            out[str(key)] = item if not isinstance(item, str) else item[:200]
        elif isinstance(item, list):
            out[str(key)] = [
                x if isinstance(x, (str, int, float, bool)) else str(x)[:100]
                for x in item[:10]
            ]
        elif isinstance(item, dict):
            out[str(key)] = _small(item, 12)
    return out


def any_active(sessions: dict, states=DEFAULT_ACTIVE) -> bool:
    return any(s.active(states) for s in sessions.values())


def latest_activity(sessions: dict) -> float | None:
    times = [s.updated_at for s in sessions.values() if s.updated_at]
    return max(times) if times else None


# --- FR3 health (C3) --------------------------------------------------------


def read_fr3(path, stale_s=3.0, now=None) -> dict:
    """``{"state": ok|red|offline|missing, ...}`` from the health file."""
    now = time.time() if now is None else now
    if not path:
        return {
            "state": "missing",
            "detail": "no health file configured (fr3.health_file)",
        }
    path = Path(path).expanduser()
    try:
        stat = path.stat()
    except OSError:
        return {
            "state": "missing",
            "detail": "no health file: the monitor is not running",
        }
    data = jsonio.read(path)
    if not isinstance(data, dict):
        return {"state": "offline", "detail": "health file unreadable"}
    updated = (
        parse_time(data.get("updated_at_epoch"))
        or parse_time(data.get("updated_at"))
        or stat.st_mtime
    )
    age = max(0.0, now - updated)
    summary = {
        "age_s": round(age, 1),
        "robot_mode": data.get("robot_mode"),
        "robot_mode_name": data.get("robot_mode_name"),
        "current_errors": list(data.get("current_errors") or [])[:10],
        "last_motion_errors": list(data.get("last_motion_errors") or [])[:10],
        "hardware_active": data.get("hardware_active"),
        "controller_active": data.get("controller_active"),
        "command_success_rate": data.get("command_success_rate"),
        "reasons": [str(r)[:200] for r in (data.get("reasons") or [])[:10]],
    }
    if age > stale_s:
        return {"state": "offline", "detail": "the monitor stopped updating", **summary}
    return {"state": "red" if data.get("red_light") else "ok", **summary}
