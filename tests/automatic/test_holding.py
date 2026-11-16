"""Measured holding fact: proof, band, steadiness, freshness, lift recheck, source."""

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
GAP = 40  # ms between the two lift readings
SETTLE = 220  # ms spanned by the close readings (settle window default 200)


def config(**changes):
    fields = {"object_id": "eggplant", "lo": BAND[0], "hi": BAND[1]}
    fields.update(changes)
    return h.HoldingConfig(**fields)


def rd(seq, value, ms, z=0.10, generation="seq"):
    return h.Reading(seq, ms, value, z, seq if generation == "seq" else generation)


def close(
    a,
    b,
    cfg=None,
    *,
    age=10,
    gap=SETTLE,
    proven=True,
    after_seq=0,
    after_ms=-1,
    now=NOW,
):
    gen = "seq" if proven else None
    first = rd(1, a, now - age - gap, generation=gen)
    second = rd(2, b, now - age, generation=gen)
    return h.assess_close(
        [first, second],
        cfg or config(),
        run_id="run",
        sample_id="eggplant",
        policy_epoch=1,
        now_ms=now,
        after_seq=after_seq,
        after_ms=after_ms,
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
    json.dumps(fact.record())
    assert "physical_allowed" not in fact.record()


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
        (100, "holding_candidate"),
        (101, "refused"),
        (500, "refused"),
    ],
)
def test_stale_readings_are_refused(age, status):
    # the latest reading may be 100 ms old; earlier ones only need the span
    fact = close(0.2986, 0.2986, age=age)
    assert fact.status == status
    if status == "refused":
        assert fact.reason == "observation_stale"
        assert fact.gate_observation() is None
    assert h.MAX_AGE_MS == offline_harness.MAX_OBSERVATION_AGE_MS == 100


def test_a_reading_from_the_future_or_not_newer_is_refused():
    assert close(0.2986, 0.2986, age=-5).status == "refused"
    assert close(0.2986, 0.2986, after_seq=2).reason == "observation_not_new"
    assert close(0.2986, 0.2986, after_ms=NOW - 10).reason == "observation_not_new"
    r = rd(2, 0.3, NOW - 10)
    again = h.assess_close(
        [r, r],
        config(),
        run_id="run",
        sample_id="eggplant",
        policy_epoch=1,
        now_ms=NOW,
        after_seq=0,
        after_ms=-1,
    )
    assert again.reason == "observation_not_new"


def test_after_the_close_command_is_required():
    r1, r2 = rd(1, 0.3, NOW - 50), rd(2, 0.3, NOW - 10)
    with pytest.raises(TypeError):
        h.assess_close(
            [r1, r2], config(), run_id="r", sample_id="s", policy_epoch=1, now_ms=NOW
        )
    with pytest.raises(ValueError):
        h.assess_close(
            [r1, r2],
            config(),
            run_id="r",
            sample_id="s",
            policy_epoch=1,
            now_ms=NOW,
            after_seq=None,
            after_ms=None,
        )


def test_repeated_or_too_close_measurements_are_not_steady_evidence():
    # the same generation twice (a cached observation) is one measurement
    first, second = (
        rd(1, 0.30, NOW - 50, generation=7),
        rd(2, 0.30, NOW - 10, generation=7),
    )
    fact = h.assess_close(
        [first, second],
        config(),
        run_id="r",
        sample_id="s",
        policy_epoch=1,
        now_ms=NOW,
        after_seq=0,
        after_ms=-1,
    )
    assert (fact.status, fact.reason) == ("unknown", "gripper_measurement_not_distinct")
    assert close(0.30, 0.30, gap=10).reason == "readings_too_close"
    assert close(0.30, 0.30, gap=199).reason == "settle_window_too_short"
    assert close(0.30, 0.30, gap=200).status == "holding_candidate"
    assert (
        close(0.30, 0.30, gap=30, cfg=config(settle_ms=30)).status
        == "holding_candidate"
    )


def test_max_age_may_only_tighten_and_interval_must_fit():
    assert close(0.2986, 0.2986, age=70, cfg=config(max_age_ms=60)).status == "refused"
    for bad in (101, 0, 1.5):
        with pytest.raises(ValueError):
            config(max_age_ms=bad)
    with pytest.raises(ValueError):
        config(min_interval_ms=100)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"lo": 0.0},  # lo 0 would call an empty close "holding"
        {"lo": -0.1},
        {"hi": 0.1},
        {"hi": 0.95},
        {"hi": 0.3},  # copies of the synthetic closed limit
        {"hi": 0.3000001},
        {"hi": 0.31},
        {"hi": 0.281},
        {"lo": 0.29, "hi": 0.45},
        {"synthetic": "yes"},
        {"stable_delta": 0.5},
        {"max_drift": 0},
        {"lift_min_m": 0},
        {"max_gap_ms": 0},
    ],
)
def test_bad_real_configuration_is_refused(kwargs):
    with pytest.raises(ValueError):
        config(**kwargs)


