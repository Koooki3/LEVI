"""Clip proposer: cut one whole recorded episode into clips of 1 to 12 seconds.

The data-processing description of VPP2 (arXiv:2610.10270, Sections 2 and 3.1
and Appendix A, as read in v1) cuts robot and human-hand video into clips of
1 to 12 seconds at natural turning points. It names two cheap physical cues:
local minima of the end-effector (or hand) velocity, and the moments the
gripper (or fingers) opens or closes. A vision-language model then merges
neighbouring clips and moves their ends (not done here). This module is that
first, model-free step for a LEVI episode.

**What comes from the paper and what is ours.** From the paper: the numbers of
the clip range (``min_s = 1``, ``max_s = 12``) and the two cues. In the paper
the range describes the clips left after a VLM merged neighbouring cue-based
clips; making it a hard limit on the cue-based clips of this step (P1) is our
reading (``PARAM_NOTES``), and a P2 merge that comes later must enforce
``max_s`` again, since merging can make a clip longer. Everything else is
**our choice (ours)**, set by hand and tuned on no gold set; the paper does
not give it, and none of it reproduces the paper: how a velocity minimum is
found (smoothing window, how deep a dip must be, how low it must go, how far
apart two cues must be), where the boundary sits, and what happens to clips
that come out too long or too short. Every such parameter is a field of
``ClipParams`` with its reason.

``propose_clips(signals, params)`` takes the whole episode (the proposer is
offline: it may look at the future), checks it (``ClipInputError`` names the
code and the cause; nothing is repaired or skipped silently), and returns
clips that cover it from first to last timestamp without a gap or overlap.
Every clip is within ``[min_s, max_s]`` (to 1e-9 s), except a lone
``too_short_episode`` clip when the whole episode is shorter than ``min_s``.
It is deterministic: no randomness, no dependence on dictionary order, ties
broken by time.

Steps, in order:

1. **Cues.** Gripper crossings (``boundary_gripper``): the kernel
   ``events.gripper`` with hysteresis on the episode's own range (or on the
   given ``gripper_bounds``), the time of the crossing. Speed minima
   (``boundary_speed_min``): the speed of the end-effector position is the
   path length over a centred window of ``smooth_s`` divided by the window
   (a time-weighted mean, so uneven sampling is not a jump); an interior
   local minimum counts when it is at most ``max_level`` of the episode's
   fast speed (95th percentile of that speed, as ``events.motion`` uses; an
   episode whose fast speed is below ``min_fast_speed`` has no speed cues) and
   its prominence (how far the speed must rise on both sides, the lower side
   counts, before it meets a lower point) is at least ``min_prominence`` of
   it. The cue sits at the middle of the dip's half-prominence valley, so a
   long pause is cut in its middle, not at a noise ripple. Cues closer than
   ``min_s`` to either end are dropped (they could only make a short clip).
   Of the speed cues, the most prominent come first and one within
   ``min_gap_s`` of a kept cue is dropped; gripper cues are never dropped
   here and win over speed cues.
2. **Merge short** (``merge_short``). The shortest clip below ``min_s`` loses
   one of its two boundaries: the weaker one (any speed cue before any
   gripper cue, the less prominent speed cue first), then the one whose merge
   stays within ``max_s``, then the shorter merge, then the earlier. Greedy,
   not an optimum.
3. **Split long** (``split_long``, ``split_long_even``). A clip over ``max_s``
   is cut into the fewest pieces ``k = ceil(length / max_s)``, one cut at a
   time from the left. A cut must leave a rest that ``k - 1`` pieces can
   still fill within ``[min_s, max_s]``; inside that range it goes at the
   most prominent speed minimum that passed the cue test of step 1 (and so
   was dropped there, or merged away, or lies in a long clip because
   ``min_gap_s`` held back its neighbour) within ``split_window`` of the even
   cut point (``split_long``), else at the even cut point
   (``split_long_even``): a ripple that is no cue is no reason to move the
   cut. With ``max_s >= 2 * min_s`` such a cut always exists, so no short
   clip is made.

What it does not do: refine or move a boundary by looking at pictures,
describe a clip, trim an idle start or end, use rotation, or judge whether a
cue is a subtask boundary (a cue is a place to look, not a boundary by
itself). A gripper crossing keeps its boundary unless it lies within
``min_s`` of an end or of another gripper crossing (then the length limit
wins and ``dropped`` says which cue went and why).

**Reusable by other projects.** Input and output are plain data: a
``ClipSignals`` of arrays in, a ``ClipProposal`` out whose ``as_dict()`` is
JSON with ``schema_version`` ``levi.clips.v1``, ``algorithm_version``, the
parameters, their SHA-256 and where each comes from (``PARAM_SOURCES``);
``ClipProposal.from_dict`` reads it back and checks the hash. Nothing is bound
to a dataset, a camera or a robot: the caller declares which columns are the
position and the gripper (``signals_from_table``). The core (``propose_clips``
and everything it calls) imports only numpy and reads no file; a caller that
has files, or a robot loop, writes its own adapter.

**Judging it.** ``evaluate`` scores the proposed boundaries and clips against
reference boundaries with ``boundary_metrics`` and, given a ``baseline``
(``candidates_baseline()``: the kept candidates of ``levi.events.candidates``),
scores the baseline on the same reference, episodes and tolerances, all of
its candidates and the first n of them where the proposer made n.
``evaluate_gold`` reads gold folders only through ``calibrate.Guard`` (it
refuses frozen, held-out and excluded sets) and is the one function here that
reads files, through ``calibrate``. No model, no Torch.
"""

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from itertools import pairwise
from typing import Any

