"""Injected clocks, cached readings and fake sinks only; no hardware imports."""

import math
import threading
from dataclasses import replace

import pytest

from levi.marvel0.manual import (
    Dependencies,
    Engine,
    Limits,
    ManualRefused,
    Reading,
    SettledEvidence,
)
from levi.marvel0.manual_worker import Ack, PoseWorker, production_factory


class Rig:
    def __init__(self, **changes):
        self.time = 100.0
        self.position = (0.5, 0.5, 0.5)
        self.quaternion = (0.0, 0.0, 0.0, 1.0)
        self.joints = (0.0,) * 7  # explicit synthetic fixture, never telemetry fallback
        self.read_seq = 0
        self.audit = []
        self.calls = []
        self.closed = False
        self.read_override = None
        self.validation = lambda reading, pose: True
        self.on_send = lambda pose: Ack(True, "synthetic_ack")
        self.limits = Limits(
            box_min_m=(0.0, 0.0, 0.0),
            box_max_m=(1.0, 1.0, 1.0),
            max_delta_m=0.1,
            max_step_m=0.02,
            max_velocity_m_s=0.05,
            period_s=0.1,
            max_reading_age_s=1.0,
            max_anchor_age_s=10.0,
            deadman_timeout_s=0.3,
            target_timeout_s=0.6,
            max_orientation_drift_rad=0.01,
            quaternion_norm_tolerance=0.001,
            max_settle_age_s=1.0,
        )
        self.limits = replace(self.limits, **changes)
        self.anchor = self.reading()
        self.worker = PoseWorker(self)
        self.engine = Engine(
            session_id="session-one",
            generation=1,
            anchor=self.anchor,
            limits=self.limits,
            worker=self.worker,
            dependencies=Dependencies(
                now=lambda: self.time,
                reading=self.reading,
                validate_path=lambda reading, pose: self.validation(reading, pose),
                audit=self.audit.append,
            ),
        )

    def reading(self):
        if self.read_override is not None:
            return self.read_override
        self.read_seq += 1
        return Reading(
            reading_id=f"reading-{self.read_seq}",
            sampled_at_mono=self.time,
            position=self.position,
            quaternion=self.quaternion,
            joints_rad=self.joints,
            ready=True,
            tool_ok=True,
            consistent=True,
        )

    def send(self, pose):
        if self.closed:
            return Ack(False, "closed")
        self.calls.append(pose)
        result = self.on_send(pose)
        if result.acknowledged:
            self.position = pose.position
        return result

    def close_admission(self):
        self.closed = True

    def target(self, delta=(0.02, 0.0, 0.0), sequence=0, **changes):
        body = {
            "session_id": "session-one",
            "generation": 1,
            "anchor_id": self.anchor.reading_id,
            "sequence": sequence,
            "delta_position_m": delta,
        }
        body.update(changes)
        return self.engine.target(**body)

    def press(self, sequence=0, pressed=True, **changes):
        body = {
            "session_id": "session-one",
            "generation": 1,
            "sequence": sequence,
            "pressed": pressed,
        }
        body.update(changes)
        return self.engine.deadman(**body)

    def evidence(self, **changes):
        data = {
            "session_id": "session-one",
            "generation": 1,
            "closed_generation": self.engine.view()["closed_generation"],
            "observed_at_mono": self.time,
            "synthetic_settled": True,
        }
        data.update(changes)
        return SettledEvidence(**data)


def test_construction_and_target_never_send_and_orientation_is_measured():
    rig = Rig()
    assert rig.engine.view()["state"] == "owned_idle"
    assert rig.engine.view()["deadman"]["pressed"] is False
    rig.target()
    rig.engine.pump()
    assert rig.calls == []
    view = rig.engine.view()
    assert view["counters"] == {
        "accepted": 1,
        "sent": 0,
        "acknowledged": 0,
        "settled": 0,
    }
    rig.press()
    rig.engine.pump()
    assert len(rig.calls) == 1
    assert rig.calls[0].quaternion == rig.anchor.quaternion
    assert math.dist(rig.calls[0].position, rig.anchor.position) <= 0.005 + 1e-12
    assert rig.engine.view()["counters"]["acknowledged"] == 1
    assert rig.engine.view()["capabilities"] == {
        "real_motion": False,
        "synthetic_only": True,
    }


