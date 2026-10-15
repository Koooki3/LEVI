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
- no segment is shorter than ``min_seconds`` (in rows, from the median
  sampling step);
- a series longer than ``MAX_ROWS`` is searched in blocks, so the time is
  bounded (the search is quadratic in the worst case). A block keeps its
  rows' count, sum and sum of squares (``Blocks``), so a run of blocks costs
  exactly its rows' squared error; ``n`` in the penalty is still the number
  of rows. The search then solves the unbinned problem with change points
  restricted to block starts (tested against unpruned partitioning), and
  the minimum length holds in rows;
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
PENALTY = 0.75
MIN_SECONDS = 0.5
MAX_PER_MINUTE = 30.0
# A longer series is averaged over blocks of rows to at most this many
# before the search (which is quadratic in the worst case).
MAX_ROWS = 3000
# Raise the penalty by this factor, at most this many times, to meet the cap.
RAISE, MAX_RAISES = 1.5, 30
# How change points are found, as an on plan freezes it
# (``candidates.algorithm``). Raise it with any change to the search, its
# cost or its blocks that the constants above do not show. v2 (2026-10-15):
# blocks keep their sufficient statistics and the penalty counts rows (v1
# scaled block means by the root of their length and counted blocks).
VERSION = "levi.events.change_points.v2"


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
    # Rows per block the search ran on (1: every row); change points then
    # fall on block starts. Gains are in row units either way, and
    # ``min_size`` is in rows (a multiple of ``bin_rows`` when binned).
    bin_rows: int = 1


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


@dataclass(frozen=True)
class Blocks:
    """Consecutive blocks of rows, each kept as its sufficient statistics:
    the first row (``starts``), the number of rows ``W`` (``counts``), their
    sum ``S`` (``sums``, one per feature) and the sum of their squared norms
    ``Q`` (``squares``). The squared error of the rows of blocks ``[a, c)``
    about their mean is ``sum(Q) - |sum(S)|^2 / sum(W)``: exactly the rows'
    own, whatever the block lengths."""

    starts: np.ndarray
    counts: np.ndarray
    sums: np.ndarray
    squares: np.ndarray


def _rows(x):
    x = np.asarray(x, dtype=float)
    return x[:, None] if x.ndim == 1 else x


def blocks(x, starts):
    """``x`` in blocks beginning at the rows ``starts``: the first is 0, they
    strictly increase and every one is a row of ``x`` (so no block is
    empty). Anything else raises ``ValueError``."""
    x = _rows(x)
    starts = np.asarray(starts, dtype=int)
    if (
        starts.ndim != 1
        or len(starts) == 0
        or starts[0] != 0
        or np.any(np.diff(starts) <= 0)
        or starts[-1] >= len(x)
    ):
        raise ValueError(
            "block starts must begin at 0, strictly increase and lie inside "
            f"the {len(x)} rows; got {starts.tolist()}"
        )
    return Blocks(
        starts=starts,
        counts=np.diff(np.append(starts, len(x))).astype(float),
        sums=np.add.reduceat(x, starts, axis=0),
        squares=np.add.reduceat(np.sum(x * x, axis=1), starts),
    )


