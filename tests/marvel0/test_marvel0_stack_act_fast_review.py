"""Review findings of the fast start (L1, the fake machine), one test group each:

* the unit of a one-shot check is unique per call, not per second;
* a person's own operation that runs makes the guided start wait without
  touching the machine-wide lock;
* a stop that comes while the FCI port is being asked drops that answer;
* the answer "no gripper stack runs" that the graph gave once is not enough to
  start B: a second answer is needed (the groups below it).

The fake machine answers as the real commands print (``stack_act_machine``)."""

import pytest
from stack_act_machine import (
    GRIPPER_NODES,
    MONITOR_NODE,
    NO_MANAGER,
    NO_MANAGER_CODE,
    ActMachine,
    node_list_print,
)
from test_marvel0_deploy import forbid_real_machine  # noqa: F401 - the guard
from test_marvel0_stack_act import start_sequence

from levi.marvel0 import stack, stack_act, stack_units


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
    # (two answers of the graph for each of the two checks)
    assert len(m.oneshots_of("node list")) == 4
    assert m.act._b_probe["by"] == "nodes" and m.act._b_probe["state"] == "absent"
    assert m.act._fci["state"] == "open"


# --- a person's own operation holds the start back, without the lock -----------------------------


def test_the_start_waits_for_a_persons_operation_and_does_not_touch_the_lock(m):
    m.m_ready()
    assert start_sequence(m) == {"done": "sequence_started"}
    for _ in range(6):
        if m.act.advance()["step"] == "probe":
            break
    assert m.act._seq["step"] == "probe" and not m.fci_probes
    # a person's stop of B runs (a main operation: not a check beside the others)
    person = m.act._begin("stop_b", "b")
    locks = []
    real = m.sup._locked
    m.sup._locked = lambda action, act: locks.append(action) or real(action, act)
    for _ in range(3):
        shown = m.act.advance()
    m.sup._locked = real
    assert (shown["state"], shown["step"], shown["reason"]) == (
        "running",
        "probe",
        None,
    ), "the start waits, it does not fail with operation_running"
    assert locks == [], "the start did not take the machine-wide lock meanwhile"
    assert not m.fci_probes, "and asked the arm's port nothing"
    # the person's operation ends: the start goes on at the next look
    m.act._finish(person, True)
    shown = m.act.advance()
    assert m.fci_probes and shown["state"] == "running"


# --- a stop that comes during the FCI check drops its answer -------------------------------------


def test_a_stop_during_the_fci_check_drops_the_answer(m):
    m.m_ready()

    def fci(ip, port):
        m.act._begin("stop_a", "a")  # (the stop supersedes the check that runs)
        return {"state": "open"}

    m.act.host.fci_probe = fci
    m.act.preflight_a({"request_id": "stop-fci-00000001"})
    assert m.act._fci is None, "an answer for a stack that is going was kept"
    check = next(o for o in m.act._ops if o["kind"] == "preflight_a")
    assert (check["state"], check["code"]) == ("failed", "superseded")
    assert "gate.fci_probe" in m.blockers("a")


# --- B starts only on a confirmed absence: one answer of the graph is not enough ----------------

GRAPH_PRESENT = node_list_print([MONITOR_NODE, *GRIPPER_NODES])
GRAPH_ABSENT = node_list_print([MONITOR_NODE])


def graph_checks(m):
    return m.oneshots_of("node list")


def press_chain(m, rid="chain-review-01", **extra):
    return m.act.sequence_start(m.chain_body(rid, **extra))


def advance_to(m, step, limit=40):
    """Look until the guided start is at ``step``, the machine answering as a
    healthy one would, and no further."""
    up = set()
    seq = {}
    for _ in range(limit):
        seq = m.act.advance()
        if seq["state"] != "running" or seq["step"] == step:
            return seq
        if seq.get("step") == "wait":
            m.t += 4  # (the marker's minimum age is 3 s)
            m.no_manager()
        m.chain_world(up, ("a",))
    raise AssertionError(f"never reached {step}: {seq}")


