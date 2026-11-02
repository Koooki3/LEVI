"""The speech gate against the real run supervisor (``run.Supervisor`` on the
scripted client of ``test_marvel0_run.py``), not a stand-in for its document:
the hosted client waiting at its start gate leaves speech open; every other
state of the run, and a second connection, close it again. The route-level
cases with a fake supervisor are in ``test_marvel0_speech_route.py``."""

import pytest
from test_marvel0_run import make_machine  # noqa: F401 - the fixture
from test_marvel0_run_supervisor import at_gate, judging, running
from test_marvel0_speech_route import policy_status

from levi.marvel0 import run_api, speech_api

# The policy server's view of a local hosted client: its connection counts
# twice there (both ends of the loopback pair), and the arm lock is LEVI's own.
HOSTED = {"established": 2, "arm": "refuse"}


@pytest.fixture
def hosted(machine, monkeypatch):
    machine.write_net(clients=1)
    monkeypatch.setattr(run_api, "supervisor", lambda: machine.sup)
    return machine


def open_for(m, **status) -> bool:
    """Whether the gate lets a worker that is already loaded run."""
    blocked = speech_api.gate(m.site, policy_status(**{**HOSTED, **status}), True)
    return blocked == []


def test_no_hosted_run_keeps_the_gate_closed(hosted):
    assert speech_api._hosted_at_start_gate() is False
    assert open_for(hosted) is False


def test_a_client_that_is_starting_keeps_the_gate_closed(hosted):
    hosted.start()
    assert hosted.view()["state"] == "starting"
    assert speech_api._hosted_at_start_gate() is False
    assert open_for(hosted) is False


def test_the_client_at_its_start_gate_opens_it_and_the_episode_closes_it(hosted):
    hosted.start()
    view = at_gate(hosted)
    assert view["clients"] == 1
    assert speech_api._hosted_at_start_gate() is True
    assert open_for(hosted) is True
    # the person presses Start next episode: the arm goes home, the policy runs
    running(hosted)
    assert speech_api._hosted_at_start_gate() is False
    assert open_for(hosted) is False
    judging(hosted)
    assert speech_api._hosted_at_start_gate() is False
    assert open_for(hosted) is False
    # the next gate opens it again
    hosted.gate(no=2)
    assert hosted.look()["state"] == "at_gate"
    assert speech_api._hosted_at_start_gate() is True


def test_a_frozen_client_keeps_the_gate_closed(hosted):
    hosted.start()
    at_gate(hosted)
    hosted.hold()
    assert hosted.view()["state"] == "held"
    assert speech_api._hosted_at_start_gate() is False
    assert open_for(hosted) is False


def test_a_second_connection_closes_it_again(hosted):
    hosted.start()
    at_gate(hosted)
    assert speech_api._hosted_at_start_gate() is True
    # another client reaches the policy server: the supervisor holds the run on
    # its next look; until then the count alone already closes the gate
    hosted.write_net(clients=2)
    hosted.sup._cache = None
    assert hosted.sup.view()["clients"] == 2
    assert speech_api._hosted_at_start_gate() is False
    assert open_for(hosted, established=4) is False
    assert hosted.look()["state"] == "held"
    assert speech_api._hosted_at_start_gate() is False


def test_a_policy_server_that_is_starting_still_refuses_at_the_gate(hosted):
    hosted.start()
    at_gate(hosted)
    assert open_for(hosted, state="starting") is False
