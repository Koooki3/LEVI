"""Offline fixtures only: no producer, robot, process or network service."""

import copy
import json
import os
import socket
import subprocess

import pytest

from levi.robot_studio import state as S

NOW = 1_800_000_000.0
URL = "/api/levi/robot-studio/state"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    from levi import paths

    monkeypatch.setattr(paths, "ROOT", tmp_path)
    return tmp_path


def telemetry(**changes):
    data = {
        "schema": S.TELEMETRY_SCHEMA,
        "robot": "fr3",
        "source_id": "offline-fixture",
        "sequence": 7,
        "sampled_at_epoch": NOW - 0.25,
        "joints_rad": [0.1, -0.5, 0.2, -1.5, 0.3, 1.0, 0.4],
        "gripper": {"opening_width_m": 0.06},
        "reported_ee": {
            "position_m": [0.3, 0.1, 0.4],
            "quaternion_xyzw": [0.0, 0.0, 0.0, 1.0],
            "frame_id": "fr3_link0",
            "end_frame": "gripper_tcp",
        },
    }
    data.update(changes)
    return data


def publish(workspace, data):
    folder = workspace / "robot-studio"
    folder.mkdir(exist_ok=True)
    path = folder / "telemetry.json"
    path.write_text(json.dumps(data))
    return path


def assert_no_measurement(out, state, reason):
    assert out["schema"] == S.STATE_SCHEMA
    assert out["state"] == state
    assert out["reason_code"] == reason
    assert out["measurement"] is None
    assert out["capabilities"] == {"read_only": True, "motion": False}


def test_fresh_snapshot_preserves_measured_values(workspace):
    data = telemetry()
    path = publish(workspace, data)
    # mtime is deliberately unrelated to the sample timestamp.
    os.utime(path, (1, 1))
    out = S.snapshot(now=NOW)
    assert out["state"] == "fresh"
    assert out["reason_code"] == "sample_fresh"
    assert out["age_s"] == 0.25
    assert out["sequence"] == data["sequence"]
    assert out["source_id"] == data["source_id"]
    assert out["sampled_at_epoch"] == data["sampled_at_epoch"]
    assert out["measurement"] == {
        key: data[key] for key in ("joints_rad", "gripper", "reported_ee")
    }
    assert out["capabilities"] == {"read_only": True, "motion": False}


@pytest.mark.parametrize("gripper", [None, {"knuckle_angle_rad": 0.4}, {"opening_width_m": 0.12}])
def test_gripper_units_and_absent_ee_are_preserved(workspace, gripper):
    publish(workspace, telemetry(gripper=gripper, reported_ee=None))
    measurement = S.snapshot(now=NOW)["measurement"]
    assert measurement["gripper"] == gripper
    assert measurement["reported_ee"] is None


def test_missing_is_honest_and_does_not_create_a_producer(workspace):
    assert_no_measurement(S.snapshot(now=NOW), "missing", "file_missing")
    assert list(workspace.iterdir()) == []


def test_stale_is_not_refreshed_by_recent_mtime(workspace):
    publish(workspace, telemetry(sampled_at_epoch=NOW - 20))
    out = S.snapshot(now=NOW)
    assert_no_measurement(out, "stale", "sample_stale")
    assert out["age_s"] == 20
    assert out["sequence"] == 7


