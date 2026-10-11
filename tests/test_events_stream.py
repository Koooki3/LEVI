"""Prefix causality, semantic provenance and bounded online event evidence."""

import pytest

from levi.events.signal_profiles import ChannelSpec, SignalProfile
from levi.events.stream import CausalSignals, Sample, StreamConfig


def profile(*, polarity="high", units="normalized", kind="measured"):
    return SignalProfile(
        channels=[
            ChannelSpec(
                role="gripper",
                feature="observation.state",
                index=6,
                kind=kind,
                units=units,
                open_level=polarity,
                valid_range=(0, 1),
            )
        ]
    )


def feed(kernel, step, value, time=None):
    time = (step + 1) * 100_000_000 if time is None else time
    return kernel.feed(
        Sample(step, time, {("observation.state", 6): value}), now_ns=time
    )


def test_future_suffix_cannot_change_prefix_and_polarity_is_explicit():
    prefixes = []
    for suffix in ([0, 1, 0], [10000, float("nan"), -10000]):
        kernel = CausalSignals(profile())
        prefix = [
            feed(kernel, step, value) for step, value in enumerate([0, 0.6, 1, 0.5, 0])
        ]
        prefixes.append(prefix)
        for step, value in enumerate(suffix, 5):
            feed(kernel, step, value)
    assert prefixes[0] == prefixes[1]
    events = [e for update in prefixes[0] for e in update.candidates]
    assert [e.event_type for e in events] == ["gripper_open", "gripper_close"]
    assert events[0].sources[0].kind == "measured"
    assert prefixes[0][2].window_steps == ((1, 2),)
    reverse = CausalSignals(profile(polarity="low", kind="commanded"))
    feed(reverse, 0, 0)
    assert feed(reverse, 1, 1).candidates[0].event_type == "gripper_close"
    assert feed(reverse, 2, 0).candidates[0].sources[0].kind == "commanded"


@pytest.mark.parametrize("missing", [None, float("nan"), float("inf"), True, 2])
def test_missing_invalid_signal_breaks_continuity(missing):
    kernel = CausalSignals(profile())
    feed(kernel, 0, 0)
    assert feed(kernel, 1, missing).unknown
    assert not feed(kernel, 2, 1).candidates
    assert feed(kernel, 3, 0).candidates


def test_gap_breaks_continuity_and_storage_is_bounded():
    kernel = CausalSignals(profile(), config=StreamConfig(max_history=2))
    feed(kernel, 0, 0)
    gap = feed(kernel, 2, 1)
    assert gap.unknown == ("observation_gap",)
    assert not gap.candidates
    for step in range(3, 103):
        feed(kernel, step, step % 2)
    assert len(kernel.history) == 2
    assert kernel.dropped_history == 100
    assert len(kernel._grippers) == 1


@pytest.mark.parametrize(
    "sample,now",
    [
        (Sample(0, 2, {}), 1),
        (Sample(0, 1, {}), 600_000_001),
        (Sample(True, 1, {}), 1),
        (Sample(0, -1, {}), 1),
    ],
)
def test_invalid_future_stale_samples_rejected_without_consuming(sample, now):
    kernel = CausalSignals(profile())
    with pytest.raises(ValueError):
        kernel.feed(sample, now_ns=now)
    assert len(kernel.history) == 0
    feed(kernel, 0, 0)
    with pytest.raises(ValueError):
        feed(kernel, 0, 1)
    assert len(kernel.history) == 1


def test_declared_units_polarity_and_position_frame_required():
    for update in ({"origin": "inferred"}, {"channels": []}):
        with pytest.raises(ValueError):
            CausalSignals(profile().model_copy(update=update))
    for changes in ({"open_level": None}, {"units": None}, {"valid_range": None}):
        channel = profile().channels[0].model_copy(update=changes)
        with pytest.raises(ValueError):
            CausalSignals(SignalProfile(channels=[channel]))
    with pytest.raises(ValueError):
        CausalSignals(
            SignalProfile(
                channels=[
                    ChannelSpec(
                        role="position",
                        feature="pose",
                        index=0,
                        axis="x",
                        units="mm",
                        frame="base",
                    )
                ]
            )
        )


def test_motion_uses_measured_metres_fixed_threshold_and_exact_window_steps():
    channels = [
        ChannelSpec(
            role="position",
            feature="pose",
            index=i,
            axis=axis,
            units="m",
            frame="base",
            kind="measured",
        )
        for i, axis in enumerate("xyz")
    ]
    kernel = CausalSignals(
        SignalProfile(channels=channels),
        config=StreamConfig(still_ns=200_000_000, max_history=2),
    )

    def pose(step, x):
        return kernel.feed(
            Sample(
                step,
                (step + 1) * 100_000_000,
                {("pose", 0): x, ("pose", 1): 0, ("pose", 2): 0},
            ),
            now_ns=(step + 1) * 100_000_000,
        )

    pose(0, 0)
    pose(1, 0.1)
    pose(2, 0.1)
    stop = pose(3, 0.1)
    assert stop.candidates[0].event_type == "motion_stop"
    assert stop.window_steps == ((1, 3),)
    assert stop.candidates[0].time_window_s == (0.1, 0.3)
    start = pose(4, 0.2)
    assert start.candidates[0].event_type == "motion_start"
    assert start.window_steps == ((3, 4),)
