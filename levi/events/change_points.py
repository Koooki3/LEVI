"""Where the statistics of an arm's signals change: penalised change points.

A training-free detector over a multi-dimensional series of one actor --
the speed of its position, its gripper level, the speed of its rotation --
each scaled to a comparable spread. It answers "did the signal's level
change here", not "what happened": a change point is a candidate for
evidence, never a subtask boundary by itself.

Method: exact penalised segmentation with a piecewise-constant mean (squared
error cost) found by optimal partitioning with pruning, the PELT family that
``ruptures`` implements. With a minimum segment length the pruning is
delayed by that length (``segment``), so the result is the exact optimum;
the tests compare it with unpruned optimal partitioning (Truong, Oudre and Vayatis 2020, "Selective review of
offline change point detection methods", arXiv:1801.00718; BSD-2-Clause).
LEVI does not depend on ``ruptures``: this is an independent numpy
implementation of the idea (see ``third_party.json``).

Guards against over-cutting, each tested:

- the penalty per change is ``penalty * (d + 1) * log(n)`` for ``d``
  features and ``n`` rows (more features or a longer episode need more
  evidence per change);
- no segment is shorter than ``min_seconds``;
- at most ``max_per_minute`` change points per minute of episode: past it the
  penalty is raised until the result fits, keeping exactly the strongest
  ``limit`` when equally strong changes would all vanish at once; the result
  says so (``capped``).

Speeds use the recorded timestamps (``events.motion``), so a dropped frame is
not a jump. Rows with a missing value are filled from their neighbours;
a feature with no spread (constant, or never finite) is left out.
"""

import math
from dataclasses import dataclass, field

import numpy as np

from . import motion

# Calibrated on development gold only (docs/EVENTS.md, "Calibration").
PENALTY = 0.5
MIN_SECONDS = 0.5
MAX_PER_MINUTE = 30.0
# Raise the penalty by this factor, at most this many times, to meet the cap.
RAISE, MAX_RAISES = 1.5, 30


@dataclass
class Result:
    """Change point rows (a row starts a new segment), their gains (cost
    saved by each, in scaled units) and how they were found."""

    rows: list[int]
    gains: list[float]
    penalty: float
    beta: float
    min_size: int
    features: list[str] = field(default_factory=list)
    capped: bool = False


def _fill(x):
    """Missing values from the nearest finite neighbours (linear inside,
    constant at the ends); None when nothing is finite."""
    x = np.asarray(x, dtype=float)
    ok = np.isfinite(x)
    if not ok.any():
        return None
    if ok.all():
        return x
    idx = np.arange(len(x))
    return np.interp(idx, idx[ok], x[ok])


def scale(x):
    """``x`` over a robust spread: the larger of a quarter of its 1-99%
    range (a full swing of a two-level channel -- a gripper -- is four units,
    as is about four standard deviations of Gaussian noise) and its noise
    level (from first differences). None when it has no spread."""
    x = _fill(x)
    if x is None or len(x) < 2:
        return None
    p1, p99 = np.percentile(x, [1, 99])
    d = np.diff(x)
    noise = np.median(np.abs(d - np.median(d))) / (0.6745 * math.sqrt(2))
    spread = max((p99 - p1) / 4, noise)
    span = max(abs(float(np.max(x))), abs(float(np.min(x))), 1.0)
    if not np.isfinite(spread) or spread <= 1e-9 * span:
        return None
    return (x - np.median(x)) / spread


def _cost(s1, s2, starts, end):
    """Squared error of the mean model on rows ``[start, end)`` for each
    start, from cumulative sums."""
    m = (end - starts).astype(float)
    diff = s1[end] - s1[starts]
    return s2[end] - s2[starts] - np.sum(diff * diff, axis=1) / m


