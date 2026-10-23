"""``levi/marvel0/deploy_api.py``: the four deploy routes over a ``Supervisor`` on
the fake machine of ``test_marvel0_deploy.py`` (nothing real is touched; the
autouse guard of that file applies here too). What the router adds to the
supervisor: the person-only rule, the strict closed bodies, the HTTP status of
every refusal, 202 and 200, and that no answer names a path."""

import json
import re
import shutil
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_marvel0_deploy import (  # noqa: F401 - the fixtures and the fake machine
    CFG,
    NOW,
    OOM,
    REQ,
    SFT,
    STOP,
    Marvel0,
    assert_no_paths,
    body,
    forbid_real_machine,
    running,
)

from levi.marvel0 import deploy, deploy_api, record
from levi.marvel0 import site as marvel0_site

DEPLOY = "/api/levi/marvel0/deploy"
STOP_URL = "/api/levi/marvel0/deploy/stop"
LOG = "/api/levi/marvel0/deploy/log"

# The status of every refusal, written out from the API contract (not taken
# from deploy.REFUSALS, which must agree with it).
CONTRACT = {
    "inference_mode_invalid": 422,
    "inference_mode_conflict": 422,
    "invalid_type": 422,
    "vlash_overlap_out_of_range": 422,
    "vlash_overlap_not_allowed": 422,
    "vlash_model_incompatible": 422,
    "request_id_invalid": 400,
    "confirm_required": 400,
    "model_unknown": 422,
    "fraction_not_allowed": 422,
    "cfg_scale_not_allowed": 422,
    "cfg_scale_out_of_range": 422,
    "unverified_not_accepted": 409,
    "busy": 423,
    "request_id_used": 409,
    "state_dir_unusable": 412,
    "checkpoint_invalid": 412,
    "runtime_files_missing": 412,
    "source_file_missing": 412,
    "source_changed": 409,
    "net_table_unreadable": 503,
    "port_taken": 409,
    "already_running": 409,
    "client_connected": 409,
    "systemd_unreadable": 503,
    "policy_unit_exists": 409,
    "arm_lock_held": 409,
    "arm_lock_unknown": 503,
    "aeri_run_live": 409,
    "aeri_registry_unreadable": 503,
    "gpu_lock_foreign": 409,
    "env_unreadable": 503,
    "gpu_unreadable": 503,
    "gpu_insufficient": 409,
    "start_failed": 502,
    "start_unconfirmed": 409,
    "nothing_to_stop": 409,
    "foreign_listener_not_stoppable": 409,
    "unit_mismatch": 409,
    "stop_failed": 502,
    "stop_incomplete": 502,
}


