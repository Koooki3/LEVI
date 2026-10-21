"""The routes of the AERI preparation card (``levi/marvel0/aeri_api.py``) on an
app of their own, over the made-up machine of ``marvel0_aeri_world.py``: who may call
them, what each refuses and why, that a request is answered again and never
acted on twice, that two writes do not run at once, and that the stops touch
only what they should (the one place a test must not reach the real
``systemctl``: its calls are recorded and answered here)."""

import dataclasses
import fcntl
import json
import os
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from marvel0_aeri_world import DEFAULT_PROFILES, World, installed_profiles

from levi.marvel0 import aeri_api, status
from levi.marvel0 import site as marvel0_site

BASE = "/api/levi/marvel0/aeri"
STOP_LIVE = {"request_id": "req-live-stop-1", "confirm": "stop live service"}
STOP_LEFT = {"request_id": "req-leftovers-1", "confirm": "stop leftover aeri units"}
VLM = "levi-aeri-vlm-8100.service"
POLICY = "levi-aeri-policy-forward.service"
MANUAL = "levi-aeri-policy-manual-eggplant.scope"


@pytest.fixture
def machine(tmp_path, monkeypatch):
    installed_profiles(monkeypatch, DEFAULT_PROFILES)
    monkeypatch.delenv("LEVI_UI_TOKEN", raising=False)
    world = World(tmp_path)
    world.set_live("stopped")
    preparer = world.preparer()
    world.prep_host = preparer.readiness.host.prep
    monkeypatch.setattr(aeri_api, "preparer", lambda: preparer)
    app = FastAPI()
    app.include_router(aeri_api.router)
    world.client = TestClient(app)
    world.api = preparer
    return world


def post(world, path, body, **kw):
    return world.client.post(f"{BASE}{path}", json=body, **kw)


def prepare_body(world, **change):
    return world.prepare_body(**change)


def runs(world):
    return sorted(p.name for p in world.runs.iterdir())


# --- the document ------------------------------------------------------------------------


def test_the_document_is_served(machine):
    r = machine.client.get(BASE)
    assert r.status_code == 200
    doc = r.json()
    assert doc["schema"] == "levi.marvel0.aeri.v1" and doc["enabled"] is True


def test_the_document_is_served_without_a_site_section(machine):
    machine.site = marvel0_site.parse(
        {k: v for k, v in machine.site_dict().items() if k != "aeri"}, "env"
    )
    doc = machine.client.get(BASE).json()
    assert doc["site"]["state"] == "unconfigured" and doc["readiness"] == []


# --- who may write -----------------------------------------------------------------------

WRITES = [
    ("/prepare", lambda w: prepare_body(w)),
    ("/live/stop", lambda w: STOP_LIVE),
    ("/leftovers/stop", lambda w: STOP_LEFT),
]


@pytest.mark.parametrize("path, body", WRITES)
def test_a_bearer_credential_cannot_change_anything(machine, path, body):
    machine.set_live("running")
    machine.facts["units"] = status.parse_units(f"{VLM} loaded active running x\n")
    r = post(
        machine, path, body(machine), headers={"authorization": "Bearer an-agent-key"}
    )
    assert r.status_code == 403 and r.json()["detail"]["code"] == "person_only"
    assert runs(machine) == [] and machine.systemctl.calls == []
    assert machine.live_stops() == []


@pytest.mark.parametrize("path, body", WRITES)
def test_the_web_ui_token_is_needed_when_the_service_has_one(
    machine, monkeypatch, path, body
):
    monkeypatch.setenv("LEVI_UI_TOKEN", "t0ken")
    r = post(machine, path, body(machine))
    assert r.status_code == 401
    assert runs(machine) == []


def test_the_get_needs_no_credential_of_a_person(machine):
    r = machine.client.get(BASE, headers={"authorization": "Bearer an-agent-key"})
    assert r.status_code == 200


