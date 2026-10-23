"""The marvel0 routes as ``levi.service.app`` serves them: the paths are there
exactly once each, behind the web UI token, and every write (the policy deploy,
the arm stack, the hosted run) refuses a Bearer credential. ``test_marvel0_deploy_route.py`` and
``test_marvel0_catalog_route.py`` test each router on an app of its own; this file
tests how ``levi/marvel0/api.py`` joins them to the service, so a tidy-up of that
join (or a FastAPI change in how a router's routes are read) cannot drop or
double a route unnoticed. The machine is the fake one of ``test_marvel0_deploy.py``:
no process, socket or ``/proc`` entry is touched; the run's supervisor is the
scripted one of ``test_marvel0_run.py``."""

import warnings

import pytest
from aeri_machine import AeriMachine
from fastapi.openapi.utils import get_openapi
from fastapi.testclient import TestClient
from site_sample import write_site
from test_marvel0_deploy import (  # noqa: F401 - the fixtures and the fake machine
    REQ,
    STOP,
    Marvel0,
    body,
    forbid_real_machine,
)
from test_marvel0_run import Machine as RunMachine
from test_marvel0_stack import Machine as StackMachine

# Imported here, not in a test: the service and what its middleware imports on
# the first request read the machine at import time (huggingface_hub reads the
# boot id), which the guard of test_marvel0_deploy.py forbids during a test.
from levi.agent import legacy, security  # noqa: F401
from levi.marvel0 import aeri_run_api, api, deploy_api, run_api, speech_api, stack_api
from levi.service import app

TOKEN = "test-ui-token"
UI = {"x-levi-ui-token": TOKEN}
BASE = "/api/levi/marvel0"
# (method, path) of everything the marvel0 adds to the service.
ROUTES = [
    ("GET", f"{BASE}/status"),
    ("GET", f"{BASE}/models"),
    ("GET", f"{BASE}/site"),
    ("POST", f"{BASE}/command/preview"),
    ("GET", f"{BASE}/deploy"),
    ("POST", f"{BASE}/deploy"),
    ("POST", f"{BASE}/deploy/stop"),
    ("GET", f"{BASE}/deploy/log"),
    ("GET", f"{BASE}/stack"),
    ("POST", f"{BASE}/stack/monitor/start"),
    ("POST", f"{BASE}/stack/monitor/stop"),
    ("POST", f"{BASE}/stack/marker/clear"),
    # The arm stack's actions (``stack_act_api.py``).
    ("POST", f"{BASE}/stack/m/start"),
    ("POST", f"{BASE}/stack/m/stop"),
    ("POST", f"{BASE}/stack/a/start"),
    ("POST", f"{BASE}/stack/a/stop"),
    ("POST", f"{BASE}/stack/b/start"),
    ("POST", f"{BASE}/stack/b/stop"),
    ("POST", f"{BASE}/stack/c/start"),
    ("POST", f"{BASE}/stack/c/stop"),
    ("POST", f"{BASE}/stack/marker"),
    ("POST", f"{BASE}/stack/a/preflight"),
    ("POST", f"{BASE}/stack/b/probe"),
    ("POST", f"{BASE}/stack/c/preflight"),
    ("POST", f"{BASE}/stack/b/gripper"),
    ("POST", f"{BASE}/stack/sequence/start"),
    ("POST", f"{BASE}/stack/sequence/stop"),
    ("POST", f"{BASE}/stack/sequence/cancel"),
    ("GET", f"{BASE}/stack/a/log"),
    ("GET", f"{BASE}/stack/b/log"),
    ("GET", f"{BASE}/stack/c/log"),
    # The hosted evaluation run (``run_api.py``).
    ("GET", f"{BASE}/run"),
    ("POST", f"{BASE}/run/challenge"),
    ("POST", f"{BASE}/run/start"),
    ("POST", f"{BASE}/run/key"),
    ("POST", f"{BASE}/run/hold"),
    ("POST", f"{BASE}/run/end"),
    ("GET", f"{BASE}/run/log"),
    # Manual preview/control admission shares run_api's supervisor.
    ("GET", f"{BASE}/arm/manual"),
    ("POST", f"{BASE}/arm/manual/challenge"),
    ("POST", f"{BASE}/arm/manual/start"),
    ("POST", f"{BASE}/arm/manual/target"),
    ("POST", f"{BASE}/arm/manual/gripper"),
    ("POST", f"{BASE}/arm/manual/deadman"),
    ("POST", f"{BASE}/arm/manual/stop"),
    ("POST", f"{BASE}/arm/manual/end"),
    # The AERI preparation card (``aeri_api.py``).
    ("GET", f"{BASE}/aeri"),
    ("POST", f"{BASE}/aeri/prepare"),
    ("POST", f"{BASE}/aeri/live/stop"),
    ("POST", f"{BASE}/aeri/leftovers/stop"),
    # The hosted AERI terminal D (``aeri_run_api.py``).
    ("GET", f"{BASE}/aeri/run"),
    ("POST", f"{BASE}/aeri/run/challenge"),
    ("POST", f"{BASE}/aeri/run/start"),
    ("POST", f"{BASE}/aeri/run/key"),
    ("POST", f"{BASE}/aeri/run/stop"),
    ("POST", f"{BASE}/aeri/run/end"),
    ("POST", f"{BASE}/aeri/run/presence"),
    ("GET", f"{BASE}/aeri/run/log"),
]
# The voice input's routes live on their own router (``speech_api.py``); their
# token, person and body checks are in test_marvel0_speech_route_mounted.py.
SPEECH_ROUTES = [
    ("GET", f"{BASE}/speech"),
    ("POST", f"{BASE}/speech/transcribe"),
    ("POST", f"{BASE}/speech/match"),
    ("POST", f"{BASE}/speech/release"),
]
# Every route that changes something: a person's, never a Bearer credential's.
WRITES = [
    (method, path)
    for method, path in ROUTES
    if method == "POST" and not path.endswith("/command/preview")
]