@pytest.fixture
def marvel0(tmp_path, monkeypatch):
    return Marvel0(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def no_token(monkeypatch):
    monkeypatch.delenv("LEVI_UI_TOKEN", raising=False)


@pytest.fixture
def client(marvel0, monkeypatch):
    app = FastAPI()
    app.include_router(deploy_api.router)
    monkeypatch.setattr(deploy_api, "supervisor", lambda: marvel0.sup)
    return TestClient(app)


def post(client, **kw):
    return client.post(DEPLOY, json=body(**kw))


def stop_post(client, **kw):
    return client.post(STOP_URL, json={**STOP, **kw})


def code_of(response):
    return response.json()["detail"]["code"]


# --------------------------------------------------------------- the contract


def test_the_status_of_every_refusal_is_the_contracts():
    assert {c: s for c, (s, _) in deploy.REFUSALS.items()} == CONTRACT


def test_the_routes_are_these_four_under_the_marvel0_prefix():
    found = sorted(
        (route.path, sorted(route.methods - {"HEAD", "OPTIONS"}))
        for route in deploy_api.router.routes
    )
    assert found == [
        ("/api/levi/marvel0/deploy", ["GET"]),
        ("/api/levi/marvel0/deploy", ["POST"]),
        ("/api/levi/marvel0/deploy/log", ["GET"]),
        ("/api/levi/marvel0/deploy/stop", ["POST"]),
    ]


def test_a_good_deploy_is_202_and_the_state_is_readable(client, marvel0):
    response = post(client)
    assert response.status_code == 202
    answer = response.json()
    assert answer["schema"] == "levi.marvel0.deploy_result.v1"
    assert answer["state"] == "starting" and answer["phase"] == "started"
    view = client.get(DEPLOY)
    assert view.status_code == 200
    assert view.json()["state"] == "starting"
    assert view.json()["deploy"]["request_id"] == REQ
    assert_no_paths(marvel0, answer)
    assert_no_paths(marvel0, view.json())


def test_a_replayed_deploy_is_202_with_the_stored_answer(client, marvel0):
    first = post(client).json()
    again = post(client)
    assert again.status_code == 202 and again.json()["replayed"] is True
    assert again.json()["deploy"] == first["deploy"]
    assert len(marvel0.systemd_runs()) == 1


def test_a_stop_is_202_and_an_already_stopped_one_is_200(client, marvel0):
    running(marvel0)
    response = stop_post(client)
    assert response.status_code == 202
    assert response.json()["stopped"] is True and response.json()["state"] == "idle"
    again = stop_post(client, request_id="req-stop-0002")
    assert again.status_code == 200
    assert again.json()["already"] is True and again.json()["stopped"] is False
    assert len(marvel0.stops()) == 1


VIEW_KEYS = {
    "schema",
    "sampled_at",
    "state",
    "reason",
    "deploy",
    "unit",
    "listener",
    "readiness",
    "failure",
    "gpu",
    "vram",
    "checks",
    "can_deploy",
    "can_stop",
    "stop_blockers",
    "live_annotation",
    "estop",
    "operation",
    "notes",
    "confirm",
}
DEPLOY_KEYS = {
    "request_id",
    "unit",
    "model_id",
    "config",
    "rtc",
    "inference_mode",
    "vlash_overlap_steps",
    "fraction",
    "cfg_guidance_scale",
    "phase",
    "started_at",
    "age_s",
    "ready_at",
    "gpu_lock",
    "source_hash",
}


def test_the_state_document_has_exactly_the_contracts_keys(client, marvel0):
    idle = client.get(DEPLOY).json()
    assert set(idle) == VIEW_KEYS
    assert set(idle["gpu"]) == {
        "state",
        "total_mib",
        "used_mib",
        "free_mib",
        "server_holds_mib",
        "server_holds_basis",
    }
    assert set(idle["vram"]) == {"margin_mib", "presets", "hint"}
    assert set(idle["listener"]) == {"port", "present", "ours", "established"}
    assert set(idle["readiness"]) == {
        "norm_stats",
        "rtc_enabled",
        "vlash_enabled",
        "rtc_warmup",
        "cfg",
        "listening",
    }
    assert set(idle["estop"]) == {"always", "keep_hand", "text"}
    assert {tuple(sorted(c)) for c in idle["checks"]} == {
        ("code", "level", "params", "text")
    }
    post(client)
    running_view = client.get(DEPLOY).json()
    assert set(running_view) == VIEW_KEYS
    assert set(running_view["deploy"]) == DEPLOY_KEYS


def test_the_deploy_and_stop_answers_have_exactly_the_contracts_keys(client, marvel0):
    answer = post(client).json()
    assert set(answer) == {
        "schema",
        "created",
        "replayed",
        "request_id",
        "phase",
        "deploy",
        "state",
        "gpu",
        "next",
    }
    assert set(answer["deploy"]) == DEPLOY_KEYS
    assert set(answer["gpu"]) == {
        "total_mib",
        "free_mib_at_final_check",
        "required_mib",
        "lock",
    }
    marvel0.server_comes_up()
    stopped = stop_post(client).json()
    assert set(stopped) == {
        "schema",
        "created",
        "replayed",
        "request_id",
        "state",
        "unit",
        "stopped",
        "client_override_used",
        "next",
    }
    log = client.get(LOG).json()
    assert set(log) == {"schema", "log_id", "size_bytes", "lines", "classification"}


# --------------------------------------------------------------- who may write


@pytest.mark.parametrize("auth", ["Bearer abc", "bearer abc", "BEARER x"])
def test_an_agent_credential_may_not_write(client, marvel0, auth):
    for url, payload in ((DEPLOY, body()), (STOP_URL, STOP)):
        response = client.post(url, json=payload, headers={"authorization": auth})
        assert response.status_code == 403
        assert code_of(response) == "person_only"
    assert marvel0.calls == [] and marvel0.sup.status()["state"] == "idle"
    assert not marvel0.systemd_runs()


def test_the_person_rule_comes_before_the_body_is_read(client, marvel0):
    response = client.post(
        DEPLOY, json={"nonsense": 1}, headers={"authorization": "Bearer abc"}
    )
    assert response.status_code == 403 and code_of(response) == "person_only"


def test_a_basic_or_other_authorization_is_not_a_bearer(client):
    response = client.post(DEPLOY, json=body(), headers={"authorization": "Basic abc"})
    assert response.status_code == 202


def test_the_ui_token_is_demanded_when_one_is_set(client, marvel0, monkeypatch):
    monkeypatch.setenv("LEVI_UI_TOKEN", "s3cret-token")
    for headers in ({}, {"x-levi-ui-token": "wrong"}, {"x-levi-ui-token": ""}):
        for url, payload in ((DEPLOY, body()), (STOP_URL, STOP)):
            response = client.post(url, json=payload, headers=headers)
            assert response.status_code == 401 and code_of(response) == "ui_token"
    assert marvel0.calls == []
    ok = client.post(DEPLOY, json=body(), headers={"x-levi-ui-token": "s3cret-token"})
    assert ok.status_code == 202


def test_reading_needs_no_person(client):
    for url in (DEPLOY, LOG):
        response = client.get(url, headers={"authorization": "Bearer abc"})
        assert response.status_code == 200


def test_person_only_can_be_called_directly():
    class Request:
        def __init__(self, headers):
            self.headers = headers

    deploy_api.person_only(Request({}))
    with pytest.raises(Exception) as caught:
        deploy_api.person_only(Request({"authorization": "Bearer x"}))
    assert caught.value.status_code == 403


# --------------------------------------------------------------- the bodies


@pytest.mark.parametrize(
    "change",
    [
        {"extra": 1},
        {"rtc": "yes"},
        {"rtc": 1},
        {"fraction": "0.22"},
        {"model_id": 5},
        {"accept_unverified": "no"},
        {"cfg_guidance_scale": "1.0"},
        {"levi_mode": "on"},
    ],
)
def test_the_deploy_body_is_strict_and_closed(client, marvel0, change):
    response = client.post(DEPLOY, json={**body(), **change})
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list), "FastAPI's own shape"
    assert marvel0.calls == []


