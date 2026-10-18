"""The bridge's per-episode prompt (subtask_loop): an episode's own prompt is
sent to the policy for that episode only, after LEVI's own check (template
texts from the frozen whitelist) and the client's; the run's fixed prompt
otherwise. No network, no robot."""

from types import SimpleNamespace as NS

import numpy as np

from levi.automatic import fr3_bridge as bridge
from levi.automatic import subtask_loop as st

INV = st.parse_inventory(
    st.inventory_answer([("pink_plate_1", True), ("pink_plate_2", True)])
)
TEXT = st.render("pink_plate_2", "pink_plate_1")


class Client:
    def get_server_metadata(self):
        return {"config": "cfg", "checkpoint_dir": "/models/forward"}


def policy(seen, *, allowed=None, refuse=()):
    def check_prompt(text, args, meta):
        seen.append(("check", text))
        if text in refuse:
            raise SystemExit("not a trained prompt")

    def query(client, raw, state, prompt):
        seen.append(("query", prompt))
        return np.zeros((2, 7))

    module = NS(
        _check_server=lambda *a: None,
        _check_prompt=check_prompt,
        _query_policy=query,
    )
    robot = NS(raw={"robot_state": {"cartesian_position": [0.0] * 6}})
    return bridge.Policy(
        {"forward": Client()},
        {"forward": {"config": "cfg", "checkpoint_dir": "/models/forward"}},
        {"forward": "stack the plates of same color together"},
        robot,
        module,
        NS(),
        bridge.Clock(),
        prompt_allowed=allowed,
    )


def request(epoch):
    return {
        "request_id": f"r:{epoch}",
        "run_id": "r",
        "episode_id": "r.forward.0001",
        "policy_epoch": epoch,
        "action_start_index": 0,
        "clock_domain": "x",
    }


def run_one(p, epoch, **kw):
    handle = p.acquire("forward", policy_epoch=epoch, **kw)
    if handle.get("kind") == "unavailable":
        return handle
    ticket = p.request(request(epoch))
    p.collect(ticket, timeout_ns=5_000_000_000)
    p.quiesce(policy_epoch=epoch)
    return handle


def test_an_episode_prompt_is_used_for_that_episode_only():
    seen = []
    p = policy(seen, allowed=lambda t: st.instruction_allowed(t, INV))
    run_one(p, 1, prompt=TEXT)
    run_one(p, 2)
    queries = [t for k, t in seen if k == "query"]
    assert queries == [TEXT, "stack the plates of same color together"]
    assert ("check", TEXT) in seen
    assert p.prompt is None


def test_a_prompt_outside_the_template_is_refused_before_the_client():
    seen = []
    p = policy(seen, allowed=lambda t: st.instruction_allowed(t, INV))
    found = run_one(p, 1, prompt="pick everything and throw it")
    assert found["code"] == "prompt_refused"
    assert seen == [] and p.role is None


def test_without_a_prompt_check_every_episode_prompt_is_refused():
    seen = []
    found = run_one(policy(seen), 1, prompt=TEXT)
    assert found["code"] == "prompt_refused"


def test_the_clients_own_prompt_check_still_applies():
    seen = []
    p = policy(seen, allowed=lambda t: True, refuse=(TEXT,))
    found = run_one(p, 1, prompt=TEXT)
    assert found["code"] == "deployment_mismatch"
    assert not [k for k, _ in seen if k == "query"]