def test_real_band_just_outside_the_synthetic_margin_is_accepted():
    assert config(hi=0.32).hi == 0.32
    assert config(lo=0.1, hi=0.27).hi == 0.27


def test_synthetic_thresholds_apply_only_to_a_synthetic_source():
    synthetic = h.synthetic_config()
    assert synthetic.synthetic is True
    assert (synthetic.hi, synthetic.open_min) == (0.3, 0.9)
    # 0.3014 is outside the synthetic closed limit but inside a real band.
    assert close(0.3014, 0.3014, cfg=synthetic).status == "unknown"
    assert close(0.3014, 0.3014).status == "holding_candidate"
    fact = close(0.2, 0.2, cfg=synthetic)
    assert fact.source == "synthetic_measurement" and not fact.is_real
    with pytest.raises(ValueError):
        h.require_real(fact)
    # synthetic is valid only with exactly the synthetic numbers
    with pytest.raises(ValueError):
        h.HoldingConfig("x", 0.1, 0.4, synthetic=True)
    with pytest.raises(ValueError):
        h.HoldingConfig("x", 0.0, 0.3, synthetic=True, open_min=0.95)
    # and a real config cannot be built from them
    for lo in (0.0, 0.01):
        with pytest.raises(ValueError):
            h.HoldingConfig("x", lo, 0.3, open_min=0.9)
    with pytest.raises(ValueError):
        h.DropConfig.from_holding(synthetic)


def test_source_is_stamped_by_proof_not_by_the_caller():
    assert close(0.2986, 0.2986).source == "verified_measurement"
    unproven = close(0.2986, 0.2986, proven=False)
    assert unproven.source == "recorded_measurement" and not unproven.is_real
    with pytest.raises(ValueError):
        h.require_real(unproven)
    assert unproven.gate_observation().source == "recorded_measurement"
    mixed = h.assess_close(
        [rd(1, 0.3, NOW - 50), rd(2, 0.3, NOW - 10, generation=None)],
        config(),
        run_id="r",
        sample_id="s",
        policy_epoch=1,
        now_ms=NOW,
        after_seq=0,
        after_ms=-1,
    )
    assert mixed.source == "recorded_measurement"
    assert h.require_real(close(0.2986, 0.2986)) is not None


def test_parse_holding_bands():
    parsed = h.parse_holding_bands({"eggplant": [0.15, 0.45]})
    assert parsed["eggplant"].band == (0.15, 0.45)
    assert parsed["eggplant"].synthetic is False
    for bad in (
        {},
        None,
        [],
        {"a": [0.1]},
        {"a": [0.5, 0.2]},
        {"a": ["x", 1]},
        {"a": [0, 0.4]},
        {"a": [0.1, 0.3]},
    ):
        with pytest.raises(ValueError):
            h.parse_holding_bands(bad)


# --- lift recheck ---------------------------------------------------------


def candidate(value=0.2986, **kw):
    # assessed 200 ms before the lift stage
    fact = close(value, value, now=NOW - 200, **kw)
    assert fact.status == "holding_candidate"
    return fact


def lift(
    fact,
    a,
    b=None,
    *,
    z=0.13,
    z2=None,
    age=10,
    gap=GAP,
    cfg=None,
    now=NOW,
    start=3,
    same_generation=False,
):
    """Two lift readings; the second is ``age`` ms before ``now``. The
    candidate's last reading is at NOW - 10 (ms), so the first sits later."""
    b = a if b is None else b
    t2 = now - age
    r1 = h.Reading(start, t2 - gap, a, z, start)
    r2 = h.Reading(
        start + 1,
        t2,
        b,
        z if z2 is None else z2,
        start if same_generation else start + 1,
    )
    return h.assess_lift(fact, r1, r2, cfg or config(), now_ms=now)


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
    expected = (
        True
        if status == "holding_verified"
        else (False if status in ("empty", "open") else None)
    )
    assert fact.holding_verified is expected


def test_a_close_alone_never_verifies_holding():
    assert candidate().holding_verified is None
    assert candidate().status == "holding_candidate"


