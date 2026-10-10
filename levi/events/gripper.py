"""Open/close crossings of one gripper channel.

A gripper channel (a measured width, a closure, a binary command) is scaled
to a range and read with hysteresis: it closes when it falls below ``LOW`` of
the range and opens when it rises above ``HIGH``, so noise around one level
is not an event. The state before the first crossing is the level the
channel starts at.

Callers differ on purpose, and say how:

- ``range_source``: ``"dataset"`` measures on the dataset's range when one is
  given (an anchored review: a gripper that only closes to 40% of its travel
  on a wide object does not cross), ``"episode"`` on the episode's own (the
  signal lines: the same grasp counts).
- ``open_level``: which end of the range is open -- ``"high"`` (a width),
  ``"low"`` (a closure) or ``"auto"``, the level the episode starts at.
- ``min_travel``: with the dataset's range, an episode whose channel moves
  less than this share of it has no events (a gripper that only jitters).
- ``start``: the initial state from the first finite sample (``"first"``)
  or from the first few (``"robust"``, see ``initial``).

Which end is open cannot be read from a channel that starts closed: with
``open_level="auto"`` such an episode reads upside down. Only a declared
``open_level`` gets it right.
"""

from dataclasses import dataclass, field
from typing import Literal

import numpy as np

# Hysteresis on the channel's range: a change counts once it crosses both.
LOW, HIGH = 0.35, 0.65

OpenLevel = Literal["high", "low", "auto"]


@dataclass
class Channel:
    """One channel read: ``u`` is the channel on [0, 1] of its range with
    open = 1 (NaN where the channel is not finite); ``crossings`` are
    ``(row, "open" | "close")``; ``bounds`` is the dataset's range when it was
    given, valid and the channel moved enough of it, else None."""

    u: np.ndarray
    lo: float
    hi: float
    open_level: Literal["high", "low"]
    bounds: tuple[float, float] | None
    crossings: list = field(default_factory=list)


def valid_bounds(bounds):
    """The dataset's (min, max) when it is a usable range, else None."""
    if bounds and np.isfinite(bounds).all() and bounds[1] - bounds[0] > 1e-9:
        return bounds
    return None


# How many leading finite samples a robust start reads.
START_SAMPLES = 3

Start = Literal["first", "robust"]


def initial(u, start: Start = "first"):
    """``(row, level)``: where reading starts and the level (on [0, 1]) the
    channel starts at. ``"first"``: the first finite sample. ``"robust"``:
    the median of the first ``START_SAMPLES`` finite samples, so one stray
    sample at the start (a stale reading, a glitch) neither sets the state
    nor makes an event; reading starts at the first sample on the median's
    side of one half."""
    idx = np.flatnonzero(np.isfinite(u))
    if start == "first" or len(idx) < 2:
        return int(idx[0]), float(u[idx[0]])
    head = idx[:START_SAMPLES]
    level = float(np.median(u[head]))
    side = level >= 0.5
    row = next(int(i) for i in head if (u[i] >= 0.5) == side)
    return row, level


def hysteresis(u, low=LOW, high=HIGH, start: Start = "first"):
    """``(row, kind)`` where ``u`` (open = 1) crosses: it closes below ``low``
    and opens above ``high``. The start (see ``initial``) sets the initial
    state (open at or above one half); non-finite samples are skipped."""
    finite = np.isfinite(u)
    if finite.sum() < 1:
        return []
    first, level = initial(u, start)
    state = "open" if level >= 0.5 else "closed"
    out = []
    for i in range(first + 1, len(u)):
        if not finite[i]:
            continue
        if state == "closed" and u[i] > high:
            state = "open"
            out.append((i, "open"))
        elif state == "open" and u[i] < low:
            state = "closed"
            out.append((i, "close"))
    return out


def read(
    values,
    bounds=None,
    *,
    range_source: Literal["dataset", "episode"] = "dataset",
    open_level: OpenLevel = "high",
    min_travel: float | None = None,
    start: Start = "first",
    low=LOW,
    high=HIGH,
):
    """The channel's crossings and how they were measured; None when there
    is nothing to read (fewer than two finite samples, a flat channel, or one
    that moved less than ``min_travel`` of the dataset's range). ``start``
    (see ``initial``) also decides ``open_level="auto"``."""
    values = np.asarray(values, dtype=float)
    finite = np.isfinite(values)
    if finite.sum() < 2:
        return None
    bounds = valid_bounds(bounds)
    lo, hi = np.nanmin(values), np.nanmax(values)
    if hi - lo <= 1e-9:
        return None
    if (
        bounds is not None
        and min_travel is not None
        and (hi - lo) < min_travel * (bounds[1] - bounds[0])
    ):
        return None
    if range_source == "dataset" and bounds is not None:
        lo, hi = bounds
    u = np.where(finite, (values - lo) / (hi - lo), np.nan)
    if open_level == "auto":
        # (No finite level at all -- an infinite sample stretched the range
        # -- reads as high, with no crossings.)
        start_level = initial(u, start)[1] if np.isfinite(u).any() else 1.0
        open_level = "low" if start_level < 0.5 else "high"
    if open_level == "low":
        u = 1 - u
    return Channel(
        u=u,
        lo=float(lo),
        hi=float(hi),
        open_level=open_level,
        bounds=bounds,
        crossings=hysteresis(u, low, high, start),
    )