import numpy as np

from . import boundary_metrics
from . import gripper as gripper_kernel

# Bump with any change to how clips are found that the parameters do not show.
VERSION = "levi.events.clips.v1"
# The schema name of ``ClipProposal.as_dict()``.
SCHEMA = "levi.clips.v1"

# Reason codes. A boundary has one of the first four; a clip's start and end
# are a boundary reason or an episode edge; clips and dropped cues add the
# rest.
BOUNDARY_GRIPPER = "boundary_gripper"
BOUNDARY_SPEED_MIN = "boundary_speed_min"
SPLIT_LONG = "split_long"
SPLIT_LONG_EVEN = "split_long_even"
EPISODE_START = "episode_start"
EPISODE_END = "episode_end"
MERGE_SHORT = "merge_short"
TOO_SHORT_EPISODE = "too_short_episode"
# Why a cue found in step 1 is not a boundary (``Dropped.why``).
NEAR_EDGE = "near_edge"
MIN_GAP = "min_gap"
BOUNDARY_REASONS = (BOUNDARY_GRIPPER, BOUNDARY_SPEED_MIN, SPLIT_LONG, SPLIT_LONG_EVEN)
CLIP_FLAGS = (MERGE_SHORT, TOO_SHORT_EPISODE)
DROPPED_WHY = (NEAR_EDGE, MIN_GAP, MERGE_SHORT)

# Float slack on lengths and distances, in seconds.
EPS = 1e-9
# Metrics default (seconds): the tolerances the proposer is judged at. A clip
# boundary is a place for a later pass to look at, so they are wider than the
# 0.1/0.2 s of ``boundary_metrics`` (ours, as the rest).
TOLERANCES = (0.2, 0.5, 1.0)