def test_one_lift_reading_is_not_enough_and_both_must_pass():
    cand = candidate()
    base = cand.readings[-1]
    with pytest.raises(TypeError):
        h.assess_lift(
            cand, h.Reading(3, NOW - 5, 0.2986, 0.13, 3), config(), now_ms=NOW
        )
    # second reading jumps: not steady
    assert lift(cand, 0.2986, 0.2686).reason == "gripper_not_steady"
    # the second reading's height did not rise
    assert lift(cand, 0.2986, z=0.13, z2=0.10).reason == "lift_too_small"
    # the same measurement twice
    assert (
        lift(cand, 0.2986, same_generation=True).reason
        == "gripper_measurement_not_distinct"
    )
    # two readings must be apart in time
    assert lift(cand, 0.2986, gap=10).reason == "readings_too_close"
    assert base.captured_ms == NOW - 210


def test_slow_close_on_air_does_not_verify_after_a_lift():
    # review scenario: 0.40 / 0.395 (still closing) -> candidate; after a 3 cm
    # lift the opening has moved to 0.20, still inside the band.
    cand = candidate(0.40)
    fact = lift(cand, 0.20, z=0.13)
    assert fact.status == "unknown" and fact.reason == "gripper_drifted_after_close"
    assert fact.holding_verified is None
    # a small drift inside max_drift is fine, a larger one is not
    assert lift(cand, 0.385).status == "holding_verified"
    assert lift(cand, 0.37).reason == "gripper_drifted_after_close"
    assert lift(cand, 0.37, cfg=config(max_drift=0.04)).status == "holding_verified"


def test_lift_needs_a_candidate_and_newer_fresh_readings():
    empty = close(0.01, 0.01)
    assert lift(empty, 0.2986).reason == "holding_candidate_required"
    verified = lift(candidate(), 0.2986)
    assert lift(verified, 0.2986, start=5).reason == "holding_candidate_required"
    cand = candidate()
    assert lift(cand, 0.2986, start=1).reason == "observation_not_new"
    assert lift(cand, 0.2986, age=101).status == "refused"
    assert lift(cand, 0.2986, age=101).gate_observation() is None
    assert lift(cand, 0.2986, age=70).reason == "observation_stale"  # first is 110 old
    assert (
        lift(cand, 0.2986, cfg=config(object_id="cup")).reason
        == "holding_candidate_required"
    )
    assert lift(cand, 0.2986, cfg=config(lo=0.2)).reason == "holding_candidate_required"
    assert lift(cand, 0.2986, cfg=config(lift_min_m=0.05)).reason == "lift_too_small"


def test_lift_must_follow_the_candidate_closely():
    # candidate captured long ago: a release and regrasp may have happened since
    cand = candidate()
    base_ms = cand.readings[-1].captured_ms
    late_now = base_ms + 6000
    fact = lift(cand, 0.2986, now=late_now, age=10)
    assert fact.reason == "lift_readings_too_late"
    ok = lift(cand, 0.2986, now=base_ms + 4000, age=10)
    assert ok.status == "holding_verified"
    assert (
        lift(cand, 0.2986, now=late_now, cfg=config(max_gap_ms=10_000)).status
        == "holding_verified"
    )


def test_lift_source_must_match_the_candidate():
    cand = candidate()
    base = cand.readings[-1]
    r1 = h.Reading(3, NOW - 50, 0.2986, 0.13, None)  # unproven
    r2 = h.Reading(4, NOW - 10, 0.2986, 0.13, None)
    fact = h.assess_lift(cand, r1, r2, config(), now_ms=NOW)
    assert fact.reason == "source_changed_between_stages"
    assert base.proven


# --- gate hand-off ----------------------------------------------------------


def test_fact_feeds_the_unchanged_gate_with_a_real_source():
    cand = candidate()
    now_cand = close(0.2986, 0.2986, age=60)
    obs = now_cand.gate_observation(approach_verified=True, at_grasp_pose=True)
    assert isinstance(obs, GateObservation)
    assert obs.source == "verified_measurement"
    assert (obs.seq, obs.captured_ms) == (2, NOW - 60)
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
    assert len(record["readings"]) == 3 and record["readings"][1]["generation"] == 3
    assert json.loads(json.dumps(record)) == record


# --- reader: the measured-gripper proof contract ---------------------------


class Clock:
    domain = "host-mono:test"

    def __init__(self):
        self.ns = 1_000_000_000

    def now(self):
        return self.ns