@pytest.fixture
def service(tmp_path, monkeypatch):
    marvel0 = Marvel0(tmp_path, monkeypatch)
    monkeypatch.setenv("LEVI_UI_TOKEN", TOKEN)
    monkeypatch.setenv("LEVI_MARVEL0_SITE", str(write_site(tmp_path)))
    monkeypatch.setattr(deploy_api, "supervisor", lambda: marvel0.sup)
    (tmp_path / "stack").mkdir()
    machine = StackMachine(tmp_path / "stack")
    monkeypatch.setattr(stack_api, "supervisor", machine.stack)
    monkeypatch.setattr(stack_api, "_VIEW", [])
    # The run's supervisor on its own scripted machine; its flock table is an
    # empty file (the real one is under /proc, which this file's guard forbids).
    (tmp_path / "run").mkdir()
    runner = RunMachine(tmp_path / "run", monkeypatch)
    (tmp_path / "locks").write_text("")
    runner.host.locks_path = str(tmp_path / "locks")
    monkeypatch.setattr(run_api, "supervisor", lambda: runner.sup)
    # The hosted AERI terminal D's supervisor, likewise on a scripted machine.
    (tmp_path / "aeri").mkdir()
    aeri_machine = AeriMachine(tmp_path / "aeri", monkeypatch)
    aeri_machine.host.locks_path = str(tmp_path / "locks")
    monkeypatch.setattr(aeri_run_api, "supervisor", lambda: aeri_machine.sup)
    return TestClient(app), marvel0


def aeri_payload(path):
    """The closed bodies of the hosted AERI terminal D's routes."""
    name = "aeri-20261021-ext-r01"
    request = {"request_id": "req-mounted-1"}
    if path.endswith("/challenge"):
        return {"purpose": "start", "name": name}
    if path.endswith("/start"):
        attest = {k: True for k in ("estop", "scene_ok", "area_clear", "arm_home")}
        return {
            **request,
            "name": name,
            "challenge": "x",
            "confirm": "y",
            "attest": attest,
        }
    if path.endswith("/key"):
        seen = {"run_id": "r", "state_seq": 1, "prompt_id": "release:1"}
        return {
            **request,
            "action": "release_round",
            "seen": seen,
            "token": "t",
            "tab_id": "tab-00000001",
        }
    if path.endswith("/end"):
        return {**request, "confirm": "not the phrase"}
    if path.endswith("/presence"):
        return {"tab_id": "tab-00000001", "visible": True}
    return request


