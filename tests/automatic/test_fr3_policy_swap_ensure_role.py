"""``SwitchingPolicy.ensure_role`` (design STEP5-AERI-RESET-DESIGN, R1 / P1b): the orchestrator makes the reset
service the running one at home before the reverse loop, so that the episodes of the loop, which may start away from
home, find it running: a switch needs the arm at home (``arm_not_home``), a role already running does not."""

from types import SimpleNamespace as NS

import pytest
from fr3_swap_fakes import HOME, refuse_unit_tools
from test_fr3_policy_swap import DEPLOYMENTS, World

from levi.automatic import fr3_bridge as B
from levi.automatic import fr3_policy_swap as S


@pytest.fixture(autouse=True)
def no_unit_tools(monkeypatch):
    refuse_unit_tools(monkeypatch)


def make_policy(w):
    class Client:
        def __init__(self, role):
            self.role = role

        def get_server_metadata(self):
            return dict(DEPLOYMENTS[self.role])

    def handshake(role):
        w.handshakes.append(role)
        return Client(role)

    w.switcher.handshake = handshake
    module = NS(_check_server=lambda *a: None, _check_prompt=lambda *a: None)
    return S.SwitchingPolicy(
        w.switcher,
        {"forward": Client("forward")},
        DEPLOYMENTS,
        {"forward": "f", "reset": "r"},
        NS(raw=None),
        module,
        NS(),
        None,
    )


def started(w):
    return [next(a for a in call if a.startswith("--unit="))[7:] for call in w.starts()]


def test_ensure_role_switches_at_home_once_and_is_idempotent_afterwards(tmp_path):
    w = World(tmp_path)
    w.switcher.startup("forward")
    w.bind()
    policy = make_policy(w)
    assert isinstance(policy, B.Policy)
    found = policy.ensure_role("reset")
    assert found == {"kind": "ready", "role": "reset", "switched": True}
    assert started(w) == ["levi-aeri-policy-forward", "levi-aeri-policy-reset"]
    assert policy.ensure_role("reset")["switched"] is False
    # An episode's acquire of the running role is no second switch.
    assert policy.acquire("reset", policy_epoch=1)["role"] == "reset"
    assert len(started(w)) == 2 and w.hook_errors == []
    assert w.records().count("committed") == 2  # the start-up and the one switch


def test_the_arm_away_from_home_refuses_the_switch_and_latches(tmp_path):
    w = World(tmp_path)
    w.switcher.startup("forward")
    w.bind()
    policy = make_policy(w)
    w.pose = [x + 0.05 for x in HOME]
    found = policy.ensure_role("reset")
    assert found["kind"] == "unavailable" and found["fault"] is True
    assert found["code"] == "policy_switch_fault" and "arm_not_home" in found["detail"]
    assert started(w) == ["levi-aeri-policy-forward"]
    assert w.switcher.latched
    again = policy.ensure_role("reset")  # latched: refused without systemd
    assert again["kind"] == "unavailable" and again["fault"] is True


def test_after_the_switch_at_home_an_episode_away_from_home_needs_no_switch(tmp_path):
    # Why the call comes before the loop: an acquire that had to switch away from
    # home is refused, one that finds the service running is not.
    w = World(tmp_path)
    w.switcher.startup("forward")
    w.bind()
    policy = make_policy(w)
    assert policy.ensure_role("reset")["switched"] is True
    w.pose = [x + 0.05 for x in HOME]  # the second subtask starts where the first ended
    assert policy.acquire("reset", policy_epoch=2)["role"] == "reset"
    assert not w.switcher.latched

    late = World(tmp_path / "late")
    late.switcher.startup("forward")
    late.bind()
    late_policy = make_policy(late)
    late.pose = [x + 0.05 for x in HOME]
    refused = late_policy.acquire("reset", policy_epoch=1)
    assert refused["kind"] == "unavailable" and "arm_not_home" in refused["detail"]


def test_a_query_still_running_is_a_refusal_not_a_fault(tmp_path):
    import threading

    w = World(tmp_path)
    w.switcher.startup("forward")
    w.bind()
    policy = make_policy(w)
    policy.ticket = NS(done=threading.Event())
    assert policy.ensure_role("reset") == {
        "kind": "unavailable",
        "code": "query_still_running",
    }
    assert started(w) == ["levi-aeri-policy-forward"] and not w.switcher.latched