def test_latest_target_only_and_rate_does_not_bank_idle_time():
    rig = Rig()
    rig.target((0.0, 0.03, 0.0), sequence=0)
    rig.target((0.03, 0.0, 0.0), sequence=1)
    rig.press()
    rig.time += 0.15
    rig.engine.pump()
    assert len(rig.calls) == 1
    assert rig.calls[0].position[1] == 0.5
    assert rig.calls[0].position[0] == pytest.approx(0.505)
    rig.engine.pump()
    assert len(rig.calls) == 1
    rig.time += 0.11
    rig.press(sequence=1)
    rig.engine.pump()
    assert len(rig.calls) == 2
    assert rig.calls[1].position[0] == pytest.approx(0.510)


@pytest.mark.parametrize(
    "change,code",
    [
        ({"session_id": "someone-else"}, "binding_mismatch"),
        ({"generation": 2}, "binding_mismatch"),
        ({"generation": True}, "binding_mismatch"),
        ({"anchor_id": "other-anchor"}, "anchor_mismatch"),
    ],
)
def test_foreign_binding_cannot_change_an_owned_session(change, code):
    rig = Rig()
    before = rig.engine.view()
    with pytest.raises(ManualRefused, match=code):
        rig.target(**change)
    assert rig.engine.view() == before
    assert rig.calls == []


def test_sequences_are_independent_and_replay_does_not_requeue():
    rig = Rig()
    rig.target(sequence=8)
    rig.press(sequence=8)
    before = rig.engine.view()
    with pytest.raises(ManualRefused, match="sequence_replayed"):
        rig.target(sequence=8)
    with pytest.raises(ManualRefused, match="sequence_replayed"):
        rig.press(sequence=8)
    assert rig.engine.view() == before


@pytest.mark.parametrize(
    "delta,code",
    [
        ((True, 0, 0), "target_invalid"),
        ((float("nan"), 0, 0), "target_invalid"),
        ((float("inf"), 0, 0), "target_invalid"),
        ((0, 0), "target_invalid"),
        ((0.1, 0.1, 0), "delta_exceeded"),
    ],
)
def test_invalid_new_intent_clears_previous_pending_target(delta, code):
    rig = Rig()
    rig.target()
    with pytest.raises(ManualRefused, match=code):
        rig.target(delta, sequence=1)
    assert rig.engine.view()["state"] == "fault_locked"
    assert rig.engine.view()["target"] is None
    assert rig.closed and rig.calls == []


def test_box_and_full_path_are_server_checks():
    rig = Rig(box_max_m=(0.51, 1.0, 1.0))
    with pytest.raises(ManualRefused, match="target_outside_box"):
        rig.target()
    rig = Rig()
    rig.validation = lambda reading, pose: False
    with pytest.raises(ManualRefused, match="path_refused"):
        rig.target()
    assert rig.calls == [] and rig.closed


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("sampled_at_mono", 98.0, "reading_stale"),
        ("sampled_at_mono", 101.0, "reading_stale"),
        ("ready", False, "reading_invalid"),
        ("tool_ok", None, "reading_invalid"),
        ("consistent", False, "reading_invalid"),
        ("joints_rad", (0.0,) * 6, "reading_invalid"),
        ("quaternion", (0, 0, 0, 0), "quaternion_invalid"),
        ("quaternion", (0, 0, 1, 0), "orientation_changed"),
    ],
)
def test_trusted_reading_still_requires_finite_fresh_explicit_gates(field, value, code):
    rig = Rig()
    rig.read_override = replace(rig.anchor, reading_id="new", **{field: value})
    assert rig.engine.tick()["reason_code"] == code
    assert rig.closed and rig.calls == []


def test_deadman_expiry_cannot_be_refreshed_into_a_resume():
    rig = Rig()
    rig.target()
    rig.press()
    rig.time += 0.31
    with pytest.raises(ManualRefused, match="session_closed"):
        rig.press(sequence=1)
    assert rig.engine.view()["reason_code"] == "deadman_expired"
    assert rig.calls == [] and rig.closed
    rig.engine.stop()
    assert rig.engine.view()["state"] == "fault_locked"


def test_target_expiry_cannot_be_refreshed_by_a_late_target():
    rig = Rig()
    rig.target()
    rig.time += 0.61
    with pytest.raises(ManualRefused, match="session_closed"):
        rig.target(sequence=1)
    assert rig.engine.view()["reason_code"] == "target_expired"


