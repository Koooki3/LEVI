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


def _position_kernel(height=None):
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
    return CausalSignals(SignalProfile(channels=channels), height_turn_m=height)


def _heights(kernel, zs):
    out = []
    for step, z in enumerate(zs):
        ns = (step + 1) * 100_000_000
        update = kernel.feed(
            Sample(step, ns, {("pose", 0): 0, ("pose", 1): 0, ("pose", 2): z}),
            now_ns=ns,
        )
        out += [
            (step, c.event_type, c.time_window_s)
            for c in update.candidates
            if c.event_type.startswith("height")
        ]
    return out


def test_height_turns_are_opt_in_reported_late_and_skip_the_start():
    zs = [0.3, 0.25, 0.2, 0.15, 0.1, 0.1, 0.15, 0.2, 0.25]
    # Off by default: existing callers see the candidates they always did.
    assert _heights(_position_kernel(), zs) == []
    # The start is the highest point (not a turn); the low at row 4 is known
    # at row 7, once the height has risen 0.08 m from it.
    found = _heights(_position_kernel(0.08), zs)
    assert [(step, kind) for step, kind, _ in found] == [(7, "height_low")]
    assert found[0][2] == pytest.approx((0.4, 0.7))


@pytest.mark.parametrize("bad", [0, -1, float("inf"), True, 11])
def test_height_turn_threshold_is_validated(bad):
    with pytest.raises(ValueError):
        _position_kernel(bad)


def test_height_turns_add_candidates_and_change_no_other():
    def kinds(height):
        kernel = _position_kernel(height)
        out = []
        for step in range(60):
            ns = (step + 1) * 100_000_000
            x = 0.01 * step if step % 20 < 10 else 0.1
            z = 0.3 - 0.02 * (step % 15)
            update = kernel.feed(
                Sample(step, ns, {("pose", 0): x, ("pose", 1): 0, ("pose", 2): z}),
                now_ns=ns,
            )
            out.append(
                (
                    [
                        c.model_dump()
                        for c in update.candidates
                        if not c.event_type.startswith("height")
                    ],
                    update.unknown,
                )
            )
        return out

    assert kinds(0.05) == kinds(None)


def test_without_height_turns_the_stream_answers_exactly_as_main_did():
    """``fixtures/events/stream-golden.json`` holds CausalSignals' updates on
    fixed synthetic input, written from main f256e131 (before
    ``height_turn_m``) by ``fixtures/events/make_stream_golden.py``. The
    code under test, without ``height_turn_m``, must give the same updates
    item by item. The on/off comparison above does not replace this: it
    cannot see a change both settings share."""
    import json
    import math
    from pathlib import Path

    golden = json.loads(
        (
            Path(__file__).parent / "fixtures" / "events" / "stream-golden.json"
        ).read_text()
    )
    kernel = CausalSignals(
        SignalProfile.model_validate(golden["profile"]),
        config=StreamConfig(**golden["config"]),
    )
    steps = []
    for step, observed, now, values in golden["inputs"]:
        clean = {}
        for key, value in values.items():
            feature, index = key.split("|")
            clean[(feature, int(index))] = math.nan if value is None else float(value)
        try:
            update = kernel.feed(Sample(step, observed, clean), now_ns=now)
        except ValueError as exc:
            steps.append({"rejected": str(exc)})
            continue
        steps.append(
            {
                "candidates": [c.model_dump(mode="json") for c in update.candidates],
                "window_steps": [list(w) for w in update.window_steps],
                "unknown": list(update.unknown),
                "dropped_history": update.dropped_history,
            }
        )
    steps = json.loads(json.dumps(steps))
    assert len(steps) == len(golden["steps"])
    for i, (got, want) in enumerate(zip(steps, golden["steps"], strict=True)):
        assert got == want, i
    kinds = {c["event_type"] for s in steps for c in s.get("candidates", ())}
    assert {"gripper_open", "gripper_close", "motion_stop", "motion_start"} <= kinds