@pytest.mark.parametrize(
    ("offset", "state", "reason"),
    [
        (-S.MAX_AGE_S, "fresh", "sample_fresh"),
        (-S.MAX_AGE_S - 0.01, "stale", "sample_stale"),
        (0.01, "fresh", "sample_fresh"),
        (S.FUTURE_SKEW_S + 0.01, "invalid", "sample_in_future"),
    ],
)
def test_sample_clock_bounds(workspace, offset, state, reason):
    publish(workspace, telemetry(sampled_at_epoch=NOW + offset))
    out = S.snapshot(now=NOW)
    assert out["state"] == state and out["reason_code"] == reason
    assert (out["measurement"] is not None) is (state == "fresh")
    if state == "fresh":
        assert out["age_s"] >= 0


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"schema": "other"}, "schema_invalid"),
        ({"robot": "other"}, "schema_invalid"),
        ({"extra": "unexpected"}, "schema_invalid"),
        ({"source_id": ""}, "schema_invalid"),
        ({"source_id": "x" * 81}, "schema_invalid"),
        ({"sequence": True}, "schema_invalid"),
        ({"sequence": -1}, "schema_invalid"),
        ({"sequence": 1.5}, "schema_invalid"),
        ({"sequence": 1 << 53}, "schema_invalid"),
        ({"sampled_at_epoch": 0}, "invalid_timestamp"),
        ({"sampled_at_epoch": True}, "invalid_timestamp"),
        ({"sampled_at_epoch": "1800000000"}, "invalid_timestamp"),
        ({"joints_rad": [0] * 6}, "invalid_joints"),
        ({"joints_rad": [0] * 8}, "invalid_joints"),
        ({"joints_rad": [0, 0, 0, False, 0, 0, 0]}, "invalid_joints"),
        ({"joints_rad": [0, 0, 0, "1", 0, 0, 0]}, "invalid_joints"),
        ({"gripper": {"opening_width_m": -0.001}}, "invalid_gripper"),
        ({"gripper": {"opening_width_m": True}}, "invalid_gripper"),
        ({"gripper": {"knuckle_angle_rad": "0.5"}}, "invalid_gripper"),
        ({"gripper": {"opening_width_m": 0.05, "knuckle_angle_rad": 0.4}}, "invalid_gripper"),
        ({"gripper": {"command": 1}}, "invalid_gripper"),
        ({"reported_ee": {}}, "invalid_reported_ee"),
    ],
)
def test_invalid_fields_never_return_measurement(workspace, changes, reason):
    publish(workspace, telemetry(**changes))
    assert_no_measurement(S.snapshot(now=NOW), "invalid", reason)


@pytest.mark.parametrize("missing", list(telemetry()))
def test_all_input_fields_are_required(workspace, missing):
    data = telemetry()
    del data[missing]
    publish(workspace, data)
    assert_no_measurement(S.snapshot(now=NOW), "invalid", "schema_invalid")


@pytest.mark.parametrize(
    "change",
    [
        {"position_m": [0, 0]},
        {"quaternion_xyzw": [0, 0, 1]},
        {"quaternion_xyzw": [0, 0, 0, 0]},
        {"quaternion_xyzw": [0, 0, 0, 2]},
        {"quaternion_xyzw": [1e308, 0, 0, 1e308]},
        {"frame_id": ""},
        {"end_frame": 4},
        {"extra": 1},
    ],
)
def test_invalid_reported_pose_is_rejected(workspace, change):
    ee = copy.deepcopy(telemetry()["reported_ee"])
    ee.update(change)
    publish(workspace, telemetry(reported_ee=ee))
    assert_no_measurement(S.snapshot(now=NOW), "invalid", "invalid_reported_ee")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("field", ["sampled_at_epoch", "joints_rad", "gripper", "reported_ee"])
def test_nonfinite_values_cannot_cross_json_boundary(workspace, value, field):
    data = telemetry()
    if field == "joints_rad":
        data[field][0] = value
    elif field == "gripper":
        data[field]["opening_width_m"] = value
    elif field == "reported_ee":
        data[field]["position_m"][0] = value
    else:
        data[field] = value
    publish(workspace, data)
    assert_no_measurement(S.snapshot(now=NOW), "invalid", "invalid_json")


@pytest.mark.parametrize("raw", [b"{", b"\xff", b'{"schema":1,"schema":2}', b"[" * 2000])
def test_bad_json_is_an_explicit_invalid_state(workspace, raw):
    path = publish(workspace, telemetry())
    path.write_bytes(raw)
    assert_no_measurement(S.snapshot(now=NOW), "invalid", "invalid_json")


def test_overflow_json_number_is_rejected(workspace):
    path = publish(workspace, telemetry())
    path.write_text(path.read_text().replace("0.06", "1e999"))
    assert_no_measurement(S.snapshot(now=NOW), "invalid", "invalid_gripper")


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo", "oversized"])
def test_only_bounded_regular_files_are_read(workspace, kind):
    path = publish(workspace, telemetry())
    path.unlink()
    if kind == "symlink":
        other = workspace / "elsewhere.json"
        other.write_text(json.dumps(telemetry()))
        path.symlink_to(other)
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.write_bytes(b" " * (S.MAX_BYTES + 1))
    reason = "too_large" if kind == "oversized" else "not_regular"
    assert_no_measurement(S.snapshot(now=NOW), "invalid", reason)