@pytest.mark.parametrize("path, body", WRITES)
def test_a_body_with_an_extra_field_or_a_wrong_type_is_a_422(machine, path, body):
    good = body(machine)
    assert post(machine, path, {**good, "extra": 1}).status_code == 422
    assert post(machine, path, {**good, "request_id": 5}).status_code == 422
    assert (
        post(
            machine, path, {k: v for k, v in good.items() if k != "confirm"}
        ).status_code
        == 422
    )
    assert runs(machine) == []


@pytest.mark.parametrize(
    "change",
    [
        {"rounds": "5"},
        {"rounds": 5.0},
        {"rounds": True},
        {"steps": "200"},
        {"start_home": "true"},
        {"start_home": 1},
        {"mode": "agent"},
        {"task": 5},
        {"gripper_confirm_s": "4"},
        {"gripper_confirm_s": True},
    ],
)
def test_the_prepare_body_is_strict(machine, change):
    r = post(machine, "/prepare", prepare_body(machine, **change))
    assert r.status_code == 422
    assert runs(machine) == []


def test_the_gripper_time_may_be_left_out_or_null_or_a_whole_number(machine):
    body = prepare_body(machine)
    body.pop("gripper_confirm_s")
    assert post(machine, "/prepare", body).status_code == 200
    assert (
        post(
            machine,
            "/prepare",
            prepare_body(
                machine, request_id="req-prepare-0002", gripper_confirm_s=None
            ),
        ).status_code
        == 200
    )
    r = post(
        machine,
        "/prepare",
        prepare_body(machine, request_id="req-prepare-0003", gripper_confirm_s=4),
    )
    assert r.status_code == 200 and r.json()["prepared"]["gripper_confirm_s"] == 4.0


# --- the gates every write passes ----------------------------------------------------------


@pytest.mark.parametrize("path, body", WRITES)
def test_the_phrase_must_be_typed_exactly(machine, path, body):
    machine.set_live("running")
    for wrong in ("", "Prepare aeri run", "prepare aeri run ", "go"):
        r = post(machine, path, {**body(machine), "confirm": wrong})
        assert r.status_code == 400 and r.json()["detail"]["code"] == "confirm_required"
    assert runs(machine) == [] and machine.live_stops() == []


@pytest.mark.parametrize("path, body", WRITES)
@pytest.mark.parametrize(
    "bad", ["", "short", "has space in it", "x" * 81, "a/b/../c1234"]
)
def test_the_request_id_is_checked(machine, path, body, bad):
    r = post(machine, path, {**body(machine), "request_id": bad})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "request_id_invalid"


@pytest.mark.parametrize("path, body", WRITES)
def test_a_switched_off_card_refuses_every_write(machine, path, body):
    machine.set_live("running")
    machine.facts["control"]["enabled"] = False
    r = post(machine, path, body(machine))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "aeri_disabled"
    machine.facts["control"]["enabled"] = None  # no control file at all
    assert (
        post(machine, path, body(machine)).json()["detail"]["code"] == "aeri_disabled"
    )
    assert runs(machine) == [] and machine.live_stops() == []


@pytest.mark.parametrize("path, body", WRITES)
@pytest.mark.parametrize("product", [False, None])
def test_only_the_product_may_write(machine, path, body, product):
    machine.set_live("running")
    machine.facts["product"] = product
    r = post(machine, path, body(machine))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_product"
    assert runs(machine) == [] and machine.live_stops() == []


@pytest.mark.parametrize("path, body", WRITES)
def test_a_missing_or_malformed_section_refuses_every_write(machine, path, body):
    machine.site = marvel0_site.parse(machine.site_dict(pin_sha="zz"), "env")
    r = post(machine, path, body(machine))
    assert r.status_code == 412 and r.json()["detail"]["code"] == "aeri_unconfigured"
    assert {"field": "aeri.pin_sha", "code": "range"} in [
        {"field": f, "code": c} for f, c in machine.site.aeri_errors
    ]


# --- prepare -------------------------------------------------------------------------------


def test_a_good_prepare_answers_and_shows_up_in_the_document(machine):
    r = post(machine, "/prepare", prepare_body(machine))
    assert r.status_code == 200, r.text
    body = r.json()
    assert (
        body["schema"] == "levi.marvel0.aeri_prepare.v1" and body["replayed"] is False
    )
    assert body["name"] == "aeri-20261021-ext-r01" == body["prepared"]["name"]
    assert runs(machine) == ["aeri-20261021-ext-r01"]
    doc = machine.client.get(BASE).json()
    assert doc["prepared"]["name"] == body["name"] and doc["prepared"]["intact"] is True