@pytest.mark.parametrize(
    "missing", ["request_id", "model_id", "rtc", "fraction", "confirm"]
)
def test_the_deploy_body_needs_its_fields(client, marvel0, missing):
    payload = body()
    del payload[missing]
    assert client.post(DEPLOY, json=payload).status_code == 422
    assert marvel0.calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {"request_id": 5, "confirm": "stop policy server"},
        {"request_id": REQ},
        {"confirm": "stop policy server"},
        {**STOP, "client_override": 3},
        {**STOP, "extra": True},
    ],
)
def test_the_stop_body_is_strict_and_closed(client, marvel0, payload):
    assert client.post(STOP_URL, json=payload).status_code == 422
    assert marvel0.calls == []


def test_a_body_that_is_not_json_is_422(client):
    assert client.post(DEPLOY, content="{not json").status_code == 422


def test_wrong_methods_are_405(client):
    assert client.put(DEPLOY, json=body()).status_code == 405
    assert client.delete(DEPLOY).status_code == 405
    assert client.get(STOP_URL).status_code == 405
    assert client.post(LOG, json={}).status_code == 405
    assert client.get(LOG + "/something").status_code == 404, "no path parameter"


def test_a_wrong_confirmation_phrase_is_400_and_runs_nothing(client, marvel0):
    response = post(client, confirm="deploy another_model")
    assert (response.status_code, code_of(response)) == (400, "confirm_required")
    assert marvel0.calls == []
    response = stop_post(client, confirm="stop")
    assert (response.status_code, code_of(response)) == (400, "confirm_required")
    assert marvel0.calls == []