def test_reached_target_still_has_a_deadline_with_deadman_alive():
    rig = Rig()
    rig.target((0.001, 0, 0))
    rig.press()
    rig.engine.pump()
    assert rig.engine.view()["target"] is None
    for sequence in range(1, 4):
        rig.time += 0.21
        try:
            rig.press(sequence=sequence)
        except ManualRefused:
            break
    assert rig.engine.tick()["state"] == "fault_locked"
    assert rig.engine.view()["reason_code"] == "target_expired"


def test_releasing_deadman_is_terminal_without_physical_stop_claim():
    rig = Rig()
    rig.target()
    rig.press()
    view = rig.press(sequence=1, pressed=False)
    assert view["state"] == "held" and view["admission"] == "closed"
    assert view["stop_evidence"] == "admission_closed"
    assert view["counters"]["settled"] == 0
    with pytest.raises(ManualRefused, match="session_closed"):
        rig.press(sequence=2)
    with pytest.raises(ManualRefused, match="settle_unproven"):
        rig.engine.end(None)
    assert rig.calls == []


def test_unknown_delivery_locks_admission_and_never_retries():
    rig = Rig()
    rig.target()
    rig.press()
    rig.on_send = lambda pose: Ack(False, "dropped_reply")
    view = rig.engine.pump()
    assert view["reason_code"] == "delivery_unknown"
    assert view["counters"] == {
        "accepted": 1,
        "sent": 1,
        "acknowledged": 0,
        "settled": 0,
    }
    rig.engine.pump()
    assert len(rig.calls) == 1 and rig.closed


def test_late_ack_is_unknown_even_if_it_says_success():
    rig = Rig()
    rig.target()
    rig.press()

    def slow(pose):
        rig.time += 0.31
        return Ack(True, "late")

    rig.on_send = slow
    view = rig.engine.pump()
    assert view["state"] == "fault_locked"
    assert view["reason_code"] == "ack_expired"
    assert view["counters"]["acknowledged"] == 0


def test_stop_during_send_is_immediate_and_ack_cannot_restore_authority():
    rig = Rig()
    rig.target()
    rig.press()
    entered, release = threading.Event(), threading.Event()

    def blocked(pose):
        entered.set()
        assert release.wait(2)
        return Ack(True, "late")

    rig.on_send = blocked
    thread = threading.Thread(target=rig.engine.pump)
    thread.start()
    try:
        assert entered.wait(2)
        # If stop waits for the sender's lock, this call cannot finish here.
        view = rig.engine.stop()
        assert view["admission"] == "closed" and view["in_flight"]
        with pytest.raises(ManualRefused, match="settle_unproven"):
            rig.engine.end(rig.evidence())
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()
    view = rig.engine.view()
    assert view["reason_code"] == "ack_after_stop"
    assert view["counters"]["acknowledged"] == 0
    assert len(rig.calls) == 1


@pytest.mark.parametrize("mode", ["stop", "replace", "expire"])
def test_prepared_send_rechecks_authority_target_and_deadline(mode):
    rig = Rig()
    rig.target()
    rig.press()

    def validation(reading, pose):
        rig.validation = lambda reading, pose: True
        if mode == "stop":
            rig.engine.stop()
        elif mode == "replace":
            rig.target((0, 0.02, 0), sequence=1)
        else:
            rig.time += 0.61
        return True

    rig.validation = validation
    view = rig.engine.pump()
    assert rig.calls == [] and view["counters"]["sent"] == 0
    if mode == "replace":
        assert view["target"]["sequence"] == 1
        rig.engine.pump()
        assert rig.calls[0].position[0] == 0.5
    elif mode == "stop":
        assert view["state"] == "held"
    else:
        assert view["reason_code"] == "prepare_expired"


def test_elapsed_preparation_does_not_allow_immediate_second_send():
    rig = Rig()
    rig.target()
    rig.press()

    def delayed(reading, pose):
        rig.time += 0.2
        rig.validation = lambda reading, pose: True
        return True

    rig.validation = delayed
    rig.engine.pump()
    assert len(rig.calls) == 1
    rig.engine.pump()
    assert len(rig.calls) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"session_id": "other"},
        {"generation": True},
        {"closed_generation": 0},
        {"observed_at_mono": 99.0},
        {"observed_at_mono": 101.0},
        {"synthetic_settled": False},
    ],
)
def test_settled_receipt_is_synthetic_fresh_and_bound_to_closure(change):
    rig = Rig()
    rig.engine.stop()
    with pytest.raises(ManualRefused, match="settle_unproven"):
        rig.engine.end(rig.evidence(**change))
    assert rig.engine.view()["counters"]["settled"] == 0


