"""The rig routes as ``levi.service.app`` serves them: the seven paths are there
exactly once each, behind the web UI token, and the two writes of the policy
deploy refuse a Bearer credential. ``test_rig_deploy_route.py`` and
``test_rig_catalog_route.py`` test each router on an app of its own; this file
tests how ``levi/rig/api.py`` joins them to the service, so a tidy-up of that
join (or a FastAPI change in how a router's routes are read) cannot drop or
double a route unnoticed. The machine is the fake one of ``test_rig_deploy.py``:
no process, socket or ``/proc`` entry is touched."""

import warnings

import pytest
from fastapi.openapi.utils import get_openapi
from fastapi.testclient import TestClient
from site_sample import write_site
from test_rig_deploy import (  # noqa: F401 - the fixtures and the fake machine
    REQ,
    STOP,
    Rig,
    body,
    forbid_real_machine,
)

# Imported here, not in a test: the service and what its middleware imports on
# the first request read the machine at import time (huggingface_hub reads the
# boot id), which the guard of test_rig_deploy.py forbids during a test.
from levi.agent import legacy, security  # noqa: F401
from levi.rig import api, deploy_api
from levi.service import app

TOKEN = "test-ui-token"
UI = {"x-levi-ui-token": TOKEN}
BASE = "/api/levi/rig"
# (method, path) of everything the rig adds to the service.
ROUTES = [
    ("GET", f"{BASE}/status"),
    ("GET", f"{BASE}/models"),
    ("GET", f"{BASE}/site"),
    ("POST", f"{BASE}/command/preview"),
    ("GET", f"{BASE}/deploy"),
    ("POST", f"{BASE}/deploy"),
    ("POST", f"{BASE}/deploy/stop"),
    ("GET", f"{BASE}/deploy/log"),
]
WRITES = [("POST", f"{BASE}/deploy"), ("POST", f"{BASE}/deploy/stop")]


@pytest.fixture
def service(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    monkeypatch.setenv("LEVI_UI_TOKEN", TOKEN)
    monkeypatch.setenv("LEVI_RIG_SITE", str(write_site(tmp_path)))
    monkeypatch.setattr(deploy_api, "supervisor", lambda: rig.sup)
    return TestClient(app), rig


def payload(path):
    if path.endswith("/stop"):
        return STOP
    if path.endswith("/deploy"):
        return body()
    return {"model_id": "m", "prompt": "p"}


def test_the_router_of_the_service_holds_each_route_once():
    pairs = [
        (route.path, method)
        for route in api.router.routes
        for method in sorted(route.methods - {"HEAD"})
    ]
    assert sorted(pairs) == sorted((path, method) for method, path in ROUTES)


def test_the_service_serves_each_route_once():
    # (this FastAPI keeps an included router whole, so ``app.routes`` does not
    # list the paths: the schema does, and warns about a doubled operation id.)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        schema = get_openapi(title="rig", version="1", routes=app.routes)["paths"]
    for method, path in ROUTES:
        assert method.lower() in schema[path], (method, path)
    assert sorted(p for p in schema if p.startswith(BASE)) == sorted(
        {path for _, path in ROUTES}
    )
    assert [str(w.message) for w in caught if "api_levi_rig" in str(w.message)] == []


@pytest.mark.parametrize("method, path", ROUTES)
def test_the_web_ui_token_is_needed_for_every_route(service, method, path):
    client, rig = service
    response = client.request(
        method, path, json=payload(path) if method == "POST" else None
    )
    assert response.status_code == 401
    assert rig.calls == [], "nothing ran behind the refusal"


@pytest.mark.parametrize("method, path", ROUTES[1:])
def test_a_route_answers_with_the_token(service, method, path):
    client, _ = service
    # (the status route reads the real machine; test_rig_route.py covers it)
    response = client.request(
        method, path, headers=UI, json=payload(path) if method == "POST" else None
    )
    assert response.status_code not in (401, 403, 404, 405), response.text


@pytest.mark.parametrize("method, path", WRITES)
@pytest.mark.parametrize("token", [True, False])
def test_a_bearer_credential_cannot_deploy_or_stop(
    service, monkeypatch, method, path, token
):
    client, rig = service
    if not token:
        monkeypatch.delenv("LEVI_UI_TOKEN")
    headers = {"authorization": "Bearer not-an-agent-credential"}
    if token:
        headers.update(UI)
    response = client.request(method, path, headers=headers, json=payload(path))
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "person_only"
    assert rig.calls == [], "no command ran"
    assert rig.systemd_runs() == []


def test_a_person_with_the_token_reaches_the_deploy_and_the_stop(service):
    client, rig = service
    started = client.post(f"{BASE}/deploy", headers=UI, json=body())
    assert started.status_code == 202, started.text
    assert started.json()["request_id"] == REQ
    assert len(rig.systemd_runs()) == 1
    # Sent again with the same id: answered again, started once.
    again = client.post(f"{BASE}/deploy", headers=UI, json=body())
    assert again.status_code == 202 and again.json()["replayed"] is True
    assert len(rig.systemd_runs()) == 1
    rig.server_comes_up()
    rig.sup.invalidate()
    stopped = client.post(f"{BASE}/deploy/stop", headers=UI, json=STOP)
    assert stopped.status_code in (200, 202), stopped.text
    assert len(rig.stops()) == 1


def test_a_read_of_the_state_needs_no_confirmation_and_changes_nothing(service):
    client, rig = service
    before = list(rig.calls)
    view = client.get(f"{BASE}/deploy", headers=UI)
    assert view.status_code == 200 and view.json()["state"] == "idle"
    assert rig.systemd_runs() == [] and rig.stops() == []
    assert all(c[0][0] != "systemd-run" for c in rig.calls[len(before) :])
