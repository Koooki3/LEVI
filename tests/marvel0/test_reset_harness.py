"""Exercise real isolated workers, persistence and request admission offline."""

import fcntl
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from levi.automatic.offline_worker import atomic_json
from levi.marvel0 import reset_harness_api as api
from levi.marvel0.reset_harness import TERMINAL, HarnessError, HarnessManager

BASE = "/api/levi/marvel0/reset-harness"
UI = {"x-levi-ui-token": "synthetic-test"}


def completed(manager, run_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        document = manager.get(run_id)
        if document["status"] in TERMINAL:
            return document
        time.sleep(0.01)
    pytest.fail("offline worker did not complete within five seconds")


@pytest.fixture
def manager(tmp_path):
    instance = HarnessManager(tmp_path / "offline")
    yield instance
    for child in list(instance._children.values()):
        child.wait(timeout=5)


@pytest.fixture
def client(manager, monkeypatch):
    monkeypatch.setenv("LEVI_UI_TOKEN", UI["x-levi-ui-token"])
    monkeypatch.setattr(api, "manager", lambda: manager)
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as served:
        yield served


def test_catalog_does_not_create_workspace(manager, client):
    answer = client.get(BASE)
    assert answer.status_code == 200
    assert answer.json()["execution_mode"] == "offline"
    assert len(answer.json()["scenarios"]) == 7
    assert answer.json()["runs"] == []
    assert not manager.root.exists()


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("normal_random", "succeeded"),
        ("empty_grasp_retry", "succeeded"),
        ("held_unknown", "blocked"),
        ("stale_obs", "blocked"),
        ("handoff_mismatch", "blocked"),
        ("unreachable", "blocked"),
        ("stop", "stopped"),
    ],
)
def test_real_worker_terminal_evidence_and_restart(manager, scenario, expected):
    queued = manager.start(f"req_{scenario}", scenario, 42)
    result = completed(manager, queued["run_id"])
    assert result["status"] == expected
    assert result["physical_execution"] is False
    assert result["handoff"]["physical_allowed"] is False
    assert result["usage"] == {
        "llm_calls": 0,
        "input_tokens": None,
        "output_tokens": None,
        "token_source": "unavailable",
        "provider": "scripted",
    }
    assert result["stage_seq"] == len(result["events"])
    restored = HarnessManager(manager.root)
    assert restored.start(f"req_{scenario}", scenario, 42) == result
    assert len(restored.catalog()["runs"]) == 1
    assert "physical" not in json.dumps(result.get("target"))


def test_concurrent_duplicate_starts_create_only_one_worker(manager):
    other = HarnessManager(manager.root)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(m.start, "duplicate", "normal_random", 4)
            for m in (manager, other)
        ]
        results = [future.result() for future in futures]
    assert results[0]["run_id"] == results[1]["run_id"]
    completed(manager, results[0]["run_id"])
    assert len(manager.catalog()["runs"]) == 1
    with pytest.raises(HarnessError, match="request_conflict"):
        manager.start("duplicate", "normal_random", 5)


@pytest.mark.parametrize(
    "body",
    [
        {"request_id": "x", "scenario_id": "normal_random", "seed": True},
        {"request_id": "x", "scenario_id": "normal_random", "seed": "1"},
        {"request_id": "x", "scenario_id": "normal_random", "seed": -1},
        {"request_id": "x", "scenario_id": "normal_random", "seed": 2**32},
        {"request_id": "../x", "scenario_id": "normal_random", "seed": 1},
        {"request_id": "x", "scenario_id": "fr3", "seed": 1},
        {
            "request_id": "x",
            "scenario_id": "normal_random",
            "seed": 1,
            "adapter": "fr3",
        },
        {
            "request_id": "x",
            "scenario_id": "normal_random",
            "seed": 1,
            "url": "http://robot",
        },
    ],
)
def test_invalid_inputs_never_launch(manager, client, body):
    response = client.post(f"{BASE}/runs", headers=UI, json=body)
    assert response.status_code == 422
    assert not manager.root.exists()


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({}, 401),
        ({"x-levi-ui-token": "wrong"}, 401),
        ({"authorization": "Bearer synthetic"}, 403),
    ],
)
def test_writes_use_existing_ui_auth(client, manager, headers, expected):
    for suffix, body in [
        ("/runs", {"request_id": "x", "scenario_id": "normal_random", "seed": 0}),
        ("/runs/reset_" + "a" * 32 + "/stop", {"request_id": "stop"}),
    ]:
        assert (
            client.post(BASE + suffix, headers=headers, json=body).status_code
            == expected
        )
    assert not manager.root.exists()