def test_the_card_forgets_its_cache_when_a_run_is_prepared(machine):
    assert machine.client.get(BASE).json()["prepared"] is None
    post(machine, "/prepare", prepare_body(machine))
    assert (
        machine.client.get(BASE).json()["prepared"]["name"] == "aeri-20261021-ext-r01"
    )


def test_a_request_is_answered_again_and_never_acted_on_twice(machine):
    first = post(machine, "/prepare", prepare_body(machine)).json()
    again = post(machine, "/prepare", prepare_body(machine))
    assert again.status_code == 200
    assert again.json() == {**first, "replayed": True}
    assert runs(machine) == ["aeri-20261021-ext-r01"]


def test_the_answer_survives_a_restart(machine):
    first = post(machine, "/prepare", prepare_body(machine)).json()
    fresh = machine.preparer(prep=machine.prep_host)  # a new process, the same disk
    again = fresh.prepare(prepare_body(machine))
    assert again == {**first, "replayed": True}
    assert runs(machine) == ["aeri-20261021-ext-r01"]


def test_the_same_id_for_another_request_is_refused(machine):
    post(machine, "/prepare", prepare_body(machine))
    r = post(machine, "/prepare", prepare_body(machine, rounds=6))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "request_id_used"
    assert runs(machine) == ["aeri-20261021-ext-r01"]


def test_the_phrase_and_the_id_are_not_part_of_what_makes_two_requests_the_same(
    machine,
):
    a = prepare_body(machine)
    b = {**a, "confirm": "prepare aeri run"}
    assert post(machine, "/prepare", a).status_code == 200
    assert post(machine, "/prepare", b).json()["replayed"] is True


def test_a_refused_request_can_be_sent_again_after_the_cause_is_gone(machine):
    machine.commit_change()
    r = post(machine, "/prepare", prepare_body(machine))
    assert r.status_code == 412 and r.json()["detail"]["code"] == "code_pin_mismatch"
    machine.reparse_site(pin_sha=machine.head()[:8])
    r = post(machine, "/prepare", prepare_body(machine))
    assert r.status_code == 200 and r.json()["replayed"] is False


@pytest.mark.parametrize(
    "mutate, status_code, code",
    [
        (lambda m: m.commit_change(), 412, "code_pin_mismatch"),
        (lambda m: m.dirty("modify"), 412, "code_pin_mismatch"),
        (
            lambda m: (m.templates / "job.base.yaml").write_text("x"),
            412,
            "templates_changed",
        ),
        (lambda m: m.set_check(mode="fail"), 422, "check_failed"),
    ],
)
def test_the_gates_of_the_preparation_answer_with_their_codes(
    machine, mutate, status_code, code
):
    mutate(machine)
    r = post(machine, "/prepare", prepare_body(machine))
    assert r.status_code == status_code and r.json()["detail"]["code"] == code
    assert runs(machine) == []


def test_a_form_that_is_not_valid_is_a_422_with_the_field(machine):
    r = post(machine, "/prepare", prepare_body(machine, task="Pick The Pepper"))
    assert r.status_code == 422
    assert r.json()["detail"] == {
        "code": "form_invalid",
        "message": "The form is not valid",
        "field": "task",
        "reason": "pattern",
    }


def test_start_home_needs_a_profile_that_has_it(machine):
    machine.reparse_site(profile="bfdee2cf")
    r = post(machine, "/prepare", prepare_body(machine))
    assert (
        r.status_code == 422 and r.json()["detail"]["code"] == "start_home_unsupported"
    )
    assert (
        post(
            machine,
            "/prepare",
            prepare_body(machine, request_id="req-prepare-0002", start_home=False),
        ).status_code
        == 200
    )


