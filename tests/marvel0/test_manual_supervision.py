"""Offline integration: real Supervisor/arm flock/person routes, injected sink only."""

import dataclasses
import os
import threading
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_marvel0_run import ATTEST, make_machine  # noqa: F401
from test_marvel0_run_supervisor import flock_busy

from levi.marvel0 import manual, run, run_api
from levi.marvel0.manual_worker import Ack, PoseWorker

BASE = "/api/levi/marvel0/arm/manual"
UI = {"x-levi-ui-token": "test-ui-token"}
LIMITS = {
    "box_min_m": [0, -1, 0],
    "box_max_m": [1, 1, 1],
    "max_delta_m": 0.1,
    "max_step_m": 0.002,
    "max_velocity_m_s": 0.01,
    "period_s": 0.1,
    "max_reading_age_s": 0.5,
    "max_anchor_age_s": 10,
    "deadman_timeout_s": 0.4,
    "target_timeout_s": 0.3,
    "max_orientation_drift_rad": 0.01,
    "quaternion_norm_tolerance": 0.00001,
    "max_settle_age_s": 0.5,
}  # These are synthetic fixture numbers, not approved physical settings.


class Sink:
    def __init__(self):
        self.poses = []
        self.closed = False

    def send(self, pose):
        assert not self.closed
        self.poses.append(pose)
        return Ack(True, "synthetic")

    def close_admission(self):
        self.closed = True


@pytest.fixture
def rig(machine, monkeypatch):
    m = machine
    m.sink = Sink()
    m.host.manual_enabled = lambda: True

    def factory(config):
        clock = m.host.monotonic

        def reading():
            return manual.Reading(
                str(clock()),
                clock(),
                (0.4, 0, 0.3),
                (0, 0, 0, 1),
                (0, -0.6, 0, -2.1, 0, 1.6, 0.8),
                True,
                True,
                True,
            )

        return manual.Engine(
            session_id=config["session_id"],
            generation=config["generation"],
            anchor=reading(),
            limits=manual.Limits(**config["limits"]),
            dependencies=manual.Dependencies(
                clock,
                reading,
                lambda *_: True,
                lambda doc: config["audit"]("manual_engine", detail=doc),
            ),
            worker=PoseWorker(m.sink),
        )

    m.host.manual_factory = factory
    monkeypatch.setenv("LEVI_UI_TOKEN", UI["x-levi-ui-token"])
    monkeypatch.setattr(run_api, "supervisor", lambda: m.sup)
    app = FastAPI()
    app.include_router(run_api.router)
    with TestClient(app) as client:
        yield client, m
    if m.sup.manual.fd is not None:
        os.close(m.sup.manual.fd)
        m.sup.manual.fd = None


def start(client, m, **changes):
    c = client.post(BASE + "/challenge", json={"limits": LIMITS}, headers=UI)
    assert c.status_code == 200, c.text
    body = {
        "limits": LIMITS,
        "challenge": c.json()["challenge"],
        "confirm": c.json()["confirm"],
        "request_id": m.rid(),
        "attest": ATTEST,
        **changes,
    }
    return client.post(BASE + "/start", json=body, headers=UI), body


def ident(m, view):
    return {
        "request_id": m.rid(),
        "session_id": view["session_id"],
        "generation": view["generation"],
    }


def reason(response):
    return response.json()["detail"].get("reason_code")


def test_production_capability_never_enables_from_client_switch(machine):
    m = machine
    assert m.enabled
    assert m.sup.manual.view()["reason_code"] == "stop_unproven"
    with pytest.raises(run.Refuse) as exc:
        m.sup.manual.challenge({"limits": LIMITS})
    assert exc.value.extra["reason_code"] == "stop_unproven"
    assert not (m.tmp / "arm.lock").exists()
    assert not m.spawned and not m.signals


def test_start_once_holds_same_arm_lock_and_blocks_all_existing_jobs(rig):
    client, m = rig
    response, body = start(client, m)
    assert response.status_code == 202, response.text
    v = response.json()
    assert v["start_request_id"] == body["request_id"]
    assert v["capabilities"]["real_motion"] is False
    assert m.sup.active()["kind"] == "marvel0-manual"
    assert run_api.active_job()["id"] == v["session_id"]
    assert run_api.run_active()
    assert flock_busy(m.tmp / "arm.lock")
    again = client.post(BASE + "/start", json=body, headers=UI)
    assert again.status_code == 202 and again.json()["replayed"]
    assert again.json()["session_id"] == v["session_id"]
    assert (
        reason(client.post(BASE + "/challenge", json={"limits": LIMITS}, headers=UI))
        == "owner_busy"
    )
    with pytest.raises(run.Refuse):
        m.start()
    assert not m.spawned and not m.sink.poses


