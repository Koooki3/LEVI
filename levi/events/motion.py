"""Speed and stillness of an arm, on the recorded timestamps.

A step's speed is its distance over the time it took. On an evenly sampled
table (``uneven`` says which are not) every step is multiplied by ``fps``,
exactly as the signal lines always did, so an even table reads as before to
the last bit -- also a long one stored as float32, as LeRobot and LEVI store
timestamps. On an uneven one (dropped frames, a jittery clock) each step's own
time is used, so a gap is not read as one fast frame; a step whose time does
not advance (a repeated timestamp) counts as one frame, as before.
"""

import numpy as np

# Still: speed below this share of the episode's fast speed (its 95th
# percentile), for at least STILL_SECONDS.
STILL, STILL_SECONDS = 0.1, 0.5
# A table is evenly sampled when every row is within this share of a frame
# of ``timestamp[0] + row / fps`` (or within float32 resolution there).
EVEN = 0.01


def uneven(times, fps):
    """The timestamps (as float64) when they are not evenly sampled at
    ``fps``, else None. None too when there is nothing to go by: fewer than
    two rows, no rate, a non-finite or a decreasing timestamp -- readers
    then keep their frame-count behaviour."""
    times = np.asarray(times, dtype=float)
    if len(times) < 2 or not fps or fps <= 0 or not np.isfinite(times).all():
        return None
    if np.any(np.diff(times) < 0):
        return None
    drift = times - times[0] - np.arange(len(times)) / fps
    # A float32 timestamp is off by up to one unit in its last place; four
    # cover the start's error and the row's.
    resolution = 4 * np.spacing(np.abs(times).astype(np.float32)).astype(float)
    tolerance = np.maximum(EVEN / fps, resolution)
    return None if np.all(np.abs(drift) <= tolerance) else times


def speeds(times, positions, fps):
    """Speed of each step between rows (one fewer than rows); NaN where a
    step is not finite."""
    step = np.diff(positions, axis=0)
    ok = np.isfinite(step).all(axis=1)
    distance = np.linalg.norm(np.nan_to_num(step), axis=1)
    timed = uneven(times, fps)
    if timed is None:
        return np.where(ok, distance * fps, np.nan)
    dt = np.diff(timed)
    with np.errstate(divide="ignore", invalid="ignore"):
        rate = np.where(dt > 0, 1 / dt, fps)
    return np.where(ok, distance * rate, np.nan)


def still_spans(times, positions, fps):
    """``[(start, end)]`` in seconds where the arm stands still: speed below
    ``STILL`` of its fast speed for at least ``STILL_SECONDS``."""
    speed = speeds(times, positions, fps)
    if not np.isfinite(speed).any():
        return []
    fast = np.nanpercentile(speed, 95)
    if fast <= 1e-9:
        return []
    # Smoothed over three frames: one noisy sample neither starts nor ends a span.
    padded = np.concatenate([[speed[0]], speed, [speed[-1]]])
    smooth = np.convolve(np.nan_to_num(padded, nan=fast), np.ones(3) / 3, "valid")
    quiet = np.concatenate([[smooth[0] < STILL * fast], smooth < STILL * fast])
    spans = []
    start = None
    for i, q in enumerate([*quiet, False]):
        if q and start is None:
            start = i
        elif not q and start is not None:
            end = min(i, len(times)) - 1
            if times[end] - times[start] >= STILL_SECONDS:
                spans.append((float(times[start]), float(times[end])))
            start = None
    return spans
