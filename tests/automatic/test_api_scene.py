# ruff: noqa: F401, F811, RUF059
"""The person's answer to a scene question through the HTTP interface
(T-API-1): the file protocol of ``adapters.human``."""

import json
import os
from pathlib import Path

import pytest
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
)

from levi.automatic import api, launch
from levi.automatic.adapters import human

DIGEST = "ab" * 32


@pytest.fixture
def ask(client, roots, tmp_path, fake_systemd):
    """A waiting human-assisted run with one open scene question."""
    write_job(roots, tmp_path, name="r-scene", strategy="human_assisted")
    job_id = job_id_of(client, "r-scene")
    plan = plan_of(client, job_id, overrides={"scenes": ["reset_required"]})
    assert post_run(client, plan, job_id).status_code == 202
    run_dir = Path(launch.read_record(launch.index_path("r-scene"))["run_dir"])
    served = Served(run_dir, plan["plan_sha256"])
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
    client.headers.update(REQ)
    client.post(
        f"{BASE}/runs/r-scene/stop", json={"command_id": "stop-end", "confirm": "stop"}
    )
    served.thread.join(30)


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