class Rig:
    """A fake robot: each observe() is one getstate read with a fresh proof."""

    def __init__(self, clock):
        self.clock, self.generation = clock, 0
        self.gripper, self.z = 0.2986, 0.10
        self.proof, self.fresh, self.fill = True, True, None

    def observe(self):
        started = self.clock.now()
        self.clock.ns += 1_000_000
        self.generation += 1
        value = {
            "observed_ns": self.clock.now(),
            "pose": [0.3, 0.0, self.z, 0, 0, 0, 0.0],  # [6] is the command
            "robot_state": {
                "gripper_position": self.gripper if self.fill is None else self.fill
            },
            "state_fresh": self.fresh,
            "gripper_measurement": None,
        }
        if self.proof:
            value["gripper_measurement"] = {
                "route": "getstate",
                "field": "gripper_pos",
                "generation": self.generation,
                "read_from_ns": started,
                "read_through_ns": self.clock.now(),
                "clock_domain": self.clock.domain,
                "value": self.gripper,
            }
        return value


def reader():
    clock = Clock()
    rig = Rig(clock)
    return clock, rig, h.ObservationReader(rig.observe, clock)


def test_reader_gives_a_proven_reading_with_the_proofs_time():
    clock, _, reader_ = reader()
    first = reader_.read()
    assert (first.seq, first.gripper, first.z_m, first.generation) == (
        1,
        0.2986,
        0.10,
        1,
    )
    assert first.captured_ms == clock.ns // 1_000_000  # read_through_ns
    assert reader_.read().seq == 2 and reader_.read().generation == 3
    assert first.proven


@pytest.mark.parametrize("case", ["no_proof", "state_stale", "sdk_zero"])
def test_reader_refuses_an_unproven_gripper_and_never_reports_empty(case):
    _, rig, reader_ = reader()
    if case == "no_proof":
        rig.proof = False  # getstate had no gripper_pos
    elif case == "state_stale":
        rig.fresh = False
    else:
        rig.fill = 0.0  # the SDK filled a zero; the proof says 0.2986
    first, second = reader_.read(), reader_.read()
    assert first.gripper is None and second.gripper is None and not first.proven
    fact = h.assess_close(
        [first, second],
        config(),
        run_id="r",
        sample_id="s",
        policy_epoch=1,
        now_ms=second.captured_ms + 5,
        after_seq=0,
        after_ms=-1,
    )
    assert fact.status == "unknown" and fact.empty_verified is None
    assert fact.gate_observation().empty_verified is None


def test_the_review_scenario_sdk_zero_without_a_proof_is_unknown():
    clock = Clock()
    obs = {
        "pose": [0, 0, 0.1, 0, 0, 0, 0.0],
        "robot_state": {"gripper_position": 0.0},
        "state_fresh": False,
        "gripper_measurement": None,
    }

    def observe():
        clock.ns += 40_000_000
        return dict(obs)

    rdr = h.ObservationReader(observe, clock)
    x, y = rdr.read(), rdr.read()
    assert x.gripper is None and y.gripper is None
    fact = h.assess_close(
        [x, y],
        config(),
        run_id="r",
        sample_id="s",
        policy_epoch=1,
        now_ms=clock.now() // 1_000_000,
        after_seq=0,
        after_ms=-1,
    )
    assert fact.status == "unknown" and fact.empty_verified is None
    assert fact.gate_observation().source == "recorded_measurement"


def test_the_same_observation_twice_is_not_two_measurements():
    clock, rig, _ = reader()
    # a cached observation: its proof window lies before the second call
    cached = rig.observe()
    rdr = h.ObservationReader(lambda: cached, clock)
    assert rdr.read().gripper is None and rdr.read().gripper is None
    # a robot whose generation does not advance (a stuck getstate)
    stuck = Rig(clock)

    def observe():
        out = stuck.observe()
        out["gripper_measurement"]["generation"] = 5
        return out

    rdr = h.ObservationReader(observe, clock)
    first = rdr.read()
    clock.ns += 50_000_000
    second = rdr.read()
    assert first.proven and second.gripper is None
    fact = h.assess_close(
        [first, second],
        config(),
        run_id="r",
        sample_id="s",
        policy_epoch=1,
        now_ms=clock.now() // 1_000_000,
        after_seq=0,
        after_ms=-1,
    )
    assert fact.status == "unknown"