class ClipInputError(ValueError):
    """The input or the parameters cannot be turned into clips. ``code`` is
    stable (``empty``, ``too_few_samples``, ``not_numeric``, ``shape_mismatch``,
    ``non_finite``, ``non_monotonic``, ``no_signal``, ``bad_bounds``,
    ``bad_table``, ``bad_params``, ``bad_record``); the message names the
    cause and where."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _number(name, value):
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ClipInputError("bad_params", f"{name} must be a number, got {value!r}")
    if not math.isfinite(value):
        raise ClipInputError("bad_params", f"{name} must be finite, got {value!r}")
    return float(value)


# Where each parameter of ``ClipParams`` comes from: ``paper`` (the VPP2 text
# gives it) or ``ours`` (we chose it; not in the paper, tuned on no gold set).
# Output carries this, so a result cannot be read as a reproduction of more
# than the paper says.
PARAM_SOURCES = {
    "min_s": "paper",
    "max_s": "paper",
    "smooth_s": "ours",
    "min_prominence": "ours",
    "max_level": "ours",
    "min_gap_s": "ours",
    "split_window": "ours",
    "gripper_min_travel": "ours",
    "gripper_min_travel_abs": "ours",
    "min_fast_speed": "ours",
}


# What "paper" does and does not cover. The paper (Section 2) cuts semantic
# clips first, finds candidate boundaries with cheap cues, and only then has a
# VLM merge neighbouring clips: "lasting 1-12 seconds" describes the clips
# after that merge. Making it a hard limit on the cue-based clips of this step
# (P1) is LEVI's reading; and a later merge (P2) can make a clip longer than
# max_s again, so whoever merges must enforce max_s once more.
PARAM_NOTES = {
    "min_s": "number from the paper; enforced hard in P1 by LEVI (our choice)",
    "max_s": (
        "number from the paper; enforced hard in P1 by LEVI (our choice); a "
        "P2 merge must enforce it again"
    ),
}


@dataclass(frozen=True)
class ClipParams:
    """Every parameter, with where it comes from. **Paper**: given by the VPP2
    text. **Ours**: chosen here by reasoning, tuned on no gold set; each has
    its reason and none reproduces the paper."""

    # Paper (Section 2: "clips lasting 1-12 seconds").
    min_s: float = 1.0
    max_s: float = 12.0
    # Ours. Smoothing window of the speed, in seconds: long enough to span
    # 5 to 15 samples at 10 to 30 Hz and so not follow one noisy step, shorter
    # than a real pause. The package already treats half a second as the
    # shortest meaningful state (``motion.STILL_SECONDS``,
    # ``change_points.MIN_SECONDS``).
    smooth_s: float = 0.5
    # Ours. A minimum counts only if the smoothed speed rises on both sides by
    # at least this share of the episode's fast speed (the 95th percentile):
    # a 20% ripple on a fast stroke is not a stop. Scale-free, so no unit.
    min_prominence: float = 0.2
    # Ours. ... and the minimum itself is at most this share of the fast
    # speed: the arm slows to half or less, rather than easing off a little.
    max_level: float = 0.5
    # Ours. Two speed cues closer than this keep only the more prominent: a
    # slowdown is the weakest of the cues, and two within 2 s are more likely
    # one pause with a ripple than two sub-steps. Gripper cues are exempt.
    min_gap_s: float = 2.0
    # Ours. A long clip is cut at the most prominent speed cue within this
    # share of a piece around the even cut point; a wide window would trade
    # the balance of the pieces for a somewhat deeper dip.
    split_window: float = 0.25
    # The kernel's own rule as ``facts.read`` uses it: with a given gripper
    # range, a channel that moves less than this share of it has no events.
    gripper_min_travel: float = 0.2
    # Ours. Without a given range the channel is read on its own, and any
    # jitter then spans the whole [0, 1]: a channel that moves less than this
    # in its own units (1 mm for a width in metres; 0.001 of a 0..1 command)
    # has no events. Used only when no ``gripper_bounds`` is given; with them
    # ``gripper_min_travel`` applies. Set it for another unit (a 0..255 count
    # needs a larger value).
    gripper_min_travel_abs: float = 1e-3
    # Ours. The fast speed (95th percentile) is the scale of the dip rules, so
    # an episode that barely moves would take its position noise for motion:
    # the path length of white noise grows with the sampling rate (about 2e-2
    # per second for sigma 2e-4 m at 30 Hz, three axes). With a fast speed
    # below this, in position units per second (2 cm/s for metres, an arm
    # that is essentially idle), there are no speed cues. Set it for another
    # unit.
    min_fast_speed: float = 0.03

    def __post_init__(self):
        values = {name: _number(name, getattr(self, name)) for name in PARAM_SOURCES}
        # Stored as floats, so ClipParams(min_s=1) and ClipParams(min_s=1.0)
        # are equal and have one hash.
        for name, value in values.items():
            object.__setattr__(self, name, value)
        rules = [
            (values["min_s"] > 0, "min_s must be positive"),
            (
                values["max_s"] >= 2 * values["min_s"],
                (
                    "max_s must be at least 2 * min_s (otherwise some lengths, "
                    "say 9 s between 5 and 8 s, have no cut into clips of the range)"
                ),
            ),
            (values["smooth_s"] > 0, "smooth_s must be positive"),
            (values["min_prominence"] > 0, "min_prominence must be positive"),
            (values["max_level"] > 0, "max_level must be positive"),
            (values["min_gap_s"] >= 0, "min_gap_s must not be negative"),
            (0 <= values["split_window"] <= 1, "split_window must be within [0, 1]"),
            (
                0 <= values["gripper_min_travel"] <= 1,
                "gripper_min_travel must be within [0, 1]",
            ),
            (values["min_fast_speed"] >= 0, "min_fast_speed must not be negative"),
            (
                values["gripper_min_travel_abs"] >= 0,
                "gripper_min_travel_abs must not be negative",
            ),
        ]
        for ok, message in rules:
            if not ok:
                raise ClipInputError("bad_params", message)

    def as_dict(self) -> dict:
        return asdict(self)

    def sha256(self) -> str:
        """SHA-256 of the parameters' canonical JSON: any change to a value
        changes it."""
        canonical = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, eq=False)
class ClipSignals:
    """One episode's whole signals. ``times``: seconds, strictly increasing.
    ``positions``: end-effector position per sample, shape ``(n, d)`` (xyz,
    any length unit: only the shape of the speed matters). ``gripper``: one
    gripper channel, shape ``(n,)``, either end open. ``gripper_bounds``: the
    channel's (min, max) when known (a declared or dataset range); without it
    the channel is read on the episode's own range. At least one of
    ``positions`` and ``gripper`` is needed."""

    times: Any
    positions: Any = None
    gripper: Any = None
    gripper_bounds: tuple[float, float] | None = None


@dataclass(frozen=True)
class Clip:
    """One proposed clip. ``start_reason`` / ``end_reason``: the reason of the
    boundary there (``boundary_gripper``, ``boundary_speed_min``,
    ``split_long``, ``split_long_even``) or ``episode_start`` /
    ``episode_end``. ``flags``: ``merge_short`` (a boundary inside it was
    dropped to make it long enough) and ``too_short_episode`` (the whole
    episode is shorter than ``min_s``)."""

    start_s: float
    end_s: float
    start_reason: str
    end_reason: str
    flags: tuple[str, ...] = ()

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    @property
    def reasons(self) -> tuple[str, ...]:
        """Every reason code that applies, start first, without repeats."""
        return tuple(dict.fromkeys([self.start_reason, self.end_reason, *self.flags]))


@dataclass(frozen=True)
class Boundary:
    time_s: float
    reason: str


@dataclass(frozen=True)
class Dropped:
    """A cue found in step 1 or a boundary removed in step 2 that is not a
    boundary of the result. ``reason`` is what it was; ``why`` is one of
    ``near_edge``, ``min_gap``, ``merge_short``."""

    time_s: float
    reason: str
    why: str


@dataclass(frozen=True)
class ClipProposal:
    clips: tuple[Clip, ...]
    dropped: tuple[Dropped, ...]
    params: ClipParams
    algorithm_version: str = VERSION

    @property
    def boundaries(self) -> tuple[Boundary, ...]:
        """The inner boundaries: every clip start but the first."""
        return tuple(Boundary(c.start_s, c.start_reason) for c in self.clips[1:])

    def as_dict(self) -> dict:
        """The proposal as JSON-able data (``levi.clips.v1``). It carries the
        algorithm version, the parameters, their hash and their sources."""
        return {
            "schema_version": SCHEMA,
            "algorithm_version": self.algorithm_version,
            "params": self.params.as_dict(),
            "params_sha256": self.params.sha256(),
            "param_sources": dict(PARAM_SOURCES),
            "param_notes": dict(PARAM_NOTES),
            "clips": [
                {
                    "start_s": c.start_s,
                    "end_s": c.end_s,
                    "duration_s": c.duration_s,
                    "start_reason": c.start_reason,
                    "end_reason": c.end_reason,
                    "flags": list(c.flags),
                    "reasons": list(c.reasons),
                }
                for c in self.clips
            ],
            "dropped": [asdict(d) for d in self.dropped],
        }

    @classmethod
    def from_dict(cls, data):
        """Read ``as_dict()`` back. Refuses (``ClipInputError``, code
        ``bad_record``) another schema, parameters that do not match their
        hash, and clips that do not follow one another."""
        try:
            if data["schema_version"] != SCHEMA:
                raise ValueError(f"schema_version is {data['schema_version']!r}")
            params = ClipParams(**data["params"])
            if params.sha256() != data["params_sha256"]:
                raise ValueError("params do not match params_sha256")
            clips = tuple(
                Clip(
                    float(c["start_s"]),
                    float(c["end_s"]),
                    c["start_reason"],
                    c["end_reason"],
                    tuple(c["flags"]),
                )
                for c in data["clips"]
            )
            dropped = tuple(
                Dropped(float(d["time_s"]), d["reason"], d["why"])
                for d in data["dropped"]
            )
            version = str(data["algorithm_version"])
        except ClipInputError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise ClipInputError(
                "bad_record", f"not a {SCHEMA} record: {exc!r}"
            ) from exc
        if (
            not clips
            or not all(c.start_s < c.end_s for c in clips)
            or any(a.end_s != b.start_s for a, b in pairwise(clips))
        ):
            raise ClipInputError(
                "bad_record",
                "clips must be non-empty, have start < end and follow one another",
            )
        return cls(clips, dropped, params, version)


@dataclass
class _Cue:
    time_s: float
    reason: str
    # Order of removal in step 2: lower goes first. A gripper cue is above any
    # speed cue; a speed cue's strength is its prominence in units of the fast
    # speed.
    strength: float


def _array(name, value, ndim):
    try:
        array = np.asarray(value, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ClipInputError(
            "not_numeric", f"{name} is not a numeric array ({exc})"
        ) from exc
    if array.ndim != ndim:
        raise ClipInputError(
            "shape_mismatch", f"{name} must have {ndim} dimension(s), got shape "
            f"{array.shape}"
        )  # fmt: skip
    return array


def _finite(name, array, times=None):
    bad = np.flatnonzero(~np.isfinite(array.reshape(len(array), -1)).all(axis=1))
    if len(bad):
        row = int(bad[0])
        at = f" (time {times[row]:g} s)" if times is not None else ""
        raise ClipInputError(
            "non_finite",
            f"{name} has {len(bad)} non-finite row(s); the first is row {row}{at}. "
            "Fix or drop corrupt rows before proposing clips (VPP2 step P0).",
        )


def _validate(signals):
    """``(times, positions | None, gripper | None, bounds | None)`` as
    arrays, or a ``ClipInputError`` that says why not."""
    times = _array("times", signals.times, 1)
    if len(times) == 0:
        raise ClipInputError("empty", "times is empty: there is no episode")
    if len(times) < 2:
        raise ClipInputError(
            "too_few_samples", "one sample has no duration; need at least 2 times"
        )
    _finite("times", times)
    steps = np.diff(times)
    bad = np.flatnonzero(steps <= 0)
    if len(bad):
        row = int(bad[0])
        raise ClipInputError(
            "non_monotonic",
            f"times must strictly increase; times[{row + 1}] = {times[row + 1]:g} "
            f"is not after times[{row}] = {times[row]:g} ({len(bad)} such step(s); "
            "sort the rows and drop repeated timestamps)",
        )
    positions = gripper = bounds = None
    if signals.positions is not None:
        positions = _array("positions", signals.positions, 2)
        if len(positions) != len(times) or positions.shape[1] < 1:
            raise ClipInputError(
                "shape_mismatch",
                f"positions has shape {positions.shape} for {len(times)} times; "
                "need (n, d) with d >= 1",
            )
        _finite("positions", positions, times)
    if signals.gripper is not None:
        gripper = _array("gripper", signals.gripper, 1)
        if len(gripper) != len(times):
            raise ClipInputError(
                "shape_mismatch",
                f"gripper has {len(gripper)} samples for {len(times)} times",
            )
        _finite("gripper", gripper, times)
    if positions is None and gripper is None:
        raise ClipInputError(
            "no_signal", "need positions or a gripper channel; got neither"
        )
    if signals.gripper_bounds is not None:
        try:
            bounds = tuple(float(v) for v in signals.gripper_bounds)
        except (TypeError, ValueError) as exc:
            raise ClipInputError("bad_bounds", f"gripper_bounds: {exc}") from exc
        if len(bounds) != 2 or not np.isfinite(bounds).all() or bounds[1] <= bounds[0]:
            raise ClipInputError(
                "bad_bounds", f"gripper_bounds must be finite (min, max), min < max; "
                f"got {signals.gripper_bounds!r}"
            )  # fmt: skip
    return times, positions, gripper, bounds


def smoothed_speed(times, positions, smooth_s):
    """The speed of ``positions`` at every sample: path length over the
    window ``[t - smooth_s / 2, t + smooth_s / 2]`` (cut at the ends of the
    episode) divided by the window's length. ``times`` strictly increasing."""
    step = np.linalg.norm(np.diff(positions, axis=0), axis=1)
    path = np.concatenate([[0.0], np.cumsum(step)])
    half = smooth_s / 2.0
    lo = np.maximum(times - half, times[0])
    hi = np.minimum(times + half, times[-1])
    return (np.interp(hi, times, path) - np.interp(lo, times, path)) / (hi - lo)


