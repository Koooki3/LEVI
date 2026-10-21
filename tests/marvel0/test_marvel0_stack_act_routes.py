"""The arm stack's action routes (``levi/marvel0/stack_act_api.py``): what is
mounted, who may call it, the shape of every body and answer, and that a write
changes the cached view at once. The machine is the fake one of
``stack_act_machine.py``: nothing is started, signalled or connected to."""

import json
import warnings

import pytest
from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.testclient import TestClient
from stack_act_machine import OURS, ActMachine
from test_marvel0_deploy import forbid_real_machine  # noqa: F401 - the guard

# Imported here, not in a test: the service reads the machine at import time.
from levi.agent import legacy, security  # noqa: F401
from levi.marvel0 import api, stack_act_api, stack_api
from levi.service import app as service_app

BASE = "/api/levi/marvel0/stack"
TOKEN = "test-ui-token"
UI = {"x-levi-ui-token": TOKEN}
# (method, path) of everything this module adds; the join in ``api.py`` must
# serve each exactly once (the list the lead adds to ``test_marvel0_routes_mounted``).
ROUTES = [
    *(("POST", f"{BASE}/{k}/start") for k in "mabc"),
    *(("POST", f"{BASE}/{k}/stop") for k in "mabc"),
    ("POST", f"{BASE}/marker"),
    ("POST", f"{BASE}/a/preflight"),
    ("POST", f"{BASE}/b/probe"),
    ("POST", f"{BASE}/c/preflight"),
    ("POST", f"{BASE}/b/gripper"),
    ("POST", f"{BASE}/sequence/start"),
    ("POST", f"{BASE}/sequence/cancel"),
    *(("GET", f"{BASE}/{k}/log") for k in "abc"),
]
WRITES = [r for r in ROUTES if r[0] == "POST"]


@pytest.fixture
def m(tmp_path):
    return ActMachine(tmp_path)


@pytest.fixture
def client(m, monkeypatch):
    monkeypatch.setattr(stack_api, "supervisor", lambda: m.sup)
    monkeypatch.setattr(stack_api, "_VIEW", [])
    monkeypatch.delenv("LEVI_UI_TOKEN", raising=False)
    web = FastAPI()
    web.include_router(stack_api.router)
    web.include_router(stack_act_api.router)
    return TestClient(web)


def body_of(m, path):
    """A well-formed body for a write route."""
    rid = "req-" + path.replace("/", "-")[-24:]
    if path.endswith("/start"):  # (also sequence/start)
        key = path.split("/")[-2]
        need = {
            "a": ["desk_done", "estop_hand"],
            "b": ["hands_clear"],
            "c": ["estop_hand", "hands_clear", "no_client"],
        }
        phrase = {
            "m": "start health monitor",
            "a": "start arm stack",
            "b": "start gripper stack",
            "c": "start robot server",
            "sequence": "start arm stack",
        }[key]
        return {
            "request_id": rid,
            "confirm": phrase,
            "attest": dict.fromkeys(need.get(key, need["a"]), True),
        }
    if path.endswith("/stop"):
        key = path.split("/")[-2]
        phrase = {
            "m": "stop health monitor",
            "a": "stop arm stack",
            "b": "stop gripper stack",
            "c": "stop robot server",
        }[key]
        return {"request_id": rid, "confirm": phrase}
    if path.endswith("/marker"):
        return {"request_id": rid, "op": "touch", "confirm": "set marker"}
    if path.endswith(("preflight", "probe", "cancel")):
        return {"request_id": rid}
    return {
        "request_id": rid,
        "action": "open",
        "confirm": "gripper open",
        "attest": {"hands_clear": True},
    }


def test_the_router_holds_each_route_once():
    pairs = sorted(
        (route.path, method)
        for route in stack_act_api.router.routes
        for method in route.methods - {"HEAD"}
    )
    expected = sorted((path, method) for method, path in ROUTES)
    assert pairs == expected


def test_the_services_join_serves_each_route_once():
    pairs = [
        (route.path, method)
        for route in api.router.routes
        for method in route.methods - {"HEAD"}
        if route.path.startswith(BASE)
        and route.path not in (BASE,)
        and "monitor" not in route.path
        and "marker/clear" not in route.path
    ]
    assert sorted(pairs) == sorted((path, method) for method, path in ROUTES)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        schema = get_openapi(title="m", version="1", routes=service_app.routes)["paths"]
    for method, path in ROUTES:
        assert method.lower() in schema[path], (method, path)
    assert [
        str(w.message) for w in caught if "api_levi_marvel0" in str(w.message)
    ] == []


def test_the_monitor_routes_of_the_stack_are_not_shadowed_by_the_unit_routes():
    paths = {r.path for r in api.router.routes}
    assert f"{BASE}/monitor/start" in paths and f"{BASE}/m/start" in paths
    first = next(r for r in api.router.routes if r.path.endswith("monitor/start"))
    assert first.endpoint.__module__ == "levi.marvel0.stack_api"