def test_hosted_evaluation_blocks_manual_and_preserves_its_owner(rig):
    client, m = rig
    m.start()
    response = client.post(BASE + "/challenge", json={"limits": LIMITS}, headers=UI)
    assert reason(response) == "owner_busy"
    assert m.sup.active()["kind"] == "marvel0-client"
    assert len(m.spawned) == 1 and not m.sink.poses


@pytest.mark.parametrize("stage", ["challenge", "start"])
@pytest.mark.parametrize(
    "blocker", ["hosted", "hosted_unreadable", "registry_unreadable", "standalone"]
)
def test_aeri_blocks_manual_before_arm_lock_and_factory(
    rig, monkeypatch, stage, blocker
):
    client, m = rig
    monkeypatch.setattr(m.host, "hosted_aeri", lambda: False)
    factory = Mock(
        side_effect=AssertionError("blocked start must not construct a worker")
    )
    monkeypatch.setattr(m.host, "manual_factory", factory)
    body = {"limits": LIMITS}
    if stage == "start":
        response = client.post(BASE + "/challenge", json=body, headers=UI)
        assert response.status_code == 200, response.text
        body = {
            **body,
            "challenge": response.json()["challenge"],
            "confirm": response.json()["confirm"],
            "request_id": m.rid(),
            "attest": ATTEST,
        }

    def unreadable():
        raise OSError("synthetic registry unavailable")

    if blocker == "hosted":
        monkeypatch.setattr(m.host, "hosted_aeri", lambda: True)
    elif blocker == "hosted_unreadable":
        monkeypatch.setattr(m.host, "hosted_aeri", unreadable)
    elif blocker == "registry_unreadable":
        monkeypatch.setattr(m.host, "aeri_runs", unreadable)
    else:
        m.aeri = [{"backend": "standalone", "runner_alive": True}]
    assert m.sup.active() is None
    assert m.sup.manual.fd is None
    assert not (m.tmp / "arm.lock").exists()
    response = client.post(BASE + "/" + stage, json=body, headers=UI)
    assert response.status_code == 409, response.text
    assert reason(response) == "owner_busy"
    factory.assert_not_called()
    assert m.sup.manual.fd is None and m.sup.manual.engine is None
    assert not (m.tmp / "arm.lock").exists()
    assert m.sup.active() is None
    assert not m.spawned and not m.signals and not m.sink.poses


@pytest.mark.parametrize("stage", ["challenge", "start"])
def test_aeri_probe_does_not_hold_run_mutex_while_aeri_view_reads_owner(
    rig, monkeypatch, stage
):
    client, m = rig
    monkeypatch.setattr(m.host, "hosted_aeri", lambda: False)
    body = {"limits": LIMITS}
    if stage == "start":
        response = client.post(BASE + "/challenge", json=body, headers=UI)
        assert response.status_code == 200, response.text
        body = {
            **body,
            "challenge": response.json()["challenge"],
            "confirm": response.json()["confirm"],
            "request_id": m.rid(),
            "attest": ATTEST,
        }
    aeri_mutex = threading.Lock()
    aeri_view_ready = threading.Event()
    probing = threading.Event()
    owner_read = threading.Event()
    errors = []

    def read_aeri_document():
        # Mirrors AERI view -> _start_why -> E/manual active(), with the
        # AERI state mutex held. The probe below takes this mutex in reverse.
        with aeri_mutex:
            aeri_view_ready.set()
            if not probing.wait(5):
                errors.append("manual probe did not begin")
                return
            m.sup.active()
            owner_read.set()

    def hosted_aeri():
        if stage == "start":
            assert flock_busy(m.host.run_lock_path())
        probing.set()
        # Bound the wait so the regression fails without leaving deadlocked
        # threads behind when the caller incorrectly holds run._mutex.
        acquired = aeri_mutex.acquire(timeout=5)
        if not acquired:
            raise OSError("synthetic cross-supervisor lock inversion")
        aeri_mutex.release()
        return False

    monkeypatch.setattr(m.host, "hosted_aeri", hosted_aeri)
    thread = threading.Thread(target=read_aeri_document)
    thread.start()
    try:
        assert aeri_view_ready.wait(5)
        response = client.post(BASE + "/" + stage, json=body, headers=UI)
    finally:
        probing.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert not errors
    assert owner_read.is_set()
    assert response.status_code == (200 if stage == "challenge" else 202), response.text


