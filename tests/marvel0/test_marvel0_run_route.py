"""The routes of the hosted run (``levi/marvel0/run_api.py``): who may call
them, what a body may hold, the shape of each answer and of each refusal, and
how ``levi/marvel0/api.py`` joins them to the service. The supervisor behind
them is the scripted one of ``test_marvel0_run.py``; nothing is started."""

import os
import subprocess
import sys
import warnings

import pytest
from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.testclient import TestClient
from test_marvel0_run import (
    ATTEST,
    GROUP,
    PROMPT,
    SP,
    make_machine,  # noqa: F401 - the fixture
)
from test_marvel0_run_supervisor import second_service

# Imported here, not in a test: the service and what its middleware imports on
# the first request read the machine at import time.
from levi.agent import legacy, security  # noqa: F401
from levi.marvel0 import api, control_site, run, run_api, stack_act
from levi.service import app as service_app

BASE = "/api/levi/marvel0"
TOKEN = "test-ui-token"
UI = {"x-levi-ui-token": TOKEN}
BEARER = {"authorization": "Bearer not-an-agent-credential"}
POSTS = ["challenge", "start", "key", "hold", "end"]
ROUTES = [
    ("GET", f"{BASE}/run"),
    ("POST", f"{BASE}/run/challenge"),
    ("POST", f"{BASE}/run/start"),
    ("POST", f"{BASE}/run/key"),
    ("POST", f"{BASE}/run/hold"),
    ("POST", f"{BASE}/run/end"),
    ("GET", f"{BASE}/run/log"),
]


@pytest.fixture
def served(machine, monkeypatch):
    """The run router on an app of its own, over the scripted machine."""
    monkeypatch.setenv("LEVI_UI_TOKEN", TOKEN)
    monkeypatch.setattr(run_api, "supervisor", lambda: machine.sup)
    web = FastAPI()
    web.include_router(run_api.router)
    return TestClient(web), machine


def detail(response) -> dict:
    return response.json()["detail"]


def start_body(m, **kw):
    form = m.form()
    token = m.sup.challenge({"purpose": "start", "form": form})
    return {
        "request_id": m.rid(),
        "form": form,
        "challenge": token["challenge"],
        "confirm": token["confirm"],
        "attest": dict(ATTEST),
        **kw,
    }


# --- who may call ----------------------------------------------------------------------------


@pytest.mark.parametrize("name", POSTS)
def test_a_bearer_credential_cannot_call_any_write(served, name):
    client, m = served
    bodies = {
        "challenge": {"purpose": "enter"},
        "start": {
            "request_id": "req-0001-abcd",
            "form": {},
            "challenge": "x",
            "confirm": "y",
            "attest": ATTEST,
        },
        "key": {"request_id": "req-0001-abcd", "action": "enter", "challenge": "x"},
        "hold": {"request_id": "req-0001-abcd"},
        "end": {"request_id": "req-0001-abcd", "confirm": "end client run"},
    }
    response = client.post(f"{BASE}/run/{name}", headers=BEARER, json=bodies[name])
    assert response.status_code == 403 and detail(response)["code"] == "person_only"
    assert m.spawned == [] and m.signals == []


@pytest.mark.parametrize("name", POSTS)
def test_a_write_needs_the_web_ui_token(served, name):
    client, _ = served
    response = client.post(
        f"{BASE}/run/{name}",
        headers={"x-levi-ui-token": "wrong"},
        json={"request_id": "req-0001-abcd"},
    )
    assert response.status_code == 401 and detail(response)["code"] == "ui_token"


def test_the_reads_change_nothing_and_name_no_path(served):
    client, m = served
    view = client.get(f"{BASE}/run")
    assert view.status_code == 200
    body = view.json()
    assert body["schema"] == "levi.marvel0.run.v1" and body["state"] == "idle"
    assert client.get(f"{BASE}/run/log", params={"lines": 5}).json()["lines"] == []
    assert m.spawned == [] and m.signals == []
    assert str(m.tmp) not in view.text