@pytest.mark.parametrize(
    "field, value",
    [
        ("route", "other"),
        ("field", "gripper_cmd"),
        ("generation", 0),
        ("clock_domain", "other-domain"),
        ("value", 0.5),  # differs from robot_state.gripper_position
        ("read_from_ns", 5),  # before this call
        ("read_through_ns", 10**18),  # after this call
    ],
)
def test_reader_checks_every_proof_field(field, value):
    clock = Clock()
    rig = Rig(clock)

    def observe():
        out = rig.observe()
        out["gripper_measurement"][field] = value
        return out

    assert h.ObservationReader(observe, clock).read().gripper is None


def test_reader_rejects_extra_fields_and_bad_constructor_input():
    clock = Clock()
    rig = Rig(clock)

    def observe():
        out = rig.observe()
        out["gripper_measurement"]["extra"] = 1
        return out

    assert h.ObservationReader(observe, clock).read().gripper is None
    assert h.ObservationReader(lambda: "x", clock).read().gripper is None
    with pytest.raises(TypeError):
        h.ObservationReader(None, clock)
    with pytest.raises(TypeError):
        h.ObservationReader(dict, object())


def test_two_proven_readings_make_a_verified_close_and_lift_end_to_end():
    clock, rig, reader_ = reader()
    after_seq, after_ms = 0, clock.ns // 1_000_000 - 1
    r1 = reader_.read()
    clock.ns += 220_000_000
    r2 = reader_.read()
    now = clock.ns // 1_000_000 + 5
    cand = h.assess_close(
        [r1, r2],
        config(),
        run_id="r",
        sample_id="s",
        policy_epoch=1,
        now_ms=now,
        after_seq=after_seq,
        after_ms=after_ms,
    )
    assert cand.status == "holding_candidate" and cand.is_real
    rig.z = 0.13
    clock.ns += 100_000_000
    r3 = reader_.read()
    clock.ns += 40_000_000
    r4 = reader_.read()
    verified = h.assess_lift(cand, r3, r4, config(), now_ms=clock.ns // 1_000_000 + 5)
    assert verified.status == "holding_verified" and verified.is_real
    assert h.require_real(verified) is verified


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
        [(0.0, 0.30)] * 12,  # stays inside the band
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
        # review scenario: an empty close sweeping through the band (no stable run)
        [(0.0, v) for v in (0.9, 0.6, 0.44, 0.35, 0.25, 0.16, 0.05, 0.0, 0.0, 0.0)],
        [(0.0, v) for v in (0.44, 0.35, 0.25, 0.16, 0.05, 0.0, 0.0, 0.0, 0.0, 0.0)],
    ],
)
def test_no_drop_event(rows):
    det = h.DropDetector(drop_config(samples=3, arm_samples=3))
    assert run(det, rows) == []


def test_arming_needs_a_steady_run_not_just_band_membership():
    cfg = drop_config(samples=2, arm_samples=3)
    sweep = [(0.0, 0.44), (0.0, 0.40), (0.0, 0.36), (0.0, 0.32), (0.0, 0.28)]
    sweep += [(0.0, 0.01)] * 4
    assert run(h.DropDetector(cfg), sweep) == []
    # the same count of steady in-band steps arms it
    steady = [(0.0, 0.30), (0.0, 0.305), (0.0, 0.31)] + [(0.0, 0.01)] * 3
    assert len(run(h.DropDetector(cfg), steady)) == 1
    # a jump inside the band restarts the run
    jump = [(0.0, 0.30), (0.0, 0.305), (0.0, 0.40)] + [(0.0, 0.01)] * 3
    assert run(h.DropDetector(cfg), jump) == []


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


def test_drop_config_describes_itself():
    text = drop_config(samples=4, arm_samples=2).describe()
    for part in (
        "lo=0.15",
        "hi=0.45",
        "samples=4",
        "arm_samples=2",
        "stable_delta=0.02",
    ):
        assert part in text


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
        {"lo": 0.1, "hi": 0.4, "stable_delta": 0.5},
    ],
)
def test_bad_drop_configuration_is_refused(kwargs):
    with pytest.raises(ValueError):
        h.DropConfig(**kwargs)


# --- residuals of the second review ------------------------------------------


def series(values, *, step_ms=100, end=NOW - 10, z=0.10):
    """Proven readings of one stage, oldest first, ending ``end`` ms."""
    n = len(values)
    return [
        h.Reading(i + 1, end - (n - 1 - i) * step_ms, v, z, i + 1)
        for i, v in enumerate(values)
    ]