def test_two_prepares_at_once_are_one_answer_and_one_busy(machine):
    machine.set_check(mode="sleep", seconds=1.5)
    answers = []

    def go(n):
        answers.append(
            post(
                machine,
                "/prepare",
                prepare_body(machine, request_id=f"req-prepare-0{n}0"),
            )
        )

    threads = [threading.Thread(target=go, args=(n,)) for n in (1, 2)]
    for t in threads:
        t.start()
        time.sleep(0.3)
    for t in threads:
        t.join(30)
    codes = sorted(a.status_code for a in answers)
    assert codes == [200, 423], [a.text for a in answers]
    busy = next(a for a in answers if a.status_code == 423)
    assert busy.json()["detail"]["code"] == "busy"
    assert len(runs(machine)) == 1


def test_preparing_does_not_take_the_run_lock(machine):
    """A running client's keys must not be refused for the minute a check takes."""
    lock = machine.hub / ".marvel0-run.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert post(machine, "/prepare", prepare_body(machine)).status_code == 200
    finally:
        os.close(fd)


def test_the_id_of_a_refused_request_is_free_again(machine):
    """Nothing was done, so the same id may carry another request (not
    ``request_id_used``)."""
    machine.commit_change()
    first = post(machine, "/prepare", prepare_body(machine))
    assert first.status_code == 412
    machine.reparse_site(pin_sha=machine.head()[:8])
    second = post(machine, "/prepare", prepare_body(machine, rounds=6))
    assert second.status_code == 200 and second.json()["prepared"]["rounds"] == 6


def test_the_history_holds_the_event_and_no_path(machine):
    post(machine, "/prepare", prepare_body(machine))
    post(
        machine,
        "/prepare",
        {**prepare_body(machine, request_id="req-prepare-0009"), "confirm": "nope"},
    )
    machine.commit_change()
    post(machine, "/prepare", prepare_body(machine, request_id="req-prepare-0010"))
    history = machine.state / "marvel0" / "aeri_prep" / "history.jsonl"
    lines = [json.loads(line) for line in history.read_text().splitlines()]
    assert [(line["event"], line.get("code")) for line in lines] == [
        ("prepare", None),
        ("prepare_refused", "code_pin_mismatch"),
    ]
    assert all(line["actor"] == "person" and line["via"] == "product" for line in lines)
    assert str(machine.root) not in history.read_text()


# --- stopping the live service ---------------------------------------------------------------


def test_the_live_service_is_stopped_with_the_command_and_the_state_read_again(machine):
    machine.set_live("running")
    r = post(machine, "/live/stop", STOP_LIVE)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (
        body["schema"] == "levi.marvel0.aeri_action.v1"
        and body["action"] == "live_stop"
    )
    assert body["already"] is False and body["leftovers"] is None
    assert body["results"] == [{"name": "levi-live", "ok": True, "detail": None}]
    assert body["live_service"]["state"] == "stopped"
    assert machine.seen_live_stop()["argv"] == [
        "systemctl",
        "--user",
        "stop",
        "levi-live.service",
    ]


def test_a_site_file_that_names_another_unit_cannot_make_the_card_stop_it(machine):
    """The unit name comes from the site file; only ``levi-live*.service`` is ever
    stopped from here (the product's own stop holds the same rule)."""
    machine.site = dataclasses.replace(
        machine.site, live=marvel0_site.Live(unit="levi-product.service")
    )
    machine.set_live("running")
    r = post(machine, "/live/stop", STOP_LIVE)
    assert r.status_code == 502, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "live_stop_failed" and detail["reason"] == "unit_invalid"
    assert machine.live_stops() == []
    assert machine.systemctl.calls == [], "no systemctl command of any kind"


def test_a_service_that_is_not_running_is_left_alone(machine):
    machine.set_live("stopped")
    body = post(machine, "/live/stop", STOP_LIVE).json()
    assert body["already"] is True and body["results"] == []
    assert machine.live_stops() == [], "the command was not even run"


def test_the_stop_is_answered_again_not_run_twice(machine):
    machine.set_live("running")
    first = post(machine, "/live/stop", STOP_LIVE).json()
    assert len(machine.live_stops()) == 1
    again = post(machine, "/live/stop", STOP_LIVE).json()
    assert again == {**first, "replayed": True}
    assert len(machine.live_stops()) == 1, "and not stopped again"