# --- the shape of a body -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "path, body",
    [
        ("challenge", {"purpose": "enter", "extra": 1}),
        ("challenge", {"purpose": "s"}),
        ("challenge", {}),
        ("start", {"request_id": "req-0001-abcd"}),
        (
            "start",
            {
                "request_id": "req-0001-abcd",
                "form": {},
                "challenge": "x",
                "confirm": "y",
                "attest": {"estop": "true", "scene_reset": True, "area_clear": True},
            },
        ),
        (
            "start",
            {
                "request_id": "req-0001-abcd",
                "form": {},
                "challenge": "x",
                "confirm": "y",
                "attest": {"estop": True, "scene_reset": True},
            },
        ),
        ("key", {"request_id": "req-0001-abcd", "action": "s", "challenge": "x"}),
        ("key", {"request_id": "req-0001-abcd", "action": "p", "challenge": "x"}),
        (
            "key",
            {
                "request_id": "req-0001-abcd",
                "action": "enter",
                "challenge": "x",
                "byte": "s",
            },
        ),
        ("key", {"request_id": "req-0001-abcd", "action": "enter"}),
        ("hold", {"request_id": "req-0001-abcd", "confirm": "x"}),
        ("hold", {}),
        ("end", {"request_id": "req-0001-abcd"}),
        ("end", {"request_id": 5, "confirm": "end client run"}),
    ],
)
def test_a_malformed_body_is_a_422_and_reaches_nothing(served, path, body):
    client, m = served
    response = client.post(f"{BASE}/run/{path}", headers=UI, json=body)
    assert response.status_code == 422
    assert (
        m.spawned == [] and m.signals == [] and m.typed() == b"" if m.children else True
    )


# --- a run through the routes --------------------------------------------------------------------


def test_a_run_through_the_routes(served):
    client, m = served
    form = m.form()
    asked = client.post(
        f"{BASE}/run/challenge", headers=UI, json={"purpose": "start", "form": form}
    )
    assert asked.status_code == 200
    token = asked.json()
    assert token["group"] == GROUP and token["confirm"] == f"start client {GROUP}"
    assert {r["level"] for r in token["preflight"]} <= {"ok", "warn"}
    body = {
        "request_id": "req-route-0001",
        "form": form,
        "challenge": token["challenge"],
        "confirm": token["confirm"],
        "attest": dict(ATTEST),
    }
    started = client.post(f"{BASE}/run/start", headers=UI, json=body)
    assert started.status_code == 202 and started.json()["state"] == "starting"
    again = client.post(f"{BASE}/run/start", headers=UI, json=body)
    assert again.status_code == 202 and again.json()["replayed"] is True
    assert len(m.spawned) == 1
    m.gate()
    state = client.get(f"{BASE}/run", headers=UI).json()
    assert state["state"] == "at_gate" and state["actions"]["enter"]["allowed"]
    token = client.post(
        f"{BASE}/run/challenge", headers=UI, json={"purpose": "enter"}
    ).json()
    sent = client.post(
        f"{BASE}/run/key",
        headers=UI,
        json={
            "request_id": "req-route-0002",
            "action": "enter",
            "challenge": token["challenge"],
        },
    )
    assert sent.status_code == 202 and sent.json()["sent"] == "enter"
    assert m.typed() == b"\r"
    held = client.post(
        f"{BASE}/run/hold", headers=UI, json={"request_id": "req-route-0003"}
    )
    assert held.status_code == 200 and held.json()["held"] is True
    ended = client.post(
        f"{BASE}/run/end",
        headers=UI,
        json={"request_id": "req-route-0004", "confirm": "end client run"},
    )
    assert ended.status_code == 202 and ended.json()["ended"] is True
    assert client.get(f"{BASE}/run").json()["state"] == "done"


def refused_with(client, path, body, status, code, headers=UI):
    response = client.post(f"{BASE}/run/{path}", headers=headers, json=body)
    assert response.status_code == status, response.text
    assert detail(response)["code"] == code, response.text
    assert detail(response)["message"]
    return detail(response)


