"""The FR3 side of the GRASP_ASSIST takeover: ``grasp_assist.AssistPort`` over
``fr3_bridge.Robot``. Imports do not connect; the robot is the caller's.

The policy admission gate stays closed for the whole takeover (``begin`` closes it,
``end`` reopens it only for a hand-back and only the closed interval this port
itself left). Motion happens only inside ``window``: one ``assist`` token per key,
with an expiry, issued to the fence and revoked when the key ends; the bridge lets
exactly four routes through (pose, clearerr, open_gripper, close_gripper), only for
that token and only from the thread that opened the window
(``Robot.assist_under_closed_gate``). Nothing here homes, resets or calls
``move_gripper``.

A gripper command goes through the client's own step (the held pose with the new
gripper value as the 7th number), so the client records it as its last command and
the policy's ``state[6]`` reports it (``fr3_env._pose_state``): never a bare
``close_gripper`` POST, which would leave ``state[6]`` at its old value.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable

from .fr3_bridge import AdmissionRefused, Robot
from .grasp_assist import AssistFault, AssistStopped
from .state_machine import MotionFence, MotionToken

# A key's token lives this long (the key's moves plus their settle waits).
KEY_TOKEN_S = 30.0


class Fr3AssistPort:
    def __init__(
        self,
        robot: Robot,
        fence: MotionFence,
        clock,
        *,
        run_id: str,
        episode_id: Callable[[], str],
        control_epoch: Callable[[], int],
        policy_epoch: Callable[[], int],
        token_s: float = KEY_TOKEN_S,
        stop_probe: Callable[[], bool] | None = None,
    ):
        for name in ("assist_under_closed_gate", "stop_admission", "observe"):
            if not callable(getattr(robot, name, None)):
                raise TypeError(f"the takeover needs a bridge Robot ({name})")
        self.robot, self.fence, self.clock = robot, fence, clock
        self.run_id = run_id
        self._episode, self._epoch, self._policy_epoch = (
            episode_id,
            control_epoch,
            policy_epoch,
        )
        self.token_ns = int(token_s * 1e9)
        self._step = None  # the open window's ``step`` callable
        self._keys = 0
        self._begun = False
        # An operator's stop registered anywhere (the run's registry), besides the
        # robot's own stop flag: seen before every command of a key.
        self.stop_probe = stop_probe
        # The closed interval of the gate this port left it in (generation and
        # closing time): only this one is reopened at a hand-back.
        self._interval = None

    # --- the gate -------------------------------------------------------------------

    def stopped(self) -> bool:
        """A stop is registered (the run's) or the robot says so (a signal)."""
        if self.stop_probe is not None and self.stop_probe():
            return True
        stopped = getattr(self.robot, "stopped", None)
        return bool(callable(stopped) and stopped())

    def _remember(self) -> None:
        snap = self.robot.admission_snapshot()
        self._interval = (
            (snap.generation, snap.closed_from_ns) if snap.mode == "closed" else None
        )

    def begin(self) -> None:
        """Close the policy admission gate (idempotent on a closed gate) and note
        the closed interval it is now."""
        self.robot.stop_admission()
        self._begun = True
        self._remember()

    def end(
        self, *, reopen: bool, before_reopen: Callable[[], bool] | None = None
    ) -> bool | None:
        """Hold the arm. With ``reopen`` the gate reopens, but only if it is the
        closed interval it is now (never a window, never open already)."""
        self.fence.revoke()
        self.robot.hold()
        if not reopen:
            return
        # hold() can block on I/O. Validate only after it returns, immediately
        # before opening admission; a refusal/exception leaves this interval closed.
        if before_reopen is not None and before_reopen() is not True:
            return False
        if self._interval is None or self._interval[1] is None:
            raise AdmissionRefused("this port holds no closed interval of the gate")
        # The bridge refuses unless the gate is still exactly that interval: a gate
        # someone else closed again, reopened or closed afresh is not reopened here.
        self.robot.reopen_admission_if(
            generation=self._interval[0], closed_from_ns=self._interval[1]
        )
        self._interval = None
        return True

    # --- reads ------------------------------------------------------------------------

    def observe(self) -> dict:
        try:
            return self.robot.observe()
        except Exception as exc:  # noqa: BLE001 - an unreadable robot is a fault
            raise AssistFault("observe_failed", f"{type(exc).__name__}: {exc}"[:200])

    # --- motion ----------------------------------------------------------------------

    @contextlib.contextmanager
    def window(self, label: str):
        """One key's permission to move: one token, issued now, revoked on exit."""
        if self._step is not None:
            raise AssistFault("window_open", "a key's window is already open")
        self._keys += 1
        token = self.fence.issue(
            MotionToken(
                run_id=self.run_id,
                transaction_id=f"assist-{self._keys:04d}-{label}",
                control_epoch=self._epoch(),
                kind="assist",
                episode_id=self._episode(),
                policy_epoch=self._policy_epoch(),
                expires_mono_ns=self.clock.now() + self.token_ns,
            )
        )
        try:
            with self.robot.assist_under_closed_gate(token=token) as step:
                self._step = step
                try:
                    yield self
                finally:
                    self._step = None
        except AdmissionRefused as exc:
            raise AssistFault("assist_refused", str(exc)[:200]) from None
        finally:
            self.fence.revoke()
            # The window left the gate closed as a new interval: that is ours now.
            with contextlib.suppress(Exception):
                self._remember()

    def _send(self, xyz, euler, gripper: float, *, checked: bool = True) -> dict:
        if self._step is None:
            raise AssistFault("no_window", "a command outside a key's window")
        if checked and self.stopped():
            # Before every command: what is not sent yet is not sent.
            raise AssistStopped("a stop was registered during a key")
        result = self._step([*xyz, *euler, gripper])
        if result.executed != "yes":
            # Not sent ("no") or maybe sent ("unknown", the bridge latched): either
            # way a comms/measurement anomaly, never a person's mistake.
            raise AssistFault(
                "command_failed", f"{result.executed}: {result.detail}"[:200]
            )
        return {"detail": result.detail, "notes": [list(n) for n in result.notes]}

    def pose_step(self, xyz, euler, gripper_cmd: float) -> dict:
        return self._send(xyz, euler, float(gripper_cmd))

    def hold_step(self, xyz, euler, gripper_cmd: float) -> bool:
        """A setpoint at the pose the tool is at, sent even after a stop was
        registered (the safe action). Not when the robot itself is stopped (a
        signal): the bridge sends nothing then, by design, and trying would latch.
        True when it was sent."""
        stopped = getattr(self.robot, "stopped", None)
        if callable(stopped) and stopped():
            return False
        self._send(xyz, euler, float(gripper_cmd), checked=False)
        return True

    def gripper_step(self, command: float, xyz, euler) -> dict:
        if command not in (0.0, 1.0):
            raise AssistFault("gripper_command", f"{command!r} is not 0.0 or 1.0")
        return self._send(xyz, euler, float(command))