def binned(x, rows):
    """``x`` in blocks of ``rows`` rows; the last block takes the rows left
    over, so it has ``rows`` to ``2 * rows - 1`` (every block has at least
    ``rows``, which keeps a minimum segment length in rows). ``x`` must have
    at least ``rows`` rows (``ValueError`` otherwise)."""
    x = _rows(x)
    rows = max(1, int(rows))
    if len(x) < rows:
        raise ValueError(f"binned needs at least {rows} rows, got {len(x)}")
    return blocks(x, np.arange(len(x) // rows) * rows)


def _cumulative(b):
    """Cumulative ``W``, ``S`` and ``Q`` (a leading zero), for costs of runs."""
    return (
        np.concatenate([[0.0], np.cumsum(b.counts)]),
        np.vstack([np.zeros(b.sums.shape[1]), np.cumsum(b.sums, axis=0)]),
        np.concatenate([[0.0], np.cumsum(b.squares)]),
    )


def _cost(acc, starts, end):
    """Squared error of the mean model on blocks ``[start, end)`` for each
    start, from cumulative statistics."""
    w, s1, s2 = acc
    m = w[end] - w[starts]
    diff = s1[end] - s1[starts]
    return s2[end] - s2[starts] - np.sum(diff * diff, axis=1) / m


def cost(b, a, c):
    """Squared error of the rows in blocks ``[a, c)`` of ``b``."""
    return float(_cost(_cumulative(b), np.array([a]), c)[0])


def _each_row(x):
    x = _rows(x)
    return blocks(x, np.arange(len(x)))


def segment(x, beta, min_size):
    """Rows where a new segment starts (not 0) minimising the squared error
    plus ``beta`` per change, no segment shorter than ``min_size`` rows."""
    return search(_each_row(x), beta, min_size)


def search(b, beta, min_size):
    """Blocks of ``b`` where a new segment starts (not 0) minimising the
    squared error of the rows plus ``beta`` per change, no segment shorter
    than ``min_size`` blocks. One row per block is ``segment``."""
    n = len(b.starts)
    min_size = max(1, int(min_size))
    if n < 2 * min_size:
        return []
    acc = _cumulative(b)
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
        total = best[starts] + _cost(acc, starts, t) + beta
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
    return _gains(_each_row(x), rows)


def _gains(b, at):
    """``gains`` of the change points at blocks ``at`` of ``b`` (row units)."""
    acc = _cumulative(b)
    edges = [0, *at, len(b.starts)]
    out = []
    for i in range(1, len(edges) - 1):
        a, m, c = edges[i - 1], edges[i], edges[i + 1]
        whole = _cost(acc, np.array([a]), c)[0]
        parts = _cost(acc, np.array([a]), m)[0] + _cost(acc, np.array([m]), c)[0]
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
    comparable scales) recorded at ``times`` (seconds). A series longer than
    ``MAX_ROWS`` is searched in blocks (``binned``); ``bin_rows`` says so."""
    times = np.asarray(times, dtype=float)
    x = _rows(x)
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
    # Blocks of ``bin_rows`` rows (the last takes the rest). Each keeps its
    # rows' sufficient statistics, so the search minimises the rows' own
    # squared error with change points restricted to block starts; the
    # penalty counts rows, so it is the same objective as unbinned. A
    # segment of ``min_blocks`` blocks has at least ``min_size`` rows.
    bin_rows = max(1, math.ceil(n / MAX_ROWS))
    b = binned(x, bin_rows)
    min_blocks = max(1, math.ceil(min_size / bin_rows))
    min_size = max(min_size, min_blocks * bin_rows)
    # The episode lasts from its first sample to one step past its last.
    minutes = max(float(finite_t[-1] - finite_t[0]) + dt, dt) / 60
    limit = max(1, math.floor(max_per_minute * minutes + 1e-9))

    def found(at, effective, beta, capped=False):
        return Result(
            [int(b.starts[i]) for i in at],
            _gains(b, at),
            effective,
            beta,
            min_size,
            names,
            capped,
            bin_rows,
        )

    beta = penalty * (d + 1) * math.log(n)
    rows = search(b, beta, min_blocks)
    if len(rows) <= limit:
        return found(rows, penalty, beta)
    # Over the cap: raise the penalty until the count fits. Equally strong
    # changes vanish together, so a raise can jump from too many to too few;
    # then the strongest ``limit`` of the last result over the cap are kept
    # (ties broken by time, earliest first).
    over = (rows, penalty, beta)
    effective = penalty
    for _ in range(MAX_RAISES):
        effective *= RAISE
        beta = effective * (d + 1) * math.log(n)
        rows = search(b, beta, min_blocks)
        if len(rows) == limit:
            return found(rows, effective, beta, True)
        if len(rows) < limit:
            break
        over = (rows, effective, beta)
    rows, effective, beta = over
    saved = _gains(b, rows)
    top = max(saved) or 1.0
    order = sorted(range(len(rows)), key=lambda i: (-round(saved[i] / top, 9), rows[i]))
    rows = sorted(rows[i] for i in order[:limit])
    return found(rows, effective, beta, True)


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