@pytest.mark.parametrize(
    "fact, value, code",
    [
        ("hosted_d", True, "d_active"),
        ("aeri_runs", [{"runner_alive": True}], "d_active"),
        ("hosted_e", True, "e_active"),
        (
            "sessions",
            {"state": "ok", "sessions": [{"status": "running", "age_s": 1}]},
            "e_active",
        ),
        ("flock", {"levi-aeri-fr3-arm.lock": [7]}, "arm_in_use"),
    ],
)
def test_the_live_stop_is_refused_while_the_robot_side_is_in_use(
    machine, fact, value, code
):
    machine.set_live("running")
    machine.facts[fact] = value
    r = post(machine, "/live/stop", STOP_LIVE)
    assert r.status_code == 409 and r.json()["detail"]["code"] == code
    assert machine.live_stops() == []


@pytest.mark.parametrize(
    "fact, value",
    [
        ("hosted_d", None),
        ("hosted_e", None),
        ("flock", {"levi-aeri-fr3-arm.lock": None}),
        ("aeri_runs", None),
    ],
)
def test_the_live_stop_is_refused_when_an_obstacle_cannot_be_read(machine, fact, value):
    machine.set_live("running")
    machine.facts[fact] = value
    r = post(machine, "/live/stop", STOP_LIVE)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "state_unreadable"
    assert machine.live_stops() == []


def test_a_live_service_whose_state_cannot_be_read_is_not_stopped(machine):
    def broken(unit):
        raise OSError("systemctl")

    machine.api.readiness.host.unit_show = broken
    r = post(machine, "/live/stop", STOP_LIVE)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "state_unreadable"
    assert machine.live_stops() == []


def test_a_command_that_leaves_the_service_running_is_a_failure_with_its_words(machine):
    machine.set_live("running")
    machine.set_live_stop(stops=False, rc=1, say="refused: a job is running")
    r = post(machine, "/live/stop", STOP_LIVE)
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert detail["code"] == "live_stop_failed" and detail["reason"] == "still_running"
    assert detail["returncode"] == 1 and "a job is running" in detail["tail"]
    # a failure is not remembered as an answer: the same request may be sent again
    machine.set_live_stop(stops=True, rc=0)
    assert post(machine, "/live/stop", STOP_LIVE).status_code == 200


def test_the_truth_is_the_service_state_not_the_exit_code(machine):
    machine.set_live("running")
    machine.set_live_stop(
        stops=True, rc=3, say="warning: could not remove the pid file"
    )
    body = post(machine, "/live/stop", STOP_LIVE).json()
    assert body["live_service"]["state"] == "stopped"
    assert body["results"][0]["detail"] == "warning: could not remove the pid file"


