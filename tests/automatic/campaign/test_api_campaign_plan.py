# ruff: noqa: F401, F811
"""``POST /campaigns/plan`` and ``POST /campaigns`` (T-API-2): the plan's
shape (interface contract §4), its refusals and the start's checks."""

import os

from campaign_guard import aeri_home_fixture, guard_fixture
from test_api_campaign_common import (
    URL,
    world,
)

from levi.automatic.campaign import conductor as K
from levi.automatic.campaign import controller as C

SHA = "[0-9a-f]{64}"


def test_a_plan_has_the_contract_shape(world):
    job = world.make_job()
    response = world.plan(job)
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["plan_sha256"] == plan["campaign_sha256"]
    assert len(plan["campaign_sha256"]) == 64 and len(plan["settings_sha256"]) == 64
    assert [s["no"] for s in plan["segments"]] == [1, 2, 3, 4]
    assert all(len(s["arms"]) == 1 and s["cards"] for s in plan["segments"])
    assert plan["switches"] == 3
    power = plan["power"]
    assert set(power) >= {"detectable_difference", "n", "rows"}
    assert power["n"] == 4 and power["post_hoc_power"] == "not reported"
    assert plan["refusals"] == []
    codes = {c["code"] for c in plan["checks"]}
    assert {"pairing", "schedule", "guide_command", "rollout_root"} <= codes
    assert all(set(c) == {"code", "ok", "severity", "detail"} for c in plan["checks"])


def test_the_same_request_is_the_same_plan_and_writes_nothing_that_stays(
    world, aeri_home
):
    job = world.make_job()
    first = world.plan(job).json()
    second = world.plan(job).json()
    assert first == second
    assert not (aeri_home / "campaigns").exists()
    scratch = [p for p in aeri_home.iterdir() if p.name.startswith(".plan-")]
    assert scratch == []
    assert not (
        aeri_home / "campaign-jobs" / "campaigns" / first["campaign_id"]
    ).exists()
    # Another seed is another campaign.
    other = world.plan(
        job,
        schedule={"kind": "counterbalanced_segments", "segment_trials": 2, "seed": 8},
    ).json()
    assert other["campaign_sha256"] != first["campaign_sha256"]
    assert other["campaign_id"] != first["campaign_id"]


def test_the_two_hosts_plan_differently(world):
    job = world.make_job()
    guided = world.plan(job).json()
    dry = world.plan(job, execution_mode="dry_run").json()
    assert guided["execution_mode"] == "guided" and dry["execution_mode"] == "dry_run"
    assert guided["campaign_sha256"] != dry["campaign_sha256"]
    assert dry["refusals"] == []


def test_a_human_assisted_job_gets_layout_cards(world):
    job = world.make_job(name="people", strategy="human_assisted")
    plan = world.plan(job).json()
    cards = [c for s in plan["segments"] for c in s["cards"]]
    assert cards and all(c.startswith("c") and c[1:].isdigit() for c in cards)
    # Every arm runs the same cards in a round.
    by_round = {}
    for segment in plan["segments"]:
        by_round.setdefault(tuple(sorted(segment["cards"])), []).append(segment["no"])
    assert all(len(numbers) == 2 for numbers in by_round.values())
    assert plan["power"]["design"] == "paired"


def test_a_plan_with_a_bad_request_is_refused(world):
    job = world.make_job()
    body = world.plan_body(job)
    cases = {
        "unknown checkpoint": {
            "arms": [
                {"id": "A", "checkpoint_id": "nope", "role": "reference"},
                {"id": "B", "checkpoint_id": "recap_b", "role": "candidate"},
            ]
        },
        "same checkpoint twice": {
            "arms": [
                {"id": "A", "checkpoint_id": "recap_b", "role": "reference"},
                {"id": "B", "checkpoint_id": "recap_b", "role": "candidate"},
            ]
        },
        "two references": {
            "arms": [
                {"id": "A", "checkpoint_id": "pi05_a", "role": "reference"},
                {"id": "B", "checkpoint_id": "recap_b", "role": "reference"},
            ]
        },
    }
    for name, over in cases.items():
        response = world.client.post(f"{URL}/plan", json={**body, **over})
        assert response.status_code == 422, name
        detail = response.json()["detail"]
        assert detail["code"] == "campaign_invalid" and detail["errors"], name
    assert (
        world.client.post(f"{URL}/plan", json={**body, "job_id": "j-nope"}).status_code
        == 404
    )
    for bad in (
        {"extra": 1},
        {"trials_per_arm": 0},
        {"arms": body["arms"][:1]},
        {"arms": [{**body["arms"][0], "id": "Z"}, body["arms"][1]]},
        {"execution_mode": "autonomous"},
        {"schedule": {"kind": "nonsense", "seed": 1}},
    ):
        answer = world.client.post(f"{URL}/plan", json={**body, **bad})
        assert answer.status_code == 422, bad


