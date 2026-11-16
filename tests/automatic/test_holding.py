"""Measured holding fact: band, steadiness, freshness, lift recheck, source."""

import json

import pytest

from levi.automatic import holding as h
from levi.automatic import offline_harness
from levi.automatic.fr3_bridge import GRIPPER_STABLE_DELTA
from levi.automatic.regrasp_gate import GateObservation, GateState, advance_regrasp

# Example only (eggplant held measured 0.2986 on 2026-11-14); a real band comes
# from the job or site configuration and the V2 calibration.
BAND = (0.15, 0.45)
NOW = 5000


def config(**changes):
    fields = {"object_id": "eggplant", "lo": BAND[0], "hi": BAND[1]}
    fields.update(changes)
    return h.HoldingConfig(**fields)


def pair(a, b, *, age=10, gap=5):
    first = h.Reading(1, NOW - age - gap, a, 0.10)
    second = h.Reading(2, NOW - age, b, 0.10)
    return first, second


def close(a, b, cfg=None, **kw):
    first, second = pair(a, b, **kw)
    return h.assess_close(
        first,
        second,
        cfg or config(),
        run_id="run",
        sample_id="eggplant",
        policy_epoch=1,
        now_ms=NOW,
    )


@pytest.mark.parametrize(
    "a, b, status, reason",
    [
        (0.2986, 0.2990, "holding_candidate", "steady_inside_holding_band"),
        (0.01, 0.012, "empty", "steady_below_holding_band"),
        (0.95, 0.96, "open", "steady_open"),
        (0.2986, 0.3300, "unknown", "gripper_not_steady"),  # jitter > 0.02
        (0.2986, None, "unknown", "gripper_reading_missing"),
        (None, None, "unknown", "gripper_reading_missing"),
        (0.2986, float("nan"), "unknown", "gripper_reading_missing"),
        (0.2986, 1.5, "unknown", "gripper_reading_missing"),
        (0.60, 0.61, "unknown", "between_band_and_open"),
        (0.145, 0.155, "unknown", "readings_straddle_band_edge"),
    ],
)
def test_close_table(a, b, status, reason):
    fact = close(a, b)
    assert (fact.status, fact.reason) == (status, reason)
    assert fact.source == "verified_measurement" and fact.is_real
    assert fact.record()["physical_allowed"] is False
    json.dumps(fact.record())


def test_jitter_boundary_is_the_bridges_constant():
    assert h.GRIPPER_STABLE_DELTA == GRIPPER_STABLE_DELTA == 0.02
    assert close(0.30, 0.3199).status == "holding_candidate"
    assert close(0.30, 0.3201).status == "unknown"


def test_empty_close_is_never_holding():
    fact = close(0.01, 0.01)
    assert fact.status == "empty"
    assert fact.holding_verified is False and fact.empty_verified is True
    assert fact.closed_measured is True and fact.open_measured is False


@pytest.mark.parametrize(
    "age, status",
    [
        (0, "holding_candidate"),
        (95, "holding_candidate"),
        (96, "refused"),
        (500, "refused"),
    ],
)
def test_stale_readings_are_refused(age, status):
    # the older reading sits 5 ms before the newer one
    fact = close(0.2986, 0.2986, age=age)
    assert fact.status == status
    if status == "refused":
        assert fact.reason == "observation_stale"
        assert fact.gate_observation() is None
    assert h.MAX_AGE_MS == offline_harness.MAX_OBSERVATION_AGE_MS == 100


def test_a_reading_from_the_future_or_not_newer_is_refused():
    assert close(0.2986, 0.2986, age=-5).status == "refused"
    first, second = pair(0.2986, 0.2986)
    again = h.assess_close(
        second,
        second,
        config(),
        run_id="run",
        sample_id="eggplant",
        policy_epoch=1,
        now_ms=NOW,
    )
    assert again.reason == "observation_not_new"
    old = h.assess_close(
        first,
        second,
        config(),
        run_id="run",
        sample_id="eggplant",
        policy_epoch=1,
        now_ms=NOW,
        after_seq=2,
    )
    assert old.reason == "observation_not_new"


