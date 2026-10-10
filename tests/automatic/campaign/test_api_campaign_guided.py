# ruff: noqa: F401, F811
"""A guided legacy-client campaign through the HTTP interface (T-API-2): the
to-do sequence (switch the policy, place the cards, run the command, say
when it is done), the card answers and the report. Nothing starts a policy
server or touches a robot port: the campaign guard fails the test if
anything tries."""

import json

from campaign_guard import aeri_home_fixture, guard_fixture
from test_api_campaign_common import (
    URL,
    wait_for,
    world,
    write_rollout,
)

GROUPS = {"A": "pi05_a", "B": "recap_b"}


def started(world, **over):
    job = world.make_job(name="people", strategy="human_assisted")
    body = {
        "primary": {"metric": "success", "label_basis": "operator_label", "alpha": 0.05}
    }
    body.update(over)
    answer, plan = world.start(job, **body)
    assert answer.status_code == 202, answer.text
    return answer.json()["campaign_id"], plan


def todo_of(world, cid, kind, segment):
    return wait_for(
        lambda: (
            (s := world.snapshot(cid))["segment"]["no"] == segment
            and s["todo"]
            and s["todo"]["kind"] == kind
            and s["todo"]
        ),
        describe=lambda: world.dump(cid),
    )


class Client:
    """The legacy client: writes the rollouts a copied command would make."""

    def __init__(self, world):
        self.world = world
        self.number = {}

    def run(self, cid, plan, segment, todo, count, **labels):
        arm = plan["segments"][segment - 1]["arms"][0]
        group = GROUPS[arm]
        note = todo["eval_note"]
        keys = []
        for _ in range(count):
            n = self.number[group] = self.number.get(group, 0) + 1
            write_rollout(
                self.world.rollouts,
                group,
                n,
                note=note,
                run_id=f"run-s{segment}",
                operator=labels.get("operator", "success"),
                agent=labels.get("agent", "success"),
            )
            keys.append(f"{group}/pick/demo_{n:04d}")
        return keys


def test_a_guided_campaign_walks_the_to_do_list_to_a_report(world):
    cid, plan = started(world)
    client = Client(world)
    switches = 0
    for number in range(1, 5):
        snap = world.wait_state(cid, ["POLICY_READY", "ENV_CONFIRM", "ARM_RUNNING"])
        if (
            snap["todo"]
            and snap["todo"]["kind"] == "switch_policy"
            and snap["segment"]["no"] == number
        ):
            switches += 1
            todo = snap["todo"]
            assert todo["checkpoint"] in GROUPS.values()
            assert todo["arm_code"] == snap["segment"]["arm_code"]
            wrong = world.confirm(cid, todo, f"sw-bad-{number}", kind="env")
            assert wrong.status_code == 409
            answer = world.confirm(cid, todo, f"sw-{number}")
            assert answer.json().get("result") == "applied", (
                answer.text,
                world.dump(cid),
            )
        todo = todo_of(world, cid, "place_cards", number)
        assert todo["cards"] == plan["segments"][number - 1]["cards"]
        command = todo["command"]
        arm = plan["segments"][number - 1]["arms"][0]
        assert "--levi-mode dual" in command
        assert f"--eval-num {len(todo['cards'])}" in command
        assert f"--rollout-group {GROUPS[arm]}" in command
        assert (
            f'--eval-note "{plan["campaign_id"]} s{number:02d} {todo["arm_code"]}"'
            in command
        )
        assert (
            todo["eval_note"]
            == f"{plan['campaign_id']} s{number:02d} {todo['arm_code']}"
        )
        assert world.confirm(cid, todo, f"env-{number}").json()["result"] == "applied"
        running = todo_of(world, cid, "segment_done", number)
        assert running["command"] == command and running["progress"] == {
            "done": 0,
            "planned": 2,
        }
        keys = client.run(cid, plan, number, running, 2)
        wait_for(
            lambda n=number: (
                world.snapshot(cid)["state"] != "ARM_RUNNING"
                or world.snapshot(cid)["segment"]["no"] != n
            )
        )
        for row in world.client.get(f"{URL}/{cid}/cards").json()["pending"]:
            if row["key"] in keys:
                answer = world.client.post(
                    f"{URL}/{cid}/cards",
                    json={
                        "command_id": "card-" + row["key"].replace("/", "-"),
                        "episode_key": row["key"],
                        "card": row["candidate_card"],
                    },
                )
                assert answer.status_code == 200, answer.text
    # The same arm twice in a row is not switched again.
    assert switches == 3, world.dump(cid)
    done = world.wait_state(cid, "REPORTED")
    assert {a["done"] for a in done["arms"]} == {4}
    assert {a["unconfirmed"] for a in done["arms"]} <= {0, 2}
    report = world.client.get(f"{URL}/{cid}/report")
    assert report.status_code == 200, report.text
    assert report.json()["basis"] == "operator_label"
