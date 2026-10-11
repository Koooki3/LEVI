"""Causal signal candidates of a bounded observed prefix; no I/O or model.

Unlike offline ``candidates.read``, this reader never estimates ranges or a
speed percentile from a whole episode. Explicit channel semantics and fixed
thresholds are frozen at construction. A candidate only suggests fresh evidence.
"""

import hashlib
import math
from collections import deque
from dataclasses import dataclass
from numbers import Real

from . import candidates, gripper
from .contracts import EventCandidate, SignalSource
from .signal_profiles import SignalProfile


@dataclass(frozen=True)
class StreamConfig:
    max_history: int = 128
    max_age_ns: int = 500_000_000
    still_ns: int = 500_000_000
    motion_speed_m_s: float = 0.01
    max_gap_ns: int = 1_000_000_000

    def __post_init__(self):
        for name, low, high in (
            ("max_history", 2, 4096),
            ("max_age_ns", 1, 30_000_000_000),
            ("still_ns", 1, 30_000_000_000),
            ("max_gap_ns", 1, 30_000_000_000),
        ):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name}: bounded positive integer required")
        speed = self.motion_speed_m_s
        if type(speed) not in (int, float) or not math.isfinite(speed) or speed <= 0:
            raise ValueError("motion_speed_m_s: positive finite number required")


@dataclass(frozen=True)
class Sample:
    step: int
    observed_ns: int
    values: dict


@dataclass(frozen=True)
class Update:
    candidates: tuple
    window_steps: tuple
    unknown: tuple
    dropped_history: int


