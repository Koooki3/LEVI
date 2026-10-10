"""Speed and stillness of an arm, on the recorded timestamps.

A step's speed is its distance over the time it took. Where a table is
evenly sampled (a step within 0.1% of ``1 / fps``) the step is multiplied by
``fps`` exactly, as the signal lines always did, so an even table reads as
before to the last bit; where frames were dropped or the clock jittered, the
step's own time is used, so a gap is not read as one fast frame.
"""

import numpy as np

# Still: speed below this share of the episode's fast speed (its 95th
# percentile), for at least STILL_SECONDS.
STILL, STILL_SECONDS = 0.1, 0.5
# A step within this share of 1 / fps counts as one nominal frame.
NOMINAL = 1e-3


def speeds(times, positions, fps):
    """Speed of each step between rows (one fewer than rows); NaN where a
    step is not finite or its time is not positive."""
    times = np.asarray(times, dtype=float)
    step = np.diff(positions, axis=0)
    ok = np.isfinite(step).all(axis=1)
    distance = np.linalg.norm(np.nan_to_num(step), axis=1)
    dt = np.diff(times)
    with np.errstate(divide="ignore", invalid="ignore"):
        nominal = np.abs(dt * fps - 1) <= NOMINAL
        rate = np.where(nominal, fps, 1 / dt)
    rate = np.where(np.isfinite(dt) & (dt > 0), rate, np.nan)
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
