"""Session association proves the rollout first, then every deployment field."""

import json
from types import SimpleNamespace

import pytest
from campaign_guard import aeri_home_fixture, guard_fixture  # noqa: F401

from levi.automatic.campaign import live_session as LS


def setup(tmp_path, monkeypatch):
    plan = {
        "campaign_id": "campaign",
        "campaign": {
            "arms": {
                "A": {
                    "group": "group",
                    "policy_forward": {
                        "config": "config",
                        "checkpoint_dir": "/models/model",
                    },
                }
            }
        },
        "children": [{"segment": 1, "arm": "A"}],
    }
    facts = {"run": [SimpleNamespace(run_id="run", episode_id="group/task/demo_0001")]}
    monkeypatch.setattr(LS.A, "collect", lambda *a: {"facts": facts})
    folder = tmp_path / ".eval_sessions"
    folder.mkdir()
    raw = {
        "schema": "levi.eval.session.v1",
        "group": "group",
        "task_folder": "task",
        "run_id": "run",
        "session_id": "session",
        "state": "running",
        "started_at": 110,
        "updated_at": 119,
        "prompt": "stack the plates",
        "policy": {"config": "config", "checkpoint_dir": "/models/model"},
        "levi": {"enabled": True, "mode": "dual_label"},
    }

    def write(value=raw, name="group__task.json"):
        (folder / name).write_text(json.dumps(value))

    def read(**over):
        return LS.associate(
            plan,
            tmp_path,
            str(tmp_path),
            1,
            prompt="stack the plates",
            not_before=100,
            now=120,
            **over,
        )

    return raw, write, read


def test_linked_public_session_has_complete_identity_without_private_path(
    tmp_path, monkeypatch
):
    _raw, write, read = setup(tmp_path, monkeypatch)
    write()
    out = read()
    assert out["status"] == "linked"
    s = out["session"]
    assert (s["root"], s["group"], s["task_folder"], s["session_id"], s["run_id"]) == (
        str(tmp_path),
        "group",
        "task",
        "session",
        "run",
    )
    assert s["dataset"] is None
    assert "/models/model" not in json.dumps(out)
    assert "href" not in out


@pytest.mark.parametrize(
    "change",
    [
        {"group": "other"},
        {"session_id": ""},
        {"started_at": 99},
        {"prompt": "different"},
        {"updated_at": 130},
        {"policy": {"config": "other", "checkpoint_dir": "/models/model"}},
        {"policy": {"config": "config", "checkpoint_dir": "/other/model"}},
        {"policy": {"config": "config", "checkpoint_dir": "model"}},
        {"levi": {"enabled": True, "mode": "unattended"}},
    ],
)
def test_deployment_mismatch_never_returns_a_session(tmp_path, monkeypatch, change):
    raw, write, read = setup(tmp_path, monkeypatch)
    write({**raw, **change})
    assert read() == {"status": "mismatch", "segment": 1}


def test_no_rollout_no_guess_even_with_a_live_session(tmp_path, monkeypatch):
    _raw, write, read = setup(tmp_path, monkeypatch)
    write()
    monkeypatch.setattr(LS.A, "collect", lambda *a: {"facts": {}})
    assert read()["status"] == "waiting"


def test_multiple_sessions_are_ambiguous_and_ended_is_explicit(tmp_path, monkeypatch):
    raw, write, read = setup(tmp_path, monkeypatch)
    write()
    write({**raw, "session_id": "second"}, "second.json")
    assert read() == {"status": "ambiguous", "segment": 1, "candidates": 2}
    write({**raw, "state": "finished"}, "second.json")
    assert read()["session"]["session_id"] == "session"
    write({**raw, "state": "finished"})
    assert read()["status"] == "ambiguous"
    (tmp_path / ".eval_sessions" / "second.json").unlink()
    assert read()["status"] == "ended"


def test_foreign_run_and_task_never_match(tmp_path, monkeypatch):
    raw, write, read = setup(tmp_path, monkeypatch)
    write({**raw, "run_id": "foreign"})
    assert read()["status"] == "waiting"
    write({**raw, "task_folder": "foreign"})
    assert read()["status"] == "waiting"