def test_the_refusals_have_their_status_and_stable_code(served):
    client, m = served
    form = m.form()
    token = client.post(
        f"{BASE}/run/challenge", headers=UI, json={"purpose": "start", "form": form}
    ).json()
    good = {
        "request_id": "req-refuse-0001",
        "form": form,
        "challenge": token["challenge"],
        "confirm": token["confirm"],
        "attest": dict(ATTEST),
    }
    refused_with(
        client, "start", {**good, "confirm": "start client"}, 400, "confirm_required"
    )
    refused_with(
        client,
        "start",
        {**good, "attest": {**ATTEST, "estop": False}},
        400,
        "attest_required",
    )
    refused_with(
        client, "start", {**good, "request_id": "short"}, 400, "request_id_invalid"
    )
    refused_with(
        client, "start", {**good, "challenge": "nope"}, 409, "challenge_invalid"
    )
    refused_with(
        client,
        "key",
        {"request_id": "req-refuse-0002", "action": "enter", "challenge": "x"},
        409,
        "state_not_allowed",
    )
    refused_with(
        client, "hold", {"request_id": "req-refuse-0003"}, 409, "state_not_allowed"
    )
    refused_with(
        client,
        "end",
        {"request_id": "req-refuse-0004", "confirm": "end client run"},
        409,
        "no_client",
    )
    m.deploy = m.ready_view(established=2)
    info = refused_with(
        client, "challenge", {"purpose": "start", "form": form}, 409, "preflight_failed"
    )
    assert info["first"] == "no_connection" and info["checks"]
    m.deploy = m.ready_view()
    low = m.form(
        model_id=SP, prompt="stack the plates of same color together", min_z=0.03
    )
    m.deploy = m.ready_view(model=SP)
    refused_with(
        client,
        "challenge",
        {"purpose": "start", "form": low},
        422,
        "min_z_below_task_floor",
    )
    refused_with(
        client,
        "challenge",
        {"purpose": "start", "form": m.form(model_id="nope")},
        422,
        "model_unknown",
    )
    m.enabled = False
    refused_with(
        client, "challenge", {"purpose": "start", "form": form}, 403, "run_disabled"
    )
    m.enabled = True
    assert m.sup._guard.acquire(blocking=False)
    try:
        refused_with(
            client, "start", {**good, "request_id": "req-refuse-0005"}, 423, "busy"
        )
    finally:
        m.sup._guard.release()
    assert m.spawned == []


def test_a_request_id_cannot_be_reused_for_another_request(served):
    client, m = served
    body = start_body(m)
    assert client.post(f"{BASE}/run/start", headers=UI, json=body).status_code == 202
    other = {**body, "form": {**body["form"], "max_steps": 20}}
    refused_with(client, "start", other, 409, "request_id_used")


def test_the_missing_site_is_a_412_on_the_read_too(served, monkeypatch):
    client, m = served

    def missing():
        raise run.fail(412, "site_missing", "no site file")

    monkeypatch.setattr(m.sup, "site_loader", missing)
    response = client.get(f"{BASE}/run")
    assert response.status_code == 412 and detail(response)["code"] == "site_missing"


def test_the_log_route_clamps_its_page(served):
    client, m = served
    m.start()
    m.say(*[f"line {n}" for n in range(300)])
    assert len(client.get(f"{BASE}/run/log", params={"lines": 5}).json()["lines"]) == 5
    assert (
        len(client.get(f"{BASE}/run/log", params={"lines": 9999}).json()["lines"])
        == 200
    )
    assert len(client.get(f"{BASE}/run/log", params={"lines": 0}).json()["lines"]) == 1


# --- the control file --------------------------------------------------------------------------


def test_the_control_file_decides_whether_hosted_runs_are_on(tmp_path, monkeypatch):
    path = tmp_path / "control.toml"
    monkeypatch.setenv("LEVI_MARVEL0_CONTROL", str(path))
    control_site.clear_cache()
    assert run._control_enabled() is False, "no file: off"
    path.write_text(control_site.TEMPLATE)
    path.chmod(0o600)
    control_site.clear_cache()
    assert run._control_enabled() is False, "the template ships switched off"
    path.write_text(
        control_site.TEMPLATE.replace(
            "[client]\nenabled            = false",
            "[client]\nenabled            = true",
        )
    )
    path.chmod(0o600)
    control_site.clear_cache()
    assert run._control_enabled() is True
    path.write_text("schema = 'levi.marvel0.control.v1'\n[client]\nenabled = 'yes'\n")
    path.chmod(0o600)
    control_site.clear_cache()
    assert run._control_enabled() is False, "a file that does not parse is off"
    control_site.clear_cache()