def test_only_synthetic_settled_receipt_ends_and_counts_once():
    rig = Rig()
    rig.engine.stop()
    view = rig.engine.end(rig.evidence())
    assert view["state"] == "ended"
    assert view["stop_evidence"] == "synthetic_settled"
    assert view["counters"]["settled"] == 1
    assert rig.engine.end(rig.evidence())["counters"]["settled"] == 1


def test_stop_timestamp_is_not_a_previous_read_timestamp():
    rig = Rig()
    rig.time += 0.2
    rig.engine.stop()
    with pytest.raises(ManualRefused, match="settle_unproven"):
        rig.engine.end(rig.evidence(observed_at_mono=100.0))


def test_clock_regression_fails_closed():
    rig = Rig()
    rig.time -= 1
    with pytest.raises(ManualRefused, match="clock_invalid"):
        rig.engine.tick()
    assert rig.closed and rig.engine.view()["state"] == "fault_locked"


def test_mutating_anchor_input_list_does_not_move_session_anchor():
    rig = Rig()
    mutable = [0.5, 0.5, 0.5]
    anchor = replace(rig.anchor, position=mutable)
    sink = Rig()
    engine = Engine(
        session_id="freeze",
        generation=1,
        anchor=anchor,
        limits=rig.limits,
        dependencies=Dependencies(
            lambda: 100.0, lambda: anchor, lambda r, p: True, lambda e: None
        ),
        worker=PoseWorker(sink),
    )
    mutable[0] = 0.8
    assert engine.view()["anchor"]["position"] == [0.5, 0.5, 0.5]


def test_no_implicit_physical_limits_or_production_transport():
    with pytest.raises(TypeError):
        Limits()
    for config in ({}, {"mode": "fake"}, {"stop_proven": True}):
        with pytest.raises(ManualRefused, match="stop_unproven"):
            production_factory(config)


def test_stop_is_not_blocked_by_external_path_validation():
    rig = Rig()
    rig.target()
    rig.press()
    entered, release = threading.Event(), threading.Event()

    def validation(reading, pose):
        entered.set()
        assert release.wait(2)
        return True

    rig.validation = validation
    thread = threading.Thread(target=rig.engine.pump)
    thread.start()
    try:
        assert entered.wait(2)
        assert rig.engine.stop()["state"] == "held"
        assert rig.closed
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()
    assert rig.calls == []
    assert rig.engine.view()["in_flight"] is False


def test_first_fault_reason_survives_later_stop_and_bad_clock():
    rig = Rig()
    rig.target()
    rig.press()
    rig.on_send = lambda pose: Ack(False, "unknown")
    rig.engine.pump()
    assert rig.engine.stop()["reason_code"] == "delivery_unknown"
    rig.time -= 1
    with pytest.raises(ManualRefused):
        rig.engine.tick()
    assert rig.engine.view()["state"] == "fault_locked"
    assert rig.engine.view()["reason_code"] == "delivery_unknown"


def test_stop_does_not_call_or_wait_for_a_blocked_clock():
    rig = Rig()
    entered, release = threading.Event(), threading.Event()

    def blocked_clock():
        entered.set()
        assert release.wait(2)
        return rig.time

    rig.engine.dependencies = replace(rig.engine.dependencies, now=blocked_clock)
    thread = threading.Thread(target=rig.engine.tick)
    thread.start()
    try:
        assert entered.wait(2)
        assert rig.press(sequence=1, pressed=False)["state"] == "held"
        assert rig.closed
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()


