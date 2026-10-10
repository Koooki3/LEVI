"""A fake evaluation client for the AERI tests, extending the live tests'
one (``tests/live_helpers.py``, unchanged) the way the real client and the
AERI orchestrator write interfaces C1 and C2:

- times in the session file as the real client writes them: ISO 8601 with
  the local UTC offset (``eval_session._iso``), not epoch numbers;
- ``levi.mode`` (``unattended`` / ``dual_label``), ``levi.reset_wait_s`` and
  ``waiting_reset_since`` (epoch seconds of the first entry into the current
  ``waiting_reset``, kept across rewrites, cleared on leaving it);
- one session file per role: a run has a forward and a reset task folder,
  hence two C2 keys; the inactive one says ``standby``. Each file also
  carries ``episode_role`` and ``aeri{run_id, state, control_epoch}``, keys
  the live reader ignores;
- only the client's seven states: any other name would pass through the
  live reader unchanged and open the GPU gate (design X1 §6).

Files are replaced atomically (temporary file + ``os.replace``), like the
real client's. No robot, no GPU, no network."""

import json
import os
import socket
import time
from datetime import datetime
from pathlib import Path

from live_helpers import Rollouts

# openpi examples/fr3_local/eval_session.py STATES (read only).
LEGACY_STATES = (
    "standby",
    "homing",
    "running",
    "waiting_reset",
    "fault",
    "stopped",
    "finished",
)


def iso(stamp: float) -> str:
    """Seconds since the epoch as the real client writes them."""
    return datetime.fromtimestamp(stamp).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def write_atomic(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value))
    os.replace(temporary, path)


class RoleRollouts(Rollouts):
    """``<root>/<group>/<task>/demo_NNNN`` and the session file of one role."""

    def __init__(
        self,
        root,
        source,
        *,
        role="forward",
        group="pi05_fake",
        task="stack_the_plates",
        text="stack the plates of same color together",
        session_id="s1",
    ):
        super().__init__(root, source, group=group, task=task, text=text)
        self.role = role
        self.session_id = session_id
        self.started_at = time.time() - 30
        self._waiting_since = None
        self._last_state = None

    @property
    def session_path(self) -> Path:
        return self.root / ".eval_sessions" / f"{self.group}__{self.task}.json"

    def session(
        self,
        state,
        *,
        now=None,
        updated_at=None,
        started_at=None,
        epoch_times=False,
        levi_enabled=True,
        mode=None,
        reset_wait_s=None,
        waiting_reset_since=None,
        run_id="",
        pid=None,
        reason="",
        episode=None,
        aeri=None,
    ):
        """Rewrite the session file in ``state`` (one of ``LEGACY_STATES``).

        ``updated_at``/``started_at`` default to ``now``/the creation time;
        ``epoch_times`` writes them as numbers (as the live tests' helper
        does) instead of ISO strings. ``waiting_reset_since`` defaults to the
        client's rule (first entry of the current wait)."""
        if state not in LEGACY_STATES:
            raise ValueError(f"{state!r} is not one of the client's states")
        now = time.time() if now is None else now
        if state == "waiting_reset":
            if self._last_state != "waiting_reset" or self._waiting_since is None:
                self._waiting_since = round(now, 3)
        else:
            self._waiting_since = None
        self._last_state = state
        since = (
            self._waiting_since if waiting_reset_since is None else waiting_reset_since
        )
        updated = now if updated_at is None else updated_at
        started = self.started_at if started_at is None else started_at
        stamp = (lambda t: t) if epoch_times else iso
        levi = {"enabled": levi_enabled, "reset_wait_s": reset_wait_s}
        if mode is not None:
            levi["mode"] = mode
        value = {
            "schema": "levi.eval.session.v1",
            "session_id": self.session_id,
            "run_id": run_id,
            "pid": os.getpid() if pid is None else pid,
            "host": socket.gethostname(),
            "started_at": stamp(started),
            "updated_at": stamp(updated),
            "state": state,
            "reason": reason,
            "waiting_reset_since": since,
            "prompt": self.text,
            "group": self.group,
            "task_folder": self.task,
            "rollout_dir": str(self.dir),
            "policy": {
                "config": "pi05_fr3_all_state",
                "checkpoint_dir": f"/ckpt/pi05_fr3_{self.role}_step49999/",
            },
            "episode": episode
            if episode is not None
            else {"no": 1, "target": 5, "counted": 0, "step": 0, "max_steps": 400},
            "levi": levi,
            "fr3": {"ok": True, "robot_mode_name": "MOVE", "errors": []},
            "last_episode": {},
            "episode_role": self.role,
        }
        if aeri is not None:
            value["aeri"] = aeri
        self.session_path.parent.mkdir(exist_ok=True)
        write_atomic(self.session_path, value)
        return value


class RunPair:
    """A forward and a reset role of one run: two task folders, two session
    files; at most one role is active, the other writes ``standby``."""

    def __init__(
        self,
        root,
        source,
        *,
        run_id="r20261010-a",
        group="pi05_fake",
        task="stack_the_plates",
        text="stack the plates of same color together",
        reset_levi_enabled=False,
    ):
        self.run_id = run_id
        self.reset_levi_enabled = reset_levi_enabled
        self.forward = RoleRollouts(
            root,
            source,
            role="forward",
            group=group,
            task=task,
            text=text,
            session_id="s-fwd",
        )
        self.reset = RoleRollouts(
            root,
            source,
            role="reset",
            group=group,
            task=f"reset_{task}",
            text=f"Reset: {text}",
            session_id="s-rst",
        )

    def write(self, active, state, *, aeri_state, control_epoch=1, now=None, **kwargs):
        """The active role in ``state``, the other in ``standby``."""
        if active not in ("forward", "reset"):
            raise ValueError(active)
        for role in (self.forward, self.reset):
            mine = role.role == active
            role.session(
                state if mine else "standby",
                now=now,
                run_id=self.run_id,
                levi_enabled=True if role is self.forward else self.reset_levi_enabled,
                mode="unattended" if role is self.forward else None,
                aeri={
                    "run_id": self.run_id,
                    "state": aeri_state,
                    "control_epoch": control_epoch,
                },
                **kwargs,
            )