class CausalSignals:
    """One episode's prefix. Missing samples break continuity, never invent it.

    ``values`` maps explicit ``(feature, index)`` keys to scalars in the
    profile's units. Supported sources are grippers with declared range and
    polarity, and measured xyz positions in metres in one declared frame.
    Batch change points and object semantics are deliberately not inferred.
    """

    def __init__(self, profile, *, episode_index=0, config=None):
        profile = SignalProfile.model_validate(profile).model_copy(deep=True)
        if profile.origin != "declared" or not profile.channels:
            raise ValueError("explicit nonempty declared signal profile required")
        if len(profile.channels) > 64:
            raise ValueError("at most 64 channels per online stream")
        self._profile = profile.model_dump_json()
        self.profile_sha256 = hashlib.sha256(self._profile.encode()).hexdigest()
        self.config = config or StreamConfig()
        if type(episode_index) is not int or episode_index < 0:
            raise ValueError("episode_index must be nonnegative")
        self.episode_index = episode_index
        self.history = deque(maxlen=self.config.max_history)
        self.dropped_history = 0
        self._last = None
        self._origin_ns = None
        self._grippers = {}
        self._motion = {}
        self._channels = tuple(profile.channels)
        for channel in self._channels:
            if channel.index > 4096:
                raise ValueError("signal dimension exceeds the event wire limit")
            if channel.role != "ignore":
                SignalSource(
                    feature=channel.feature,
                    dimension=channel.dimension,
                    index=channel.index,
                    kind=channel.kind,
                    units=channel.units,
                    frame=channel.frame,
                )
        positions = {}
        for channel in self._channels:
            if channel.role == "gripper":
                if (
                    channel.open_level is None
                    or channel.valid_range is None
                    or channel.units not in ("m", "mm", "1", "normalized", "binary")
                ):
                    raise ValueError("gripper needs explicit polarity, range and units")
            elif channel.role == "position":
                if (
                    channel.kind != "measured"
                    or channel.units != "m"
                    or not channel.frame
                ):
                    raise ValueError(
                        "positions need measured metres and a declared frame"
                    )
                group = (channel.actor_id, channel.feature, channel.frame)
                dims = positions.setdefault(group, {})
                if channel.axis in dims:
                    raise ValueError("duplicate position axis")
                dims[channel.axis] = channel
            elif channel.role != "ignore":
                raise ValueError(
                    "online rotation/change-point reader is not implemented"
                )
        if any(set(dims) != {"x", "y", "z"} for dims in positions.values()):
            raise ValueError("online position source needs complete xyz")
        self._positions = tuple(
            tuple(dims[a] for a in "xyz") for dims in positions.values()
        )

    @property
    def profile(self):
        return SignalProfile.model_validate_json(self._profile)

    def feed(self, sample, *, now_ns):
        c = self.config
        if (
            type(sample.step) is not int
            or sample.step < 0
            or type(sample.observed_ns) is not int
            or type(now_ns) is not int
            or sample.observed_ns < 0
            or sample.observed_ns > now_ns
            or now_ns - sample.observed_ns > c.max_age_ns
        ):
            raise ValueError("future, stale or invalid observation")
        previous = self._last
        if previous and (
            sample.step <= previous.step or sample.observed_ns <= previous.observed_ns
        ):
            raise ValueError("observation step and time must strictly advance")
        if self._origin_ns is None:
            self._origin_ns = sample.observed_ns
        unknown, clean = [], {}
        for channel in self._channels:
            if channel.role == "ignore":
                continue
            value = sample.values.get(channel.key)
            if (
                isinstance(value, bool)
                or not isinstance(value, Real)
                or not math.isfinite(value)
            ):
                unknown.append(f"missing_signal:{channel.feature}:{channel.index}")
                continue
            if channel.valid_range is not None:
                lo, hi = channel.valid_range
                if not lo <= value <= hi:
                    unknown.append(f"out_of_range:{channel.feature}:{channel.index}")
                    continue
            clean[channel.key] = float(value)
        sample = Sample(sample.step, sample.observed_ns, clean)
        gap = previous and (
            sample.step != previous.step + 1
            or sample.observed_ns - previous.observed_ns > c.max_gap_ns
        )
        if gap:
            self._grippers.clear()
            self._motion.clear()
            unknown.append("observation_gap")
        found, windows = [], []

        def add(kind, channels, start_ns=None, start_step=None):
            channel = channels[0]
            sources = [
                SignalSource(
                    feature=x.feature,
                    dimension=x.dimension,
                    index=x.index,
                    kind=x.kind,
                    units=x.units,
                    frame=x.frame,
                )
                for x in channels
            ]
            time_s = (sample.observed_ns - self._origin_ns) / 1e9
            start_step = sample.step if start_step is None else start_step
            windows.append((start_step, sample.step))
            start_ns = sample.observed_ns if start_ns is None else start_ns
            start = (start_ns - self._origin_ns) / 1e9
            key = hashlib.sha256(str(channel.key).encode()).hexdigest()[:12]
            found.append(
                EventCandidate(
                    id=f"{kind}_{key}_{sample.step}",
                    episode_index=self.episode_index,
                    event_type=kind,
                    actor_id=channel.actor_id,
                    center_time_s=time_s,
                    time_window_s=(start, time_s),
                    source_frame_index=sample.step,
                    sources=sources,
                    salience=candidates.PRIORITY[
                        "gripper" if kind.startswith("gripper") else "still"
                    ],
                )
            )

        for channel in self._channels:
            if channel.role != "gripper":
                continue
            value = clean.get(channel.key)
            if value is None:
                self._grippers.pop(channel.key, None)
                continue
            lo, hi = channel.valid_range
            level = (value - lo) / (hi - lo)
            if channel.open_level == "low":
                level = 1 - level
            state = self._grippers.get(channel.key)
            # Feed the shared hysteresis kernel the last logical state and
            # this new sample; fixed endpoints preserve state inside the band.
            if state is not None:
                crossings = gripper.hysteresis([1.0 if state else 0.0, level])
                if crossings:
                    kind = crossings[-1][1]
                    add(
                        f"gripper_{kind}",
                        (channel,),
                        previous.observed_ns,
                        previous.step,
                    )
                    state = kind == "open"
            else:
                state = level >= 0.5
            self._grippers[channel.key] = state

        for channels in self._positions:
            key = tuple(x.key for x in channels)
            if any(x.key not in clean for x in channels):
                self._motion.pop(key, None)
                continue
            point = tuple(clean[x.key] for x in channels)
            state = self._motion.get(key)
            if state is not None:
                old_point, old_ns, old_step, moving, quiet_since, quiet_step = state
                dt = (sample.observed_ns - old_ns) / 1e9
                speed = math.dist(point, old_point) / dt
                fast = speed > c.motion_speed_m_s
                if fast:
                    if moving is False:
                        add("motion_start", channels, old_ns, old_step)
                    moving, quiet_since, quiet_step = True, None, None
                else:
                    if quiet_since is None:
                        quiet_since, quiet_step = old_ns, old_step
                    if (
                        sample.observed_ns - quiet_since >= c.still_ns
                        and moving is not False
                    ):
                        add("motion_stop", channels, quiet_since, quiet_step)
                        moving = False
                state = (
                    point,
                    sample.observed_ns,
                    sample.step,
                    moving,
                    quiet_since,
                    quiet_step,
                )
            else:
                state = point, sample.observed_ns, sample.step, None, None, None
            self._motion[key] = state
        if len(self.history) == self.history.maxlen:
            self.dropped_history += 1
        self.history.append(sample)
        self._last = sample
        return Update(
            tuple(found), tuple(windows), tuple(unknown), self.dropped_history
        )