def assess(readings, cfg=None):
    return h.assess_close(
        readings,
        cfg or config(),
        run_id="run",
        sample_id="eggplant",
        policy_epoch=1,
        now_ms=NOW,
        after_seq=0,
        after_ms=-1,
    )


def test_settled_window_is_required_not_just_two_close_readings():
    # still closing: 0.40 -> 0.395 looks steady between two neighbours
    assert assess(series([0.40, 0.395], step_ms=40)).reason == "settle_window_too_short"
    # a long enough window that keeps moving is not settled either
    moving = series([0.40, 0.385, 0.37, 0.355])
    assert assess(moving).reason == "gripper_not_steady"
    settled = series([0.2986, 0.2990, 0.2994])
    assert assess(settled).status == "holding_candidate"
    assert assess(series([0.2986, 0.2994], step_ms=200)).status == "holding_candidate"
    with pytest.raises(ValueError):
        assess(series([0.3]))


def test_slow_close_on_air_is_rejected_even_with_a_large_max_drift():
    # review scenario: 0.40, 0.395 (still closing), lift reads 0.20
    loose = config(max_drift=0.06, settle_ms=200)
    early = assess(series([0.40, 0.395], step_ms=40), loose)
    assert early.status == "unknown" and early.reason == "settle_window_too_short"
    assert early.gate_observation().holding_verified is None
    # the settle window cannot be skipped: no candidate, no verified lift
    slow = assess(series([0.40, 0.385, 0.37]), loose)
    assert slow.status == "unknown"
    cand = candidate()
    verified = lift(cand, 0.2994, cfg=loose)  # a normal hold stays verified
    assert verified.status == "holding_verified"
    # max_drift only bounds compression during the lift
    assert lift(cand, 0.25, cfg=loose).status == "holding_verified"
    assert lift(cand, 0.25).reason == "gripper_drifted_after_close"


def test_normal_hold_is_still_verified_after_the_settle_window():
    cand = h.assess_close(
        series([0.2986, 0.2990, 0.2994], end=NOW - 200),
        config(),
        run_id="run",
        sample_id="eggplant",
        policy_epoch=1,
        now_ms=NOW - 200,
        after_seq=0,
        after_ms=-1,
    )
    assert cand.status == "holding_candidate" and cand.is_real
    verified = lift(cand, 0.2994, 0.2990, z=0.13, start=4)
    assert verified.status == "holding_verified"


def test_settle_ms_is_configuration():
    for bad in (0, -1, 5001, 1.5):
        with pytest.raises(ValueError):
            config(settle_ms=bad)
    assert (
        assess(series([0.3, 0.3], step_ms=100), config(settle_ms=100)).status
        == "holding_candidate"
    )
    assert assess(series([0.3, 0.3], step_ms=100)).status == "unknown"


def test_the_margin_around_the_synthetic_limit_is_exact_not_float_noise():
    assert abs(0.28 - 0.3) < h.SYNTHETIC_MARGIN  # the float edge: 0.0199999...
    assert config(lo=0.20, hi=0.28).band == (0.20, 0.28)
    assert config(lo=0.32, hi=0.5).band == (0.32, 0.5)
    for bad in ((0.20, 0.2801), (0.31, 0.5), (0.3199, 0.5), (0.2999, 0.4)):
        with pytest.raises(ValueError):
            config(lo=bad[0], hi=bad[1])


def test_drop_arming_checks_the_whole_window_not_adjacent_steps():
    # review scenario: a scan descending 0.015 per step (adjacent <= 0.02) must
    # not arm, however long it takes, and so never fires object_dropped
    det = h.DropDetector(drop_config())
    sweep = [0.9 - 0.015 * i for i in range(60)]
    assert run(det, [(0.0, max(v, 0.0)) for v in sweep]) == []
    # a window that really holds still arms
    cfg = drop_config(samples=2, arm_samples=3)
    still = [(0.0, 0.30), (0.0, 0.305), (0.0, 0.31)] + [(0.0, 0.01)] * 3
    assert len(run(h.DropDetector(cfg), still)) == 1
    # three steps spanning 0.03 do not
    wide = [(0.0, 0.30), (0.0, 0.315), (0.0, 0.33)] + [(0.0, 0.01)] * 3
    assert run(h.DropDetector(cfg), wide) == []
    # and the window slides: a long steady run after a wobble arms
    slide = [(0.0, 0.40), (0.0, 0.30)] + [(0.0, 0.30)] * 3 + [(0.0, 0.01)] * 3
    assert len(run(h.DropDetector(cfg), slide)) == 1
