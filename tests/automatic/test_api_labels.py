# ruff: noqa: F401, F811, RUF059
"""The operator's blind label through the HTTP interface (T-API-1): nothing
shows how the automatic system judged an episode before the operator gave
a success or failure label."""

import json

import pytest
from test_api_common import (
    client,
    finished_run,
    home,
    roots,
)

from levi.automatic import api, launch, metrics
from levi.automatic.journal import Journal

BASE = "/api/levi/automatic"
VERDICTS = ("success", "failure", "undecided", "none", "unknown")


def episodes_of(run_id):
    entry = launch.read_record(launch.index_path(run_id))
    return list(metrics.ended_forward(Journal.read(entry["run_dir"]).events))


def label(client, run_id, episode, value, **extra):
    return client.post(
        f"{BASE}/runs/{run_id}/labels",
        json={"episode_id": episode, "value": value, **extra},
    )


@pytest.fixture
def run(client, tmp_path, roots):
    run_id = finished_run(client, tmp_path, roots, name="r-lab")
    return run_id, episodes_of(run_id)


def test_a_decided_label_reveals_only_its_own_episode(client, run):
    run_id, (first, second) = run
    before = client.get(f"{BASE}/runs/{run_id}/events", params={"limit": 500}).json()
    assert api.HIDDEN in {e["reason"] for e in before["events"]}
    answer = label(client, run_id, first, "success")
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["revealed"] is True
    assert body["label"]["value"] == "success" and body["label"]["by"] == "ui"
    assert set(body) == {"label", "revealed", "card"}
    # The card describes the last ended episode: the second, still blind.
    card = body["card"]
    assert card["operator_label"]["episode_id"] == second
    assert card["operator_label"]["automatic_verdict"] is None
    assert card["operator_label"]["hidden_until_labelled"] is True
    after = client.get(f"{BASE}/runs/{run_id}/events", params={"limit": 500}).json()
    hidden = [e for e in after["events"] if e["reason"] == api.HIDDEN]
    assert (
        0
        < len(hidden)
        < len([e for e in before["events"] if e["reason"] == api.HIDDEN])
    )
    # Labelling the second one reveals its verdict on the card.
    second_answer = label(client, run_id, second, "failure").json()
    shown = second_answer["card"]["operator_label"]
    assert second_answer["revealed"] is True
    assert shown["hidden_until_labelled"] is False
    assert shown["automatic_verdict"] in VERDICTS
    # All labelled: the run's own rates are no longer withheld.
    report = client.get(f"{BASE}/runs/{run_id}/metrics").json()
    assert report["withheld"] == [] and report["autonomous"] is not None


def test_discarded_and_unclear_reveal_nothing(client, run):
    run_id, (first, _) = run
    for value in ("discarded", "unclear"):
        body = label(client, run_id, first, value).json()
        assert body["revealed"] is False
        assert body["label"]["value"] == value
        assert json.dumps(body["card"]).count("automatic_verdict") == 1
    events = client.get(f"{BASE}/runs/{run_id}/events", params={"limit": 500}).json()
    assert api.HIDDEN in {e["reason"] for e in events["events"]}


def test_a_label_can_be_corrected_and_stays_revealed(client, run):
    run_id, (first, _) = run
    assert label(client, run_id, first, "success").json()["revealed"] is True
    again = label(client, run_id, first, "failure")
    assert again.status_code == 200 and again.json()["revealed"] is True
    assert again.json()["label"]["supersedes"] == 1


def test_the_same_request_id_gives_the_same_answer(client, run):
    run_id, (first, second) = run
    one = label(client, run_id, first, "success", request_id="req-lab-1")
    two = label(client, run_id, first, "success", request_id="req-lab-1")
    assert one.json() == two.json()
    other = label(client, run_id, first, "failure", request_id="req-lab-1")
    assert other.status_code == 409
    assert other.json()["detail"]["code"] == "request_id_used"
    lines = metrics.LabelStore(
        launch.read_record(launch.index_path(run_id))["run_dir"]
    ).lines("operator_label")
    assert len(lines) == 1


def test_only_ended_forward_episodes_take_a_label(client, run):
    run_id, (first, _) = run
    unknown = first.replace(first.rsplit(":", 1)[-1], "e9999")
    assert label(client, run_id, "not an episode", "success").status_code == 422
    assert label(client, run_id, "../x", "success").status_code == 422
    missing = label(client, run_id, unknown, "success")
    assert missing.status_code in (409, 422)
    reset = label(client, run_id, first.replace("forward", "reset"), "success")
    assert reset.status_code in (409, 422)
    assert label(client, run_id, first, "maybe").status_code == 422
    assert not metrics.LabelStore(
        launch.read_record(launch.index_path(run_id))["run_dir"]
    ).lines("operator_label")


def test_only_a_person_labels(client, run):
    run_id, (first, _) = run
    bearer = client.post(
        f"{BASE}/runs/{run_id}/labels",
        json={"episode_id": first, "value": "success"},
        headers={"authorization": "Bearer abc"},
    )
    assert bearer.status_code == 403
    assert bearer.json()["detail"]["code"] == "person_only"
    client.headers.pop("x-levi-ui-token")
    assert label(client, run_id, first, "success").status_code == 401


def test_nothing_reveals_the_verdict_before_the_label(client, run):
    """Every route that could carry the verdict, read before any label."""
    run_id, _ = run
    texts = [
        client.get(f"{BASE}/runs/{run_id}").text,
        client.get(f"{BASE}/runs/{run_id}/events", params={"limit": 500}).text,
    ]
    joined = "\n".join(texts)
    for word in ("goal_verified", "horizon_exhausted", "operator_stop"):
        assert word not in joined, word
    report = client.get(f"{BASE}/runs/{run_id}/metrics").json()
    assert report["autonomous"] is None and report["early_termination"] is None
    # How the episodes ended is a hint of the verdict too (an early stop
    # means the detector fired): not before the label.
    assert report["agreement"]["by_ended_by"] is None
    assert report["agreement"]["total"]["operator_decided"] == 0
    snapshot = client.get(f"{BASE}/runs/{run_id}").json()
    assert snapshot["pending_card"] is None
