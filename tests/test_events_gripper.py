"""The shared gripper-crossing kernel (``levi/events/gripper.py``).

Its callers' differences are arguments: the range a crossing is measured on,
which end is open, how far a gripper must move to count. The two readers that
had their own copies (``agent.signals`` and ``agent.anchored``) must read
every channel exactly as their copies did; the copies are kept below,
verbatim from main d68f656, and compared on random channels."""

import numpy as np
import pytest

from levi.agent.anchored import crossings
from levi.agent.signals import gripper_events
from levi.events import gripper

LOW, HIGH = 0.35, 0.65


def _legacy_gripper_events(times, values, bounds=None):
    finite = np.isfinite(values)
    if finite.sum() < 2:
        return []
    lo, hi = np.nanmin(values), np.nanmax(values)
    if hi - lo <= 1e-9:
        return []
    if bounds and np.isfinite(bounds).all() and bounds[1] - bounds[0] > 1e-9:
        if (hi - lo) < 0.2 * (bounds[1] - bounds[0]):
            return []
    else:
        bounds = None
    u = np.where(finite, (values - lo) / (hi - lo), np.nan)
    flip = u[np.argmax(finite)] < 0.5
    if flip:
        u = 1 - u
    binary = bounds is None or np.all(
        np.isin(np.round(values[finite], 6), np.round(bounds, 6))
    )
    events = []
    state = "open"
    for i, x in enumerate(u):
        if np.isnan(x):
            continue
        if state == "open" and x < LOW:
            state = "closed"
            j = i
            while j + 1 < len(u) and not np.isnan(u[j + 1]) and u[j + 1] < u[j] - 1e-6:
                j += 1
            event = {"t": float(times[i]), "kind": "close"}
            if bounds and not binary:
                share = (values[j] - bounds[0]) / (bounds[1] - bounds[0])
                event["level"] = float(np.clip(1 - share if flip else share, 0, 1))
            events.append(event)
        elif state == "closed" and x > HIGH:
            state = "open"
            events.append({"t": float(times[i]), "kind": "open"})
    return events


def _legacy_crossings(values, bounds, open_level="high"):
    values = np.asarray(values, dtype=float)
    finite = np.isfinite(values)
    if finite.sum() < 2:
        return []
    lo, hi = (
        bounds
        if bounds and np.isfinite(bounds).all() and bounds[1] - bounds[0] > 1e-9
        else (np.nanmin(values), np.nanmax(values))
    )
    if hi - lo <= 1e-9:
        return []
    u = (values - lo) / (hi - lo)
    if open_level == "low":
        u = 1 - u
    first = int(np.argmax(finite))
    state = "open" if u[first] >= 0.5 else "closed"
    out = []
    for i in range(first + 1, len(u)):
        if not finite[i]:
            continue
        if state == "closed" and u[i] > HIGH:
            state = "open"
            out.append((i, "open"))
        elif state == "open" and u[i] < LOW:
            state = "closed"
            out.append((i, "close"))
    return out


def _channel(rng):
    n = int(rng.integers(0, 60))
    kind = rng.integers(0, 5)
    if kind == 0:  # binary flag
        values = rng.integers(0, 2, n).astype(float)
    elif kind == 1:  # steps between a few levels
        values = rng.choice(rng.uniform(0, 1, 3), n)
    elif kind == 2:  # noisy random walk
        values = np.cumsum(rng.normal(0, 0.2, n))
    elif kind == 3:  # constant
        values = np.full(n, float(rng.uniform(0, 1)))
    else:  # uniform noise
        values = rng.uniform(-1, 2, n)
    values = values * float(rng.choice([1.0, 0.08, 255.0]))
    if n and rng.integers(0, 3) == 0:
        values[rng.integers(0, n, int(rng.integers(1, 4)))] = np.nan
    if n and rng.integers(0, 12) == 0:
        values[int(rng.integers(0, n))] = rng.choice([np.inf, -np.inf])
    bounds = None
    pick = rng.integers(0, 4)
    if pick == 1:
        bounds = (0.0, float(np.nanmax(np.abs(values[np.isfinite(values)]), initial=1)))
    elif pick == 2:
        bounds = (float(rng.uniform(-1, 0)), float(rng.uniform(0, 300)))
    elif pick == 3:
        bounds = (1.0, 1.0)  # degenerate
    return values, bounds