@pytest.mark.parametrize("method, path", WRITES)
def test_a_bearer_credential_cannot_write(client, m, method, path):
    answer = client.request(
        method,
        path,
        json=body_of(m, path),
        headers={"Authorization": "Bearer not-an-agent"},
    )
    assert (
        answer.status_code == 403 and answer.json()["detail"]["code"] == "person_only"
    )
    assert not m.calls and not m.oneshots


@pytest.mark.parametrize("method, path", ROUTES)
def test_the_web_ui_token_is_needed_for_every_route(m, monkeypatch, method, path):
    monkeypatch.setenv("LEVI_UI_TOKEN", TOKEN)
    monkeypatch.setattr(stack_api, "supervisor", lambda: m.sup)
    web = TestClient(service_app)
    answer = web.request(
        method, path, json=body_of(m, path) if method == "POST" else None
    )
    assert answer.status_code == 401
    assert not m.calls and not m.oneshots
    ok = web.request(
        method, path, json=body_of(m, path) if method == "POST" else None, headers=UI
    )
    assert ok.status_code not in (401, 403, 404, 405)


@pytest.mark.parametrize("method, path", WRITES)
def test_a_body_with_an_unknown_field_or_a_wrong_type_is_refused(
    client, m, method, path
):
    good = body_of(m, path)
    assert client.post(path, json={**good, "surprise": 1}).status_code == 422
    assert client.post(path, json={**good, "request_id": 12345678}).status_code == 422
    if "attest" in good:
        assert (
            client.post(
                path, json={**good, "attest": {"hands_clear": "true"}}
            ).status_code
            == 422
        )
    assert (
        client.post(
            path, json={k: v for k, v in good.items() if k == "confirm"}
        ).status_code
        == 422
    )
    assert not m.calls and not m.oneshots


def test_the_monitor_start_and_stop_go_through_the_unit_routes_too(client, m):
    answer = client.post(
        f"{BASE}/m/start",
        json={
            "request_id": "req-m-start-1",
            "confirm": "start health monitor",
            "attest": {},
        },
    )
    assert answer.status_code == 202 and answer.json()["done"] == "monitor_started"
    assert len(m.systemd_runs()) == 1
    m.health(hardware=False, controller=False)
    stopped = client.post(
        f"{BASE}/m/stop",
        json={"request_id": "req-m-stop-1", "confirm": "stop health monitor"},
    )
    assert stopped.status_code == 202 and stopped.json()["done"] == "monitor_stopped"
    assert m.stop_calls() == [
        ["systemctl", "--user", "stop", "levi-fr3-health-monitor.service"]
    ]


def test_a_start_is_202_and_changes_the_cached_view_at_once(client, m):
    m.ready_for_a()
    before = client.get(BASE).json()["act"]["terminals"]["a"]
    assert before["state"] == "idle" and before["can_start"] is True
    answer = client.post(f"{BASE}/a/start", json=body_of(m, f"{BASE}/a/start"))
    assert answer.status_code == 202
    assert answer.json() == {"done": "started", "terminal": "a", "unit": OURS["a"]}
    after = client.get(BASE).json()["act"]["terminals"]["a"]
    assert after["state"] == "starting"


def test_every_refusal_has_the_error_shape_and_its_status(client, m):
    answer = client.post(f"{BASE}/a/start", json=body_of(m, f"{BASE}/a/start"))
    detail = answer.json()["detail"]
    assert (
        answer.status_code == 409
        and detail["code"].startswith("gate.")
        and detail["message"]
    )
    answer = client.post(
        f"{BASE}/a/start", json={**body_of(m, f"{BASE}/a/start"), "confirm": "no"}
    )
    assert (
        answer.status_code == 400
        and answer.json()["detail"]["code"] == "confirm_required"
    )
    assert "start arm stack" in answer.json()["detail"]["message"]


def test_a_start_without_the_control_file_is_403_in_plain_english(
    tmp_path, monkeypatch
):
    m = ActMachine(tmp_path, enabled=False)
    monkeypatch.setattr(stack_api, "supervisor", lambda: m.sup)
    monkeypatch.setattr(stack_api, "_VIEW", [])
    monkeypatch.delenv("LEVI_UI_TOKEN", raising=False)
    web = FastAPI()
    web.include_router(stack_api.router)
    web.include_router(stack_act_api.router)
    c = TestClient(web)
    for path in ("a/start", "b/start", "c/start", "sequence/start"):
        answer = c.post(f"{BASE}/{path}", json=body_of(m, f"{BASE}/{path}"))
        assert answer.status_code == 403, path
        assert answer.json()["detail"]["code"] == "control_disabled"
        assert "switched off" in answer.json()["detail"]["message"]
    assert c.get(BASE).json()["act"]["enabled"] is False
    assert c.get(f"{BASE}/a/log").status_code == 200  # the rest of the page still works


def test_a_stop_is_202_and_already_stopped_is_200(client, m):
    m.start_a()
    answer = client.post(f"{BASE}/a/stop", json=body_of(m, f"{BASE}/a/stop"))
    assert answer.status_code == 202 and answer.json()["done"] == "stopping"
    again = client.post(
        f"{BASE}/a/stop",
        json={**body_of(m, f"{BASE}/a/stop"), "request_id": "req-a-stop-again"},
    )
    assert again.status_code == 200 and again.json()["done"] == "already_stopped"


