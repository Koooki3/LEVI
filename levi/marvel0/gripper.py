"""Finite open, close and opening-ratio goals through the gripper stack B's
controller, and nothing else.

One ``ros2 action send_goal`` to ``/robotiq/robotiq_gripper_controller/
gripper_cmd`` (position 0.0 is open, 0.7929 closed; ``max_effort 0.0``, which
the hardware ignores: the grip force is a launch argument of B), as
``franka_control/tests/gripper_test.sh`` does it by hand. It runs as a one-shot
transient unit the page waits for (``systemd-run --user --pipe --wait``) behind
the same environment wrapper as the stack, with ``timeout`` inside and
``RuntimeMaxSec`` outside. The robot server C is not involved and is not
needed: the test works with B alone.

The result is read from the command's own output (``reached_goal``,
``stalled``, the goal's final status) and kept for the page as ``last``. A test
that printed no result is a failed test, never a passed one. The command runs
under ``timeout -s INT -k 5 20``: SIGINT is what makes ``send_goal`` cancel the
goal, so a test that ran out of time is stopped, not left moving; one that had
to be killed, or whose end is not known, says that the gripper may still be
moving. What the judge is given is the status of ``systemd-run --pipe --wait``,
not of ``timeout``: a ``timeout`` that had to SIGKILL its program dies by that
signal itself, the unit then ends by a signal, and ``systemd-run`` reports it as
255 (EXIT_EXCEPTION, systemd's run.c as the review of 2026-10-21 read it, not
yet measured; the rehearsal's ``timeout_check`` feeds the real status through
``judge`` and asserts the verdict), not as the 137 a shell would see.

The gates (B ready, no hosted run, the arm lock free, hands clear, one at a
time) are ``stack_act.Act``'s; this module only builds the command, runs it and
reads what it printed. It is a **motion of the gripper**: the page says so, and
the person who presses the button has attested that hands are clear. That
attestation is a recorded click, not a proof.
"""

import math
import re

from . import stack_units

# systemd's backstop: after the command's own `timeout` (SIGINT at 20 s, SIGKILL
# 5 s later) plus margin. The page's runner waits a little longer still.
RUNTIME_MAX_S = stack_units.GRIPPER_TIMEOUT_S + stack_units.GRIPPER_KILL_AFTER_S + 10
RUN_TIMEOUT_S = RUNTIME_MAX_S + 15  # systemd-run itself
STARTUP_FAILED = 125  # run_command's code for a command that could not run

_REACHED = re.compile(r"reached_goal:\s*(true|false)", re.IGNORECASE)
_STALLED = re.compile(r"stalled:\s*(true|false)", re.IGNORECASE)
_STATUS = re.compile(r"Goal finished with status:\s*([A-Z_]+)")
_REJECTED = re.compile(r"Goal was rejected|Goal rejected", re.IGNORECASE)
# send_goal.py:156-176 (the SIGINT handler) and :197-211: what it prints when
# the goal is cancelled and the goal was sent at all.
_CANCELED = re.compile(r"Goal canceled\.|Goal finished with status:\s*CANCELED")
_SENT = re.compile(r"Sending goal:|Goal accepted with ID")
GAVE_UP = "did not finish in time"  # stack.run_command's text when it stopped waiting

# code -> English sentence (the page translates the code).
RESULT_TEXT = {
    "reached": "The gripper reached the position.",
    "stalled": "The gripper stopped before the position (something is in the jaws?).",
    "not_reached": "The gripper did not report that it reached the position.",
    "no_result": "No result was printed (is the gripper stack running?).",
    "timeout": (
        "The test did not finish in 20 s and was stopped with a cancel. Wait "
        "until the gripper is still before reaching in."
    ),
    "timeout_killed": (
        "The test was stopped without a confirmed cancel: the gripper may still "
        "be moving. Keep hands out of the jaws until it is still."
    ),
    "cancelled": (
        "The test was cancelled because the gripper stack is stopping; the "
        "gripper may still be moving for a moment."
    ),
    "rejected": "The gripper controller rejected the goal.",
    "start_failed": "systemd could not run the test.",
}


def parse_result(text) -> dict:
    """``{"reached", "stalled", "status"}`` from the output of ``ros2 action
    send_goal`` (each None when its line is missing)."""
    plain = stack_units.strip_ansi(text)
    reached = _REACHED.search(plain)
    stalled = _STALLED.search(plain)
    status = _STATUS.search(plain)
    return {
        "reached": None if reached is None else reached.group(1).lower() == "true",
        "stalled": None if stalled is None else stalled.group(1).lower() == "true",
        "status": None if status is None else status.group(1),
        "rejected": bool(_REJECTED.search(plain)),
        "cancelled": bool(_CANCELED.search(plain)),
        "sent": bool(_SENT.search(plain)),
    }