def test_clock_sample_and_commit_are_serialized_across_threads():
    rig = Rig()
    entered, release, second_sampled = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    calls = 0

    def sampled_clock():
        nonlocal calls
        calls += 1
        if calls == 1:
            value = 100.0
            entered.set()
            assert release.wait(2)
            return value
        second_sampled.set()
        return 100.1

    rig.engine.dependencies = replace(rig.engine.dependencies, now=sampled_clock)
    results, errors = [], []

    def sample():
        try:
            results.append(rig.engine._now())
        except Exception as exc:  # noqa: BLE001 - collect injected thread failures
            errors.append(exc)

    attempted = threading.Event()

    def second_call():
        attempted.set()
        sample()

    first = threading.Thread(target=sample)
    second = threading.Thread(target=second_call)
    first.start()
    try:
        assert entered.wait(2)
        second.start()
        assert attempted.wait(2)
        assert not second_sampled.wait(0.05)
    finally:
        release.set()
        first.join(2)
        if second.ident is not None:
            second.join(2)
    assert not first.is_alive() and not second.is_alive()
    assert errors == [] and sorted(results) == [100.0, 100.1]
    assert rig.engine.view()["state"] == "owned_idle"


def test_source_read_and_commit_are_serialized_but_stop_does_not_wait():
    rig = Rig()
    entered, release = threading.Event(), threading.Event()
    calls = 0

    def source():
        nonlocal calls
        calls += 1
        value = replace(rig.anchor, reading_id=f"cached-{calls}")
        if calls == 1:
            entered.set()
            assert release.wait(2)
        return value

    rig.engine.dependencies = replace(rig.engine.dependencies, reading=source)
    results, errors = [], []

    def read():
        try:
            results.append(rig.engine._read(rig.time))
        except Exception as exc:  # noqa: BLE001 - collect injected thread failures
            errors.append(exc)

    first = threading.Thread(target=read)
    second = threading.Thread(target=read)
    first.start()
    try:
        assert entered.wait(2)
        second.start()
        assert rig.engine.stop()["state"] == "held"
        assert calls == 1
    finally:
        release.set()
        first.join(2)
        if second.ident is not None:
            second.join(2)
    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert {r.reading_id for r in results} == {"cached-1", "cached-2"}
    assert rig.engine.view()["state"] == "held"


def test_actual_source_timestamp_regression_is_a_fault():
    rig = Rig()
    rig.time += 0.2
    rig.engine.tick()
    rig.read_override = replace(
        rig.anchor, reading_id="actually-older", sampled_at_mono=100.1
    )
    assert rig.engine.tick()["reason_code"] == "reading_regressed"
    assert rig.closed


def test_pre_stop_clock_sample_cannot_commit_as_post_closure_evidence():
    rig = Rig()
    clock_returned = threading.Event()
    commit_waiting = threading.Event()
    release_commit = threading.Event()
    original_lock = rig.engine._lock
    results, errors = [], []

    def clock():
        value = rig.time
        clock_returned.set()
        return value

    class CommitBarrier:
        def __enter__(self):
            if threading.current_thread() is sampler and clock_returned.is_set():
                # The clock has returned its old value but _now has not yet
                # acquired the engine lock to commit it. Stop can complete.
                commit_waiting.set()
                assert release_commit.wait(2)
            original_lock.acquire()
            return self

        def __exit__(self, _kind, _value, _traceback):
            original_lock.release()

    def sample():
        try:
            results.append(rig.engine._now())
        except Exception as exc:  # noqa: BLE001 - report test-thread failures
            errors.append(exc)

    rig.engine.dependencies = replace(rig.engine.dependencies, now=clock)
    sampler = threading.Thread(target=sample)
    rig.engine._lock = CommitBarrier()
    sampler.start()
    try:
        assert commit_waiting.wait(2)
        rig.time = 100.2
        stopped = rig.engine.stop()
        assert stopped["admission"] == "closed"
        assert stopped["stop_evidence"] == "admission_closed"
    finally:
        release_commit.set()
        sampler.join(2)
    assert not sampler.is_alive()
    assert errors == [] and results == [100.0]
    assert rig.engine._closed_at is None
    with pytest.raises(ManualRefused, match="settle_unproven"):
        rig.engine.end(rig.evidence(observed_at_mono=100.0))
    assert rig.engine.view()["counters"]["settled"] == 0
    assert rig.engine.end(rig.evidence())["state"] == "ended"


def test_extreme_finite_workspace_is_refused_before_a_worker_is_constructed():
    with pytest.raises(ManualRefused, match="limits_invalid"):
        Rig(
            box_min_m=(-1e308,) * 3,
            box_max_m=(1e308,) * 3,
            max_delta_m=1e308,
            max_step_m=1e308,
            max_velocity_m_s=1e308,
            period_s=1.0,
            target_timeout_s=10.0,
            deadman_timeout_s=10.0,
            max_anchor_age_s=100.0,
        )
