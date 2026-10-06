"""A recorded stretch of real motion for what cannot be reversed.

When the object left the gripper's reach after a release, no frame of the
forward episode shows the arm going to where the object lies, taking it, and
bringing it back to the pose where the reversed episode can take over. That is
new behaviour and new camera images: signal processing can draw the arm's path
(``reference``), but only a robot (or a faithful simulator) can produce the
images that go with it. So the export asks for it (``capture_request``), takes
the recording back as an ordinary pool episode, and joins it to the reversed
rest of the forward episode where the two agree (``find_join``).

The join is checked, never assumed: the recording starts where the forward
episode ended, the gripper closes on the object (and the width, when
measured, says it holds something) and the pose, the gripper and the wrist
camera at the join match the forward episode's hold.
"""

from dataclasses import dataclass

import numpy as np

from . import profile, vision


def reference(start: np.ndarray, goal: np.ndarray, steps: int) -> np.ndarray:
    """A minimum-jerk path in pose space from ``start`` to ``goal`` (7-vectors,
    gripper command kept at the start value until the last step): the arm's
    side of the bridge, for the operator to follow or to compare a recording
    against. It does not avoid obstacles and does not know where the object
    lies."""
    steps = max(2, int(steps))
    s = np.linspace(0.0, 1.0, steps)
    blend = 10 * s**3 - 15 * s**4 + 6 * s**5
    path = start[None, :] + blend[:, None] * (goal - start)[None, :]
    path[:, 6] = start[6]
    path[-1, 6] = goal[6]
    return path.astype(np.float32)


def capture_request(
    key: str,
    task: str,
    reset_task: str,
    end_state: np.ndarray,
    anchor_state: np.ndarray,
    anchor_row: int,
    reason: str,
    hold_width: float | None,
    cameras: list[str],
    fps: float,
) -> dict:
    """What to record for one forward episode so that its reset can be
    completed. The recording is an ordinary episode (same cameras, fps and
    state layout as the forward ones) that starts from the forward episode's
    last pose, with the object where it came to rest, and ends holding the
    object at the anchor pose."""
    distance = float(np.linalg.norm(end_state[:3] - anchor_state[:3]))
    steps = max(8, round(2.0 * fps * max(distance, 0.05) / 0.25))
    return {
        "schema": "levi.pool.reset.capture_request.v1",
        "forward_episode": key,
        "forward_task": task,
        "reset_task": reset_task,
        "why": reason,
        "start_pose": [round(float(x), 5) for x in end_state[:6]],
        "end_pose": [round(float(x), 5) for x in anchor_state[:6]],
        "end_gripper": "closed on the object",
        "anchor_row": anchor_row,
        "hold_width_m": None if hold_width is None else round(float(hold_width), 4),
        "cameras": cameras,
        "fps": fps,
        "reference_steps": steps,
        "record": "from the pose above with the object where it lies, approach, grasp, "
        "bring it to the end pose and stop there with the gripper closed",
    }


@dataclass
class Join:
    ok: bool
    row: int | None = None  # last row of the record kept
    reason: str | None = None
    metrics: dict | None = None
    # 2*pi multiples to add to the recording's angles so that they continue
    # the forward episode's (roll near +-pi is on either side of the cut).
    angle_shift: list[float] | None = None

    def record(self) -> dict:
        return {
            "ok": self.ok,
            "row": self.row,
            "reason": self.reason,
            **(self.metrics or {}),
        }


def find_join(
    record_state: np.ndarray,
    record_width: np.ndarray | None,
    forward_end: np.ndarray,
    anchor_state: np.ndarray,
    gripper: int,
    opened: float,
    anchor_width: float | None = None,
) -> Join:
    """Where the recording hands over to the reversed forward episode."""
    metrics: dict = {}
    start = float(np.linalg.norm(record_state[0, :3] - forward_end[:3]))
    metrics["start_distance"] = round(start, 5)
    if start > profile.SPLICE_START_TOL:
        return Join(False, reason="bridge_start_mismatch", metrics=metrics)
    held = record_state[:, gripper] != opened
    # A grasp happened: the gripper starts open and closes. A recording that is
    # closed from the first row grasped nothing.
    if held[0] or not held.any():
        return Join(False, reason="bridge_no_grasp", metrics=metrics)
    first_hold = int(np.argmax(held))
    best, best_row = None, None
    for i in range(first_hold, len(record_state)):
        if not held[i]:
            break  # the gripper let go again: nothing after is a clean hold
        d = float(np.linalg.norm(record_state[i, :3] - anchor_state[:3]))
        r = float(np.abs(_wrap(record_state[i, 3:6] - anchor_state[3:6])).max())
        near = d <= profile.JOIN_POSITION_TOL and r <= profile.JOIN_ROTATION_TOL
        if near and (best is None or d <= best):
            best, best_row = d, i
    if best_row is None:
        return Join(False, reason="bridge_never_reaches_anchor", metrics=metrics)
    metrics["position_jump"] = round(best, 5)
    if record_width is not None:
        w = float(record_width[best_row])
        metrics["hold_width"] = round(w, 4)
        if anchor_width is not None:
            metrics["anchor_width"] = round(float(anchor_width), 4)
            held_like_forward = abs(w - anchor_width) <= profile.BRIDGE_WIDTH_TOL
        else:
            held_like_forward = w < float(np.nanmax(record_width)) - profile.HOLD_MARGIN
        if not held_like_forward:
            return Join(False, best_row, "bridge_nothing_held", metrics)
    shift = (
        2
        * np.pi
        * np.round((anchor_state[3:6] - record_state[best_row, 3:6]) / (2 * np.pi))
    )
    return Join(True, best_row, None, metrics, [float(x) for x in shift])


def _wrap(delta):
    return (np.asarray(delta) + np.pi) % (2 * np.pi) - np.pi


def visual_join(forward_frame, record_frame) -> tuple[bool, dict]:
    """Do the two wrist-camera frames at the join show the same hold? Both the
    scene match and the colours must agree: colours alone are the same for a
    table with and without the object."""
    m = vision.measure(forward_frame, record_frame)
    ok = m["same"] >= profile.SPLICE_SAME and m["hist"] >= profile.SPLICE_HIST
    return ok, m
