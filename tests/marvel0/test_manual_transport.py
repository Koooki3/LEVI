"""All remote calls injected; no sockets, robot ports, ROS or services."""

import threading

import pytest
from test_manual_supervision import ATTEST, LIMITS
from test_marvel0_run import make_machine  # noqa: F401
from test_marvel0_run_supervisor import flock_busy

from levi.marvel0 import manual, run
from levi.marvel0 import manual_transport as real
from levi.marvel0.manual_worker import production_factory

STOPS = {
    "settle_window_s": 0.2,
    "settle_position_m": 0.001,
    "settle_angle_rad": 0.001,
    "gripper_settle_rad": 0.001,
}


class Remote:
    def __init__(self):
        self.now = 100.0
        self.calls = []
        self.session = None
        self.open = False
        self.evidence = "none"
        self.seq = 1
        self.instance = "instance"
        self.fail_start = False
        self.block_stop = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def state(self):
        return {
            "ok": True,
            "instance_id": self.instance,
            "session_id": self.session,
            "admission_open": self.open,
            "stop_evidence": self.evidence,
            "sample": {
                "pose7": [0.4, 0, 0.3, 0, 0, 0, 1],
                "joints": [0, -0.6, 0, -2.1, 0, 1.6, 0.8],
                "frame": "base",
                "seq": self.seq,
                "joint_seq": self.seq,
                "monotonic_time": self.now,
                "joint_monotonic_time": self.now,
            },
            "gripper": {
                "terminal": True,
                "seq": self.seq,
                "monotonic_time": self.now,
                "theta": 0,
            },
        }

    def request(self, method, path, body):
        self.calls.append((method, path, body))
        if path == "capabilities":
            return {
                "ok": True,
                "instance_id": self.instance,
                "protocol_version": 1,
                "clock_domain": "same_host_monotonic",
                "monotonic_time": self.now,
                "translation": True,
                "gripper": True,
            }
        if path == "start":
            self.session = body["session_id"]
            self.open = True
            if self.fail_start:
                raise TimeoutError("delivered but response unknown")
        if path == "stop":
            self.entered.set()
            if self.block_stop:
                assert self.release.wait(3)
            self.open = False
            self.evidence = "hold_published_unproven"
        if path == "end":
            self.session = None
            self.evidence = "proven"
        return self.state()

    def advance(self, seconds=0.1):
        self.now += seconds
        self.seq += 1


def config(remote):
    return {
        "session_id": "session",
        "generation": 1,
        "limits": LIMITS,
        "stop_limits": STOPS,
        "robot_server_url": "http://127.0.0.1:5000",
        "audit": lambda *a, **k: None,
        "request": remote.request,
        "now": lambda: remote.now,
    }


def test_construct_transport_and_cached_view_do_not_connect():
    remote = Remote()
    cfg = config(remote)
    real.Transport(
        cfg["robot_server_url"],
        cfg["session_id"],
        manual.Limits(**LIMITS),
        request=remote.request,
    )
    assert not remote.calls
    engine = real.create(cfg)
    before = list(remote.calls)
    assert engine.view()["capabilities"]["real_motion"]
    assert engine.view()["anchor"]["position"] == [0.4, 0, 0.3]
    assert remote.calls == before


def test_unknown_start_is_fault_owner_and_does_not_retry():
    remote = Remote()
    remote.fail_start = True
    engine = real.create(config(remote))
    assert engine.view()["state"] == "fault_locked"
    assert engine.view()["admission"] == "closed"
    assert len([c for c in remote.calls if c[1] == "start"]) == 1
    with pytest.raises(manual.ManualRefused, match="physical_stop_unproven"):
        engine.physical_evidence()


def test_stop_does_not_wait_for_remote_http_or_claim_physical_stop():
    remote = Remote()
    remote.block_stop = True
    engine = real.create(config(remote))
    engine.stop()
    assert remote.entered.wait(1)
    assert engine.view()["admission"] == "closed"
    assert engine.view()["stop_evidence"] != "physical_settled"
    remote.release.set()


