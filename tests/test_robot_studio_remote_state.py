"""Read-only remote source tests: every HTTP call mocked, no actual ports."""

import json
import threading
from types import SimpleNamespace

import pytest

from levi.robot_studio import remote_state as R
from levi.robot_studio import state as S

URL = "http://127.0.0.1:5000"


class Source:
    def __init__(self):
        self.now = 100.0
        self.calls = []
        self.doc = {
            "ok": True,
            "instance_id": "instance",
            "session_id": None,
            "admission_open": False,
            "sample": {
                "pose7": [0.4, 0, 0.3, 0, 0, 0, 1],
                "joints": [0, -0.5, 0, -2, 0, 1.5, 0.8],
                "frame": "fr3_link0",
                "seq": 11,
                "joint_seq": 12,
                "monotonic_time": 99.9,
                "joint_monotonic_time": 99.8,
            },
            "gripper": {
                "supported": True,
                "theta": 0.4,
                "seq": 13,
                "monotonic_time": 99.7,
            },
        }
        self.reader = R.Reader(
            request=self.request,
            monotonic=lambda: self.now,
            epoch=lambda: 1800000000.0 + self.now,
        )

    def request(self, url):
        self.calls.append(url)
        return json.dumps(self.doc).encode()

    def snapshot(self):
        return self.reader.snapshot(URL)


def test_measured_joints_and_knuckle_follow_without_manual_session():
    s = Source()
    out = s.snapshot()
    assert out["schema"] == S.STATE_SCHEMA and out["state"] == "fresh"
    assert out["measurement"]["joints_rad"] == s.doc["sample"]["joints"]
    assert out["measurement"]["gripper"] == {"knuckle_angle_rad": 0.4}
    assert out["measurement"]["reported_ee"]["end_frame"] == "robot_server_desk_EE"
    assert out["measurement"]["reported_ee"]["frame_id"] == "fr3_link0"
    assert out["capabilities"] == {"read_only": True, "motion": False}
    assert out["source_detail"]["channels"]["pose"]["sequence"] == 11
    s.now += 0.2
    s.doc["sample"]["joints"][0] = 0.3
    s.doc["sample"]["joint_seq"] += 1
    s.doc["sample"]["joint_monotonic_time"] = s.now
    s.doc["gripper"].update(theta=0.7, seq=14, monotonic_time=s.now)
    assert s.snapshot()["measurement"]["joints_rad"][0] == 0.3
    assert s.snapshot()["measurement"]["gripper"]["knuckle_angle_rad"] == 0.7


@pytest.mark.parametrize("channel", ["pose", "joint"])
def test_each_channel_age_independently_blocks_whole_measurement(channel):
    s = Source()
    if channel == "gripper":
        s.doc["gripper"]["monotonic_time"] = 98.0
    else:
        s.doc["sample"][
            "monotonic_time" if channel == "pose" else "joint_monotonic_time"
        ] = 98.0
    out = s.snapshot()
    assert out["state"] == "stale" and out["measurement"] is None


@pytest.mark.parametrize(
    "mutation, arm_remains_valid",
    [
        (lambda d: d["gripper"].pop("theta"), True),
        (lambda d: d["gripper"].update(supported=False), True),
        (lambda d: d["sample"].update(joints=[0] * 6), False),
        (lambda d: d["sample"].update(pose7=[0, 0, 0, 0, 0, 0, 0]), False),
        (lambda d: d["sample"].update(joint_seq=True), False),
        (lambda d: d.update(instance_id=""), False),
        # Nonstandard JSON constants invalidate the entire response document.
        (lambda d: d["gripper"].update(theta=float("nan")), False),
    ],
)
def test_invalid_measurement_never_defaults_joints_or_open_gripper(
    mutation, arm_remains_valid
):
    source = Source()
    mutation(source.doc)
    out = source.snapshot()
    if arm_remains_valid:
        assert out["state"] == "fresh" and out["measurement"]["gripper"] is None
        assert out["measurement"]["joints_rad"] == source.doc["sample"]["joints"]
    else:
        assert out["measurement"] is None and out["state"] == "unavailable"


def test_reused_identity_and_regressing_sequence_are_rejected():
    s = Source()
    s.snapshot()
    s.now += 0.2
    s.doc["gripper"]["theta"] += 0.1
    assert (
        s.snapshot()["source_detail"]["gripper_reason_code"]
        == "remote_gripper_identity_reused"
    )
    s.now += 0.2
    s.doc["gripper"]["theta"] -= 0.1
    s.doc["sample"]["joint_seq"] = 2
    assert s.snapshot()["reason_code"] == "remote_sample_regressed"


def test_server_restart_discards_old_source_then_accepts_new_identity():
    s = Source()
    first = s.snapshot()
    s.now += 0.2
    s.doc["instance_id"] = "new-instance"
    assert s.snapshot()["reason_code"] == "remote_instance_changed"
    s.now += 0.2
    second = s.snapshot()
    assert second["state"] == "fresh" and second["source_id"] != first["source_id"]


