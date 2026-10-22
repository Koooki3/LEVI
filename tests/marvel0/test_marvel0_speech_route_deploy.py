"""The one call ``deploy_api`` makes into the speech worker's manager: the
policy deploy runs inside ``held_off()``, which releases the speech worker
(so it holds no GPU memory the deploy's checks would count), waits until it is
gone, and lets no worker start until the deploy has answered; best effort,
without ever failing the deploy. The supervisor is a stand-in: nothing real is
started."""

import contextlib
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from levi.marvel0 import deploy_api
from levi.stt import manager as stt_manager
from levi.stt.errors import SttError

BODY = {
    "request_id": "deploy-req-0001",
    "model_id": "pi05_fr3_all_step49999",
    "rtc": True,
    "fraction": 0.22,
    "confirm": "deploy pi05_fr3_all_step49999",
}


class Supervisor:
    def __init__(self, events):
        self.events = events

    def deploy(self, body):
        self.events.append("deploy")
        return {"state": "starting", "request_id": body["request_id"]}

    def status(self):
        self.events.append("status")
        return {"state": "idle"}

    def log(self, lines):
        self.events.append("log")
        return {"lines": []}

    def stop(self, body):
        self.events.append("stop")
        return {"already": True}


@pytest.fixture(autouse=True)
def no_token(monkeypatch):
    monkeypatch.delenv("LEVI_UI_TOKEN", raising=False)


@pytest.fixture
def events(monkeypatch):
    log = []
    monkeypatch.setattr(deploy_api, "supervisor", lambda: Supervisor(log))
    return log


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(deploy_api.router)
    return TestClient(app)


class Speech:
    """Stands for the manager: records the hold around the deploy."""

    def __init__(self, events, error=None):
        self.events, self.error = events, error

    @contextlib.contextmanager
    def held_off(self):
        self.events.append("hold")
        if self.error:
            self.events.append("release failed")  # logged by the real one
        try:
            yield
        finally:
            self.events.append("unhold")


def install(monkeypatch, manager):
    monkeypatch.setattr(stt_manager, "_MANAGER", [manager] if manager else [])


def test_the_deploy_runs_inside_the_speech_hold(client, events, monkeypatch):
    install(monkeypatch, Speech(events))
    response = client.post("/api/levi/marvel0/deploy", json=BODY)
    assert response.status_code == 202
    assert events == ["hold", "deploy", "unhold"]


def test_a_refused_deploy_still_ends_the_hold(client, events, monkeypatch):
    install(monkeypatch, Speech(events))

    class Refusing(Supervisor):
        def deploy(self, body):
            self.events.append("deploy")
            raise RuntimeError("refused")

    monkeypatch.setattr(deploy_api, "supervisor", lambda: Refusing(events))
    with pytest.raises(RuntimeError):
        client.post("/api/levi/marvel0/deploy", json=BODY)
    assert events == ["hold", "deploy", "unhold"]


def test_a_read_a_log_or_a_stop_does_not_hold_the_speech_worker(
    client, events, monkeypatch
):
    install(monkeypatch, Speech(events))
    assert client.get("/api/levi/marvel0/deploy").status_code == 200
    assert client.get("/api/levi/marvel0/deploy/log").status_code == 200
    stop = {"request_id": "stop-req-00001", "confirm": "stop policy server"}
    assert client.post("/api/levi/marvel0/deploy/stop", json=stop).status_code == 200
    assert events == ["status", "log", "stop"]


def test_the_real_hold_refuses_a_cold_start_during_the_deploy(
    client, events, monkeypatch
):
    """A transcription that arrives while the deploy runs is refused with
    ``held`` (the HTTP layer words it as ``speech_policy_starting``), and
    nothing is spawned; after the deploy a start is allowed again."""
    spawned = []

    def command(cpu_affinity):
        spawned.append(cpu_affinity)
        raise AssertionError("no worker may start during a deploy")

    manager = stt_manager.Manager(command=command, watch=False)
    install(monkeypatch, manager)
    seen = {}

    class Deploying(Supervisor):
        def deploy(self, body):
            try:
                manager.transcribe("req-00000001", b"x" * 64)
            except SttError as exc:
                seen["code"] = exc.code
            return super().deploy(body)

    monkeypatch.setattr(deploy_api, "supervisor", lambda: Deploying(events))
    assert client.post("/api/levi/marvel0/deploy", json=BODY).status_code == 202
    assert seen == {"code": "held"} and spawned == []
    assert manager._holds == 0


def test_the_real_hold_with_no_worker_running_starts_nothing(monkeypatch):
    manager = stt_manager.Manager(watch=False)
    install(monkeypatch, manager)
    with stt_manager.held_off():
        assert manager.status()["state"] == "off"
    assert manager.status()["state"] == "off" and manager.worker_pid() is None


def test_a_release_that_fails_does_not_fail_the_deploy(client, events, monkeypatch):
    manager = stt_manager.Manager(watch=False)

    def broken():
        raise RuntimeError("could not stop")

    monkeypatch.setattr(manager, "release", broken)
    install(monkeypatch, manager)
    assert client.post("/api/levi/marvel0/deploy", json=BODY).status_code == 202
    assert events == ["deploy"] and manager._holds == 0


def test_the_hold_counts_overlapping_deploys(monkeypatch):
    manager = stt_manager.Manager(watch=False)
    inside = threading.Event()
    leave = threading.Event()

    def first():
        with manager.held_off():
            inside.set()
            leave.wait(5)

    thread = threading.Thread(target=first)
    thread.start()
    try:
        assert inside.wait(5)
        with manager.held_off():
            assert manager._holds == 2
        assert manager._holds == 1
    finally:
        leave.set()
        thread.join(5)
    assert manager._holds == 0