def segment(x, beta, min_size):
    """Rows where a new segment starts (not 0) minimising the squared error
    plus ``beta`` per change, no segment shorter than ``min_size`` rows."""
    x = np.asarray(x, dtype=float)
    if x.ndim == 1:
        x = x[:, None]
    n = len(x)
    min_size = max(1, int(min_size))
    if n < 2 * min_size:
        return []
    s1 = np.vstack([np.zeros(x.shape[1]), np.cumsum(x, axis=0)])
    s2 = np.concatenate([[0.0], np.cumsum(np.sum(x * x, axis=1))])
    best = np.full(n + 1, np.inf)
    best[0] = -beta
    last = np.zeros(n + 1, dtype=int)
    # Candidate starts, and the end at which each first failed the pruning
    # test (-1: not yet).
    starts = np.array([0])
    failed = np.array([-1])
    for t in range(min_size, n + 1):
        new = t - min_size
        if new >= min_size:
            starts = np.append(starts, new)
            failed = np.append(failed, -1)
        total = best[starts] + _cost(s1, s2, starts, t) + beta
        k = int(np.argmin(total))
        best[t], last[t] = total[k], starts[k]
        # Delayed pruning. A start s with F(s) + C(s, t) > F(t) can never be
        # the best start for an end t' with t' - t >= min_size (then t is an
        # admissible start for t', and the squared error does not grow by
        # splitting). For t < t' < t + min_size it still can, so it is kept
        # until then; pruning it at once loses the optimum when min_size > 2.
        failed = np.where((failed < 0) & (total - beta > best[t] + 1e-9), t, failed)
        keep = (failed < 0) | (t + 1 - failed < min_size)
        starts, failed = starts[keep], failed[keep]
    rows, t = [], n
    while t > 0:
        t = int(last[t])
        if t > 0:
            rows.append(t)
    return sorted(rows)


def gains(x, rows):
    """Cost saved by each change point against merging it with its
    neighbours' segments."""
    x = np.asarray(x, dtype=float)
    if x.ndim == 1:
        x = x[:, None]
    s1 = np.vstack([np.zeros(x.shape[1]), np.cumsum(x, axis=0)])
    s2 = np.concatenate([[0.0], np.cumsum(np.sum(x * x, axis=1))])
    edges = [0, *rows, len(x)]
    out = []
    for i in range(1, len(edges) - 1):
        a, b, c = edges[i - 1], edges[i], edges[i + 1]
        whole = _cost(s1, s2, np.array([a]), c)[0]
        parts = _cost(s1, s2, np.array([a]), b)[0] + _cost(s1, s2, np.array([b]), c)[0]
        out.append(float(max(0.0, whole - parts)))
    return out


def detect(
    times,
    x,
    *,
    names=None,
    penalty=PENALTY,
    min_seconds=MIN_SECONDS,
    max_per_minute=MAX_PER_MINUTE,
):
    """Change points of a series ``x`` (rows x features, already on
    comparable scales) recorded at ``times`` (seconds)."""
    times = np.asarray(times, dtype=float)
    x = np.asarray(x, dtype=float)
    if x.ndim == 1:
        x = x[:, None]
    n, d = x.shape
    names = list(names or [f"f{i}" for i in range(d)])
    finite_t = times[np.isfinite(times)]
    steps = np.diff(finite_t)
    steps = steps[steps > 0]
    dt = float(np.median(steps)) if len(steps) else 0.0
    min_size = max(2, math.ceil(min_seconds / dt - 1e-9)) if dt > 0 else 2
    empty = Result([], [], penalty, 0.0, min_size, names)
    if n < 2 * min_size or d == 0 or len(finite_t) < 2:
        return empty
    # The episode lasts from its first sample to one step past its last.
    minutes = max(float(finite_t[-1] - finite_t[0]) + dt, dt) / 60
    limit = max(1, math.floor(max_per_minute * minutes + 1e-9))
    beta = penalty * (d + 1) * math.log(n)
    rows = segment(x, beta, min_size)
    if len(rows) <= limit:
        return Result(rows, gains(x, rows), penalty, beta, min_size, names)
    # Over the cap: raise the penalty until the count fits. Equally strong
    # changes vanish together, so a raise can jump from too many to too few;
    # then the strongest ``limit`` of the last result over the cap are kept
    # (ties broken by time, earliest first).
    over = (rows, penalty, beta)
    effective = penalty
    for _ in range(MAX_RAISES):
        effective *= RAISE
        beta = effective * (d + 1) * math.log(n)
        rows = segment(x, beta, min_size)
        if len(rows) == limit:
            return Result(rows, gains(x, rows), effective, beta, min_size, names, True)
        if len(rows) < limit:
            break
        over = (rows, effective, beta)
    rows, effective, beta = over
    saved = gains(x, rows)
    top = max(saved) or 1.0
    order = sorted(range(len(rows)), key=lambda i: (-round(saved[i] / top, 9), rows[i]))
    rows = sorted(rows[i] for i in order[:limit])
    return Result(rows, gains(x, rows), effective, beta, min_size, names, True)


# --- features of one actor ----------------------------------------------------


def _row_speed(times, values, fps):
    """Speed per row (the step into it; row 0 takes the first step's)."""
    v = motion.speeds(times, values, fps)
    return np.concatenate([v[:1], v]) if len(v) else np.zeros(len(values))