def interior_minima(values):
    """Indices of the local minima of ``values`` that are strictly lower than
    both neighbours, a run of equal values counting once (at its middle).
    A run that touches either end is not a minimum: the end of the episode is
    not a turning point."""
    out, n, i = [], len(values), 0
    while i < n:
        j = i
        while j + 1 < n and values[j + 1] == values[i]:
            j += 1
        if 0 < i and j < n - 1 and values[i - 1] > values[i] < values[j + 1]:
            out.append((i + j) // 2)
        i = j + 1
    return out


def _sparse_max(values):
    table, k = [values], 1
    while 2 * k <= len(values):
        table.append(np.maximum(table[-1][:-k], table[-1][k:]))
        k *= 2
    return table


def _range_max(table, lo, hi):
    """Maximum of the values at ``lo..hi`` inclusive."""
    level = (hi - lo + 1).bit_length() - 1
    return max(table[level][lo], table[level][hi - (1 << level) + 1])


def prominences(values, indices):
    """Prominence of the minimum at each index: the lower of the two highest
    points reached on each side before the series meets a strictly lower
    point (or its end), minus the value there."""
    series = np.asarray(values, dtype=float)
    n, listed = len(series), series.tolist()
    left, right, stack = [-1] * n, [n] * n, []
    for i in range(n):
        while stack and listed[stack[-1]] >= listed[i]:
            stack.pop()
        left[i] = stack[-1] if stack else -1
        stack.append(i)
    stack = []
    for i in range(n - 1, -1, -1):
        while stack and listed[stack[-1]] >= listed[i]:
            stack.pop()
        right[i] = stack[-1] if stack else n
        stack.append(i)
    table = _sparse_max(series)
    return [
        float(
            min(_range_max(table, left[i] + 1, i), _range_max(table, i, right[i] - 1))
            - series[i]
        )
        for i in indices
    ]


def _speed_cues(times, speed, params):
    fast = float(np.percentile(speed, 95))
    if not fast > max(EPS, params.min_fast_speed):
        return []
    found = [i for i in interior_minima(speed) if speed[i] <= params.max_level * fast]
    out = []
    for i, prominence in zip(found, prominences(speed, found), strict=True):
        if prominence < params.min_prominence * fast:
            continue
        # The valley at half the prominence: its middle is the cue.
        level = speed[i] + prominence / 2
        a = b = i
        while a > 0 and speed[a - 1] <= level:
            a -= 1
        while b < len(speed) - 1 and speed[b + 1] <= level:
            b += 1
        out.append(
            _Cue(
                float((times[a] + times[b]) / 2),
                BOUNDARY_SPEED_MIN,
                prominence / fast,
            )
        )
    return out


def _gripper_cues(times, values, bounds, params):
    if bounds is None:
        # No declared range: the channel is scaled to its own, so jitter would
        # look like a full open and close. Below the absolute floor it is not
        # a gripper that moved.
        finite = np.asarray(values, dtype=float)
        finite = finite[np.isfinite(finite)]
        if len(finite) and np.ptp(finite) < params.gripper_min_travel_abs:
            return []
    found = gripper_kernel.read(
        values,
        bounds,
        # A given range is the scale of the hysteresis; without one, the
        # episode's own.
        range_source="dataset" if bounds is not None else "episode",
        open_level="auto",
        min_travel=params.gripper_min_travel,
    )
    if found is None:
        return []
    return [
        _Cue(float(times[row]), BOUNDARY_GRIPPER, math.inf)
        for row, _ in found.crossings
    ]


def _merge_short(start, end, cues, params):
    """Step 2. ``(kept cues, removed cues)``."""
    cues, removed = list(cues), []
    while cues:
        edges = [start, *(c.time_s for c in cues), end]
        lengths = [b - a for a, b in pairwise(edges)]
        j = min(range(len(lengths)), key=lambda i: (lengths[i], i))
        if lengths[j] >= params.min_s - EPS:
            break
        options = []
        if j > 0:
            options.append((j - 1, lengths[j - 1] + lengths[j]))
        if j < len(cues):
            options.append((j, lengths[j] + lengths[j + 1]))
        drop, _ = min(
            options,
            key=lambda o: (cues[o[0]].strength, o[1] > params.max_s + EPS, o[1], o[0]),
        )
        removed.append(cues.pop(drop))
    return cues, removed


def _split_long(start, end, cues, params, candidates):
    """Step 3: the cues plus the cuts that make every clip at most ``max_s``.
    ``candidates``: every speed minimum that passed the cue test, whether or
    not it became a boundary."""
    edges = [start, *(c.time_s for c in cues), end]
    cuts = []
    for a, b in pairwise(edges):
        while b - a > params.max_s + EPS:
            length = b - a
            pieces = max(2, math.ceil((length - EPS) / params.max_s))
            even = length / pieces
            # The first cut must leave a rest that pieces - 1 clips can fill.
            lo = max(a + params.min_s, b - (pieces - 1) * params.max_s)
            hi = min(a + params.max_s, b - (pieces - 1) * params.min_s)
            ideal = a + even
            if lo > hi:  # float rounding at a limit
                lo = hi = ideal
            reach = params.split_window * even
            near = [
                c
                for c in candidates
                if lo <= c.time_s <= hi and abs(c.time_s - ideal) <= reach
            ]
            if near:
                best = max(
                    near,
                    key=lambda c: (c.strength, -abs(c.time_s - ideal), -c.time_s),
                )
                cut, reason = best.time_s, SPLIT_LONG
            else:
                cut, reason = ideal, SPLIT_LONG_EVEN
            cuts.append(_Cue(cut, reason, 0.0))
            a = cut
    return sorted([*cues, *cuts], key=lambda c: c.time_s)


def propose_clips(signals: ClipSignals, params: ClipParams | None = None):
    """Clips covering the episode, from its first to its last timestamp. See
    the module docstring for the steps and ``ClipInputError`` for what is
    refused."""
    params = params or ClipParams()
    times, positions, gripper_values, bounds = _validate(signals)
    start, end = float(times[0]), float(times[-1])
    if end - start < params.min_s - EPS:
        return ClipProposal(
            (Clip(start, end, EPISODE_START, EPISODE_END, (TOO_SHORT_EPISODE,)),),
            (),
            params,
        )
    found = []
    if positions is not None:
        speed = smoothed_speed(times, positions, params.smooth_s)
        found = _speed_cues(times, speed, params)
    cues = list(found)
    if gripper_values is not None:
        cues += _gripper_cues(times, gripper_values, bounds, params)

    dropped, kept = [], []
    for cue in cues:
        if (
            cue.time_s - start < params.min_s - EPS
            or end - cue.time_s < params.min_s - EPS
        ):
            dropped.append(Dropped(cue.time_s, cue.reason, NEAR_EDGE))
        else:
            kept.append(cue)
    accepted = [c for c in kept if c.reason == BOUNDARY_GRIPPER]
    speed_cues = [c for c in kept if c.reason == BOUNDARY_SPEED_MIN]
    for cue in sorted(speed_cues, key=lambda c: (-c.strength, c.time_s)):
        if all(abs(cue.time_s - a.time_s) >= params.min_gap_s - EPS for a in accepted):
            accepted.append(cue)
        else:
            dropped.append(Dropped(cue.time_s, cue.reason, MIN_GAP))
    accepted.sort(key=lambda c: c.time_s)

    merged, removed = _merge_short(start, end, accepted, params)
    dropped += [Dropped(c.time_s, c.reason, MERGE_SHORT) for c in removed]
    final = _split_long(start, end, merged, params, found)

    edges = [start, *(c.time_s for c in final), end]
    starts = [EPISODE_START, *(c.reason for c in final)]
    ends = [*(c.reason for c in final), EPISODE_END]
    merged_at = [c.time_s for c in removed]
    clips = tuple(
        Clip(
            edges[i],
            edges[i + 1],
            starts[i],
            ends[i],
            (MERGE_SHORT,)
            if any(edges[i] < t < edges[i + 1] for t in merged_at)
            else (),
        )
        for i in range(len(edges) - 1)
    )
    dropped.sort(key=lambda d: (d.time_s, d.why, d.reason))
    return ClipProposal(clips, tuple(dropped), params)


def signals_from_table(table, feature, position, gripper_index, gripper_bounds=None):
    """``ClipSignals`` of a table with a ``timestamp`` column and one state
    vector column ``feature`` (a LeRobot-style table): ``position`` are the
    indices of the end-effector position in the vector, ``gripper_index`` the
    gripper channel. The caller declares them; nothing is inferred from
    names. Works on anything indexed like a pandas DataFrame, and imports
    nothing."""
    try:
        times = table["timestamp"].to_numpy(dtype=float)
        state = np.asarray(list(table[feature]), dtype=float)
        if state.ndim != 2:
            raise ValueError(f"{feature} rows are not equal-length vectors")
        positions = state[:, list(position)]
        values = state[:, gripper_index]
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise ClipInputError(
            "bad_table", f"cannot read {feature!r} and a timestamp from the table: {exc!r}"
        ) from exc  # fmt: skip
    return ClipSignals(times, positions, values, gripper_bounds)


def candidates_baseline(info=None, profile=None, **read_kwargs):
    """The baseline for ``evaluate``: the kept candidates (status
    ``proposed``, in evidence priority) that ``levi.events.candidates.read``
    gives for the episode's ``table``, as times. Without ``info`` it reads
    the layout of ``calibrate`` (an FR3-style ``observation.state`` of x, y,
    z, rx, ry, rz, gripper width; the records ``calibrate.collect`` makes);
    otherwise ``info`` and ``profile`` are those of ``candidates.read``.
    ``read_kwargs`` go to ``candidates.read`` (sources, merge distance,
    change-point penalty); the defaults are the plan's."""

    def baseline(episode):
        from . import calibrate, candidates
        from .signal_profiles import resolve

        used_info, used_profile = info, profile
        if used_info is None:
            used_info = calibrate.INFO
            used_profile = used_profile or resolve(calibrate.INFO, calibrate.PROFILE)
        found = candidates.read(
            episode["table"], used_info, used_profile, **read_kwargs
        )
        return [c.center_time_s for c in candidates.proposed(found)]

    return baseline


def _segments(edges):
    return [{"start": a, "end": b, "subtask": None} for a, b in pairwise(edges)]


def _signals_of(episode):
    return episode["signals"]


def evaluate(
    episodes,
    params=None,
    *,
    signals_of=_signals_of,
    baseline=None,
    tolerances=TOLERANCES,
    ious=boundary_metrics.IOUS,
):
    """Score the proposer on reference boundaries, and a baseline on the same.

    ``episodes``: records with ``id``, ``boundaries`` (the reference's inner
    boundaries, seconds on the signals' timeline) and ``duration`` (seconds);
    ``signals_of(record)`` gives the record's ``ClipSignals`` (by default its
    ``signals`` entry); the records ``calibrate.collect`` returns fit
    ``evaluate_gold``. ``baseline(record)`` gives a baseline's boundary times
    in priority order (``candidates_baseline()``).

    An episode that the proposer or the baseline cannot read is scored in
    neither arm and is listed under ``skipped`` with the code and the cause,
    so both arms always use the same gold and the same episodes.

    Returns the boundary scores of the proposer (recall, precision, F1, false
    candidates per minute at each tolerance, from ``boundary_metrics``), the
    segment F1 of its clips against the reference segments (no subtask names:
    the overlap alone decides), its clip counts and, with a baseline, the
    boundary scores of all its candidates and of its first n candidates where
    the proposer made n boundaries (the baseline is an evidence ordering, not
    a clip proposer: it makes many candidates, so read its recall beside its
    count)."""
    params = params or ClipParams()
    reference, proposed, durations = {}, {}, {}
    ref_segments, clip_segments, base, skipped = {}, {}, {}, []
    counts = {reason: 0 for reason in BOUNDARY_REASONS}
    clips = 0
    for i, episode in enumerate(episodes):
        key = episode.get("id", i)
        try:
            proposal = propose_clips(signals_of(episode), params)
        except ClipInputError as err:
            skipped.append({"id": key, "code": err.code, "message": err.message})
            continue
        if baseline is not None:
            try:
                times_base = [float(t) for t in baseline(episode)]
            except Exception as exc:  # noqa: BLE001 - one bad episode is a skipped row
                skipped.append(
                    {"id": key, "code": "baseline_error", "message": repr(exc)}
                )
                continue
            base[key] = times_base
        first, last = proposal.clips[0].start_s, proposal.clips[-1].end_s
        inner = sorted({float(b) for b in episode["boundaries"] if first < b < last})
        reference[key] = inner
        proposed[key] = [b.time_s for b in proposal.boundaries]
        durations[key] = float(episode["duration"])
        ref_segments[key] = _segments([first, *inner, last])
        clip_segments[key] = _segments([first, *(c.end_s for c in proposal.clips)])
        clips += len(proposal.clips)
        for boundary in proposal.boundaries:
            counts[boundary.reason] += 1
    scores = boundary_metrics.boundary_scores(
        reference, proposed, durations, tolerances
    )
    minutes = scores["minutes"]
    result = {
        "schema_version": "levi.clip_proposal_eval.v1",
        "algorithm_version": VERSION,
        "params": params.as_dict(),
        "params_sha256": params.sha256(),
        "param_sources": dict(PARAM_SOURCES),
        "param_notes": dict(PARAM_NOTES),
        "episodes": len(reference),
        "skipped": skipped,
        "boundary": scores,
        "segments": boundary_metrics.segment_f1(ref_segments, clip_segments, ious),
        "clips": {
            "total": clips,
            "per_minute": round(clips / minutes, 3) if minutes else None,
            "boundaries_by_reason": counts,
        },
        "baseline": None,
    }
    if baseline is not None:
        result["baseline"] = {
            "all": boundary_metrics.boundary_scores(
                reference, base, durations, tolerances
            ),
            "same_count": boundary_metrics.boundary_scores(
                reference,
                {k: base[k][: len(proposed[k])] for k in base},
                durations,
                tolerances,
            ),
        }
    return result


def evaluate_gold(
    folders,
    root,
    params=None,
    *,
    gripper_bounds=None,
    mapping=(),
    exclude=(),
    exclusion_lists=(),
    guard=None,
    baseline=True,
    **kwargs,
):
    """``evaluate`` on the episodes of gold folders (``calibrate``'s layout:
    reference segments, a raw capture's pose and gripper CSVs), read only
    through ``calibrate.Guard``: a folder or source whose path holds
    ``frozen``, ``heldout`` or ``held-out``, or that is on an exclusion list
    (``exclusion_lists`` and the ``LEVI_POOL_HELDOUT`` lists), ends the run
    with ``SystemExit`` instead of being skipped. ``guard`` replaces the one
    built from those lists. ``gripper_bounds``: the gripper channel's declared
    (min, max), passed to every episode (without it each is read on its own
    range, with the absolute travel floor). ``baseline``: ``True`` for
    ``candidates_baseline()``, ``False`` for none, or a callable. The result
    adds the gold episodes read (with the SHA-256 of their files), those
    skipped and why, and the guards. This is the one function here that reads
    files, and it reads them through ``calibrate``."""
    from ..pool import settings
    from . import calibrate

    guard = guard or calibrate.Guard(
        root,
        calibrate.REFUSE,
        [*map(str, exclusion_lists), *map(str, settings.heldout_files())],
    )
    episodes, skipped = calibrate.collect(
        list(folders),
        root,
        [tuple(m) for m in mapping],
        list(exclude),
        calibrate.REFUSE,
        guard,
    )
    names = calibrate.NAMES

    def signals_of(episode):
        return signals_from_table(
            episode["table"],
            "observation.state",
            [names.index(axis) for axis in ("x", "y", "z")],
            names.index("gripper"),
            gripper_bounds,
        )

    if baseline is True:
        baseline = candidates_baseline()
    result = evaluate(
        episodes,
        params,
        signals_of=signals_of,
        baseline=baseline or None,
        **kwargs,
    )
    result["gold"] = {
        "folders": [str(f) for f in folders],
        "episodes": [
            {"id": e["id"], "source": e["source"], "sha256": e["sha256"]}
            for e in episodes
        ],
        "skipped": skipped,
        "guards": guard.record(),
    }
    return result
