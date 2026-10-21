"""Review findings of the fast start (L1, the fake machine), one test group each:

* the unit of a one-shot check is unique per call, not per second.

The fake machine answers as the real commands print (``stack_act_machine``)."""

import pytest
from stack_act_machine import ActMachine
from test_marvel0_deploy import forbid_real_machine  # noqa: F401 - the guard

from levi.marvel0 import stack_units


@pytest.fixture
def m(tmp_path):
    return ActMachine(tmp_path)


def oneshot_units(m):
    """The unit names of the one-shot checks run so far, in order."""
    return [
        next(a for a in call if a.startswith("--unit=")).removeprefix("--unit=")
        for call in m.calls
        if call[0] == "systemd-run" and "--pipe" in call
    ]


# --- the unit of a one-shot check is unique per call ---------------------------------------------


def test_two_checks_of_one_kind_in_the_same_second_get_different_units(m):
    m.m_ready()
    nodes = m.specs.nodes_cmd
    inner, begun = [], []

    def meanwhile(cmd):
        # (a second check of the same kind, in the same second, while the first
        # one still runs: the early check of B and a person's click on the FCI
        # check, which asks about B as well)
        if not begun:
            begun.append(True)
            inner.append(m.act._oneshot(nodes, "probe-b-nodes", "second", 8))

    m.on_oneshot = meanwhile
    code, _ = m.act._oneshot(nodes, "probe-b-nodes", "first", 8)
    assert code == 0 and inner[0][0] == 0, "neither check met the other's unit"
    names = oneshot_units(m)
    assert len(names) == 2 and len(set(names)) == 2, names
    prefix = stack_units.ONESHOT_PREFIX + "probe-b-nodes-"
    assert all(n.startswith(prefix) for n in names), names
    # (systemd's unit names allow letters, digits and - _ . : only)
    assert all(n.replace("-", "").replace("_", "").isalnum() for n in names), names


def test_a_click_on_the_fci_check_during_the_early_check_of_b_loses_nothing(m):
    """The collision this closes: the second ``systemd-run`` failed ("already
    loaded"), its check read as no answer, the slow 20 s question was taken, and
    the record of the check of B was written by whoever finished last."""
    m.m_ready()
    clicked = []

    def click(cmd):
        if " node list " in " ".join(cmd) and not clicked:
            clicked.append(m.act.preflight_a({"request_id": "click-fci-0000001"}))

    m.on_oneshot = click
    m.act.probe_b({"request_id": "early-probe-0000001"})
    assert clicked and clicked[0]["done"] == "preflight_started"
    assert m.oneshots_of("list_controllers") == [], "the slow question was not needed"
    assert len(m.oneshots_of("node list")) == 2
    assert m.act._b_probe["by"] == "nodes" and m.act._b_probe["state"] == "absent"
    assert m.act._fci["state"] == "open"
