# ruff: noqa: F401, F811
"""Guided campaigns, the cases off the main path (T-API-2): a segment the
person calls done early, card answers, what a dry-run campaign refuses."""

from campaign_guard import aeri_home_fixture, guard_fixture
from test_api_campaign_common import (
    URL,
    wait_for,
    world,
    write_rollout,
)
from test_api_campaign_guided import Client, started, todo_of


def walk_to_running(world, cid, number, plan):
    snap = world.wait_state(cid, ["POLICY_READY", "ENV_CONFIRM", "ARM_RUNNING"])
    if snap["todo"] and snap["todo"]["kind"] == "switch_policy":
        assert (
            world.confirm(cid, snap["todo"], f"sw-{number}").json()["result"]
            == "applied"
        )
    todo = todo_of(world, cid, "place_cards", number)
    assert world.confirm(cid, todo, f"env-{number}").json()["result"] == "applied"
    return todo_of(world, cid, "segment_done", number)


def test_a_segment_called_done_early_waits_for_the_persons_decision(world):
    cid, plan = started(world)
    running = walk_to_running(world, cid, 1, plan)
    Client(world).run(cid, plan, 1, running, 1)
    wait_for(
        lambda: (
            ((world.snapshot(cid)["todo"] or {}).get("progress") or {}).get("done") == 1
        )
    )
    done = world.confirm(cid, running, "done-1")
    assert done.json()["result"] == "applied", done.text
    held = world.wait_todo(cid, "recover_run")
    assert held["state"] == "WAIT_HUMAN"
    assert held["wait_reason"] == "segment_short"
    assert held["todo"]["reason"] == "segment_short"
    # Without the person's word the short segment does not count as done.
    accepted = world.confirm(
        cid, held["todo"], "short-1", kind="env", options={"accept_short_segment": True}
    )
    assert accepted.json()["result"] == "applied"
    nxt = world.wait_state(cid, ["POLICY_READY", "ENV_CONFIRM"])
    assert nxt["segment"]["no"] == 2
    arms = {a["code"]: a for a in nxt["arms"]}
    assert sum(a["done"] for a in arms.values()) == 1


def test_card_answers_are_checked_and_can_be_taken_back(world):
    cid, plan = started(world)
    running = walk_to_running(world, cid, 1, plan)
    keys = Client(world).run(cid, plan, 1, running, 2)
    pending = wait_for(lambda: world.client.get(f"{URL}/{cid}/cards").json()["pending"])
    assert [p["key"] for p in pending] == keys
    assert all(p["candidate_card"] in plan["segments"][0]["cards"] for p in pending)

    def answer(command, key, card):
        return world.client.post(
            f"{URL}/{cid}/cards",
            json={"command_id": command, "episode_key": key, "card": card},
        )

    assert answer("k-1", "nope/pick/demo_0001", "c001").status_code == 422
    assert answer("k-2", keys[0], "c999").status_code == 422
    first = answer("k-3", keys[0], pending[0]["candidate_card"])
    assert first.status_code == 200 and first.json()["result"] == "applied"
    assert (
        answer("k-3", keys[0], pending[0]["candidate_card"]).json()["result"]
        == "repeated"
    )
    left = world.client.get(f"{URL}/{cid}/cards").json()["pending"]
    assert [p["key"] for p in left] == keys[1:]
    taken_back = answer("k-4", keys[0], None)
    assert taken_back.status_code == 200
    again = world.client.get(f"{URL}/{cid}/cards").json()["pending"]
    assert [p["key"] for p in again] == keys


def test_a_dry_run_campaign_has_no_card_answers_and_no_policy_switch(world):
    job = world.make_job()
    answer, _ = world.start(job, execution_mode="dry_run")
    cid = answer.json()["campaign_id"]
    snap = world.wait_todo(cid, "place_cards")
    assert world.client.get(f"{URL}/{cid}/cards").status_code == 409
    post = world.client.post(
        f"{URL}/{cid}/cards",
        json={"command_id": "k-1", "episode_key": "k", "card": "c1"},
    )
    assert post.status_code == 409
    wrong = world.confirm(cid, snap["todo"], "sw-1", kind="switch_policy")
    assert wrong.status_code == 409
    done = world.confirm(cid, snap["todo"], "sd-1", kind="segment_done")
    assert done.status_code == 409


def test_a_guided_campaign_never_starts_or_asks_a_policy_server(world, aeri_home):
    """The backend only records what the person has to do: no unit, no
    socket (the campaign guard would fail the test), and the one record of a
    wished policy is a file."""
    cid, _ = started(world)
    snap = world.wait_state(cid, ["POLICY_READY"])
    assert snap["todo"]["kind"] == "switch_policy"
    wanted = aeri_home / "campaigns" / cid / "ctl" / "wanted.json"
    assert wanted.exists()
    assert not (aeri_home / "campaigns" / cid / "ctl" / "serving.json").exists()
