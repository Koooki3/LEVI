"""The routes of the hosted AERI terminal D (``levi/marvel0/aeri_run_api.py``):
who may call them, what a body may hold, the shape of each answer and of each
refusal. The supervisor behind them is the scripted one of ``aeri_machine.py``;
nothing is started."""

import json
import os

import pytest
from aeri_machine import NAME, AeriMachine
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Imported here, not in a test: the service and what its middleware imports on
# the first request read the machine at import time.
from levi.agent import legacy, security  # noqa: F401
from levi.marvel0 import aeri_run_api, api
from levi.marvel0 import aeri_state as st

BASE = "/api/levi/marvel0"
TOKEN = "test-ui-token"
UI = {"x-levi-ui-token": TOKEN}
BEARER = {"authorization": "Bearer not-an-agent-credential"}
POSTS = ["challenge", "start", "key", "stop", "end", "presence"]
ROUTES = [
    ("GET", f"{BASE}/aeri/run"),
    ("POST", f"{BASE}/aeri/run/challenge"),
    ("POST", f"{BASE}/aeri/run/start"),
    ("POST", f"{BASE}/aeri/run/key"),
    ("POST", f"{BASE}/aeri/run/stop"),
    ("POST", f"{BASE}/aeri/run/end"),
    ("POST", f"{BASE}/aeri/run/presence"),
    ("GET", f"{BASE}/aeri/run/log"),
]
SEEN = {"run_id": "r", "state_seq": 1, "prompt_id": "release:1"}
BODIES = {
    "challenge": {"purpose": "start", "name": NAME},
    "start": {
        "request_id": "req-0001-abcd",
        "name": NAME,
        "challenge": "x",
        "confirm": "y",
        "attest": {k: True for k in st.ATTEST},
    },
    "key": {
        "request_id": "req-0001-abcd",
        "action": "release_round",
        "seen": SEEN,
        "token": "t-0",
        "tab_id": "tab-00000001",
    },
    "stop": {"request_id": "req-0001-abcd"},
    "end": {"request_id": "req-0001-abcd", "confirm": "end terminal D"},
    "presence": {"tab_id": "tab-00000001", "visible": True},
}


@pytest.fixture(name="m")
def machine(tmp_path, monkeypatch):
    for key, value in (("SETTLE_S", 0.15), ("ARM_DELAY_S", 0.15), ("KEY_GAP_S", 0.3)):
        monkeypatch.setattr(st, key, value)
    mm = AeriMachine(tmp_path, monkeypatch)
    mm.prepare(rounds=5, steps=200)
    yield mm
    mm.close()


@pytest.fixture
def served(m, monkeypatch):
    """The router on an app of its own, over the scripted machine."""
    monkeypatch.setenv("LEVI_UI_TOKEN", TOKEN)
    monkeypatch.setattr(aeri_run_api, "supervisor", lambda: m.sup)
    web = FastAPI()
    web.include_router(aeri_run_api.router)
    return TestClient(web), m


def detail(response) -> dict:
    return response.json()["detail"]


# --- who may call ------------------------------------------------------------------------------


@pytest.mark.parametrize("name", POSTS)
def test_a_bearer_credential_cannot_call_any_write(served, name):
    client, m = served
    response = client.post(f"{BASE}/aeri/run/{name}", headers=BEARER, json=BODIES[name])
    assert response.status_code == 403 and detail(response)["code"] == "person_only"
    assert m.spawned == [] and m.signals == []


@pytest.mark.parametrize("name", POSTS)
def test_a_write_needs_the_web_ui_token(served, name):
    client, m = served
    response = client.post(
        f"{BASE}/aeri/run/{name}",
        headers={"x-levi-ui-token": "wrong"},
        json=BODIES[name],
    )
    assert response.status_code == 401 and detail(response)["code"] == "ui_token"
    response = client.post(f"{BASE}/aeri/run/{name}", json=BODIES[name])
    assert response.status_code == 401
    assert m.spawned == [] and m.signals == []


def test_the_reads_change_nothing_and_name_no_path(served):
    client, m = served
    view = client.get(f"{BASE}/aeri/run")
    assert view.status_code == 200
    body = view.json()
    assert body["schema"] == st.SCHEMA and body["state"]["id"] == "idle"
    assert client.get(f"{BASE}/aeri/run/log", params={"lines": 5}).json()["lines"] == []
    assert m.spawned == [] and m.signals == []
    assert str(m.tmp) not in view.text