def features(table, info, profile=None, stats=None):
    """``{actor: (names, matrix, sources)}``: each actor's scaled features --
    the speed of its position, its gripper level (measured before commanded)
    and the speed of its rotation -- one row per table row. ``sources`` are
    the channels each feature was read from (``SignalSource``)."""
    from ..agent.signals import _matrix
    from .contracts import SignalSource
    from .signal_profiles import grippers, infer, positions

    profile = profile or infer(info)
    times = table["timestamp"].to_numpy(dtype=float)
    fps = float(info.get("fps") or 0) or None
    if not fps and len(times) > 1:
        steps = np.diff(times)
        steps = steps[np.isfinite(steps) & (steps > 0)]
        fps = 1 / float(np.median(steps)) if len(steps) else 1.0
    cache = {}

    def column(feature):
        if feature not in cache:
            cache[feature] = (
                _matrix(table, feature) if feature in table.columns else None
            )
        return cache[feature]

    by_key = {c.key: c for c in profile.channels}

    def source(feature, index, kind):
        c = by_key.get((feature, index))
        return SignalSource(
            feature=feature,
            dimension=c.dimension if c else None,
            index=index,
            kind=kind,
            units=c.units if c else None,
            frame=c.frame if c else None,
        )

    out = {}
    actors = list(dict.fromkeys(c.actor_id for c in profile.channels))
    moved = positions(profile)
    turned = positions(profile, "rotation")
    for actor in actors:
        names, cols, sources = [], [], []
        if actor in moved:
            feature, dims = moved[actor]
            m = column(feature)
            if m is not None and max(dims) < m.shape[1]:
                f = scale(_row_speed(times, m[:, dims], fps))
                if f is not None:
                    names.append(f"{feature}.speed")
                    cols.append(f)
                    sources.append([source(feature, d, "derived") for d in dims])
        mine = [c for c in grippers(profile) if c.actor_id == actor]
        mine.sort(key=lambda c: c.kind != "measured")
        for c in mine:
            m = column(c.feature)
            if m is None or c.index >= m.shape[1]:
                continue
            f = scale(m[:, c.index])
            if f is not None:
                names.append(f"{c.feature}.{c.dimension or c.index}")
                cols.append(f)
                sources.append([source(c.feature, c.index, c.kind)])
                break
        if actor in turned:
            feature, dims = turned[actor]
            m = column(feature)
            if m is not None and max(dims) < m.shape[1]:
                angles = m[:, dims].astype(float)
                units = {by_key[(feature, d)].units for d in dims}
                if units == {"deg"}:
                    angles = np.deg2rad(angles)
                filled = [_fill(angles[:, i]) for i in range(angles.shape[1])]
                if all(a is not None for a in filled):
                    unwrapped = np.unwrap(np.stack(filled, axis=1), axis=0)
                    f = scale(_row_speed(times, unwrapped, fps))
                    if f is not None:
                        names.append(f"{feature}.angular_speed")
                        cols.append(f)
                        sources.append([source(feature, d, "derived") for d in dims])
        if cols:
            out[actor] = (names, np.stack(cols, axis=1), sources)
    return out


def candidates(
    table,
    info,
    profile=None,
    stats=None,
    episode_index=0,
    **params,
):
    """``[EventCandidate]`` of type ``change_point``, every actor, sorted by
    time. ``salience`` orders them by the cost each saves against the
    penalty (``gain / (gain + beta)``): an evidence priority, not a
    probability. ``params`` go to ``detect``."""
    from .contracts import EventCandidate, window_at

    times = table["timestamp"].to_numpy(dtype=float)
    out = []
    for actor, (names, matrix, sources) in features(
        table, info, profile, stats
    ).items():
        result = detect(times, matrix, names=names, **params)
        flat = [s for group in sources for s in group][:16]
        for k, (row, gain) in enumerate(zip(result.rows, result.gains, strict=True)):
            if not np.isfinite(times[row]):
                continue
            out.append(
                EventCandidate(
                    id=f"cp_{actor}_{k:03d}",
                    episode_index=episode_index,
                    event_type="change_point",
                    actor_id=actor,
                    center_time_s=float(times[row]),
                    time_window_s=window_at(times, row),
                    source_frame_index=int(row),
                    sources=flat,
                    salience=float(gain / (gain + result.beta))
                    if result.beta > 0
                    else 1.0,
                )
            )
    return sorted(out, key=lambda c: (c.center_time_s, c.actor_id))
