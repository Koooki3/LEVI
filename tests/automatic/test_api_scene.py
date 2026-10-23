# ruff: noqa: F401, F811, RUF059
"""The person's answer to a scene question through the HTTP interface
(T-API-1): the file protocol of ``adapters.human``."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import scaled
from test_api_common import (
    REQ,
    client,
    home,
    job_id_of,
    roots,
    wait_for,
    write_job,
)
from test_api_runs import (
    BASE,
    Served,
    fake_systemd,
    plan_of,
    post_run,
    stop_by_page,
)

from levi.automatic import api, launch
from levi.automatic.adapters import human

DIGEST = "ab" * 32


@pytest.fixture
def ask(client, roots, tmp_path, fake_systemd, request):
    """A waiting human-assisted run with one open scene question."""
    write_job(roots, tmp_path, name="r-scene", strategy="human_assisted")
    job_id = job_id_of(client, "r-scene")
    plan = plan_of(client, job_id, overrides={"scenes": ["reset_required"]})
    assert post_run(client, plan, job_id).status_code == 202
    run_dir = Path(launch.read_record(launch.index_path("r-scene"))["run_dir"])
    served = Served(run_dir, plan["plan_sha256"])
    # Register cleanup before any setup assertion: pytest does not resume a
    # yield fixture whose setup failed, but always runs its finalizers.
    request.addfinalizer(
        lambda: served.close(
            stop_by_page(client, "r-scene", "stop-end"), timeout=scaled(15), strict=True
        )
    )
    wait_for(lambda: client.get(f"{BASE}/runs/r-scene").json()["state"] == "WAIT_HUMAN")
    frame = "cd" * 32
    question = {
        "request_id": "scene-q1",
        "nonce": "n0nce-value",
        "run_id": "r-scene",
        "episode_id": "r-scene.forward.0001",
        "contract": {"id": "c", "version": "1", "status": "confirmed"},
        "predicates": [
            {
                "name": "object_at_source",
                "text": "the object is at the source",
                "required": True,
            },
            {"name": "gripper_open", "text": "the gripper is open", "required": False},
        ],
        "frames": [
            {"ref": "side:1", "sha256": frame, "file": f"evidence/frames/{frame}.jpg"}
        ],
        "frames_sha256": DIGEST,
        "asked_ns": 1,
        "answers": ["true", "false", "null"],
    }
    transport = human.FileTransport(run_dir / "scene")
    transport.ask(question)
    yield client, run_dir, transport, question


def test_scene_setup_failure_still_closes_runner_and_command_pump(
    client, roots, tmp_path, fake_systemd, monkeypatch
):
    """Fail before yield after the runner starts; execute pytest's finalizer."""
    finalizers = []
    started = []
    original_served, original_wait = Served, wait_for

    def served(*args):
        value = original_served(*args)
        started.append(value)
        return value

    def fail_after_ready(predicate):
        original_wait(predicate)
        raise RuntimeError("injected scene setup failure")

    monkeypatch.setitem(ask.__wrapped__.__globals__, "Served", served)
    monkeypatch.setitem(ask.__wrapped__.__globals__, "wait_for", fail_after_ready)
    fixture = ask.__wrapped__(
        client,
        roots,
        tmp_path,
        fake_systemd,
        SimpleNamespace(addfinalizer=finalizers.append),
    )
    try:
        with pytest.raises(RuntimeError, match="injected scene setup failure"):
            next(fixture)
        assert len(finalizers) == 1 and len(started) == 1
    finally:
        try:
            for finish in reversed(finalizers):
                finish()
        finally:
            # Even a regression which loses the finalizer must not leak this
            # test's runner into later tests. The assertion above still fails.
            for value in started:
                if value.thread.is_alive() or value.pumps():
                    value.close(stop_by_page(client, "r-scene", "stop-fallback"))
    assert not started[0].thread.is_alive()
    assert started[0].pumps() == [] and not started[0].forced


def answer(client, **over):
    body = {
        "request_id": "scene-q1",
        "nonce": "n0nce-value",
        "frames_sha256": DIGEST,
        "predicates": {"object_at_source": True},
        **over,
    }
    return client.post(f"{BASE}/runs/r-scene/scene-answer", json=body)


def test_the_snapshot_shows_the_open_question(ask):
    client, run_dir, transport, question = ask
    shown = client.get(f"{BASE}/runs/r-scene").json()["scene_question"]
    assert shown["request_id"] == "scene-q1" and shown["nonce"] == "n0nce-value"
    assert shown["frames_sha256"] == DIGEST and shown["frames"] == ["cd" * 32]
    assert [p["name"] for p in shown["predicates"]] == [
        "object_at_source",
        "gripper_open",
    ]
    assert shown["predicates"][0]["required"] is True
    assert isinstance(shown["asked_at"], int) and shown["timeout_s"] > 0
    assert str(run_dir) not in json.dumps(shown)


def test_an_answer_is_written_for_the_provider(ask):
    client, run_dir, transport, question = ask
    response = answer(
        client, predicates={"object_at_source": True, "gripper_open": None}
    )
    assert response.status_code == 202
    assert response.json() == {"request_id": "scene-q1", "accepted": True}
    taken = transport.take_answers()
    assert len(taken) == 1
    assert taken[0]["request_id"] == "scene-q1"
    assert taken[0]["nonce"] == "n0nce-value" and taken[0]["frames_sha256"] == DIGEST
    assert taken[0]["predicates"] == {"object_at_source": True, "gripper_open": None}
    # The same request again: the same answer, no second file.
    again = answer(client, predicates={"object_at_source": True, "gripper_open": None})
    assert again.json() == response.json() and transport.take_answers() == []