# --------------------------------------------------------------- refusals by HTTP


def break_arm(r):
    r.hold(r.site.arm_lock_file, 5001)


def break_gpu_lock(r):
    r.hold(r.site.gpu_lock_file, 6001)


def break_python(r):
    Path(r.site.python).unlink()


def break_checkpoint(r):
    shutil.rmtree(r.ckpt())


def break_source(r):
    # (the serving scripts are checked as runtime files first)
    Path(r.site.openpi_checkout, r.site.source.hash_files[2]).unlink()


def break_aeri(r):
    r.aeri = [{"run_id": "r1", "backend": "standalone", "runner_alive": True}]


def break_net(r):
    r.fail.add("net-tcp")
    r.write_net()


def break_memory(r):
    r.gpu = [32607 - 5000, 32607]


def break_systemd_run(r):
    r.fail.add("systemd-run")


def break_unconfirmed(r):
    # An earlier start (another request id) whose systemd-run did not answer.
    r.systemd_run_answer = (deploy.TIMED_OUT, "did not finish in time")
    r.refused(r.deploy, request_id="req-000099-zzzz")
    r.systemd_run_answer = None
    r.calls.clear()


DEPLOY_REFUSALS = [
    ("model_unknown", {"model": "nope"}, None),
    ("fraction_not_allowed", {"fraction": 0.25}, None),
    ("cfg_scale_not_allowed", {"cfg_guidance_scale": 1.0}, None),
    (
        "cfg_scale_out_of_range",
        {"model": CFG, "accept_unverified": True, "cfg_guidance_scale": 9.0},
        None,
    ),
    ("unverified_not_accepted", {"model": CFG}, None),
    ("checkpoint_invalid", {}, break_checkpoint),
    ("runtime_files_missing", {}, break_python),
    ("source_file_missing", {}, break_source),
    ("net_table_unreadable", {}, break_net),
    ("port_taken", {}, lambda r: r.listen()),
    ("client_connected", {}, lambda r: r.connect()),
    ("systemd_unreadable", {}, lambda r: r.fail.add("list-units")),
    (
        "policy_unit_exists",
        {},
        lambda r: setattr(r, "foreign_units", ["levi-aeri-policy-forward"]),
    ),
    ("arm_lock_held", {}, break_arm),
    ("aeri_run_live", {}, break_aeri),
    ("aeri_registry_unreadable", {}, lambda r: setattr(r, "aeri_error", True)),
    ("gpu_lock_foreign", {}, break_gpu_lock),
    ("env_unreadable", {}, lambda r: r.fail.add("show-environment")),
    ("gpu_unreadable", {}, lambda r: r.fail.add("nvidia-smi")),
    ("gpu_insufficient", {}, break_memory),
    ("start_failed", {}, break_systemd_run),
    ("start_unconfirmed", {}, break_unconfirmed),
]


@pytest.mark.parametrize(
    "code, change, setup", DEPLOY_REFUSALS, ids=[c for c, _, _ in DEPLOY_REFUSALS]
)
def test_a_deploy_refusal_has_the_contracts_status_and_code(
    client, marvel0, code, change, setup
):
    if setup:
        setup(marvel0)
    response = post(client, **change)
    assert response.status_code == CONTRACT[code], response.text
    detail = response.json()["detail"]
    assert detail["code"] == code and detail["message"]
    assert_no_paths(marvel0, detail)
    if code != "start_failed":
        assert marvel0.systemd_runs() == []


