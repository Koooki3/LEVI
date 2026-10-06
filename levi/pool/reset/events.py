"""Grasps and releases, from the gripper command and (when the capture has it)
the measured finger width.

The command is binary (open / closed) and leads the fingers: a measured
opening takes a few rows after the command flips. Reversal turns that around
unless it is corrected, so each event also records where the finger motion
ends (``motion_end``).
"""

from dataclasses import dataclass
from typing import Literal

import numpy as np

from . import profile


@dataclass
class GripEvent:
    kind: Literal["grasp", "release"]
    row: int  # first row of the new command
    motion_end: int  # first row at which the fingers have stopped moving
    held: bool  # release: the fingers were closed on something; grasp: True
    width_before: float | None = None

    def record(self) -> dict:
        return {
            "kind": self.kind,
            "row": self.row,
            "motion_end": self.motion_end,
            "held": self.held,
            "width_before": self.width_before,
        }


def _release_end(row: int, nxt: int, width, lead: int) -> int:
    if width is None:
        return min(nxt - 1, row + lead)
    top = float(np.nanmax(width))
    for i in range(row, nxt):
        if width[i] >= profile.OPEN_FRACTION * top:
            return i
    return min(nxt - 1, row + lead)


def _grasp_end(row: int, nxt: int, width, lead: int) -> int:
    """First row at which the width stops changing (the fingers closed or
    stalled on the object)."""
    if width is None:
        return min(nxt - 1, row + lead)
    before = width[max(0, row - 1)]
    for i in range(row, nxt - 1):
        if abs(width[i + 1] - width[i]) < 5e-4 and width[i] < before - 5e-4:
            return i
    return min(nxt - 1, row + lead)


def find_events(
    open_command: np.ndarray, width: np.ndarray | None, lead_rows: int
) -> list[GripEvent]:
    """``open_command`` is 1 where the command is open. Events are the rows
    where it changes; a release is closed -> open, a grasp open -> closed."""
    cmd = np.asarray(open_command) > 0.5
    n = len(cmd)
    changes = [int(i) for i in np.flatnonzero(cmd[1:] != cmd[:-1]) + 1]
    out = []
    for j, row in enumerate(changes):
        nxt = changes[j + 1] if j + 1 < len(changes) else n
        if cmd[row]:  # closed -> open
            before = None if width is None else float(width[max(0, row - 1)])
            held = before is None or before < profile.HOLD_WIDTH_MAX
            out.append(
                GripEvent(
                    "release",
                    row,
                    _release_end(row, nxt, width, lead_rows),
                    held,
                    before,
                )
            )
        else:
            out.append(
                GripEvent("grasp", row, _grasp_end(row, nxt, width, lead_rows), True)
            )
    return out


def lead_edits(
    events: list[GripEvent], closed: float, opened: float
) -> dict[int, float]:
    """Gripper command rewrites for the reversed episode, by source row.

    Forward, the command flips at ``row`` and the fingers move until
    ``motion_end``. Reversed, the fingers move over those same rows the other
    way, so the new command must already have flipped when that motion starts,
    that is at ``motion_end``: the rows ``row .. motion_end - 1`` take the
    value the reversed command has after the flip (closed for a release,
    open for a grasp)."""
    edits: dict[int, float] = {}
    for event in events:
        value = closed if event.kind == "release" else opened
        for i in range(event.row, event.motion_end):
            edits[i] = value
    return edits