def test_route_lifecycle_and_terminal_stop(client, manager):
    response = client.post(
        f"{BASE}/runs",
        headers=UI,
        json={
            "request_id": "ui_1",
            "scenario_id": "normal_random",
            "seed": 0,
        },
    )
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    result = completed(manager, run_id)
    assert client.get(f"{BASE}/runs/{run_id}").json() == result
    assert (
        client.post(
            f"{BASE}/runs/{run_id}/stop", headers=UI, json={"request_id": "stop1"}
        ).json()
        == result
    )
    assert not (manager.root / run_id / "stop.json").exists()


@pytest.mark.parametrize("scenario", ["normal_random", "empty_grasp_retry"])
def test_route_and_history_preserve_closure_and_holding_proof(
    client, manager, scenario
):
    response = client.post(
        f"{BASE}/runs",
        headers=UI,
        json={"request_id": f"proof_{scenario}", "scenario_id": scenario, "seed": 42},
    )
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    result = completed(manager, run_id)
    response = client.get(f"{BASE}/runs/{run_id}")
    assert response.status_code == 200
    observed = response.json()
    assert observed == result == HarnessManager(manager.root).get(run_id)
    assert observed["status"] == "succeeded"
    assert observed["adapter"] == "fake"
    assert observed["execution_mode"] == "offline"
    assert observed["physical_execution"] is False
    assert observed["handoff"]["eligible"] is True
    assert observed["handoff"]["physical_allowed"] is False

    regrasp = [event for event in observed["events"] if event["stage"] == "regrasp"]
    lifts = [event for event in observed["events"] if event["stage"] == "lift_verify"]
    assert len(regrasp) == len(lifts) == (2 if scenario == "empty_grasp_retry" else 1)
    for closed, lifted in zip(regrasp, lifts, strict=True):
        decision = closed["evidence"]["gate_decision"]
        assert decision["required_skill"] == "lift_and_verify"
        assert decision["evidence"]["closed_measured"] is True
        assert decision["evidence"]["open_measured"] is False
        assert decision["physical_allowed"] is False
        assert decision["model_proposal_allowed"] is False
        proof = lifted["evidence"]["holding"]["evidence"]
        for field in (
            "before_seq",
            "after_seq",
            "before_captured_ms",
            "after_captured_ms",
        ):
            assert type(proof[field]) is int
        assert proof["before_seq"] == decision["evidence"]["seq"] < proof["after_seq"]
        assert proof["before_captured_ms"] == decision["evidence"]["captured_ms"]
        assert proof["before_captured_ms"] < proof["after_captured_ms"]

    assert observed["holding"] == lifts[-1]["evidence"]["holding"]
    assert observed["holding"]["status"] == "pass"
    handoff = next(event for event in observed["events"] if event["stage"] == "handoff")
    assert (
        handoff["evidence"]["observation_seq"]
        > observed["holding"]["evidence"]["after_seq"]
    )
    assert observed["final_observation"]["status"] == "pass"
    assert observed["final_observation"]["evidence"]["released"] is True
    assert observed["final_observation"]["evidence"]["object_at_target"] is True
    assert observed["final_observation"]["evidence"]["gripper_measurement"] == 1.0


