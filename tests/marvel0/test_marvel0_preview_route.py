"""The routes of the camera preview (``levi/marvel0/preview_api.py``) on an app of
their own over a preview whose worker is a scripted process: who may call them,
the shape of each answer and of each refusal, the picture and its headers. No
camera, process or socket is touched. How ``api.py`` joins the routes to the
service is in ``test_marvel0_routes_mounted.py``."""

import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_marvel0_preview import SITE, Rig

from levi.marvel0 import preview, preview_api

BASE = "/api/levi/marvel0"
TOKEN = "test-ui-token"
UI = {"x-levi-ui-token": TOKEN}
BEARER = {"authorization": "Bearer not-an-agent-credential"}
JPEG = b"\xff\xd8\xff\xe0 a whole picture \xff\xd9"


@pytest.fixture
def served(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVI_UI_TOKEN", TOKEN)
    rig = Rig(tmp_path)
    monkeypatch.setattr(preview, "manager", lambda: rig.mgr)
    web = FastAPI()
    web.include_router(preview_api.router)
    return TestClient(web), rig


def detail(response) -> dict:
    return response.json()["detail"]


@pytest.mark.parametrize("name", ["start", "stop"])
def test_a_bearer_credential_cannot_call_a_write(served, name):
    client, rig = served
    response = client.post(f"{BASE}/preview/{name}", headers=BEARER)
    assert response.status_code == 403 and detail(response)["code"] == "person_only"
    assert rig.procs == [] and rig.signals == []


@pytest.mark.parametrize("name", ["start", "stop"])
def test_a_write_needs_the_web_ui_token(served, name):
    client, rig = served
    response = client.post(f"{BASE}/preview/{name}", headers={"x-levi-ui-token": "x"})
    assert response.status_code == 401 and detail(response)["code"] == "ui_token"
    response = client.post(f"{BASE}/preview/{name}")
    assert response.status_code == 401
    assert rig.procs == []


def test_the_view_is_a_read_that_changes_nothing(served):
    client, rig = served
    response = client.get(f"{BASE}/preview")
    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == "levi.marvel0.preview.v1"
    assert body["state"] == "off" and body["may_start"] is True
    assert rig.procs == [] and rig.signals == []
    assert str(rig.tmp) not in response.text


def test_a_person_starts_the_preview_and_the_answer_is_the_view(served):
    client, rig = served
    response = client.post(f"{BASE}/preview/start", headers=UI)
    assert response.status_code == 202
    assert response.json()["state"] == "starting" and len(rig.procs) == 1
    again = client.post(f"{BASE}/preview/start", headers=UI)
    assert again.status_code == 202 and len(rig.procs) == 1  # not twice


@pytest.mark.parametrize(
    "why",
    ["run_active", "aeri_active", "client_connected", "arm_in_use", "state_unreadable"],
)
def test_a_start_that_may_not_go_on_is_a_409_with_its_reason(served, why):
    client, rig = served
    rig.blocked = [why]
    response = client.post(f"{BASE}/preview/start", headers=UI)
    assert response.status_code == 409
    body = detail(response)
    assert body["code"] == why and body["message"] == preview.REFUSALS[why][1]
    assert body["reasons"] == [why]
    assert rig.procs == []
    assert client.get(f"{BASE}/preview").json()["state"] == "refused"


def test_only_the_product_may_open_the_cameras_a_409_not_product(served):
    client, rig = served
    rig.real = False
    response = client.post(f"{BASE}/preview/start", headers=UI)
    assert response.status_code == 409
    body = detail(response)
    assert body["code"] == "not_product"
    assert body["message"] == preview.REFUSALS["not_product"][1]
    assert rig.procs == []
    view = client.get(f"{BASE}/preview").json()
    assert view["state"] == "refused" and view["blocked_by"] == ["not_product"]


def test_a_stop_that_needed_sigkill_is_said_in_the_view(tmp_path, monkeypatch):
    import signal

    from test_marvel0_preview import Rig

    rig = Rig(tmp_path, ends_on=signal.SIGKILL)
    monkeypatch.setattr(preview, "manager", lambda: rig.mgr)
    monkeypatch.setenv("LEVI_UI_TOKEN", TOKEN)
    web = FastAPI()
    web.include_router(preview_api.router)
    client = TestClient(web)
    rig.running()
    body = client.post(f"{BASE}/preview/stop", headers=UI).json()
    assert body["state"] == "off" and body["killed"] is True
    assert client.get(f"{BASE}/preview").json()["killed"] is True


def test_the_site_file_missing_answers_412_as_everywhere(served):
    client, rig = served

    def missing():
        raise HTTPException(412, {"code": "site_missing", "message": "no site file"})

    rig.host.site_loader = missing
    response = client.post(f"{BASE}/preview/start", headers=UI)
    assert response.status_code == 412 and detail(response)["code"] == "site_missing"
    assert rig.procs == []


def test_a_worker_that_cannot_be_started_is_unavailable(served):
    client, rig = served

    def unavailable(site, out):
        raise preview.Refused("preview_unavailable")

    rig.host.command = unavailable
    response = client.post(f"{BASE}/preview/start", headers=UI)
    assert response.status_code == 503
    assert detail(response)["code"] == "preview_unavailable"


def test_stop_lets_the_cameras_go_and_is_idempotent(served):
    client, rig = served
    client.post(f"{BASE}/preview/start", headers=UI)
    rig.pictures()
    rig.mgr.tick()
    stopped = client.post(f"{BASE}/preview/stop", headers=UI)
    assert stopped.status_code == 200
    body = stopped.json()
    assert body["state"] == "off" and body["stopped_by"] == "requested"
    assert rig.procs[0].code is not None
    again = client.post(f"{BASE}/preview/stop", headers=UI)
    assert again.status_code == 200 and again.json()["state"] == "off"


def test_the_picture_is_a_jpeg_that_is_never_cached_with_its_age(served):
    client, rig = served
    rig.running()
    (rig.out / "wrist.jpg").write_bytes(JPEG)
    response = client.get(f"{BASE}/preview/frame", params={"camera": "wrist"})
    assert response.status_code == 200 and response.content == JPEG
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert 0 <= int(response.headers["x-levi-frame-age-ms"]) < 5000
    assert "x-levi-frame-episode" not in response.headers


def test_a_frame_request_keeps_the_preview_alive(served):
    client, rig = served
    rig.running()
    rig.clock.advance(preview.IDLE_S - 1)
    client.get(f"{BASE}/preview/frame", params={"camera": "side"})
    rig.clock.advance(preview.IDLE_S - 1)
    rig.pictures()
    assert client.get(f"{BASE}/preview").json()["state"] == "on"
    rig.clock.advance(2)
    assert client.get(f"{BASE}/preview").json()["state"] == "off"


def test_without_a_picture_the_answer_is_a_404_with_the_reason(served):
    client, rig = served
    response = client.get(f"{BASE}/preview/frame", params={"camera": "side"})
    assert response.status_code == 404 and detail(response)["code"] == "preview_off"
    assert detail(response)["message"] == preview.FRAME_REASONS["preview_off"]
    client.post(f"{BASE}/preview/start", headers=UI)  # starting: no picture yet
    response = client.get(f"{BASE}/preview/frame", params={"camera": "wrist"})
    assert response.status_code == 404 and detail(response)["code"] == "no_frame"
    assert str(rig.tmp) not in response.text


@pytest.mark.parametrize("query", [{}, {"camera": "back"}, {"camera": "../side"}])
def test_a_camera_other_than_side_or_wrist_is_a_422(served, query):
    client, rig = served
    rig.running()
    response = client.get(f"{BASE}/preview/frame", params=query)
    assert response.status_code == 422


def test_no_answer_names_a_path_or_a_serial(served):
    client, rig = served
    texts = [client.get(f"{BASE}/preview").text]
    texts.append(client.post(f"{BASE}/preview/start", headers=UI).text)
    rig.pictures()
    rig.mgr.tick()
    texts.append(client.post(f"{BASE}/preview/stop", headers=UI).text)
    text = json.dumps(texts)
    for secret in (str(rig.tmp), SITE.cameras.view, SITE.cameras.wrist):
        assert secret not in text