def test_target_deadman_pump_release_and_unproven_end_keep_lock(rig):
    client, m = rig
    response, _ = start(client, m)
    v = response.json()
    identity = ident(m, v)
    target = {
        **identity,
        "anchor_id": v["anchor_id"],
        "sequence": 0,
        "delta_position_m": [0.01, 0, 0],
    }
    assert client.post(BASE + "/target", json=target, headers=UI).status_code == 200
    m.sup.manual.tick()
    assert not m.sink.poses
    assert (
        client.post(
            BASE + "/deadman",
            json={**ident(m, v), "sequence": 0, "pressed": True},
            headers=UI,
        ).status_code
        == 200
    )
    m.sup.manual.tick()
    assert len(m.sink.poses) == 1
    assert m.sink.poses[0].quaternion == (0, 0, 0, 1)
    assert m.sink.poses[0].position[0] <= 0.40100000001
    stopped = client.post(BASE + "/stop", json=ident(m, v), headers=UI)
    assert stopped.json()["admission"] == "closed"
    assert stopped.json()["stop_evidence"] == "admission_closed"
    assert (
        reason(client.post(BASE + "/end", json=ident(m, v), headers=UI))
        == "settle_unproven"
    )
    assert flock_busy(m.tmp / "arm.lock")
    assert m.sup.active() is not None
    m.sup.manual.tick()
    assert len(m.sink.poses) == 1


def test_only_trusted_injected_synthetic_observer_allows_release(rig):
    client, m = rig
    response, _ = start(client, m)
    v = response.json()

    def evidence(engine):
        view = engine.view()
        return manual.SettledEvidence(
            view["session_id"],
            view["generation"],
            view["closed_generation"],
            m.host.monotonic(),
            True,
        )

    m.host.manual_settled = evidence
    response = client.post(BASE + "/end", json=ident(m, v), headers=UI)
    assert response.status_code == 200, response.text
    assert response.json()["stop_evidence"] == "synthetic_settled"
    assert m.sup.active() is None and not flock_busy(m.tmp / "arm.lock")


def test_stale_identity_cannot_close_another_session(rig):
    client, m = rig
    response, _ = start(client, m)
    v = response.json()
    response = client.post(
        BASE + "/stop",
        json={**ident(m, v), "generation": v["generation"] + 1},
        headers=UI,
    )
    assert reason(response) == "session_mismatch"
    assert not m.sink.closed


def test_challenge_binds_limits_and_is_consumed(rig):
    client, m = rig
    c = client.post(BASE + "/challenge", json={"limits": LIMITS}, headers=UI).json()
    body = {
        "request_id": m.rid(),
        "limits": {**LIMITS, "max_step_m": 0.003},
        "challenge": c["challenge"],
        "confirm": c["confirm"],
        "attest": ATTEST,
    }
    response = client.post(BASE + "/start", json=body, headers=UI)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "challenge_stale"
    assert not m.sink.poses and not flock_busy(m.tmp / "arm.lock")