def test_max_age_may_only_tighten():
    assert close(0.2986, 0.2986, age=50, cfg=config(max_age_ms=40)).status == "refused"
    with pytest.raises(ValueError):
        config(max_age_ms=101)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"lo": 0.0},  # lo 0 would call an empty close "holding"
        {"lo": -0.1},
        {"hi": 0.1},
        {"hi": 0.3},  # the synthetic closed limit may not be reused
        {"hi": 0.95},
        {"source": "bogus"},
        {"stable_delta": 0.5},
        {"lift_min_m": 0},
    ],
)
def test_bad_real_configuration_is_refused(kwargs):
    with pytest.raises(ValueError):
        config(**kwargs)


def test_synthetic_thresholds_apply_only_to_a_synthetic_source():
    synthetic = h.synthetic_config()
    assert synthetic.source == "synthetic_measurement"
    assert (synthetic.hi, synthetic.open_min) == (0.3, 0.9)
    # 0.3014 is outside the synthetic closed limit but inside a real band.
    assert close(0.3014, 0.3014, cfg=synthetic).status == "unknown"
    assert close(0.3014, 0.3014).status == "holding_candidate"
    fact = close(0.2, 0.2, cfg=synthetic)
    assert fact.source == "synthetic_measurement" and not fact.is_real
    with pytest.raises(ValueError):
        h.require_real(fact)
    assert h.require_real(close(0.2986, 0.2986)) is not None
    # A real source cannot be built from the synthetic numbers.
    for source in ("verified_measurement", "recorded_measurement"):
        with pytest.raises(ValueError):
            h.HoldingConfig("x", 0.0, 0.3, source=source, open_min=0.9)
        with pytest.raises(ValueError):
            h.HoldingConfig("x", 0.01, 0.3, source=source, open_min=0.9)
    # The drop detector refuses a synthetic band.
    with pytest.raises(ValueError):
        h.DropConfig.from_holding(synthetic)


def test_parse_holding_bands():
    parsed = h.parse_holding_bands({"eggplant": [0.15, 0.45]})
    assert parsed["eggplant"].band == (0.15, 0.45)
    assert parsed["eggplant"].source == "verified_measurement"
    for bad in (
        {},
        None,
        [],
        {"a": [0.1]},
        {"a": [0.5, 0.2]},
        {"a": ["x", 1]},
        {"a": [0, 0.4]},
    ):
        with pytest.raises(ValueError):
            h.parse_holding_bands(bad)


# --- lift recheck ---------------------------------------------------------


def candidate():
    fact = close(0.2986, 0.2986)
    assert fact.status == "holding_candidate"
    return fact


def lift(fact, value, *, z=0.13, seq=3, age=2, cfg=None, now=NOW):
    return h.assess_lift(
        fact, h.Reading(seq, now - age, value, z), cfg or config(), now_ms=now
    )


@pytest.mark.parametrize(
    "value, z, status, reason",
    [
        (0.2986, 0.13, "holding_verified", "inside_band_after_lift"),
        (
            0.2986,
            0.1201,
            "holding_verified",
            "inside_band_after_lift",
        ),  # just over 2 cm
        (0.2986, 0.1199, "unknown", "lift_too_small"),
        (0.2986, 0.10, "unknown", "lift_too_small"),
        (0.2986, None, "unknown", "lift_height_missing"),
        (0.01, 0.13, "empty", "fell_below_holding_band_after_close"),
        (0.01, 0.10, "empty", "fell_below_holding_band_after_close"),
        (0.95, 0.13, "open", "opened_after_close"),
        (0.60, 0.13, "unknown", "between_band_and_open"),
        (None, 0.13, "unknown", "gripper_reading_missing"),
    ],
)
def test_lift_table(value, z, status, reason):
    fact = lift(candidate(), value, z=z)
    assert (fact.status, fact.reason) == (status, reason)
    assert fact.holding_verified is (
        True
        if status == "holding_verified"
        else (False if status in ("empty", "open") else None)
    )


def test_a_close_alone_never_verifies_holding():
    assert candidate().holding_verified is None
    assert candidate().status == "holding_candidate"