def test_remote_instance_change_faults_without_new_start():
    remote = Remote()
    engine = real.create(config(remote))
    remote.instance = "other"
    remote.advance(0.11)
    engine.tick()
    assert engine.view()["state"] == "fault_locked"
    assert len([c for c in remote.calls if c[1] == "start"]) == 1


def test_real_evidence_is_separate_from_synthetic_and_requires_remote_settle():
    remote = Remote()
    engine = real.create(config(remote))
    engine.stop()
    assert remote.entered.wait(1)
    with pytest.raises(manual.ManualRefused):
        engine.end(manual.SettledEvidence("session", 1, 1, 100.0, True))
    with pytest.raises(manual.ManualRefused):
        engine.physical_evidence()
    remote.evidence = "measured_settled"
    evidence = engine.physical_evidence()
    assert isinstance(evidence, real.PhysicalSettledEvidence)
    assert engine.end(evidence)["stop_evidence"] == "physical_settled"


def test_gripper_requires_deadman_and_sends_only_finite_goal():
    remote = Remote()
    engine = real.create(config(remote))
    ident = {"session_id": "session", "generation": 1}
    with pytest.raises(manual.ManualRefused, match="deadman_required"):
        engine.gripper(**ident, sequence=0, opening_ratio=0.5)
    engine.deadman(**ident, sequence=0, pressed=True)
    engine.gripper(**ident, sequence=0, opening_ratio=0.5)
    goal = [c for c in remote.calls if c[1] == "gripper"]
    assert len(goal) == 1 and goal[0][2]["opening_ratio"] == 0.5
    assert goal[0][2]["expires_at_mono"] <= remote.now + LIMITS["target_timeout_s"]
    with pytest.raises(manual.ManualRefused):
        engine.gripper(**ident, sequence=0, opening_ratio=0.8)
    assert len([c for c in remote.calls if c[1] == "gripper"]) == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:5000",
        "http://example.com:5000",
        "http://127.0.0.1:5000/path",
        "http://user@127.0.0.1:5000",
        "http://127.0.0.1:5000?x=1",
    ],
)
def test_url_is_fixed_local_http_without_userinfo_or_paths(url):
    with pytest.raises(manual.ManualRefused):
        real.Transport(url, "s", manual.Limits(**LIMITS))


def test_stop_thresholds_are_explicit_before_any_remote_call():
    remote = Remote()
    cfg = config(remote)
    cfg.pop("stop_limits")
    with pytest.raises(manual.ManualRefused):
        real.create(cfg)
    assert not remote.calls


def test_real_supervisor_unknown_start_keeps_same_arm_flock(machine, monkeypatch):
    m = machine
    remote = Remote()
    remote.now = m.host.monotonic()
    remote.fail_start = True
    m.host.manual_factory = production_factory
    m.host.manual_enabled = lambda: True
    monkeypatch.setattr(
        real.Transport,
        "_http",
        lambda self, method, path, body: remote.request(method, path, body),
    )
    challenge = m.sup.manual.challenge({"limits": LIMITS, "stop_limits": STOPS})
    assert challenge["confirm"] == "start real translation session"
    view = m.sup.manual.start(
        {
            "limits": LIMITS,
            "stop_limits": STOPS,
            "request_id": m.rid(),
            "attest": ATTEST,
            "confirm": challenge["confirm"],
            "challenge": challenge["challenge"],
        }
    )
    assert view["state"] == "fault_locked" and view["environment"] == "production"
    assert flock_busy(m.tmp / "arm.lock") and m.sup.active()["kind"] == "marvel0-manual"
    with pytest.raises(run.Refuse):
        m.start()
    assert len([c for c in remote.calls if c[1] == "start"]) == 1
    if m.sup.manual.fd is not None:
        import os

        os.close(m.sup.manual.fd)
        m.sup.manual.fd = None