def test_a_request_id_used_for_another_body_is_409(client):
    assert post(client).status_code == 202
    response = post(client, rtc=False)
    assert (response.status_code, code_of(response)) == (409, "request_id_used")


def test_an_invalid_request_id_is_400(client, marvel0):
    response = post(client, request_id="short")
    assert (response.status_code, code_of(response)) == (400, "request_id_invalid")
    assert marvel0.calls == []


def test_the_state_folder_inside_source_data_is_412(client, marvel0):
    marvel0.sup.workspace = type(marvel0.workspace).at(
        Path(marvel0.site.rollout_root) / "ws"
    )
    response = post(client)
    assert (response.status_code, code_of(response)) == (412, "state_dir_unusable")


STOP_REFUSALS = [
    ("nothing_to_stop", False, lambda r: None),
    ("foreign_listener_not_stoppable", False, lambda r: r.listen()),
    (
        "unit_mismatch",
        True,
        lambda r: r.units[record.unit_name(SFT)]["argv"].append("--x"),
    ),
    ("stop_failed", True, lambda r: setattr(r, "stop_mode", "fail")),
    ("stop_incomplete", True, lambda r: setattr(r, "stop_mode", "port_stays")),
    ("client_connected", True, lambda r: r.connect()),
    ("net_table_unreadable", True, lambda r: (r.fail.add("net-tcp"), r.write_net())),
    ("systemd_unreadable", True, lambda r: r.fail.add("show-state")),
]


@pytest.mark.parametrize(
    "code, deployed, setup", STOP_REFUSALS, ids=[c for c, _, _ in STOP_REFUSALS]
)
def test_a_stop_refusal_has_the_contracts_status_and_code(
    client, marvel0, code, deployed, setup
):
    if deployed:
        running(marvel0)
    setup(marvel0)
    response = stop_post(client)
    assert response.status_code == CONTRACT[code], response.text
    detail = response.json()["detail"]
    assert detail["code"] == code
    assert_no_paths(marvel0, detail)
    if code != "stop_incomplete":
        assert marvel0.stops() == [] or code == "stop_failed"


def test_a_stop_forced_with_the_phrase_stops_under_a_client(client, marvel0):
    running(marvel0)
    marvel0.connect()
    response = stop_post(client, client_override="stop with client connected")
    assert response.status_code == 202
    assert response.json()["client_override_used"] is True


def test_a_second_operation_is_423(client, marvel0):
    entered, release = threading.Event(), threading.Event()

    def hold(argv):
        entered.set()
        assert release.wait(10)

    marvel0.on_systemd_run = hold
    worker = threading.Thread(target=lambda: post(client))
    worker.start()
    try:
        assert entered.wait(10)
        response = post(client, request_id="req-000002-bbbb")
        assert (response.status_code, code_of(response)) == (423, "busy")
        assert response.json()["detail"]["operation"]["action"] == "deploy"
        response = stop_post(client)
        assert (response.status_code, code_of(response)) == (423, "busy")
    finally:
        release.set()
        worker.join(10)


# --------------------------------------------------------------- the site file


@pytest.fixture
def no_site(marvel0, tmp_path):
    missing = tmp_path / "elsewhere" / "site.toml"
    marvel0.sup.site_loader = lambda: marvel0_site.require(
        {"LEVI_MARVEL0_SITE": str(missing)}
    )
    return missing