def test_what_forbids_only_the_start_is_a_refusal_of_the_plan(world, monkeypatch):
    job = world.make_job()
    monkeypatch.delenv("LEVI_SETUP_DOC")
    monkeypatch.delenv(C.ROLLOUT_ROOT_ENV)
    plan = world.plan(job).json()
    assert set(plan["refusals"]) == {"guide_unavailable", "rollout_root_missing"}
    assert any(not c["ok"] and c["severity"] == "error" for c in plan["checks"])
    body = world.plan_body(job)
    body.update(
        plan_sha256=plan["plan_sha256"], confirm="start-campaign", request_id="r-1"
    )
    answer = world.client.post(URL, json=body)
    assert answer.status_code == 422
    assert answer.json()["detail"]["code"] == "plan_refused"
    # A dry-run campaign needs neither.
    assert world.plan(job, execution_mode="dry_run").json()["refusals"] == []


def test_a_guide_that_changes_the_command_unsafe_is_refused(world, monkeypatch):
    job = world.make_job()
    monkeypatch.setattr(
        C, "read_guide", lambda: (_ for _ in ()).throw(ValueError("no guide here"))
    )
    plan = world.plan(job).json()
    assert plan["refusals"] == ["guide_unavailable"]
    assert "no guide here" in str(plan["checks"])


def test_the_start_needs_the_person_the_hash_and_a_confirmation(world, aeri_home):
    job = world.make_job()
    body = world.plan_body(job)
    plan = world.plan(job).json()
    good = {
        **body,
        "plan_sha256": plan["plan_sha256"],
        "confirm": "start-campaign",
        "request_id": "r-1",
    }
    # An agent's Bearer credential never starts a campaign.
    bearer = world.client.post(URL, json=good, headers={"authorization": "Bearer x"})
    assert bearer.status_code == 403
    assert bearer.json()["detail"]["code"] == "person_only"
    assert (
        world.client.post(
            URL, json=good, headers={"x-levi-ui-token": "wrong"}
        ).status_code
        == 401
    )
    assert world.client.post(URL, json={**good, "confirm": "start"}).status_code == 422
    stale = world.client.post(URL, json={**good, "plan_sha256": "0" * 64})
    assert stale.status_code == 412
    assert stale.json()["detail"]["code"] == "plan_changed"
    assert not (aeri_home / "campaigns").exists()


def test_a_start_is_idempotent_and_never_repeats_a_campaign(world, aeri_home):
    job = world.make_job()
    first, plan = world.start(job, execution_mode="dry_run")
    assert first.status_code == 202
    cid = first.json()["campaign_id"]
    assert cid == plan["campaign_id"]
    folder = aeri_home / "campaigns" / cid
    assert oct(folder.stat().st_mode & 0o777) == "0o700"
    assert oct((aeri_home / "campaigns").stat().st_mode & 0o777) == "0o700"
    again, _ = world.start(job, execution_mode="dry_run")
    assert again.status_code == 202 and again.json() == first.json()
    other, _ = world.start(job, request_id="req-start-2", execution_mode="dry_run")
    assert other.status_code == 409
    assert other.json()["detail"]["code"] == "campaign_exists"
    # The request id belongs to the first body.
    body = world.plan_body(job, execution_mode="dry_run", trials_per_arm=6)
    plan6 = world.client.post(f"{URL}/plan", json=body).json()
    body.update(
        plan_sha256=plan6["plan_sha256"],
        confirm="start-campaign",
        request_id="req-start-1",
    )
    reused = world.client.post(URL, json=body)
    assert reused.status_code == 409
    assert reused.json()["detail"]["code"] == "request_id_used"


def test_another_campaign_on_the_robot_is_refused(world, aeri_home):
    job = world.make_job()
    held = K.RobotLock(aeri_home, "main", "other-campaign")
    try:
        answer, _ = world.start(job)
        assert answer.status_code == 409
        assert answer.json()["detail"]["code"] == "robot_busy"
        assert not (aeri_home / "campaigns").exists()
    finally:
        held.close()


def test_job_files_of_a_start_are_new_files_only(world, aeri_home):
    job = world.make_job()
    started, _ = world.start(job, execution_mode="dry_run")
    cid = started.json()["campaign_id"]
    folder = aeri_home / "campaign-jobs" / "campaigns" / cid
    names = sorted(p.name for p in folder.iterdir())
    assert "campaign.plan.json" in names and "campaign.yaml" in names
    assert sum("__s0" in n and n.endswith(".yaml") for n in names) == 4
    assert os.path.exists(aeri_home / "campaign-jobs" / C.DRY_CONTRACT_FILE)