# --- the arm stack asks whether a client is on -------------------------------------------------


@pytest.fixture
def probe(machine, monkeypatch):
    """The stack's run probe as ``run_api`` registers it, over the scripted
    machine (the list is the module's own; monkeypatch puts it back)."""
    monkeypatch.setattr(stack_act, "_RUN_PROBES", [])
    monkeypatch.setattr(run_api, "supervisor", lambda: machine.sup)
    run_api.register()
    return machine


def test_the_arm_stack_is_told_whether_a_client_is_on(probe):
    m = probe
    assert stack_act.hosted_run_active() is False
    m.start()
    assert stack_act.hosted_run_active() is True
    m.end()
    assert stack_act.hosted_run_active() is False


def test_a_client_a_restart_left_counts_before_anyone_opens_the_run_card(probe):
    m = probe
    m.start()
    after = second_service(m)
    assert after._adopted is False
    m.sup = after
    assert stack_act.hosted_run_active() is True, "adopted by the probe itself"
    assert after._adopted is True
    for fd in (after._arm_fd,):
        if fd is not None:
            os.close(fd)


def test_a_run_state_that_cannot_be_read_blocks_the_stack(probe, monkeypatch):
    m = probe

    def broken():
        raise OSError("no record")

    monkeypatch.setattr(m.sup, "active", broken)
    assert stack_act.hosted_run_active() is None, "None is treated as active"


def test_the_service_registers_the_probe_when_it_starts():
    # A fresh interpreter: the other tests replace the probe in this one.
    code = (
        "from levi.marvel0 import api, run_api, stack_act;"
        "print(stack_act._RUN_PROBES == [run_api.run_active])"
    )
    answer = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert answer.stdout.strip() == "True", answer.stderr[-500:]


# --- the join to the service ---------------------------------------------------------------------


def test_the_router_of_the_service_holds_each_run_route_once():
    pairs = [
        (route.path, method)
        for route in api.router.routes
        for method in sorted(route.methods - {"HEAD"})
        if route.path.startswith(f"{BASE}/run")
    ]
    assert sorted(pairs) == sorted((path, method) for method, path in ROUTES)


def test_the_service_serves_each_run_route_once():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        schema = get_openapi(title="marvel0", version="1", routes=service_app.routes)[
            "paths"
        ]
    for method, path in ROUTES:
        assert method.lower() in schema[path], (method, path)
    assert sorted(p for p in schema if p.startswith(f"{BASE}/run")) == sorted(
        {path for _, path in ROUTES}
    )
    assert [
        str(w.message)
        for w in caught
        if "run" in str(w.message) and "marvel0" in str(w.message)
    ] == []


@pytest.fixture
def service(machine, monkeypatch):
    monkeypatch.setenv("LEVI_UI_TOKEN", TOKEN)
    monkeypatch.setattr(run_api, "supervisor", lambda: machine.sup)
    return TestClient(service_app), machine


@pytest.mark.parametrize("method, path", ROUTES)
def test_the_service_needs_its_token_for_every_run_route(service, method, path):
    client, m = service
    response = client.request(
        method, path, json={"purpose": "enter"} if method == "POST" else None
    )
    assert response.status_code == 401
    assert m.spawned == [] and m.signals == []


@pytest.mark.parametrize("name", POSTS)
def test_the_service_refuses_a_bearer_credential_on_every_write(service, name):
    client, _ = service
    response = client.post(
        f"{BASE}/run/{name}",
        headers={**UI, **BEARER},
        json={
            "request_id": "req-0001-abcd",
            "purpose": "enter",
            "action": "enter",
            "challenge": "x",
            "confirm": "y",
            "form": {},
            "attest": ATTEST,
        },
    )
    assert response.status_code == 403 and detail(response)["code"] == "person_only"


def test_the_service_serves_the_read_with_the_token(service):
    client, _ = service
    response = client.get(f"{BASE}/run", headers=UI)
    assert response.status_code == 200 and response.json()["profile"] == "real"
    assert PROMPT not in response.text