def test_without_a_site_file_every_route_is_412_with_the_template(
    client, marvel0, no_site
):
    responses = [
        client.get(DEPLOY),
        post(client),
        stop_post(client),
        client.get(LOG),
    ]
    for response in responses:
        assert response.status_code == 412
        detail = response.json()["detail"]
        assert detail["code"] == "site_missing"
        assert detail["message"] == "marvel0 site file missing"
        assert detail["looked_at"] == str(no_site)
        assert "[paths]" in detail["template"]
    assert marvel0.calls == []
    assert not (marvel0.tmp / "ws").exists(), "nothing was read or written"


def test_an_invalid_site_file_is_412_with_its_errors(client, marvel0, tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("schema = 'nope'\n")
    bad.chmod(0o644)
    marvel0.sup.site_loader = lambda: marvel0_site.require(
        {"LEVI_MARVEL0_SITE": str(bad)}
    )
    response = client.get(DEPLOY)
    assert response.status_code == 412
    assert response.json()["detail"]["code"] == "site_invalid"
    assert response.json()["detail"]["errors"]


# --------------------------------------------------------------- the log


def test_the_log_route_clamps_and_never_names_a_path(client, marvel0):
    empty = client.get(LOG)
    assert empty.status_code == 200
    assert empty.json()["log_id"] is None and empty.json()["lines"] == []
    running(marvel0)
    marvel0.log(*[f"line {n}" for n in range(300)], "/tmp/x/y/z.txt")
    for asked, got in (("lines=1000", 200), ("lines=0", 1), ("lines=5", 5), ("", 80)):
        url = LOG + (f"?{asked}" if asked else "")
        assert len(client.get(url).json()["lines"]) == got
    response = client.get(LOG + "?lines=3")
    assert response.json()["lines"][-1] == "z.txt"
    assert response.json()["log_id"].endswith(".log")
    assert "/" not in response.json()["log_id"]
    assert_no_paths(marvel0, response.json())
    assert client.get(LOG + "?lines=abc").status_code == 422


def test_the_log_shows_the_failure_class(client, marvel0):
    running(marvel0, ready=False)
    marvel0.log(OOM)
    assert client.get(LOG).json()["classification"]["failure"] == "xla_oom"


# --------------------------------------------------------------- no path anywhere


def test_no_answer_of_a_session_names_a_path(client, marvel0):
    answers = [client.get(DEPLOY).json()]
    answers.append(post(client).json())
    marvel0.server_comes_up()
    marvel0.sup.invalidate()
    answers.append(client.get(DEPLOY).json())
    answers.append(client.get(LOG).json())
    answers.append(stop_post(client).json())
    answers.append(client.get(DEPLOY).json())
    for answer in answers:
        assert_no_paths(marvel0, answer)
    assert json.dumps(answers).count("levi-aeri-policy-manual-marvel0-") > 0
    assert not re.search(r"/home/|/tmp/", json.dumps(answers))


def test_vlash_deploy_route_preserves_exact_mode_and_overlap(client, marvel0):
    script = Path(marvel0.site.openpi_checkout) / "lab/vlash/serve_policy_vlash.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# fake VLASH serving entrypoint")
    for relative in (
        "lab/vlash/runtime.py",
        "lab/vlash/run_collect_vlash.py",
        "examples/fr3_local/run_robotiq_client.py",
        "examples/fr3_local/run_robotiq_client_collect_data.py",
    ):
        source = Path(marvel0.site.openpi_checkout) / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("# fake isolated source")
    answer = post(client, rtc=False, inference_mode="vlash", vlash_overlap_steps=3)
    assert answer.status_code == 202
    deployed = answer.json()["deploy"]
    assert set(deployed) == DEPLOY_KEYS
    assert {
        key: deployed[key] for key in ("rtc", "inference_mode", "vlash_overlap_steps")
    } == {
        "rtc": False,
        "inference_mode": "vlash",
        "vlash_overlap_steps": 3,
    }
    view = client.get(DEPLOY).json()
    assert view["deploy"] == deployed
    assert view["readiness"]["vlash_enabled"] is False
    assert view["readiness"]["rtc_enabled"] is None
