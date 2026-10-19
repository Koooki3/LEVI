"""``levi/rig/catalog_api.py``: ``GET /api/levi/rig/models``, ``GET
/api/levi/rig/site`` and ``POST /api/levi/rig/command/preview``. A router on
its own with a temporary site file and checkpoints folder; the preview
executes and writes nothing, and no response but the preview's text to copy
(and the missing-file message) names a path on the machine."""

import json
import os
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from site_sample import make_checkpoint, make_checkpoints, write_site
from test_rig_command import PROMPT, golden_argv

from levi.automatic import policies
from levi.rig import catalog_api, command, models
from levi.rig import site as rig_site

MODELS = "/api/levi/rig/models"
SITE = "/api/levi/rig/site"
PREVIEW = "/api/levi/rig/command/preview"
# An absolute path in a text: a slash that starts a word, then folders.
PATH_IN_TEXT = re.compile(r"(?<![\w.:-])/(?:[A-Za-z0-9_.~+@%=,-]+/)+")


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A fake rig: a site file, a checkpoints folder, and a client on a router
    that has only the routes under test."""
    make_checkpoints(tmp_path)
    monkeypatch.setenv("LEVI_RIG_SITE", str(write_site(tmp_path)))
    rig_site.clear_cache()
    models.cache_clear()
    app = FastAPI()
    app.include_router(catalog_api.router)
    yield TestClient(app), tmp_path
    rig_site.clear_cache()
    models.cache_clear()


def preview(client, **body):
    return client.post(
        PREVIEW, json={"model_id": "pi05_fr3_all_step49999", "prompt": PROMPT, **body}
    )


def tree(path):
    return sorted(
        (str(p.relative_to(path)), p.stat().st_mtime_ns) for p in path.rglob("*")
    )


# --- GET /models ------------------------------------------------------------------------


def test_models_answers_the_contract(machine):
    client, _ = machine
    reply = client.get(MODELS)
    assert reply.status_code == 200
    doc = reply.json()
    assert set(doc) == {"schema", "models", "skipped", "problems", "defaults"}
    assert doc["schema"] == "levi.rig.models.v1"
    assert [m["id"] for m in doc["models"]] == [
        "pi05_fr3_all_step49999",
        "recap_cfg_r1_step10000_jax",
        "recap_cfg_r2_best_step14300_jax",
        "recap_cfg_r2_step15000_jax",
    ]
    assert doc["models"][0]["verified"] is True and doc["models"][1]["labels"] == [
        "unverified"
    ]
    assert {"id": "reset_r1_step20000", "reason": "excluded_by_site"} in doc["skipped"]
    assert doc["defaults"] == {"rtc": True, "fraction": 0.22, "cfg_guidance_scale": 1.0}


def test_models_reads_the_folder_once_in_thirty_seconds(machine, monkeypatch):
    client, _ = machine
    calls = []
    real = policies.discover_report

    def counting(root, **kwargs):
        calls.append(root)
        return real(root, **kwargs)

    monkeypatch.setattr(policies, "discover_report", counting)
    for _ in range(3):
        assert client.get(MODELS).status_code == 200
    assert len(calls) == 1


def test_models_shows_a_new_checkpoint_after_the_cache_is_cleared(machine):
    client, tmp_path = machine
    assert len(client.get(MODELS).json()["models"]) == 4
    make_checkpoint(
        tmp_path / "openpi" / "checkpoints", "new_jax", config="pi05_fr3_all_state"
    )
    models.cache_clear()
    assert "new_jax" in [m["id"] for m in client.get(MODELS).json()["models"]]


def test_models_with_a_missing_folder_answers_a_problem(machine):
    client, tmp_path = machine
    import shutil

    shutil.rmtree(tmp_path / "openpi")
    doc = client.get(MODELS).json()
    assert doc["models"] == [] and doc["problems"] == ["root_missing"]


# --- GET /site -----------------------------------------------------------------------------


def test_site_answers_the_sanitised_view(machine):
    client, tmp_path = machine
    reply = client.get(SITE)
    assert reply.status_code == 200
    doc = reply.json()
    assert doc["schema"] == "levi.rig.site_view.v1" and doc["source"] == "env"
    assert doc["names"] == {"openpi_checkout": "openpi", "rollout_root": "rollouts"}
    assert doc["cameras"] == {"view": "262622274256", "wrist": "262622275490"}
    assert doc["fixed_flags"] == rig_site.fixed_flags(rig_site.require())
    assert str(tmp_path) not in reply.text


# --- POST /command/preview ------------------------------------------------------------------


def test_preview_answers_the_documented_command(machine):
    client, tmp_path = machine
    reply = preview(client)
    assert reply.status_code == 200
    doc = reply.json()
    assert set(doc) == {
        "schema",
        "ok",
        "model",
        "server_requirements",
        "cwd",
        "env",
        "argv",
        "command",
        "warnings",
        "hands",
    }
    assert doc["schema"] == "levi.rig.command.v1" and doc["ok"] is True
    assert doc["model"] == {
        "id": "pi05_fr3_all_step49999",
        "config": "pi05_fr3_all_state",
        "restricted_prompts": False,
        "verified": True,
    }
    assert doc["server_requirements"] == {
        "model_id": "pi05_fr3_all_step49999",
        "config": "pi05_fr3_all_state",
        "rtc": True,
    }
    assert doc["argv"] == golden_argv(tmp_path)
    assert doc["cwd"] == str(tmp_path / "openpi")
    assert doc["env"] == ["no_proxy=localhost,127.0.0.1,::1"]
    assert doc["command"].startswith(
        f"cd {tmp_path / 'openpi'} && \\\n  env -u VIRTUAL_ENV"
    )
    assert [w["code"] for w in doc["warnings"]] == ["min_z_below_task_table"]
    assert doc["hands"] == command.HANDS


def test_preview_with_rtc_off_tells_the_server_it_needs(machine):
    client, _ = machine
    doc = preview(client, rtc=False, rtc_horizon=4).json()
    assert doc["server_requirements"]["rtc"] is False
    assert "--open-loop-horizon=4" in doc["argv"] and "--rtc" not in doc["argv"]


def test_preview_of_an_unverified_model_warns(machine):
    client, _ = machine
    doc = preview(client, model_id="recap_cfg_r2_best_step14300_jax").json()
    assert "unverified_model" in [w["code"] for w in doc["warnings"]]
    assert doc["model"]["verified"] is False


def test_preview_changes_nothing_on_disk(machine):
    client, tmp_path = machine
    before = tree(tmp_path)
    assert preview(client).status_code == 200
    assert preview(client, rtc=False).status_code == 200
    assert tree(tmp_path) == before


def test_preview_uses_no_process_and_no_socket(machine, monkeypatch):
    client, _ = machine
    import socket
    import subprocess

    def forbidden(*args, **kwargs):
        raise AssertionError("the preview started a process or opened a socket")

    for owner, name in (
        (subprocess, "run"),
        (subprocess, "Popen"),
        (subprocess, "check_output"),
        (os, "system"),
        (socket, "create_connection"),
    ):
        monkeypatch.setattr(owner, name, forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    assert preview(client).status_code == 200


REFUSED = [
    ({"model_id": "no_such_model"}, "model_unknown", "model_id"),
    ({"model_id": "reset_r1_step20000"}, "model_unknown", "model_id"),
    ({"prompt": ""}, "prompt_invalid", "prompt"),
    ({"prompt": "pick 'it'"}, "prompt_invalid", "prompt"),
    ({"prompt": "-pick"}, "prompt_invalid", "prompt"),
    ({"max_steps": 0}, "max_steps_out_of_range", "max_steps"),
    ({"max_steps": 3001}, "max_steps_out_of_range", "max_steps"),
    ({"eval_num": 51}, "eval_num_out_of_range", "eval_num"),
    ({"min_z": 0}, "min_z_not_positive", "min_z"),
    ({"min_z": 0.01}, "min_z_below_floor", "min_z"),
    ({"min_z": 0.3}, "min_z_above_home", "min_z"),
    ({"rtc_horizon": 9}, "rtc_horizon_out_of_range", "rtc_horizon"),
    ({"rtc": False, "rtc_horizon": 11}, "rtc_horizon_out_of_range", "rtc_horizon"),
    ({"rollout_group": "../x"}, "rollout_group_invalid", "rollout_group"),
    ({"note": "a$b"}, "note_invalid", "note"),
    ({"note": ""}, "note_invalid", "note"),
]


@pytest.mark.parametrize("body,code,field", REFUSED)
def test_preview_refuses_with_the_code_and_the_field(machine, body, code, field):
    client, tmp_path = machine
    reply = preview(client, **body)
    assert reply.status_code == 422
    detail = reply.json()["detail"]
    assert detail["code"] == code and detail["field"] == field and detail["message"]
    assert str(tmp_path) not in reply.text


def test_a_bad_prompt_names_its_reason(machine):
    client, _ = machine
    assert preview(client, prompt="a$b").json()["detail"]["reason"] == "forbidden_char"


def test_a_restricted_model_refuses_an_unknown_instruction(machine):
    client, tmp_path = machine
    make_checkpoint(
        tmp_path / "openpi" / "checkpoints",
        "pi05_fr3_sp_v1",
        config="pi05_fr3_all_state",
    )
    models.cache_clear()
    reply = preview(client, model_id="pi05_fr3_sp_v1", prompt="wave at the camera")
    assert reply.status_code == 422
    assert reply.json()["detail"]["code"] == "prompt_unknown_for_model"
    ok = preview(
        client,
        model_id="pi05_fr3_sp_v1",
        prompt="stack the plates of same color together",
    )
    assert ok.status_code == 200 and ok.json()["model"]["restricted_prompts"] is True


@pytest.mark.parametrize(
    "body",
    [
        {"levi_mode": "on"},
        {"max_xy": 5.0},
        {"home_pose": [0, 0, 0, 0, 0, 0]},
        {"rollout_root": "/tmp/elsewhere"},
        {"allow_unknown_prompt": True},
        {"remote_port": 9},
        {"extra": 1},
    ],
)
def test_preview_takes_no_field_beyond_the_form(machine, body):
    client, _ = machine
    reply = preview(client, **body)
    assert reply.status_code == 422
    assert isinstance(reply.json()["detail"], list)  # FastAPI's own shape


@pytest.mark.parametrize(
    "body",
    [
        {"max_steps": "5"},
        {"max_steps": 5.5},
        {"max_steps": True},
        {"eval_num": "5"},
        {"min_z": "0.03"},
        {"min_z": True},
        {"rtc": "yes"},
        {"rtc": 1},
        {"rtc_horizon": "6"},
        {"prompt": 5},
        {"note": ["x"]},
    ],
)
def test_preview_bodies_are_strictly_typed(machine, body):
    client, _ = machine
    assert preview(client, **body).status_code == 422


def test_preview_needs_a_model_and_a_prompt(machine):
    client, _ = machine
    assert client.post(PREVIEW, json={"prompt": PROMPT}).status_code == 422
    assert (
        client.post(PREVIEW, json={"model_id": "pi05_fr3_all_step49999"}).status_code
        == 422
    )
    assert client.post(PREVIEW, json={}).status_code == 422


def test_preview_takes_a_whole_number_where_a_float_is_expected(machine):
    client, _ = machine
    assert preview(client, min_z=1).json()["detail"]["code"] == "min_z_above_home"


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity"])
def test_a_number_that_is_not_finite_is_not_a_min_z(machine, number):
    client, _ = machine
    reply = client.post(
        PREVIEW,
        content=f'{{"model_id": "pi05_fr3_all_step49999", "prompt": "x", "min_z": {number}}}',
        headers={"content-type": "application/json"},
    )
    assert reply.status_code == 422
    assert reply.json()["detail"]["code"] == "invalid_type"
    assert reply.json()["detail"]["field"] == "min_z"


def test_a_failed_self_check_is_a_500_with_a_code_and_no_command(machine, monkeypatch):
    client, tmp_path = machine

    def broken(argv, site, restricted):
        raise command.SelfCheckError("test")

    monkeypatch.setattr(command, "verify_argv", broken)
    reply = preview(client)
    assert reply.status_code == 500
    assert reply.json()["detail"]["code"] == "command_self_check"
    assert str(tmp_path) not in reply.text


def test_wrong_methods_are_refused(machine):
    client, _ = machine
    assert client.post(MODELS).status_code == 405
    assert client.post(SITE).status_code == 405
    assert client.get(PREVIEW).status_code == 405
    assert client.delete(MODELS).status_code == 405


# --- the site file is missing or invalid ----------------------------------------------------------


def test_every_route_answers_site_missing_with_the_path_and_a_template(
    machine, monkeypatch
):
    client, tmp_path = machine
    missing = tmp_path / "gone.toml"
    monkeypatch.setenv("LEVI_RIG_SITE", str(missing))
    for reply in (client.get(MODELS), client.get(SITE), preview(client)):
        assert reply.status_code == 412
        assert reply.json()["detail"] == {
            "code": "site_missing",
            "message": "rig site file missing",
            "looked_at": str(missing),
            "template": rig_site.TEMPLATE,
        }


def test_every_route_answers_site_invalid_with_the_errors(machine, monkeypatch):
    client, tmp_path = machine
    monkeypatch.setenv(
        "LEVI_RIG_SITE", str(write_site(tmp_path, name="bad.toml", server={"port": 0}))
    )
    for reply in (client.get(MODELS), client.get(SITE), preview(client)):
        assert reply.status_code == 412
        assert reply.json()["detail"] == {
            "code": "site_invalid",
            "message": "rig site file is invalid",
            "errors": [{"field": "server.port", "code": "range"}],
        }
        assert str(tmp_path) not in reply.text


def test_a_missing_site_reads_nothing_else(machine, monkeypatch):
    client, tmp_path = machine
    monkeypatch.setenv("LEVI_RIG_SITE", str(tmp_path / "gone.toml"))

    def forbidden(*args, **kwargs):
        raise AssertionError("read something other than the site file")

    monkeypatch.setattr(policies, "discover_report", forbidden)
    assert client.get(MODELS).status_code == 412
    assert preview(client).status_code == 412


def test_the_site_is_found_again_after_it_is_created(machine, monkeypatch):
    client, tmp_path = machine
    wanted = tmp_path / "later.toml"
    monkeypatch.setenv("LEVI_RIG_SITE", str(wanted))
    assert client.get(SITE).status_code == 412
    write_site(tmp_path, name="later.toml")
    assert client.get(SITE).status_code == 200


# --- no machine path in an answer ------------------------------------------------------------------


def test_no_answer_but_the_preview_text_names_a_path(machine, monkeypatch):
    client, tmp_path = machine
    answers = [
        client.get(MODELS),
        client.get(SITE),
        preview(client, prompt="a$b"),
        preview(client, model_id="nope"),
        preview(client, max_steps=0),
    ]
    monkeypatch.setenv(
        "LEVI_RIG_SITE", str(write_site(tmp_path, name="bad.toml", server={"port": 0}))
    )
    answers += [client.get(MODELS), client.get(SITE)]
    for reply in answers:
        assert str(tmp_path) not in reply.text, reply.request.url
        assert not PATH_IN_TEXT.search(reply.text), reply.request.url


def test_the_preview_holds_a_path_only_in_cwd_command_and_argv(machine):
    client, tmp_path = machine
    doc = preview(client).json()
    for key in ("cwd", "command", "argv"):
        assert str(tmp_path) in json.dumps(doc[key])
    rest = {k: v for k, v in doc.items() if k not in {"cwd", "command", "argv"}}
    assert str(tmp_path) not in json.dumps(rest)
    assert not PATH_IN_TEXT.search(json.dumps(rest))