def test_the_live_stop_holds_the_run_lock_and_waits_for_nobody(machine):
    machine.set_live("running")
    fd = os.open(machine.hub / ".marvel0-run.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = post(machine, "/live/stop", STOP_LIVE)
    finally:
        os.close(fd)
    assert r.status_code == 423 and r.json()["detail"]["code"] == "busy"
    assert machine.live_stops() == []


def test_the_leftover_stop_holds_the_run_lock_and_waits_for_nobody(machine):
    with_leftovers(machine)
    fd = os.open(machine.hub / ".marvel0-run.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = post(machine, "/leftovers/stop", STOP_LEFT)
    finally:
        os.close(fd)
    assert r.status_code == 423 and r.json()["detail"]["code"] == "busy"
    assert machine.systemctl.calls == []


def test_the_lock_is_let_go_after_the_stop(machine):
    machine.set_live("running")
    post(machine, "/live/stop", STOP_LIVE)
    fd = os.open(machine.hub / ".marvel0-run.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


# --- stopping the leftovers --------------------------------------------------------------------

LEFT = f"{VLM} loaded active running a\n{POLICY} loaded active running b\n{MANUAL} loaded active running c\n"


def with_leftovers(machine):
    machine.facts["units"] = status.parse_units(LEFT)


def test_the_leftover_units_are_stopped_one_by_one_and_the_hand_started_one_is_not(
    machine,
):
    with_leftovers(machine)
    r = post(machine, "/leftovers/stop", STOP_LEFT)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "leftovers_stop" and body["already"] is False
    assert sorted(x["name"] for x in body["results"]) == sorted([VLM, POLICY])
    assert all(x["ok"] for x in body["results"])
    assert sorted(
        c[-1] for c in machine.systemctl.calls if c[0] == "systemctl"
    ) == sorted([VLM, POLICY])
    assert MANUAL not in [c[-1] for c in machine.systemctl.calls]
    assert all(
        c[:3] == ["systemctl", "--user", "stop"] for c in machine.systemctl.calls
    )


def test_nothing_to_stop_is_an_answer_not_an_error(machine):
    body = post(machine, "/leftovers/stop", STOP_LEFT).json()
    assert body["already"] is True and body["results"] == []
    assert machine.systemctl.calls == []


def test_a_unit_that_will_not_stop_fails_the_request_with_every_result(machine):
    with_leftovers(machine)
    machine.systemctl.fail = {VLM}
    r = post(machine, "/leftovers/stop", STOP_LEFT)
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert detail["code"] == "stop_failed"
    by_name = {x["name"]: x for x in detail["results"]}
    assert by_name[VLM]["ok"] is False and "Unit is masked" in by_name[VLM]["detail"]
    assert by_name[POLICY]["ok"] is True, "the others were still tried"


@pytest.mark.parametrize(
    "fact, value, code",
    [
        ("hosted_d", True, "d_active"),
        ("hosted_e", True, "e_active"),
        ("flock", {"levi-aeri-fr3-arm.lock": [7]}, "arm_in_use"),
        ("flock", {".gpu.lock": [7]}, "gpu_in_use"),
    ],
)
def test_nothing_is_stopped_while_the_robot_side_or_the_gpu_is_in_use(
    machine, fact, value, code
):
    with_leftovers(machine)
    machine.facts[fact] = value
    r = post(machine, "/leftovers/stop", STOP_LEFT)
    assert r.status_code == 409 and r.json()["detail"]["code"] == code
    assert machine.systemctl.calls == []


def test_an_unreadable_unit_list_stops_nothing(machine):
    machine.api.readiness.host.units = lambda: {"state": "unknown", "items": []}
    r = post(machine, "/leftovers/stop", STOP_LEFT)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "state_unreadable"
    assert machine.systemctl.calls == []


def test_the_leftover_stop_is_answered_again_not_run_twice(machine):
    with_leftovers(machine)
    first = post(machine, "/leftovers/stop", STOP_LEFT).json()
    count = len(machine.systemctl.calls)
    again = post(machine, "/leftovers/stop", STOP_LEFT).json()
    assert again == {**first, "replayed": True}
    assert len(machine.systemctl.calls) == count


def test_the_history_records_the_units_that_were_stopped(machine):
    with_leftovers(machine)
    post(machine, "/leftovers/stop", STOP_LEFT)
    history = machine.state / "marvel0" / "aeri_prep" / "history.jsonl"
    line = json.loads(history.read_text().splitlines()[-1])
    assert line["event"] == "leftovers_stop" and sorted(line["units"]) == sorted(
        [VLM, POLICY]
    )


# --- the module-level pieces -----------------------------------------------------------------


def test_the_routes_are_exactly_these_four():
    pairs = sorted(
        (r.path, m)
        for r in aeri_api.router.routes
        for m in sorted(r.methods - {"HEAD"})
    )
    assert pairs == sorted(
        [
            (BASE, "GET"),
            (f"{BASE}/prepare", "POST"),
            (f"{BASE}/live/stop", "POST"),
            (f"{BASE}/leftovers/stop", "POST"),
        ]
    )


def test_the_router_is_part_of_the_pages_router():
    from levi.marvel0 import api

    paths = {r.path for r in api.router.routes}
    assert {
        BASE,
        f"{BASE}/prepare",
        f"{BASE}/live/stop",
        f"{BASE}/leftovers/stop",
    } <= paths