def _robust_start_agrees(values):
    """Whether the median of the first three finite samples sits on the same
    side of the episode's mid-range as the first one (then the robust start
    of T-A-03 reads exactly as the first sample did)."""
    finite = np.flatnonzero(np.isfinite(values))
    if len(finite) < 2:
        return True
    lo, hi = np.nanmin(values), np.nanmax(values)
    if not np.isfinite(hi - lo) or hi - lo <= 1e-9:
        return True
    u = (values[finite[:3]] - lo) / (hi - lo)
    return (u[0] >= 0.5) == (np.median(u) >= 0.5)


@pytest.mark.filterwarnings("ignore::RuntimeWarning")
def test_both_readers_match_their_former_copies_on_random_channels():
    rng = np.random.default_rng(20261010)
    changed = 0
    for _ in range(3000):
        values, bounds = _channel(rng)
        times = np.arange(len(values)) / 10
        legacy = _legacy_gripper_events(times, values.copy(), bounds)
        # The kernel with the first-sample start is the former copy exactly.
        first = gripper.read(
            values.copy(),
            bounds,
            range_source="episode",
            open_level="auto",
            min_travel=0.2,
            start="first",
        )
        assert [(e["t"], e["kind"]) for e in legacy] == [
            (float(times[i]), kind) for i, kind in (first.crossings if first else [])
        ]
        # The signal lines read as the former copy did by default; the
        # explicit robust start differs only where the first sample
        # disagrees with the next two.
        assert gripper_events(times, values.copy(), bounds) == legacy
        robust = gripper_events(times, values.copy(), bounds, start="robust")
        if _robust_start_agrees(values):
            assert robust == legacy
        else:
            changed += 1
        for level in ("high", "low"):
            assert crossings(values.copy(), bounds, level) == _legacy_crossings(
                values.copy(), bounds, level
            )
    # The robust branch was exercised.
    assert changed > 0


def test_the_range_source_decides_a_wide_grasp():
    values = np.array([0.08] * 10 + [0.03] * 10 + [0.08] * 10)
    episode = gripper.read(values, (0.0, 0.08), range_source="episode")
    dataset = gripper.read(values, (0.0, 0.08), range_source="dataset")
    assert episode.crossings == [(10, "close"), (20, "open")]
    assert (episode.lo, episode.hi) == (0.03, 0.08)
    assert dataset.crossings == []
    assert (dataset.lo, dataset.hi) == (0.0, 0.08)


def test_the_open_level_is_declared_or_read_from_the_start():
    closure = np.array([0, 0, 1, 1, 0, 0.0])  # 1 = closed
    assert gripper.read(closure, open_level="low").crossings == [
        (2, "close"),
        (4, "open"),
    ]
    auto = gripper.read(closure, open_level="auto")
    assert auto.open_level == "low"
    assert auto.crossings == [(2, "close"), (4, "open")]
    # Declared high: the same channel starts closed and opens first.
    assert gripper.read(closure, open_level="high").crossings == [
        (2, "open"),
        (4, "close"),
    ]


def test_min_travel_needs_the_dataset_range():
    jitter = np.array([0.08, 0.079] * 10)
    assert (
        gripper.read(jitter, (0.0, 0.08), range_source="episode", min_travel=0.2)
        is None
    )
    # Without the dataset's range there is nothing to compare the travel with.
    assert gripper.read(jitter, None, range_source="episode", min_travel=0.2).crossings
    # A degenerate range is no range.
    assert gripper.read(jitter, (1.0, 1.0), min_travel=0.2).bounds is None


def test_nothing_to_read():
    assert gripper.read([]) is None
    assert gripper.read([1.0]) is None
    assert gripper.read([np.nan, 1.0, np.nan]) is None
    assert gripper.read([0.5] * 5) is None


def test_hysteresis_holds_between_the_two_levels():
    u = np.array([1, 0.5, 0.36, 0.34, 0.5, 0.64, 0.66, 0.4, np.nan, 0.2])
    assert gripper.hysteresis(u) == [(3, "close"), (6, "open"), (9, "close")]
    assert gripper.hysteresis(np.array([np.nan, np.nan])) == []