@pytest.mark.parametrize(
    "route,body",
    [
        ("challenge", {"limits": LIMITS}),
        (
            "start",
            {
                "limits": LIMITS,
                "request_id": "test-req-0000",
                "challenge": "x",
                "confirm": "x",
                "attest": ATTEST,
            },
        ),
        (
            "target",
            {
                "request_id": "test-req-0000",
                "session_id": "x",
                "generation": 1,
                "anchor_id": "x",
                "sequence": 1,
                "delta_position_m": [0, 0, 0],
            },
        ),
        (
            "deadman",
            {
                "request_id": "test-req-0000",
                "session_id": "x",
                "generation": 1,
                "sequence": 1,
                "pressed": False,
            },
        ),
        ("stop", {"request_id": "test-req-0000", "session_id": "x", "generation": 1}),
        ("end", {"request_id": "test-req-0000", "session_id": "x", "generation": 1}),
    ],
)
def test_writes_are_person_only_and_closed(rig, route, body):
    client, m = rig
    response = client.post(
        BASE + "/" + route,
        json=body,
        headers={"authorization": "Bearer synthetic-agent"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "person_only"
    assert client.post(BASE + "/" + route, json=body).status_code in (401, 403)
    assert (
        client.post(
            BASE + "/" + route, json={**body, "transport": "real"}, headers=UI
        ).status_code
        == 422
    )
    assert not m.sink.poses


def test_restart_does_not_resume_or_accept_prior_identity(rig):
    client, m = rig
    response, _ = start(client, m)
    v = response.json()
    restarted = run.Supervisor(
        dataclasses.replace(m.host), site_loader=lambda: m.site, workspace=m.workspace
    )
    assert restarted.manual.view()["state"] == "idle"
    with pytest.raises(run.Refuse):
        restarted.manual.stop(ident(m, v))
    assert flock_busy(m.tmp / "arm.lock")
    assert not m.sink.poses


def test_start_factory_can_be_revoked_without_waiting_for_its_return(rig):
    _client, m = rig
    entered, release = threading.Event(), threading.Event()
    original = m.host.manual_factory

    def blocked(config):
        entered.set()
        assert release.wait(5)
        return original(config)

    m.host.manual_factory = blocked
    c = m.sup.manual.challenge({"limits": LIMITS})
    body = {
        "limits": LIMITS,
        "challenge": c["challenge"],
        "confirm": c["confirm"],
        "request_id": m.rid(),
        "attest": ATTEST,
    }
    answers, errors = [], []

    def starting():
        try:
            answers.append(m.sup.manual.start(body))
        except Exception as exc:  # noqa: BLE001 - report all worker thread errors
            errors.append(exc)

    thread = threading.Thread(target=starting, daemon=True)
    thread.start()
    try:
        assert entered.wait(2)
        state = m.sup.manual.view()
        assert state["state"] == "starting"
        assert m.sup.active()["kind"] == "marvel0-manual"
        stopped = m.sup.manual.stop(ident(m, state))
        assert stopped["reason_code"] == "start_cancelled"
        assert thread.is_alive() and not m.sink.poses
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive() and not errors
    assert answers[0]["admission"] == "closed"
    assert m.sup.active() is not None and flock_busy(m.tmp / "arm.lock")
    m.sup.manual.tick()
    assert not m.sink.poses


def test_target_receipt_replay_does_not_enqueue_or_accept_changed_payload(rig):
    client, m = rig
    response, _ = start(client, m)
    v = response.json()
    body = {
        **ident(m, v),
        "anchor_id": v["anchor_id"],
        "sequence": 0,
        "delta_position_m": [0.01, 0, 0],
    }
    first = client.post(BASE + "/target", json=body, headers=UI)
    duplicate = client.post(BASE + "/target", json=body, headers=UI)
    assert first.json()["counters"]["accepted"] == 1
    assert (
        duplicate.json()["replayed"] and duplicate.json()["counters"]["accepted"] == 1
    )
    changed = client.post(BASE + "/target", json={**body, "sequence": 1}, headers=UI)
    assert reason(changed) == "request_id_used"
    assert not m.sink.poses


def test_deadman_receipt_does_not_extend_lease_on_http_retry(rig):
    client, m = rig
    response, _ = start(client, m)
    v = response.json()
    body = {**ident(m, v), "sequence": 0, "pressed": True}
    assert client.post(BASE + "/deadman", json=body, headers=UI).status_code == 200
    m.clock.advance(0.3)
    assert client.post(BASE + "/deadman", json=body, headers=UI).json()["replayed"]
    m.clock.advance(0.11)
    m.sup.manual.tick()
    assert m.sup.manual.view()["reason_code"] == "deadman_expired"
    assert not m.sink.poses


def test_manual_integrated_audit_failure_closes_before_send(rig, monkeypatch):
    from levi.marvel0 import manual_supervision

    client, m = rig
    response, _ = start(client, m)
    v = response.json()

    def broken(*args, **kwargs):
        raise OSError("synthetic disk error")

    monkeypatch.setattr(manual_supervision.jsonio, "append_line", broken)
    body = {
        **ident(m, v),
        "anchor_id": v["anchor_id"],
        "sequence": 0,
        "delta_position_m": [0.01, 0, 0],
    }
    response = client.post(BASE + "/target", json=body, headers=UI)
    assert reason(response) == "audit_failed"
    assert m.sup.manual.view()["admission"] == "closed"
    assert m.sup.active() is not None and not m.sink.poses


@pytest.mark.parametrize("action", ["stop", "end"])
def test_stop_audit_failure_is_structured_and_retains_owner(rig, monkeypatch, action):
    from levi.marvel0 import manual_supervision

    client, m = rig
    response, _ = start(client, m)
    v = response.json()

    def broken(*args, **kwargs):
        raise OSError("synthetic disk error")

    monkeypatch.setattr(manual_supervision.jsonio, "append_line", broken)
    response = client.post(BASE + "/" + action, json=ident(m, v), headers=UI)
    assert response.status_code == 409
    assert reason(response) == "audit_failed"
    assert m.sup.manual.view()["admission"] == "closed"
    assert m.sup.active() is not None and not m.sink.poses


def test_cancelled_late_factory_audit_failure_still_publishes_closed_owner(
    rig, monkeypatch
):
    from levi.marvel0 import manual_supervision

    _client, m = rig
    entered, release = threading.Event(), threading.Event()
    original_factory = m.host.manual_factory
    append = manual_supervision.jsonio.append_line

    def factory(config):
        engine = original_factory(config)
        entered.set()
        assert release.wait(5)
        return engine

    m.host.manual_factory = factory
    challenge = m.sup.manual.challenge({"limits": LIMITS})
    errors = []

    def start_pending():
        try:
            m.sup.manual.start(
                {
                    "limits": LIMITS,
                    "request_id": m.rid(),
                    "attest": ATTEST,
                    "challenge": challenge["challenge"],
                    "confirm": challenge["confirm"],
                }
            )
        except run.Refuse as exc:
            errors.append(exc)

    thread = threading.Thread(target=start_pending, daemon=True)
    thread.start()

    def broken(*args, **kwargs):
        raise OSError("synthetic disk error")

    try:
        assert entered.wait(2)
        v = m.sup.manual.view()
        m.sup.manual.stop(ident(m, v))
        monkeypatch.setattr(manual_supervision.jsonio, "append_line", broken)
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert errors[0].extra["reason_code"] == "audit_failed"
    assert m.sup.manual.view()["state"] == "fault_locked"
    assert m.sup.manual.preparing is None
    assert m.sup.active() is not None and m.sink.closed
    monkeypatch.setattr(manual_supervision.jsonio, "append_line", append)
    m.sup.manual.tick()

    def evidence(engine):
        v = engine.view()
        return manual.SettledEvidence(
            v["session_id"],
            v["generation"],
            v["closed_generation"],
            m.host.monotonic(),
            True,
        )

    m.host.manual_settled = evidence
    assert m.sup.manual.end(ident(m, m.sup.manual.view()))["state"] == "ended"
    assert m.sup.active() is None


def test_production_manual_refusal_does_not_adopt_or_signal_a_legacy_live_client(
    machine,
):
    from test_marvel0_run_supervisor import second_service

    from levi.marvel0.manual_supervision import CONFIRM

    m = machine
    m.start()
    after = second_service(m)
    before = m.record()
    for call, body in [
        (after.manual.challenge, {"limits": LIMITS}),
        (
            after.manual.start,
            {
                "limits": LIMITS,
                "request_id": m.rid(),
                "challenge": "unused",
                "confirm": CONFIRM,
                "attest": ATTEST,
            },
        ),
    ]:
        with pytest.raises(run.Refuse) as exc:
            call(body)
        assert exc.value.extra["reason_code"] == "stop_unproven"
    assert not after._adopted
    assert not m.signals
    assert m.record() == before


def test_receipt_limit_audit_failure_is_structured_and_retains_lock(rig, monkeypatch):
    from levi.marvel0 import manual_supervision

    client, m = rig
    response, _ = start(client, m)
    v = response.json()
    m.sup.manual._receipts = {("old", str(i)): {} for i in range(4096)}

    def broken(*args, **kwargs):
        raise OSError("synthetic disk error")

    monkeypatch.setattr(manual_supervision.jsonio, "append_line", broken)
    response = client.post(
        BASE + "/target",
        headers=UI,
        json={
            **ident(m, v),
            "anchor_id": v["anchor_id"],
            "sequence": 0,
            "delta_position_m": [0.01, 0, 0],
        },
    )
    assert response.status_code == 409 and reason(response) == "audit_failed"
    assert m.sup.manual.view()["admission"] == "closed"
    assert m.sup.active() is not None and not m.sink.poses


def test_failed_new_start_cannot_relabel_an_ended_session_request(rig):
    client, m = rig
    response, first = start(client, m)
    v = response.json()

    def evidence(engine):
        state = engine.view()
        return manual.SettledEvidence(
            state["session_id"],
            state["generation"],
            state["closed_generation"],
            m.host.monotonic(),
            True,
        )

    m.host.manual_settled = evidence
    assert m.sup.manual.end(ident(m, v))["state"] == "ended"

    def failed(config):
        raise manual.ManualRefused("synthetic_failure")

    m.host.manual_factory = failed
    response, second = start(client, m)
    assert reason(response) == "synthetic_failure"
    state = m.sup.manual.view()
    assert state["session_id"] == v["session_id"]
    assert state["start_request_id"] == first["request_id"]
    assert state["start_request_id"] != second["request_id"]
    assert m.sup.active() is None
