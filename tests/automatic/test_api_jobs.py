# ruff: noqa: F401, F811
"""``POST /api/levi/automatic/jobs``: the wizard form becomes a new job file
(T-API-1)."""

import copy
import os

import pytest
from test_api_common import (
    REQ,
    client,
    home,
    job_id_of,
    make_checkpoint,
    roots,
)

from levi.automatic import api, cli, launch

BASE = "/api/levi/automatic"


@pytest.fixture
def checkpoints(tmp_path, monkeypatch):
    root = tmp_path / "ckpt"
    root.mkdir()
    make_checkpoint(root, "fwd-a", role="forward")
    make_checkpoint(root, "rst-a", role="reset", config="some_reset_config")
    monkeypatch.setenv(api.POLICY_ROOT_ENV, str(root))
    api._POLICY_CACHE.update(key=None, at=0.0, report=None)
    return root


FORM = {
    "request_id": "req-job-1",
    "name": "wiz-a",
    "task": {"instruction": "stack the plates", "reset_instruction": "unstack them"},
    "policy_forward": {"checkpoint_id": "fwd-a"},
    "reset": {"strategy": "human_assisted", "scene_check": "provider"},
    "run": {"episodes": 3, "max_steps": 40},
    "recording": {"group": "aeri"},
}


def form(**over):
    value = copy.deepcopy(FORM)
    value.update(over)
    return value


def post(client, body, **kw):
    return client.post(f"{BASE}/jobs", json=body, **kw)


def test_a_form_becomes_a_new_valid_job_that_plans(
    client, roots, checkpoints, tmp_path
):
    response = post(client, form())
    assert response.status_code == 201, response.text
    assert response.json()["id"] == job_id_of(client, "wizard/wiz-a")
    path = roots / "wizard" / "wiz-a.yaml"
    text = path.read_text()
    assert str(tmp_path) not in text
    assert "# forward policy: fwd-a (config pi05_fr3_all_state)" in text
    job = cli.load_job(path)
    assert job["config"].reset_strategy == "human_assisted"
    assert job["config"].episodes == 3 and job["config"].forward_max_steps == 40
    # human_assisted writes no reset policy and no reset instruction.
    assert "reset:\n    max_steps" not in text and "reset_instruction" not in text
    plan = client.post(
        f"{BASE}/plan",
        json={"job_id": response.json()["id"], "execution_mode": "dry_run"},
    ).json()
    assert plan["launchable"] is True and plan["reset_mode"] == "human_assisted"
    assert plan["roles"] == ["forward"]
    assert not [p for p in (roots / "wizard").iterdir() if p.name.startswith(".")]


def test_single_reset_policy_needs_and_writes_the_reset_policy(
    client, roots, checkpoints
):
    body = form(
        reset={"strategy": "single_reset_policy", "scene_check": "provider"},
        policy_reset={"checkpoint_id": "rst-a"},
    )
    assert post(client, body).status_code == 201
    text = (roots / "wizard" / "wiz-a.yaml").read_text()
    assert "# reset policy: rst-a" in text and "reset_instruction: " in text
    job = cli.load_job(roots / "wizard" / "wiz-a.yaml")
    assert job["config"].reset_strategy == "single_reset_policy"
    missing = form(
        name="wiz-b",
        request_id="req-job-2",
        reset={"strategy": "single_reset_policy", "scene_check": "provider"},
    )
    answer = post(client, missing)
    assert answer.status_code == 422
    assert answer.json()["detail"]["errors"][0]["field"] == "policy_reset"


def test_human_assisted_refuses_a_reset_policy(client, roots, checkpoints):
    answer = post(client, form(policy_reset={"checkpoint_id": "rst-a"}))
    assert answer.status_code == 422
    assert answer.json()["detail"]["errors"][0]["field"] == "policy_reset"
    assert not (roots / "wizard").exists()


def test_checkpoints_must_come_from_the_discovery(client, roots, checkpoints):
    answer = post(client, form(policy_forward={"checkpoint_id": "/etc/passwd"}))
    assert answer.status_code == 422
    assert answer.json()["detail"]["code"] == "job_invalid"
    answer = post(client, form(policy_forward={"checkpoint_id": "rst-a"}))
    fields = [e["field"] for e in answer.json()["detail"]["errors"]]
    assert answer.status_code == 422 and fields == ["policy_forward.checkpoint_id"]