def test_stop_is_durable_and_cooperative(manager):
    from levi.marvel0.reset_harness import initial_document

    run_id = "reset_" + "a" * 32
    directory = manager.root / run_id
    directory.mkdir(parents=True)
    spec = {
        "run_id": run_id,
        "request_id": "held",
        "scenario_id": "normal_random",
        "seed": 0,
    }
    atomic_json(directory / "request.json", spec)
    atomic_json(directory / "status.json", initial_document(spec))
    fd = os.open(directory / "worker.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert manager.stop(run_id, "stop1")["status"] == "queued"
        assert (
            json.loads((directory / "stop.json").read_text())["request_id"] == "stop1"
        )
        manager.stop(run_id, "stop2")
        assert (
            json.loads((directory / "stop.json").read_text())["request_id"] == "stop1"
        )
        with pytest.raises(HarnessError, match="run_active"):
            manager.start("second", "normal_random", 0)
        from levi.automatic.offline_worker import execute

        assert execute(directory) == 0
        assert manager.get(run_id)["status"] == "stopped"
        assert not any(
            event["stage"] == "policy" for event in manager.get(run_id)["events"]
        )
    finally:
        os.close(fd)


def test_missing_worker_is_failed_without_retry_or_pid_signal(manager):
    queued = manager.start("gone", "normal_random", 0)
    result = completed(manager, queued["run_id"])
    directory = manager.root / result["run_id"]
    # Wait for process lease to disappear before simulating a crash record.
    for child in list(manager._children.values()):
        child.wait(timeout=5)
    from levi.marvel0.reset_harness import initial_document

    result = initial_document(json.loads((directory / "request.json").read_text()))
    result.update(status="running")
    atomic_json(directory / "status.json", result)
    restarted = HarnessManager(manager.root)
    lost = restarted.get(result["run_id"])
    assert lost["status"] == "failed" and lost["stage"] == "worker_lost"
    assert lost["handoff"]["eligible"] is False
    assert restarted.start("gone", "normal_random", 0) == lost


def test_symlink_run_and_tampered_physical_evidence_refused(manager, tmp_path):
    queued = manager.start("tamper", "normal_random", 0)
    result = completed(manager, queued["run_id"])
    path = manager.root / result["run_id"] / "status.json"
    result["physical_execution"] = True
    atomic_json(path, result)
    with pytest.raises(HarnessError, match="invalid_run_record"):
        manager.get(result["run_id"])
    alias = "reset_" + "b" * 32
    (manager.root / alias).symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(HarnessError, match="run_missing"):
        manager.get(alias)


def test_socket_guard_is_child_local_and_blocks_dns_unix_and_tcp():
    code = """
import socket
from levi.automatic.offline_worker import disable_network
disable_network()
for attempt in (lambda: socket.socket(), lambda: socket.socket(socket.AF_UNIX),
                lambda: socket.getaddrinfo("localhost", 1),
                lambda: socket.create_connection(("127.0.0.1", 1))):
    try:
        attempt()
    except PermissionError:
        pass
    else:
        raise AssertionError("network guard bypassed")
"""
    answer = subprocess.run(
        [sys.executable, "-B", "-c", code], capture_output=True, timeout=5, check=False
    )
    assert answer.returncode == 0, answer.stderr.decode()
    import socket

    assert isinstance(socket.socket, type)  # Parent server still has its socket class.


def test_finish_between_read_and_lease_check_does_not_report_lost(manager, monkeypatch):
    queued = manager.start("race", "normal_random", 0)
    finished = completed(manager, queued["run_id"])
    running = dict(finished, status="running")
    reads = iter([running, finished])
    monkeypatch.setattr(manager, "_read", lambda _: next(reads))
    monkeypatch.setattr(manager, "_active", lambda _: False)
    assert manager.get(finished["run_id"])["status"] == "succeeded"


def test_incomplete_record_is_reported_without_blocking_new_runs(manager):
    broken_id = "reset_" + "d" * 32
    (manager.root / broken_id).mkdir(parents=True)
    catalog = manager.catalog()
    assert catalog["runs"] == []
    assert catalog["issues"] == [{"run_id": broken_id, "code": "invalid_run_record"}]
    queued = manager.start("after_partial", "normal_random", 0)
    assert completed(manager, queued["run_id"])["status"] == "succeeded"
    assert len(manager.catalog()["issues"]) == 1


@pytest.mark.parametrize(
    "field",
    [
        "updated_at",
        "events",
        "stage_seq",
        "handoff",
        "holding",
        "status",
        "elapsed_ms",
        "final_observation",
        "target",
        "usage",
        "seed",
    ],
)
def test_corrupt_fields_are_isolated_from_valid_history(manager, field):
    first = manager.start("corrupt", "normal_random", 42)
    result = completed(manager, first["run_id"])
    path = manager.root / result["run_id"] / "status.json"
    for invalid in ("missing", None, {"invalid": True}):
        broken = dict(result)
        if invalid == "missing":
            del broken[field]
        else:
            broken[field] = invalid
        # target/final_observation null are valid in general, but a succeeded
        # run cannot lose its final proof. Target is separately checked below.
        if field == "target" and invalid is None:
            continue
        atomic_json(path, broken)
        assert manager.catalog()["runs"] == []
        assert manager.catalog()["issues"] == [
            {"run_id": result["run_id"], "code": "invalid_run_record"}
        ]
    atomic_json(path, result)
    assert len(manager.catalog()["runs"]) == 1


def test_spawn_failure_keeps_a_terminal_record(manager, monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("synthetic failure")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    with pytest.raises(HarnessError, match="worker_start_failed"):
        manager.start("spawn_failure", "normal_random", 0)
    result = manager.catalog()["runs"][0]
    assert result["status"] == "failed"
    assert result["stage"] == result["events"][-1]["stage"] == "worker_start_failed"
    assert result["stage_seq"] == len(result["events"]) == 1
    assert manager.start("spawn_failure", "normal_random", 0) == result


def test_mount_and_capabilities_leave_real_modes_unchanged():
    from levi.automatic.api import capabilities
    from levi.marvel0.api import router

    paths = [route.path for route in router.routes]
    assert paths.count(BASE) == 1
    assert paths.count(f"{BASE}/runs") == 1
    assert "/api/levi/marvel0/aeri/run/start" in paths
    caps = capabilities()
    assert caps["offline_reset_harness"]["physical_execution"] is False
    assert caps["modes"]["autonomous"]["available"] is False


def test_offline_report_uses_workers_and_retains_expected_failures(tmp_path):
    from levi.automatic.offline_report import run_matrix

    report = run_matrix(tmp_path / "matrix", 42)
    assert report["passed"] is True
    assert report["physical_execution"] is False
    assert len(report["results"]) == 7
    assert sum(row["run"]["status"] == "succeeded" for row in report["results"]) == 2
    assert (tmp_path / "matrix" / "reset-harness-report.json").is_file()


@pytest.mark.parametrize("timeout_s,expected", [(20, "blocked"), (0, "failed")])
def test_worker_terminal_snapshot_is_written_once(
    tmp_path, monkeypatch, timeout_s, expected
):
    from levi.automatic import offline_worker
    from levi.marvel0.reset_harness import initial_document

    directory = tmp_path / ("reset_" + "f" * 32)
    directory.mkdir()
    spec = {
        "run_id": directory.name,
        "request_id": "one_terminal_write",
        "scenario_id": "held_unknown",
        "seed": 42,
    }
    atomic_json(directory / "request.json", spec)
    atomic_json(directory / "status.json", initial_document(spec))
    writes = []

    def record(path, document):
        writes.append(document)
        atomic_json(path, document)

    monkeypatch.setattr(offline_worker, "atomic_json", record)
    assert offline_worker.execute(directory, timeout_s=timeout_s) == (
        1 if expected == "failed" else 0
    )
    terminal = [row for row in writes if row["status"] in TERMINAL]
    assert len(terminal) == 1
    assert terminal[0]["status"] == expected
    if expected == "failed":
        assert terminal[0]["events"][-1]["reason"] == "worker_deadline"