def payload(path):
    if path.endswith("/run/challenge"):
        return {"purpose": "enter"}
    if path.endswith("/run/start"):
        return {
            "request_id": "req-mounted-1",
            "form": {},
            "challenge": "x",
            "confirm": "y",
            "attest": {"estop": True, "scene_reset": True, "area_clear": True},
        }
    if path.endswith("/run/key"):
        return {"request_id": "req-mounted-1", "action": "enter", "challenge": "x"}
    if path.endswith("/run/hold"):
        return {"request_id": "req-mounted-1"}
    if path.endswith("/run/end"):
        return {"request_id": "req-mounted-1", "confirm": "not the phrase"}
    if "/aeri/run/" in path:
        return aeri_payload(path)
    if "/stack/" in path:
        return {"request_id": "req-mounted-1", "confirm": "not the phrase"}
    if path.endswith("/aeri/prepare"):
        return {
            "request_id": "req-mounted-1",
            "mode": "external",
            "task": "pick the pepper into the white plate",
            "rounds": 5,
            "steps": 200,
            "start_home": False,
            "confirm": "not the phrase",
        }
    if "/aeri/" in path:
        return {"request_id": "req-mounted-1", "confirm": "not the phrase"}
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
    speech = [
        (route.path, method)
        for route in speech_api.router.routes
        for method in sorted(route.methods - {"HEAD"})
    ]
    assert sorted(speech) == sorted((path, method) for method, path in SPEECH_ROUTES)


def test_the_service_serves_each_route_once():
    # (this FastAPI keeps an included router whole, so ``app.routes`` does not
    # list the paths: the schema does, and warns about a doubled operation id.)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        schema = get_openapi(title="marvel0", version="1", routes=app.routes)["paths"]
    for method, path in ROUTES + SPEECH_ROUTES:
        assert method.lower() in schema[path], (method, path)
    assert sorted(p for p in schema if p.startswith(BASE)) == sorted(
        {path for _, path in ROUTES + SPEECH_ROUTES}
    )
    assert [
        str(w.message) for w in caught if "api_levi_marvel0" in str(w.message)
    ] == []


@pytest.mark.parametrize("method, path", ROUTES)
def test_the_web_ui_token_is_needed_for_every_route(service, method, path):
    client, marvel0 = service
    response = client.request(
        method, path, json=payload(path) if method == "POST" else None
    )
    assert response.status_code == 401
    assert marvel0.calls == [], "nothing ran behind the refusal"


@pytest.mark.parametrize("method, path", ROUTES[1:])
def test_a_route_answers_with_the_token(service, method, path):
    client, _ = service
    # (the status route reads the real machine; test_marvel0_route.py covers it)
    response = client.request(
        method, path, headers=UI, json=payload(path) if method == "POST" else None
    )
    assert response.status_code not in (401, 404, 405), response.text
    if response.status_code == 403:
        # No control file here, so starting is switched off: an answer of its
        # own. Any other 403 would be the person check, which a token passes.
        assert response.json()["detail"]["code"] in (
            "control_disabled",
            "aeri_disabled",
        ), response.text


@pytest.mark.parametrize("method, path", WRITES)
@pytest.mark.parametrize("token", [True, False])
def test_a_bearer_credential_cannot_change_anything(
    service, monkeypatch, method, path, token
):
    client, marvel0 = service
    if not token:
        monkeypatch.delenv("LEVI_UI_TOKEN")
    headers = {"authorization": "Bearer not-an-agent-credential"}
    if token:
        headers.update(UI)
    response = client.request(method, path, headers=headers, json=payload(path))
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "person_only"
    assert marvel0.calls == [], "no command ran"
    assert marvel0.systemd_runs() == []


def test_a_person_with_the_token_reaches_the_deploy_and_the_stop(service):
    client, marvel0 = service
    started = client.post(f"{BASE}/deploy", headers=UI, json=body())
    assert started.status_code == 202, started.text
    assert started.json()["request_id"] == REQ
    assert len(marvel0.systemd_runs()) == 1
    # Sent again with the same id: answered again, started once.
    again = client.post(f"{BASE}/deploy", headers=UI, json=body())
    assert again.status_code == 202 and again.json()["replayed"] is True
    assert len(marvel0.systemd_runs()) == 1
    marvel0.server_comes_up()
    marvel0.sup.invalidate()
    stopped = client.post(f"{BASE}/deploy/stop", headers=UI, json=STOP)
    assert stopped.status_code in (200, 202), stopped.text
    assert len(marvel0.stops()) == 1


def test_a_read_of_the_state_needs_no_confirmation_and_changes_nothing(service):
    client, marvel0 = service
    before = list(marvel0.calls)
    view = client.get(f"{BASE}/deploy", headers=UI)
    assert view.status_code == 200 and view.json()["state"] == "idle"
    assert marvel0.systemd_runs() == [] and marvel0.stops() == []
    assert all(c[0][0] != "systemd-run" for c in marvel0.calls[len(before) :])