def test_the_check_a_start_waits_for_is_one_answer_that_does_not_yet_start_b(m):
    """A's start uses the first answer (it must not wait for two), and that answer
    alone is not enough for B."""
    m.m_ready()
    press_chain(m)
    seq = advance_to(m, "await_a")
    assert len(m.systemd_runs("a")) == 1, "A started on the first answer"
    assert len(graph_checks(m)) == 1
    assert m.act._b_probe["state"] == "absent" and m.act._b_probe["by"] == "nodes"
    assert m.act._b_probe["confirmed"] is False
    assert (m.terminal("b")["state"], m.terminal("b")["reason"]) == (
        "idle",
        "b.not_running",
    )
    assert seq["state"] == "running"


def test_the_second_answer_is_asked_while_a_starts_up_and_b_follows_at_once(m):
    m.m_ready()
    press_chain(m)
    advance_to(m, "await_a")
    seen = []
    m.on_systemd_run = lambda argv: seen.append(
        (
            next(a for a in argv if a.startswith("--unit=")),
            m.act._b_probe and m.act._b_probe["confirmed"],
        )
    )
    m.act.advance()  # A is not ready yet: the second check is made beside it
    assert len(graph_checks(m)) == 2
    assert m.act._b_probe["confirmed"] is True and m.act._seq["step"] == "await_a"
    m.chain_world(set(), ("a",))  # A comes up
    seq = m.run_chain(ready=("a", "b", "c"))
    assert seq["state"] == "done", seq
    assert len(graph_checks(m)) == 2, "two answers, no third"
    b_unit = stack.STACK_UNITS["B"].removesuffix(".service")
    assert (f"--unit={b_unit}", True) in seen, "B started on a confirmed absence"
    steps = {i["step"]: i["state"] for i in seq["steps"]}
    assert steps["b_probe"] == "done" and steps["b"] == "done"


def test_a_press_on_b_with_one_unconfirmed_answer_checks_again_and_starts_nothing(m):
    m.start_a()
    m.act._b_probe = {
        "at": m.t,
        "state": "absent",
        "by": "nodes",
        "graph": "absent",
        "confirmed": False,
    }
    b = m.terminal("b")
    # the button is there to press, and says what the press will do
    assert (b["state"], b["reason"], b["can_start"]) == ("idle", "b.not_running", True)
    assert [(r["code"], r["level"]) for r in b["start_blockers"]] == [
        ("b_probe_unconfirmed", "warn")
    ]
    m.oneshots.clear()
    with pytest.raises(stack.Refuse) as caught:
        m.act.start("b", m.start_body("b"))
    assert caught.value.code == "b_probe_started"
    assert m.systemd_runs("b") == [], "nothing is started on one answer of the graph"
    assert len(graph_checks(m)) == 1 and m.act._b_probe["confirmed"] is True
    assert m.terminal("b")["start_blockers"] == []
    assert m.act.start("b", m.start_body("b", "start-b-0002"))["done"] == "started"


def test_a_gripper_stack_that_shows_up_in_the_second_answer_is_final(m):
    m.m_ready()
    first = []

    def graph(cmd):
        if " node list " in " ".join(cmd):
            first.append(1)
            # (the stack has come up, or was only not discovered yet, by now)
            m.nodes_out = GRAPH_PRESENT if len(first) > 1 else GRAPH_ABSENT

    m.on_oneshot = graph
    press_chain(m)
    seq = m.run_chain(ready=("a",))
    assert m.act._b_probe["state"] == "present"
    assert (seq["state"], seq["reason"]) in (
        ("failed", "gate.b_absent"),
        ("failed", "b_not_seen"),
    )
    assert m.systemd_runs("b") == [], "B was not started on top of a gripper stack"
    assert m.systemd_runs("a") and m.terminal("a")["state"] == "ready"


def test_a_check_a_person_asks_for_ends_on_a_gripper_stack_its_second_answer_sees(m):
    """Nothing fresh goes before this check, so it makes two answers of the graph;
    the second one is final when it sees a gripper stack."""
    m.m_ready()

    def graph(cmd):
        if " node list " in " ".join(cmd):
            m.nodes_out = GRAPH_ABSENT if len(graph_checks(m)) == 1 else GRAPH_PRESENT

    m.on_oneshot = graph
    m.act.probe_b({"request_id": "review-probe-00003"})
    assert len(graph_checks(m)) == 2 and m.oneshots_of("list_controllers") == []
    assert m.act._b_probe["state"] == "present" and m.act._b_probe["by"] == "nodes"
    assert m.terminal("b")["state"] == "foreign_running"