def judge(code, text) -> dict:
    """``{"ok", "code", "reached", "stalled"}`` for a finished command: its exit
    code and what it printed. ``ok`` only when the gripper reported that it
    reached the goal and did not stall.

    A command that was ended without a result is one of three things. It was
    cancelled (``send_goal`` printed that it cancelled the goal after the
    SIGINT of ``timeout`` or of systemd): ``timeout``. It was killed, or it
    ended in some way that is not an exit 0 or ``timeout``'s 124 (a status the
    page does not know: 255 from ``systemd-run --wait`` for a unit that ended
    by a signal, a traceback's 1, ...), or a goal was sent and neither a
    result nor a cancel was printed, or the page's runner gave up on it:
    ``timeout_killed``, and the gripper may still be moving. No goal was sent
    and ``timeout`` ended the wait for the server (124): ``timeout``."""
    if code == STARTUP_FAILED:
        return {"ok": False, "code": "start_failed", "reached": None, "stalled": None}
    parsed = parse_result(text)
    out = {"reached": parsed["reached"], "stalled": parsed["stalled"]}
    if parsed["rejected"]:
        return {"ok": False, "code": "rejected", **out}
    if parsed["cancelled"]:
        return {"ok": False, "code": "timeout", **out}
    no_result = parsed["reached"] is None and parsed["stalled"] is None
    # (a goal that was sent and left without an answer: it may still be moving)
    unanswered = no_result and parsed["sent"]
    # Killed, however the status is spelled: a shell's 137, python's -9, and the
    # 255 that systemd-run --wait gives for a unit that ended by a signal.
    killed = code in (137, 255) or code < 0
    # Or ended without a result in a way that is not "done" (0) or "timeout
    # asked it to stop" (124): nothing says it cancelled, so nothing says it
    # stopped. (With a goal known to be sent, 124 is told apart below.)
    ended_unknown = no_result and code not in (0, 124)
    if killed or ended_unknown or GAVE_UP in text:
        return {"ok": False, "code": "timeout_killed", **out}
    if code == 124:  # `timeout` ended it with SIGINT
        verdict = "timeout_killed" if unanswered else "timeout"
        return {"ok": False, "code": verdict, **out}
    if no_result:
        return {"ok": False, "code": "no_result", **out}
    if parsed["stalled"]:
        return {"ok": False, "code": "stalled", **out}
    if parsed["reached"]:
        return {"ok": True, "code": "reached", **out}
    return {"ok": False, "code": "not_reached", **out}


def unit_name(now) -> str:
    return f"{stack_units.ONESHOT_PREFIX}gripper-{int(now)}"


MISSING_RATIO = object()


# Nominal URDF geometry, identical to src/lib/robot-studio/robotiq-2f85.ts
# and franka_control/robot_server/fr3_robot_server/robotiq_gripper_server_ros2.py.
# These link translations convert opening width, not installation safety limits.
_PAD_X0 = 0.03060114
_PAD_DX = 0.03152616 + 0.00563134
_PAD_DZ = -0.00376347 + 0.04718515


def _pad_x(theta):
    return _PAD_X0 + _PAD_DX * math.cos(theta) - _PAD_DZ * math.sin(theta)


def _opening_ratio_to_position(ratio):
    opened = stack_units.GRIPPER_POSITIONS["open"]
    closed = stack_units.GRIPPER_POSITIONS["close"]
    if ratio == 0:
        return closed
    if ratio == 1:
        return opened
    closed_pad = _pad_x(closed)
    width = ratio * 2 * (_pad_x(opened) - closed_pad)
    low, high = opened, closed
    # Same monotonic inverse as the existing frontend and shared server.
    for _ in range(64):
        middle = 0.5 * (low + high)
        if 2 * (_pad_x(middle) - closed_pad) > width:
            low = middle
        else:
            high = middle
    return 0.5 * (low + high)


def target_position(action, opening_ratio=MISSING_RATIO):
    """Validate one bounded goal; opening ratio 0 is closed and 1 is open."""
    if action == "position":
        if (
            type(opening_ratio) not in (int, float)
            or not 0 <= opening_ratio <= 1
            or not math.isfinite(opening_ratio)
        ):
            raise ValueError("position requires a finite opening_ratio in [0, 1]")
        return _opening_ratio_to_position(opening_ratio)
    if not isinstance(action, str) or action not in stack_units.GRIPPER_POSITIONS:
        raise ValueError("unknown gripper action")
    if opening_ratio is not MISSING_RATIO:
        raise ValueError("opening_ratio is only valid for position")
    return stack_units.GRIPPER_POSITIONS[action]


def execute(
    run,
    specs,
    action,
    now,
    unit_hook=None,
    cancelled=None,
    opening_ratio=MISSING_RATIO,
) -> dict:
    """Run one bounded goal (``open``, ``close``, or ``position``) and report
    it: ``{"action", "ok", "code", "reached", "stalled", "at"}``. ``run`` is the
    host's guarded runner; ``unit_hook(name)`` is told the one-shot unit's name
    before it starts, so that a stop of B can end the test (SIGINT, which
    cancels the goal); ``cancelled()`` is asked after that, and a test that was
    ended meanwhile is not started."""
    position = target_position(action, opening_ratio)
    target = {"opening_ratio": opening_ratio} if action == "position" else {}
    unit = unit_name(now)
    if unit_hook is not None:
        unit_hook(unit)
    if cancelled is not None and cancelled():
        return {
            "action": action,
            **target,
            "ok": False,
            "code": "cancelled",
            "reached": None,
            "stalled": None,
            "at": round(now, 3),
        }
    argv = stack_units.oneshot_argv(
        unit,
        specs.gripper_cmd(position),
        RUNTIME_MAX_S,
        f"FR3 gripper test ({action})",
    )
    code, text = run(argv, RUN_TIMEOUT_S)
    result = judge(code, text)
    return {"action": action, **target, **result, "at": round(now, 3)}