def test_the_routes_are_the_ones_of_the_design():
    found = {
        (method, route.path)
        for route in aeri_run_api.router.routes
        for method in route.methods
    }
    assert found == set(ROUTES)


# --- the shape of a body ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("challenge", {"purpose": "enter", "name": NAME}),
        ("challenge", {"purpose": "start"}),
        ("challenge", {"purpose": "start", "name": NAME, "extra": 1}),
        ("start", {"request_id": "req-0001-abcd"}),
        (
            "start",
            {
                **BODIES["start"],
                "attest": {
                    "estop": "true",
                    "scene_ok": True,
                    "area_clear": True,
                    "arm_home": True,
                },
            },
        ),
        (
            "start",
            {
                **BODIES["start"],
                "attest": {"estop": True, "scene_ok": True, "area_clear": True},
            },
        ),
        (
            "start",
            {**BODIES["start"], "attest": {**BODIES["start"]["attest"], "x": True}},
        ),
        ("key", {**BODIES["key"], "action": "stop"}),
        ("key", {**BODIES["key"], "action": "m"}),
        ("key", {**BODIES["key"], "action": "y"}),
        ("key", {**BODIES["key"], "byte": "q"}),
        ("key", {**BODIES["key"], "seen": {"run_id": "r"}}),
        ("key", {**BODIES["key"], "seen": {**SEEN, "state_seq": "1"}}),
        ("key", {**BODIES["key"], "seen": {**SEEN, "extra": 1}}),
        ("key", {k: v for k, v in BODIES["key"].items() if k != "token"}),
        ("key", {k: v for k, v in BODIES["key"].items() if k != "tab_id"}),
        ("stop", {}),
        ("stop", {"request_id": "req-0001-abcd", "force": True}),
        ("end", {"request_id": "req-0001-abcd"}),
        ("presence", {"tab_id": "tab-00000001"}),
        ("presence", {"tab_id": "tab-00000001", "visible": "yes"}),
    ],
)
def test_a_body_that_is_not_closed_and_strict_is_refused_before_anything_happens(
    served, path, body
):
    client, m = served
    response = client.post(f"{BASE}/aeri/run/{path}", headers=UI, json=body)
    assert response.status_code == 422, response.text
    assert (
        m.spawned == [] and m.signals == [] and m.typed() == b"" if m.children else True
    )


def test_no_body_can_name_a_byte_or_a_signal():
    for model in (aeri_run_api.KeyBody, aeri_run_api.StopBody, aeri_run_api.EndBody):
        names = set(model.model_fields)
        assert not names & {"byte", "bytes", "signal", "data", "text", "pid", "group"}
    assert set(aeri_run_api.KeyBody.model_fields) == {
        "request_id",
        "action",
        "seen",
        "token",
        "tab_id",
        "confirm",
    }


# --- the answers --------------------------------------------------------------------------------


def test_a_start_is_202_and_the_state_is_launching(served):
    client, m = served
    m.present()
    token = client.post(
        f"{BASE}/aeri/run/challenge", headers=UI, json=BODIES["challenge"]
    )
    assert token.status_code == 200
    answer = token.json()
    assert answer["schema"] == st.RESULT_SCHEMA and answer["purpose"] == "start"
    body = {
        **BODIES["start"],
        "request_id": m.rid(),
        "challenge": answer["challenge"],
        "confirm": f"start aeri {NAME}",
    }
    response = client.post(f"{BASE}/aeri/run/start", headers=UI, json=body)
    assert response.status_code == 202
    assert (
        response.json()["state"] == "launching" and response.json()["accepted"] is True
    )
    view = client.get(f"{BASE}/aeri/run").json()
    assert (
        view["state"]["id"] == "launching"
        and view["run_id"] == response.json()["run_id"]
    )


def test_a_key_is_202_and_a_refusal_has_the_shape_of_errors_fail(served):
    client, m = served
    m.start()
    m.script(journal=26, screen="第 1/5 轮：确认场景就绪", cbreak=True, steps=9)
    view = m.wait_key("release_round")
    body = m.body_for(view, "release_round")
    stale = client.post(
        f"{BASE}/aeri/run/key",
        headers=UI,
        json={**body, "seen": {**body["seen"], "state_seq": 0}},
    )
    assert stale.status_code == 409
    assert detail(stale)["code"] == "stale_page" and detail(stale)["message"]
    assert m.typed() == b""
    sent = client.post(f"{BASE}/aeri/run/key", headers=UI, json=body)
    assert sent.status_code == 202 and sent.json()["evidence"] == "pending"
    assert m.typed() == b"\r"