def test_multi_tab_cache_does_not_repeat_poll_and_does_not_freeze_sample_age():
    s = Source()
    out = []
    threads = [
        threading.Thread(target=lambda: out.append(s.snapshot())) for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(2)
    assert len(s.calls) == 1 and len(out) == 8
    s.now += 0.05
    assert s.snapshot()["age_s"] > out[0]["age_s"]
    assert len(s.calls) == 1


def test_failed_read_discards_cached_pose_instead_of_showing_old_as_fresh():
    s = Source()
    s.snapshot()
    s.now += 0.2

    def failed(url):
        raise TimeoutError("offline")

    s.reader.request = failed
    out = s.snapshot()
    assert out["measurement"] is None and out["reason_code"] == "remote_unavailable"


def test_future_time_is_not_applied():
    s = Source()
    s.doc["gripper"]["monotonic_time"] = 101.0
    out = s.snapshot()
    assert out["state"] == "fresh"
    assert out["measurement"]["gripper"] is None
    assert out["source_detail"]["gripper_reason_code"] == "remote_gripper_in_future"


def test_no_redirect_proxy_or_post_and_bounded_timeout_response(monkeypatch):
    requests = []
    created = []

    class Connection:
        def __init__(self, host, port, timeout):
            created.append((host, port, timeout))

        def request(self, *args, **kwargs):
            requests.append((args, kwargs))

        def getresponse(self):
            return SimpleNamespace(status=302, read=lambda size: b"{}")

        def close(self):
            pass

    monkeypatch.setenv("http_proxy", "http://untrusted.example:9000")
    monkeypatch.setattr(R.http.client, "HTTPConnection", Connection)
    with pytest.raises(S.Invalid, match="remote_http_failed"):
        R.fetch(URL)
    assert len(created) == 1 and created[0] == ("127.0.0.1", 5000, R.TIMEOUT_S)
    assert requests[0][0] == ("GET", "/manual/v1/state")
    assert len(requests) == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:5000",
        "http://example.com:5000",
        "http://127.0.0.1:5000/pose",
        "http://u@127.0.0.1:5000",
        "http://127.0.0.1:5000?url=x",
    ],
)
def test_untrusted_url_variants_rejected(url):
    with pytest.raises(S.Invalid):
        R._address(url)


def test_existing_product_profile_and_explicit_manual_switch_are_required(monkeypatch):
    from levi.marvel0 import control_site, site, stack

    loaded = SimpleNamespace(manual_errors=(), manual=SimpleNamespace(enabled=True))
    monkeypatch.setattr(stack, "_read_self_cgroup", lambda: "own-product-cgroup")
    monkeypatch.setattr(stack, "_in_main_checkout", lambda: True)
    profile = ["fake"]
    seen = []

    def existing_profile(cgroup, main):
        seen.append((cgroup, main))
        return {"profile": profile[0]}

    monkeypatch.setattr(stack, "profile", existing_profile)
    monkeypatch.setattr(control_site, "load", lambda: loaded)
    monkeypatch.setattr(
        site,
        "require",
        lambda: SimpleNamespace(server=SimpleNamespace(robot_server_url=URL)),
    )
    assert R.configured_url() is None
    profile[0] = "real"
    assert R.configured_url() == URL
    loaded.manual.enabled = False
    assert R.configured_url() is None
    loaded.manual.enabled = True
    loaded.manual_errors = (("manual.enabled", "type"),)
    assert R.configured_url() is None
    assert seen == [("own-product-cgroup", True)] * 4


def test_disabled_uses_original_file_reader_without_network(monkeypatch):
    monkeypatch.setattr(R, "configured_url", lambda: None)
    monkeypatch.setattr(S, "snapshot", lambda: {"fixture": "existing file source"})
    monkeypatch.setattr(
        R._READER, "snapshot", lambda url: pytest.fail("network called")
    )
    assert R.snapshot() == {"fixture": "existing file source"}


def test_enabled_source_get_endpoint_is_read_only_and_ignores_query_url(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from levi.robot_studio.api import router

    s = Source()
    monkeypatch.setattr(R, "configured_url", lambda: URL)
    monkeypatch.setattr(R, "_READER", s.reader)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        out = client.get("/api/levi/robot-studio/state?url=http://evil.example/pose")
        assert out.status_code == 200 and out.json()["state"] == "fresh"
        assert out.headers["cache-control"] == "no-store"
        assert (
            client.post(
                "/api/levi/robot-studio/state", json={"start": True}
            ).status_code
            == 405
        )
    assert s.calls == [URL]


def test_stale_gripper_does_not_hide_fresh_arm_or_default_open():
    source = Source()
    source.doc["gripper"]["monotonic_time"] = 98.0
    out = source.snapshot()
    assert out["state"] == "fresh"
    assert out["measurement"]["joints_rad"] == source.doc["sample"]["joints"]
    assert out["measurement"]["gripper"] is None
    assert out["source_detail"]["gripper_reason_code"] == "remote_gripper_stale"
    assert out["source_detail"]["channels"]["gripper"]["sampled_at_mono"] == 98.0


def test_missing_gripper_object_retains_actual_arm_samples():
    source = Source()
    source.doc.pop("gripper")
    out = source.snapshot()
    assert out["state"] == "fresh" and out["measurement"]["gripper"] is None
    assert out["source_detail"]["gripper_reason_code"] == "remote_gripper_missing"
    assert out["source_detail"]["channels"]["joint"]["sampled_at_mono"] == 99.8