def test_lift_needs_a_candidate_and_a_newer_fresh_reading():
    empty = close(0.01, 0.01)
    assert lift(empty, 0.2986).reason == "holding_candidate_required"
    verified = lift(candidate(), 0.2986)
    assert lift(verified, 0.2986, seq=4).reason == "holding_candidate_required"
    cand = candidate()
    assert lift(cand, 0.2986, seq=2).reason == "observation_not_new"
    assert lift(cand, 0.2986, age=101).status == "refused"
    assert lift(cand, 0.2986, age=101).gate_observation() is None
    other = config(object_id="cup")
    assert lift(cand, 0.2986, cfg=other).reason == "holding_candidate_required"
    assert lift(cand, 0.2986, cfg=config(lo=0.2)).reason == "holding_candidate_required"
    assert lift(cand, 0.2986, cfg=config(lift_min_m=0.05)).reason == "lift_too_small"


# --- gate hand-off ----------------------------------------------------------


def test_fact_feeds_the_unchanged_gate_with_a_real_source():
    cand = candidate()
    obs = cand.gate_observation(approach_verified=True, at_grasp_pose=True)
    assert isinstance(obs, GateObservation)
    assert obs.source == "verified_measurement"
    assert (obs.seq, obs.captured_ms) == (2, NOW - 10)
    assert obs.closed_measured is True and obs.open_measured is False
    assert obs.holding_verified is None

    state = GateState(
        "run", "eggplant", 1, phase="close", attempts=1, phase_started_ms=NOW - 100
    )
    state2, decision = advance_regrasp(state, obs, (0, 0, 0, 0, 0, 0, 0.0), now_ms=NOW)
    assert decision["reason"] == "closure_requires_lift_holding_verification"
    assert decision["evidence"]["source"] == "verified_measurement"
    assert decision["evidence"]["evidence_kind"] == "recorded_or_verified"

    verified = lift(cand, 0.2986).gate_observation(
        approach_verified=True, at_grasp_pose=True
    )
    _, decision = advance_regrasp(state2, verified, (0, 0, 0, 0, 0, 0, 0.0), now_ms=NOW)
    assert decision["reason"] == "holding_verified_requires_handoff_check"
    assert decision["required_skill"] == "validate_handoff"

    # The gate's own freshness check still applies to the fact's capture time.
    late = advance_regrasp(state2, verified, (0, 0, 0, 0, 0, 0, 0.0), now_ms=NOW + 200)[
        1
    ]
    assert late["reason"] == "observation_stale"


def test_empty_lift_allows_a_gate_retry_and_open_does_not_claim_empty():
    cand = candidate()
    obs = lift(cand, 0.01).gate_observation(approach_verified=True, at_grasp_pose=True)
    assert obs.empty_verified is True and obs.holding_verified is False
    state = GateState(
        "run",
        "eggplant",
        1,
        phase="lift_verify",
        attempts=1,
        phase_started_ms=NOW - 100,
    )
    _, decision = advance_regrasp(state, obs, (0,) * 6 + (0.0,), now_ms=NOW)
    assert decision["required_skill"] == "open_gripper"
    opened = lift(cand, 0.95).gate_observation()
    assert opened.empty_verified is None and opened.open_measured is True


def test_record_is_self_contained_json():
    record = lift(candidate(), 0.2986).record()
    assert record["schema"] == "levi.holding_fact.v1"
    for key in (
        "source",
        "band",
        "open_min",
        "stable_delta",
        "lift_min_m",
        "readings",
        "seq",
        "captured_ms",
        "lift_m",
        "run_id",
        "sample_id",
        "policy_epoch",
    ):
        assert key in record
    assert record["band"] == [0.15, 0.45]
    assert json.loads(json.dumps(record)) == record


# --- adapter ---------------------------------------------------------------


def test_observation_reader_reads_measured_values_only_and_never_guesses():
    clock = iter(range(1000, 2000, 10))
    calls = []

    def observe():
        calls.append(1)
        return {
            "pose": [0.3, 0.0, 0.12, 0, 0, 0, 0.0],  # [6] is the command
            "robot_state": {"gripper_position": 0.2986},
        }

    reader = h.ObservationReader(observe, lambda: next(clock))
    first = reader.read()
    assert (first.seq, first.captured_ms, first.gripper, first.z_m) == (
        1,
        1000,
        0.2986,
        0.12,
    )
    assert reader.read().seq == 2 and len(calls) == 2
    missing = h.ObservationReader(lambda: {"pose": [0.1]}, lambda: 5).read()
    assert missing.gripper is None and missing.z_m is None
    assert h.ObservationReader(lambda: "x", lambda: 5).read().gripper is None
    with pytest.raises(TypeError):
        h.ObservationReader(None, lambda: 5)