def test_control_manual_section_is_explicit_and_malformed_is_off():
    from levi.marvel0 import control_site

    base = {"schema": control_site.SCHEMA}
    assert not control_site.parse(base).manual.enabled
    assert control_site.parse(dict(base, manual={"enabled": True})).manual.enabled
    broken = control_site.parse(dict(base, manual={"enabled": "true"}))
    assert not broken.manual.enabled and broken.manual_errors


def test_capabilities_unavailable_before_start_never_retains_or_stops():
    remote = Remote()
    cfg = config(remote)

    def fail(method, path, body):
        remote.calls.append((method, path, body))
        raise TimeoutError("server absent")

    cfg["request"] = fail
    with pytest.raises(manual.ManualRefused, match="manual_server_unavailable"):
        real.create(cfg)
    assert [c[1] for c in remote.calls] == ["capabilities"]


def test_successful_start_with_invalid_anchor_is_retained():
    remote = Remote()
    original = remote.state

    def bad():
        doc = original()
        doc["sample"]["pose7"] = [float("nan")] * 7
        return doc

    remote.state = bad
    engine = real.create(config(remote))
    assert engine.view()["state"] == "fault_locked"
    assert engine.view()["capabilities"]["real_motion"]


def test_end_response_lost_is_not_physical_proof_or_retried():
    remote = Remote()
    engine = real.create(config(remote))
    engine.stop()
    assert remote.entered.wait(1)
    remote.evidence = "measured_settled"
    original = remote.request

    def lost(method, path, body):
        answer = original(method, path, body)
        if path == "end":
            raise TimeoutError("response lost")
        return answer

    engine.transport.request = lost
    with pytest.raises(manual.ManualRefused):
        engine.physical_evidence()
    assert engine.view()["state"] != "ended"
    assert not engine.transport.end_proven
    assert len([c for c in remote.calls if c[1] == "end"]) == 1


def test_end_audit_failure_reentry_does_not_resend_remote_end():
    remote = Remote()
    cfg = config(remote)
    broken = [False]

    def audit(*args, **kwargs):
        if broken[0]:
            raise OSError("disk")

    cfg["audit"] = audit
    engine = real.create(cfg)
    engine.stop()
    assert remote.entered.wait(1)
    remote.evidence = "measured_settled"
    proof = engine.physical_evidence()
    broken[0] = True
    with pytest.raises(manual.ManualRefused, match="audit_failed"):
        engine.end(proof)
    broken[0] = False
    engine.end(engine.physical_evidence())
    assert len([c for c in remote.calls if c[1] == "end"]) == 1


def test_heartbeat_closed_ack_is_refused():
    remote = Remote()
    engine = real.create(config(remote))
    remote.open = False
    with pytest.raises(manual.ManualRefused, match="manual_heartbeat_unknown"):
        engine.deadman(session_id="session", generation=1, sequence=0, pressed=True)
    assert engine.view()["admission"] == "closed"


def test_queued_remote_dispatch_is_revoked_without_waiting_for_inflight_http():
    remote = Remote()
    engine = real.create(config(remote))
    entered, release = threading.Event(), threading.Event()
    original = remote.request
    errors = []

    def blocked(method, path, body):
        if path == "heartbeat":
            entered.set()
            assert release.wait(3)
        return original(method, path, body)

    engine.transport.request = blocked

    def send(path):
        try:
            engine.transport.mutation(
                path, {"opening_ratio": 0.5} if path == "gripper" else None
            )
        except manual.ManualRefused as exc:
            errors.append(exc.code)

    first = threading.Thread(target=send, args=("heartbeat",))
    second = threading.Thread(target=send, args=("gripper",))
    first.start()
    assert entered.wait(1)
    second.start()
    engine.stop()
    assert engine.view()["admission"] == "closed"
    release.set()
    first.join(2)
    second.join(2)
    assert not first.is_alive() and not second.is_alive()
    assert "admission_closed" in errors
    assert not any(call[1] == "gripper" for call in remote.calls)