def test_a_real_runs_question_is_answered_only_at_its_terminal(ask, monkeypatch):
    """Decision UI-2: for a run with the same criterion as the terminal-only
    resume (registered by scripts/aeri.py) the page's answer is refused
    before anything is written; the snapshot says so for the page."""
    client, run_dir, transport, question = ask
    monkeypatch.setattr(api, "_terminal_resume_only", lambda run: True)
    snap = client.get(f"{BASE}/runs/r-scene").json()
    assert snap["scene_answer_terminal_only"] is True
    assert snap["scene_question"]["request_id"] == "scene-q1"  # still shown
    response = answer(client)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "scene_answer_terminal_only"
    assert "UI-2" in detail["message"] and "scripts/aeri.py" in detail["message"]
    assert transport.take_answers() == []
    assert not list((run_dir / "scene" / "answers").glob("**/*.json"))


def test_a_run_that_is_not_real_still_takes_the_pages_answer(ask):
    client, run_dir, transport, question = ask
    assert client.get(f"{BASE}/runs/r-scene").json()["scene_answer_terminal_only"] is (
        False
    )
    assert answer(client).status_code == 202


@pytest.mark.parametrize(
    "over",
    [
        {"request_id": "other-q"},
        {"nonce": "wrong"},
        {"frames_sha256": "00" * 32},
    ],
)
def test_an_answer_to_no_open_question_is_unsolicited(ask, over):
    client, run_dir, transport, _ = ask
    response = answer(client, **over)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "unsolicited"
    assert transport.take_answers() == []


def test_the_answer_must_fit_the_question(ask):
    client, run_dir, transport, _ = ask
    missing = answer(client, predicates={"gripper_open": True})
    assert missing.status_code == 422
    assert (
        missing.json()["detail"]["errors"][0]["field"] == "predicates.object_at_source"
    )
    extra = answer(client, predicates={"object_at_source": True, "invented": False})
    assert extra.status_code == 422
    assert extra.json()["detail"]["errors"][0]["field"] == "predicates.invented"
    for bad in ({"object_at_source": "yes"}, {"object_at_source": 1}, {}):
        assert answer(client, predicates=bad).status_code == 422
    assert transport.take_answers() == []


def test_a_request_id_is_never_a_path(ask):
    client, run_dir, transport, _ = ask
    for bad in ("../x", "a/b", "..", "x" * 300):
        response = client.post(
            f"{BASE}/runs/r-scene/scene-answer",
            json={
                "request_id": bad,
                "nonce": "n",
                "frames_sha256": DIGEST,
                "predicates": {"a": True},
            },
        )
        assert response.status_code == 422
    assert transport.take_answers() == []


def test_a_linked_question_file_is_not_followed(ask, tmp_path):
    client, run_dir, transport, question = ask
    secret = tmp_path / "evil.json"
    secret.write_text(json.dumps({**question, "request_id": "linked-q"}))
    os.symlink(secret, run_dir / "scene" / "questions" / "linked-q.json")
    response = answer(client, request_id="linked-q")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "unsolicited"


def test_only_a_person_answers(ask):
    client, run_dir, transport, _ = ask
    bearer = client.post(
        f"{BASE}/runs/r-scene/scene-answer",
        json={
            "request_id": "scene-q1",
            "nonce": "n0nce-value",
            "frames_sha256": DIGEST,
            "predicates": {"object_at_source": True},
        },
        headers={"authorization": "Bearer abc"},
    )
    assert bearer.status_code == 403
    client.headers.pop("x-levi-ui-token")
    assert answer(client).status_code == 401
    assert transport.take_answers() == []


def test_the_page_cannot_send_an_episode_verdict(ask, monkeypatch):
    """L2-AERI-OPERATOR-EARLY-STOP, as UI-2: the operator's s/f/d is typed at
    the run's terminal only. There is no route for it, and the command
    channel refuses one (real run or not) before any inbox file exists."""
    from levi.automatic import control

    client, run_dir, transport, question = ask
    for real in (False, True):
        monkeypatch.setattr(api, "_terminal_resume_only", lambda run, real=real: real)
        body = {"command_id": "verdict-page", "label": "success"}
        response = client.post(f"{BASE}/runs/r-scene/episode-verdict", json=body)
        assert response.status_code in (404, 405)
        value = {
            "schema": control.COMMAND_SCHEMA,
            "command_id": "verdict-page",
            "kind": "episode_verdict",
            "principal_id": "operator",
            "issued_wall_ns": 1,
        }
        with pytest.raises(control.CommandError) as refused:
            control.write_command(run_dir, value)
        assert refused.value.code == "terminal_only"
    inbox = run_dir / "control" / "inbox"
    assert not list(inbox.glob("verdict-page*")) if inbox.exists() else True
    # A file put in the inbox by hand is refused by the runner, not carried out.
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "verdict-hand.json").write_text(
        json.dumps({**value, "command_id": "verdict-hand"})
    )
    found = control.wait_result(run_dir, "verdict-hand", 10)
    assert (
        found is not None and found["ok"] is False and found["code"] == "terminal_only"
    )
    assert "terminal" in found["detail"]