def test_parent_symlink_cannot_escape_workspace(workspace, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside-robot-studio")
    (outside / "telemetry.json").write_text(json.dumps(telemetry()))
    (workspace / "robot-studio").symlink_to(outside, target_is_directory=True)
    assert_no_measurement(S.snapshot(now=NOW), "invalid", "unsafe_path")


def test_workspace_symlink_is_rejected(workspace, monkeypatch):
    from levi import paths

    publish(workspace, telemetry())
    alias = workspace.parent / (workspace.name + "-link")
    alias.symlink_to(workspace, target_is_directory=True)
    monkeypatch.setattr(paths, "ROOT", alias)
    assert_no_measurement(S.snapshot(now=NOW), "invalid", "unsafe_path")


def test_read_failure_is_unavailable_without_exposing_paths(workspace, monkeypatch):
    def fail(root):
        raise PermissionError("private-path-and-content")

    monkeypatch.setattr(S, "_read", fail)
    out = S.snapshot(now=NOW)
    assert_no_measurement(out, "unavailable", "read_failed")
    assert "private-path" not in json.dumps(out)


def test_change_during_read_is_not_accepted(workspace, monkeypatch):
    path = publish(workspace, telemetry())
    original_read = os.read
    changed = False

    def read_and_change(fd, count):
        nonlocal changed
        raw = original_read(fd, count)
        if not changed:
            changed = True
            path.write_bytes(raw + b" ")
        return raw

    monkeypatch.setattr(S.os, "read", read_and_change)
    assert_no_measurement(S.snapshot(now=NOW), "invalid", "changed_during_read")


def guard_external_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("passive reader attempted network or subprocess access")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)


def file_snapshot(workspace):
    return sorted(
        (str(p.relative_to(workspace)), p.stat().st_size, p.stat().st_mtime_ns)
        for p in workspace.rglob("*") if p.is_file()
    )


def test_reader_has_no_network_process_or_file_write_effects(workspace, monkeypatch):
    publish(workspace, telemetry())
    before = file_snapshot(workspace)
    guard_external_effects(monkeypatch)
    original_open = os.open

    def read_only_open(path, flags, *args, **kwargs):
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(S.os, "open", read_only_open)
    assert S.snapshot(now=NOW)["state"] == "fresh"
    assert file_snapshot(workspace) == before


def test_get_and_post_are_pure_and_parameter_cannot_select_another_path(workspace, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from levi.robot_studio.api import router

    publish(workspace, telemetry())
    before = file_snapshot(workspace)
    monkeypatch.setattr(S.time, "time", lambda: NOW)
    guard_external_effects(monkeypatch)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get(URL + "?path=/outside/other.json&url=http://127.0.0.1:5000/pose")
        assert response.status_code == 200
        assert response.json()["state"] == "fresh"
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["measurement"]["gripper"] == {"opening_width_m": 0.06}
        assert client.post(URL, json={"start": True}).status_code == 405
    assert file_snapshot(workspace) == before


def test_missing_and_invalid_also_use_http_200(workspace, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from levi.robot_studio.api import router

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        missing = client.get(URL)
        assert missing.status_code == 200 and missing.json()["state"] == "missing"
        publish(workspace, telemetry(joints_rad=[0]))
        invalid = client.get(URL)
        assert invalid.status_code == 200 and invalid.json()["state"] == "invalid"


def test_real_service_mount_uses_ordinary_ui_auth(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from levi import paths
    from levi.service import app

    # Import the service before replacing ROOT: its configure() also uses the
    # original STATE/CACHE constants. Then isolate only this passive reader.
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setenv("LEVI_UI_TOKEN", "test-ui-token")
    monkeypatch.setattr(S.time, "time", lambda: NOW)
    publish(tmp_path, telemetry())
    guard_external_effects(monkeypatch)
    # Deliberately do not enter the production lifespan: no services or workers.
    client = TestClient(app)
    try:
        assert client.get(URL).status_code == 401
        headers = {"x-levi-ui-token": "test-ui-token"}
        response = client.get(URL, headers=headers)
        assert response.status_code == 200 and response.json()["state"] == "fresh"
        assert client.post(URL, headers=headers).status_code == 405
    finally:
        client.close()


def test_time_spent_reading_is_included_in_freshness(workspace, monkeypatch):
    publish(workspace, telemetry())
    times = iter((NOW, NOW + 2.0))
    monkeypatch.setattr(S.time, "time", lambda: next(times))
    out = S.snapshot()
    assert_no_measurement(out, "stale", "sample_stale")
    assert out["age_s"] == 2.25