def test_a_second_answer_that_proves_nothing_goes_on_to_the_controller_manager(m):
    m.m_ready()

    def graph(cmd):
        if " node list " in " ".join(cmd):
            m.nodes_out = GRAPH_ABSENT if len(graph_checks(m)) == 1 else ""

    m.on_oneshot = graph
    m.act.probe_b({"request_id": "review-probe-00004"})
    assert len(graph_checks(m)) == 2 and len(m.oneshots_of("list_controllers")) == 1
    got = m.act._b_probe
    assert (got["state"], got["by"], got["graph"]) == ("absent", "probe", "unknown")
    assert got["confirmed"] is True  # (the manager's own word)


def test_a_second_answer_that_proves_nothing_goes_to_the_controller_manager(m):
    m.m_ready()
    m.nodes_out = GRAPH_ABSENT

    def second(cmd):
        if " node list " in " ".join(cmd) and len(graph_checks(m)) >= 2:
            m.nodes_out = ""  # (discovery had not run this time)

    m.on_oneshot = second
    m.controllers_out, m.controllers_code = NO_MANAGER, NO_MANAGER_CODE
    press_chain(m)
    seq = m.run_chain(ready=("a", "b", "c"))
    assert seq["state"] == "done", seq
    assert len(m.oneshots_of("list_controllers")) >= 1
    assert len(graph_checks(m)) == 2 and len(m.systemd_runs("b")) == 1


def test_a_stale_first_answer_is_not_a_second_one(m):
    m.m_ready()
    m.act._b_probe = {
        "at": m.t - stack_act.B_PROBE_TTL_S - 5,
        "state": "absent",
        "by": "nodes",
        "confirmed": True,
    }
    op = m.act._begin("probe_b", "b")
    got = m.act._probe_b(m.specs, op, confirm=False)
    m.act._finish(op, True)
    assert got["state"] == "absent" and got["confirmed"] is False
    # (and now the answer before it is fresh: the next one confirms it)
    op = m.act._begin("probe_b", "b")
    assert m.act._probe_b(m.specs, op, confirm=False)["confirmed"] is True


def test_the_graph_alone_is_two_answers_when_a_person_asks(m):
    m.m_ready()
    m.act.probe_b({"request_id": "review-probe-00001"})
    assert len(graph_checks(m)) == 2
    assert m.act._b_probe["confirmed"] is True and m.act._b_probe["by"] == "nodes"


def test_the_controller_managers_word_and_b_s_own_stop_need_no_second_answer(m):
    m.m_ready()
    m.nodes_out = ""  # (the graph proves nothing: the manager is asked)
    m.act.probe_b({"request_id": "review-probe-00002"})
    assert m.act._b_probe["by"] == "probe" and m.act._b_probe["confirmed"] is True
    assert len(graph_checks(m)) == 1, "a graph that proves nothing is not asked twice"
    # B's own stop
    m.nodes_out = None
    m.start_a()
    m.start_b()
    m.act.stop("b", m.stop_body("b"))
    assert m.act._b_probe["by"] == "stop" and m.act._b_probe["confirmed"] is True


def test_a_start_that_stops_at_a_gets_its_second_answer_while_it_waits(m):
    """Only a chain that goes straight on to B asks for the second answer while A
    starts up; one that stops at A (or pauses before B) asks while it waits for
    the person, where nothing else is going on."""
    m.m_ready()
    start_sequence(m)  # (through A only)
    advance_to(m, "await_a")
    assert len(graph_checks(m)) == 1
    m.act.advance()  # A is not ready yet
    assert len(graph_checks(m)) == 1, "not beside A's start-up"
    m.chain_world(set(), ("a",))
    seq = m.act.advance()
    assert seq["state"] == "waiting_confirm" and seq["next"] == "b"
    assert len(graph_checks(m)) == 1
    m.act.advance()  # (waiting for the person: the second answer is made now)
    assert len(graph_checks(m)) == 2 and m.act._b_probe["confirmed"] is True