# --- drop detector ---------------------------------------------------------


def drop_config(**kw):
    return h.DropConfig(BAND[0], BAND[1], **kw)


def run(detector, rows, start=0):
    hits = []
    for i, (commanded, measured) in enumerate(rows, start):
        hit = detector.feed(i, i * 100, commanded, measured)
        if hit is not None:
            hits.append((i, hit))
    return hits


def test_drop_fires_once_after_n_low_samples_with_the_run_start():
    det = h.DropDetector(drop_config(samples=3, arm_samples=2))
    rows = [(0.0, 0.30)] * 3 + [(0.0, 0.01)] * 5
    assert run(det, rows) == [(5, (3, 300))]


@pytest.mark.parametrize(
    "rows",
    [
        [(0.0, 0.30)] * 12,  # stays in band
        [(1.0, 0.30)] * 3 + [(1.0, 0.01)] * 6,  # command open
        [(0.0, 0.30)] * 3 + [(1.0, 0.01)] * 6,  # released on purpose
        [(0.0, 0.01)] * 12,  # never held: an empty close is not a drop
        [(0.0, 0.30)] * 3 + [(0.0, 0.01)] * 2 + [(0.0, 0.30)] + [(0.0, 0.01)] * 2,
        [(0.0, 0.30)] * 3 + [(0.0, 0.01), (0.0, None), (0.0, 0.01), (0.0, 0.01)],
        [(0.0, 0.95)] * 3 + [(0.0, 0.01)] * 6,  # never in band: not armed
        [
            (0.0, 0.30),
            (0.0, 0.30),
            (0.0, 0.01),
            (0.0, 0.01),
            (0.0, 0.01),
        ],  # one arm sample
    ],
)
def test_no_drop_event(rows):
    det = h.DropDetector(drop_config(samples=3, arm_samples=3))
    assert run(det, rows) == []


def test_drop_rearms_only_after_holding_again_and_gap_resets():
    det = h.DropDetector(drop_config(samples=2, arm_samples=2))
    rows = [(0.0, 0.3)] * 2 + [(0.0, 0.01)] * 4
    assert len(run(det, rows)) == 1
    assert run(det, [(0.0, 0.01)] * 4, 10) == []
    det.reset()
    assert run(det, [(0.0, 0.3)] * 2 + [(0.0, 0.01)] * 2, 20) != []
    det = h.DropDetector(drop_config(samples=2, arm_samples=2))
    run(det, [(0.0, 0.3)] * 2)
    det.reset()  # observation gap
    assert run(det, [(0.0, 0.01)] * 4, 5) == []


def test_drop_uses_the_measured_value_not_the_command():
    det = h.DropDetector(drop_config(samples=2, arm_samples=1))
    # command 0.0 (closed) reads as "below the band" if mistaken for the opening
    assert run(det, [(0.0, 0.30)] * 20) == []
    # and a measured empty with the command at 0.4 (still closed) fires
    det = h.DropDetector(drop_config(samples=2, arm_samples=1))
    assert len(run(det, [(0.4, 0.30)] + [(0.4, 0.01)] * 2)) == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"lo": 0.0, "hi": 0.4},
        {"lo": 0.4, "hi": 0.2},
        {"lo": 0.1, "hi": 1.0},
        {"lo": 0.1, "hi": 0.4, "samples": 0},
        {"lo": 0.1, "hi": 0.4, "arm_samples": True},
        {"lo": 0.1, "hi": 0.4, "roles": ()},
        {"lo": 0.1, "hi": 0.4, "roles": ("other",)},
        {"lo": 0.1, "hi": 0.4, "command_closed_below": 0},
    ],
)
def test_bad_drop_configuration_is_refused(kwargs):
    with pytest.raises(ValueError):
        h.DropConfig(**kwargs)