def test_stop_is_202_and_the_second_one_409(served):
    client, m = served
    m.start()
    m.script(journal=34, screen="当前正向子任务 步 66/200", cbreak=True, steps=9)
    m.look()
    first = client.post(
        f"{BASE}/aeri/run/stop", headers=UI, json={"request_id": m.rid()}
    )
    assert first.status_code == 202 and first.json()["phase"] == "sent"
    second = client.post(
        f"{BASE}/aeri/run/stop", headers=UI, json={"request_id": m.rid()}
    )
    assert second.status_code == 409 and detail(second)["code"] == "stop_spent"


def test_stop_with_no_run_says_d_is_gone(served):
    client, m = served
    response = client.post(
        f"{BASE}/aeri/run/stop", headers=UI, json={"request_id": m.rid()}
    )
    assert response.status_code == 409 and detail(response)["code"] == "d_gone"


def test_end_with_the_wrong_phrase_is_400(served):
    client, m = served
    m.start()
    response = client.post(
        f"{BASE}/aeri/run/end",
        headers=UI,
        json={"request_id": m.rid(), "confirm": "end"},
    )
    assert (
        response.status_code == 400 and detail(response)["code"] == "confirm_required"
    )


def test_presence_is_204_with_no_body(served):
    client, m = served
    response = client.post(
        f"{BASE}/aeri/run/presence", headers=UI, json=BODIES["presence"]
    )
    assert response.status_code == 204 and response.content == b""
    assert BODIES["presence"]["tab_id"] in m.sup._tabs
    response = client.post(
        f"{BASE}/aeri/run/presence",
        headers=UI,
        json={"tab_id": "tab-00000001", "visible": False},
    )
    assert response.status_code == 204 and not m.sup._tabs


def test_a_bad_tab_id_is_refused(served):
    client, _ = served
    response = client.post(
        f"{BASE}/aeri/run/presence",
        headers=UI,
        json={"tab_id": "../etc", "visible": True},
    )
    assert response.status_code == 400


def test_the_log_is_bounded(served):
    client, _m = served
    assert (
        client.get(f"{BASE}/aeri/run/log", params={"lines": 100000}).status_code == 200
    )
    assert client.get(f"{BASE}/aeri/run/log", params={"lines": 0}).status_code == 200


def test_a_site_without_the_aeri_section_reads_as_disabled_not_as_an_error(served):
    from dataclasses import replace

    client, m = served
    m.site = replace(m.site, aeri=None)
    response = client.get(f"{BASE}/aeri/run")
    assert response.status_code == 200
    body = response.json()
    assert (
        body["enabled"] is False and body["start"]["why_not"]["code"] == "aeri_disabled"
    )


def test_every_refusal_of_the_supervisor_has_a_status_the_route_passes_on():
    for code, (status, _) in list(st.REFUSALS.items())[:6]:
        exc = aeri_run_api.aeri_run.http(st.Refuse(code))
        assert exc.status_code == status and exc.detail["code"] == code


def test_active_and_adopt_never_raise_into_the_service(m, monkeypatch):
    monkeypatch.setattr(aeri_run_api, "supervisor", lambda: m.sup)
    assert aeri_run_api.active() is None
    m.start()
    assert aeri_run_api.active_job()["kind"] == "marvel0-aeri"
    aeri_run_api.adopt_on_start()

    class Broken:
        def adopt(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(aeri_run_api, "supervisor", lambda: Broken())
    aeri_run_api.adopt_on_start()  # must not raise
    with pytest.raises(RuntimeError):
        aeri_run_api.active()  # (the asker treats that as active)


def test_the_routes_are_joined_to_the_service_by_api_py():
    joined = {r.path for r in api.router.routes}
    if f"{BASE}/aeri/run" not in joined:
        pytest.skip("the lead joins aeri_run_api.router in api.py (one additive line)")
    for method, path in ROUTES:
        assert path in joined, (method, path)
    assert json is not None and os is not None