def test_a_checkpoint_of_unstated_role_serves_neither_role(client, roots, checkpoints):
    make_checkpoint(checkpoints, "mystery", config="some_other_config")
    api._POLICY_CACHE.update(key=None, at=0.0, report=None)
    listed = client.get(f"{BASE}/policies").json()["checkpoints"]
    assert {c["id"]: c["role"] for c in listed}["mystery"] == "unknown"
    answer = post(client, form(policy_forward={"checkpoint_id": "mystery"}))
    assert answer.status_code == 422
    assert not (roots / "wizard").exists()


def test_without_a_policy_root_the_message_names_the_setting(client, roots):
    answer = post(client, form())
    assert answer.status_code == 422
    assert api.POLICY_ROOT_ENV in answer.text


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("name",), "../x"),
        (("name",), "a/b"),
        (("name",), ""),
        (("name",), "x" * 200),
        (("run", "episodes"), -1),
        (("run", "episodes"), "3"),
        (("run", "max_steps"), 0),
        (("task", "instruction"), ""),
        (("reset", "strategy"), "other"),
        (("recording", "group"), "../g"),
        (("request_id",), "has space"),
    ],
)
def test_field_errors_are_per_field(client, roots, checkpoints, path, value):
    body = form()
    target = body
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    answer = post(client, body)
    assert answer.status_code == 422
    detail = answer.json()["detail"]
    assert detail["code"] == "job_invalid"
    assert ".".join(path) in [e["field"] for e in detail["errors"]]
    assert not (roots / "wizard").exists()


def test_unknown_form_fields_are_refused(client, roots, checkpoints):
    answer = post(client, form(rollout_root="/data"))
    assert answer.status_code == 422
    assert not (roots / "wizard").exists()


def test_a_used_name_is_never_overwritten(client, roots, checkpoints):
    assert post(client, form()).status_code == 201
    path = roots / "wizard" / "wiz-a.yaml"
    before = path.read_bytes()
    other = form(request_id="req-job-9", run={"episodes": 9, "max_steps": 40})
    answer = post(client, other)
    assert answer.status_code == 409
    assert answer.json()["detail"]["code"] == "job_exists"
    assert path.read_bytes() == before


def test_the_same_request_again_is_the_same_job(client, roots, checkpoints):
    first = post(client, form())
    again = post(client, form())
    assert first.status_code == again.status_code == 201
    assert first.json() == again.json()
    # A new request id with the same content is the same file, not a conflict.
    third = post(client, form(request_id="req-job-3"))
    assert third.status_code == 201 and third.json()["id"] == first.json()["id"]
    # One request id for another form is a conflict.
    clash = post(client, form(run={"episodes": 5, "max_steps": 40}))
    assert clash.status_code == 409
    assert clash.json()["detail"]["code"] == "request_id_used"


def test_text_cannot_inject_keys_or_comments(client, roots, checkpoints):
    nasty = 'line one\nreset:\n  strategy: single_reset_policy # "x"\n- a: b'
    body = form(task={"instruction": nasty})
    assert post(client, body).status_code == 201
    job = cli.load_job(roots / "wizard" / "wiz-a.yaml")
    assert job["texts"]["forward"] == nasty
    assert job["config"].reset_strategy == "human_assisted"


def test_operator_attested_needs_a_contract_the_form_cannot_name(
    client, roots, checkpoints
):
    body = form(
        reset={"strategy": "human_assisted", "scene_check": "operator_attested"}
    )
    answer = post(client, body)
    assert answer.status_code == 422
    assert answer.json()["detail"]["code"] == "job_invalid"
    assert not [p for p in (roots / "wizard").iterdir() if not p.name.startswith(".")]
    assert not list((roots / "wizard").iterdir())


def test_only_a_person_writes_a_job(client, roots, checkpoints):
    bearer = post(client, form(), headers={"authorization": "Bearer abc"})
    assert bearer.status_code == 403
    assert bearer.json()["detail"]["code"] == "person_only"
    client.headers.pop("x-levi-ui-token")
    assert post(client, form()).status_code == 401
    assert not (roots / "wizard").exists()


def test_no_job_root_is_503(client, checkpoints, monkeypatch):
    monkeypatch.delenv(launch.JOB_ROOTS_ENV)
    answer = post(client, form())
    assert answer.status_code == 503
    assert answer.json()["detail"]["code"] == "no_job_root"


def test_a_read_only_root_is_reported(client, roots, checkpoints):
    roots.chmod(0o500)
    try:
        if os.access(roots, os.W_OK):
            pytest.skip("running as a user who ignores permissions")
        answer = post(client, form())
        assert answer.status_code == 503
        assert answer.json()["detail"]["code"] == "job_write_failed"
    finally:
        roots.chmod(0o700)
