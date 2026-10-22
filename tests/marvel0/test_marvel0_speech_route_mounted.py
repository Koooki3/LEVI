"""The four speech routes as ``levi.service.app`` serves them: each once, behind
the web UI token, and refusing a Bearer credential. ``test_marvel0_speech_route.py``
tests the router on an app of its own; this file tests how the service joins it
(``levi/service.py``), so a tidy-up of that join cannot drop or double a route
unnoticed. The machine behind it is the fake one of that file."""

import warnings

import pytest
from fastapi.openapi.utils import get_openapi
from fastapi.testclient import TestClient
from site_sample import write_site
from test_marvel0_speech_route import (
    FakeManager,
    Matcher,
    Policy,
    wav_bytes,
)

# Imported here, not in a test: the service and what its middleware imports on
# the first request read the machine at import time.
from levi.agent import legacy, security  # noqa: F401
from levi.marvel0 import deploy_api, speech_api
from levi.marvel0 import site as marvel0_site
from levi.service import app

TOKEN = "test-ui-token"
UI = {"x-levi-ui-token": TOKEN}
BASE = "/api/levi/marvel0/speech"
ROUTES = [
    ("GET", BASE),
    ("POST", f"{BASE}/transcribe"),
    ("POST", f"{BASE}/match"),
    ("POST", f"{BASE}/release"),
]


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVI_UI_TOKEN", TOKEN)
    monkeypatch.setenv("LEVI_MARVEL0_SITE", str(write_site(tmp_path)))
    marvel0_site.clear_cache()
    manager = FakeManager()
    monkeypatch.setattr(deploy_api, "supervisor", lambda: Policy())
    monkeypatch.setattr(speech_api, "manager", lambda: manager)
    monkeypatch.setattr(speech_api, "_matcher", lambda: Matcher())
    monkeypatch.setattr(speech_api, "RATE", speech_api.RateLimit(10, 60.0))
    yield TestClient(app), manager
    marvel0_site.clear_cache()


def send(client, method, path, headers=None):
    if path.endswith("/transcribe"):
        return client.post(
            f"{path}?request_id=speech-req-0001",
            content=wav_bytes(),
            headers={"content-type": "audio/wav", **(headers or {})},
        )
    body = (
        {"json": {"transcript": "stack the plates"}} if path.endswith("/match") else {}
    )
    return client.request(method, path, headers=headers, **body)


def test_the_service_serves_each_speech_route_once():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        schema = get_openapi(title="marvel0", version="1", routes=app.routes)["paths"]
    for method, path in ROUTES:
        assert method.lower() in schema[path], (method, path)
    assert sorted(p for p in schema if p.startswith(BASE)) == sorted(
        {path for _, path in ROUTES}
    )
    assert [str(w.message) for w in caught if "speech" in str(w.message)] == []


@pytest.mark.parametrize("method, path", ROUTES)
def test_the_web_ui_token_is_needed_for_every_speech_route(service, method, path):
    client, manager = service
    assert send(client, method, path).status_code == 401
    assert manager.calls == [] and manager.released == 0


@pytest.mark.parametrize("method, path", ROUTES)
def test_a_person_with_the_token_reaches_every_speech_route(service, method, path):
    client, _ = service
    response = send(client, method, path, UI)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("method, path", ROUTES)
def test_a_bearer_credential_cannot_use_speech(service, method, path):
    client, manager = service
    response = send(
        client, method, path, {**UI, "authorization": "Bearer not-an-agent-credential"}
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "person_only"
    assert manager.calls == [] and manager.released == 0