def test_the_marker_and_the_checks_and_the_gripper_answer(client, m):
    m.m_ready()
    touch = client.post(f"{BASE}/marker", json=body_of(m, f"{BASE}/marker"))
    assert touch.status_code == 200 and touch.json() == {"done": "marker_set"}
    pre = client.post(f"{BASE}/a/preflight", json={"request_id": "req-preflight-a1"})
    assert pre.status_code == 202 and pre.json()["kind"] == "preflight_a"


def test_the_b_check_route_asks_the_gripper_stack_alone_and_works_with_a_up(client, m):
    m.start_a()  # A runs: the check before A would be refused, this one is not
    m.fci_probes.clear()
    before = client.get(BASE).json()["act"]
    assert before["checks"]["can_probe_b"] is True
    answer = client.post(f"{BASE}/b/probe", json={"request_id": "req-probe-b-001"})
    assert answer.status_code == 202
    assert answer.json() == {"done": "probe_started", "kind": "probe_b"}
    after = client.get(BASE).json()["act"]
    assert m.fci_probes == [], "no FCI contact"
    assert after["checks"]["b_probe"]["state"] == "absent"
    assert after["operation"]["kind"] == "probe_b"
    # a unit of B exists: refused in the error shape, with its status
    m.start_b()
    refused_again = client.post(
        f"{BASE}/b/probe", json={"request_id": "req-probe-b-002"}
    )
    assert refused_again.status_code == 409
    assert refused_again.json()["detail"]["code"] == "gate.b_unit_absent"
    # (the view is cached for two seconds and a refused request changes nothing,
    # so it is not dropped by the refusal: B was started on the machine directly)
    stack_api._VIEW.clear()
    assert client.get(BASE).json()["act"]["checks"]["can_probe_b"] is False


def test_the_b_check_route_is_403_without_the_control_file(tmp_path, monkeypatch):
    m = ActMachine(tmp_path, enabled=False)
    monkeypatch.setattr(stack_api, "supervisor", lambda: m.sup)
    monkeypatch.setattr(stack_api, "_VIEW", [])
    monkeypatch.delenv("LEVI_UI_TOKEN", raising=False)
    web = FastAPI()
    web.include_router(stack_api.router)
    web.include_router(stack_act_api.router)
    answer = TestClient(web).post(
        f"{BASE}/b/probe", json={"request_id": "req-probe-b-off1"}
    )
    assert answer.status_code == 403
    assert answer.json()["detail"]["code"] == "control_disabled"


def test_the_gripper_route_runs_one_test(client, m):
    m.start_a()
    m.start_b()
    answer = client.post(f"{BASE}/b/gripper", json=body_of(m, f"{BASE}/b/gripper"))
    assert answer.status_code == 202
    assert answer.json() == {"done": "gripper_test_started", "action": "open"}
    last = client.get(BASE).json()["act"]["gripper_test"]["last"]
    assert last["ok"] is True and last["action"] == "open"
    bad = client.post(
        f"{BASE}/b/gripper",
        json={**body_of(m, f"{BASE}/b/gripper"), "action": "squeeze"},
    )
    assert bad.status_code == 422


def test_the_sequence_routes(client, m):
    started = client.post(
        f"{BASE}/sequence/start", json=body_of(m, f"{BASE}/sequence/start")
    )
    assert started.status_code == 202 and started.json() == {"done": "sequence_started"}
    seq = client.get(BASE).json()["act"]["sequence"]
    assert seq["state"] == "running" and seq["step"] == "monitor"
    cancelled = client.post(
        f"{BASE}/sequence/cancel", json={"request_id": "req-seq-cancel1"}
    )
    assert cancelled.status_code == 200 and cancelled.json() == {
        "done": "sequence_cancelled"
    }


def test_the_log_route(client, m):
    m.start_a()
    m.emit("a", "first", "second")
    answer = client.get(f"{BASE}/a/log?lines=1")
    assert answer.status_code == 200
    assert (
        answer.json()["lines"]
        == [
            "first",
            "second",
        ][-1:]
    )
    assert answer.json()["unit"] == OURS["a"]
    for bad in (
        "m",
        "x",
    ):  # (no route: M's log is the journal, not a file of the page's)
        assert client.get(f"{BASE}/{bad}/log").status_code == 404
    empty = client.get(f"{BASE}/c/log").json()
    assert empty["lines"] == []


def test_no_answer_names_a_path(client, m):
    m.start_a()
    m.start_b()
    client.post(f"{BASE}/b/gripper", json=body_of(m, f"{BASE}/b/gripper"))
    texts = [
        json.dumps(client.get(BASE).json()),
        json.dumps(client.get(f"{BASE}/a/log").json()),
        json.dumps(
            client.post(f"{BASE}/c/start", json=body_of(m, f"{BASE}/c/start")).json()
        ),
    ]
    for text in texts:
        assert str(m.tmp) not in text and "/home/" not in text


def test_the_doc_of_every_route_says_what_it_does():
    for route in stack_act_api.router.routes:
        assert (route.endpoint.__doc__ or "").strip(), route.path
