"""The guided start through B and C ("start whole stack"): ``sequence/start`` with
``through = "c"`` runs M, the marker, the wait and A as before and then B and C
by itself, each by the single start's own ``_do_start`` (every gate again) with
the confirmations the request carried, on the fake machine of
``stack_act_machine.py`` (L1). It pauses before B and C only when asked, ends at
the first step that fails and leaves what is up running, skips what is ready
when pressed again, is superseded by "stop whole stack" and by a cancel, and
never outlives the process.

Nothing here starts a process or opens a socket (the guard of
``test_marvel0_deploy.py``)."""

import pytest
from stack_act_machine import (
    CONTROLLERS_OUT,
    NO_MANAGER,
    NO_MANAGER_CODE,
    OURS,
    ActMachine,
)
from test_marvel0_deploy import forbid_real_machine  # noqa: F401 - the guard
from test_marvel0_stack_act import A_BREAKS, B_BREAKS, C_BREAKS

from levi.marvel0 import record, stack, stack_act


@pytest.fixture
def m(tmp_path):
    return ActMachine(tmp_path)


def refused(call, *args):
    with pytest.raises(stack.Refuse) as caught:
        call(*args)
    return caught.value.code


def press(m, rid="chain-0000001", **extra):
    return m.act.sequence_start(m.chain_body(rid, **extra))


def steps(m):
    return [(i["step"], i["state"]) for i in m.doc()["sequence"]["steps"]]


def states(seq):
    return {i["step"]: i["state"] for i in seq["steps"]}


def runs(m):
    return [
        next(a for a in c if a.startswith("--unit=")).split("=", 1)[1]
        for c in m.systemd_runs()
    ]


def a_record(m, key):
    return record.read_record(m.state_root / key)


def test_one_press_runs_m_a_b_and_c_and_ends_done(m):
    assert press(m) == {"done": "sequence_started"}
    seq = m.run_chain()
    assert (seq["state"], seq["reason"]) == ("done", None)
    assert runs(m) == [
        stack.MONITOR_UNIT,
        *(OURS[k].removesuffix(".service") for k in "abc"),
    ]
    assert {k: m.terminal(k)["state"] for k in "abc"} == {k: "ready" for k in "abc"}
    assert set(states(seq).values()) == {"done"}
    assert seq["through"] == "c" and seq["pauses"] == {"b": False, "c": False}


def test_what_the_chain_started_it_started_with_the_confirmations_of_the_request(m):
    press(m, "chain-record-1")
    m.run_chain()
    assert a_record(m, "a")["attest"] == {"desk_done": True, "estop_hand": True}
    assert a_record(m, "b")["attest"] == {"hands_clear": True}
    assert a_record(m, "c")["attest"] == {
        "estop_hand": True,
        "hands_clear": True,
        "no_client": True,
    }
    assert {a_record(m, k)["request_id"] for k in "abc"} == {"chain-record-1"}
    seq = m.doc()["sequence"]
    assert "attest" not in seq and "request_id" not in seq
    assert not any(k.startswith("_") for k in seq)


def test_the_old_body_keeps_the_old_behaviour(m):
    """No ``through``: the guided start of old, B and C are the person's."""
    body = m.start_body("a", "seq-old-000001") | m.body(
        "sequence_start",
        "seq-old-000001",
        attest={"desk_done": True, "estop_hand": True},
    )
    assert m.act.sequence_start(body) == {"done": "sequence_started"}
    seq = m.doc()["sequence"]
    assert (seq["through"], seq["pauses"]) == ("a", {"b": True, "c": True})
    assert [i["step"] for i in seq["steps"]] == list(stack_act.A_STEPS)
    seq = m.run_chain()
    assert (seq["state"], seq["next"]) == ("waiting_confirm", "b")
    for _ in range(6):
        m.chain_world(set())
        m.act.advance()
    assert m.systemd_runs("b") == [] and m.systemd_runs("c") == []
    assert m.terminal("b")["can_start"] is True
    assert states(m.doc()["sequence"]) == {s: "done" for s in stack_act.A_STEPS}


# --- the confirmations: the whole request is refused for a missing one -------------------------

EVERY = {k: True for k in stack_act.ATTEST_KEYS}


@pytest.mark.parametrize(
    "dropped, named",
    [
        (["desk_done"], "desk_done"),
        (["estop_hand"], "estop_hand"),
        (["hands_clear"], "hands_clear"),
        (["no_client"], "no_client"),
        (["hands_clear", "no_client"], "hands_clear, no_client"),
        (list(EVERY), "desk_done, estop_hand, hands_clear, no_client"),
    ],
)
def test_a_missing_confirmation_of_a_stage_it_will_reach_refuses_the_whole_request(
    m, dropped, named
):
    m.m_ready()
    body = m.chain_body("chain-attest-1")
    body["attest"] = {k: v for k, v in EVERY.items() if k not in dropped}
    with pytest.raises(stack.Refuse) as caught:
        m.act.sequence_start(body)
    assert caught.value.code == "attest_missing"
    assert caught.value.extra["keys"] == named
    # nothing was begun, started or set: not even the marker
    assert m.doc()["sequence"] == {"state": "idle", "kind": None}
    assert m.systemd_runs() == [] and not m.marker_path.exists()


def test_a_confirmation_that_is_not_true_or_not_known_is_refused(m):
    m.m_ready()
    body = m.chain_body("chain-attest-2")
    assert (
        refused(m.act.sequence_start, {**body, "attest": {**EVERY, "hands_clear": 1}})
        == "attest_missing"
    )
    assert (
        refused(m.act.sequence_start, {**body, "attest": {**EVERY, "e_stop": True}})
        == "attest_invalid"
    )


def test_a_pause_asks_for_no_confirmation_of_the_stage_it_waits_before(m):
    m.m_ready()
    only_a = {"desk_done": True, "estop_hand": True}
    no_client = {k: v for k, v in EVERY.items() if k != "no_client"}
    no_hands = {k: v for k, v in EVERY.items() if k != "hands_clear"}
    n = [0]

    def send(attest, **options):
        n[0] += 1
        body = m.chain_body(f"chain-p-{n[0]:07d}", **options)
        return m.act.sequence_start({**body, "attest": attest})

    # both pauses: only A is the chain's own start
    assert send(only_a, pause_before_b=True, pause_before_c=True) == {
        "done": "sequence_started"
    }
    m.act.sequence_cancel({"request_id": "cancel-000001"})
    # pausing before C only: B is the chain's, C is the person's (no no_client) ...
    assert send(no_client, pause_before_c=True) == {"done": "sequence_started"}
    m.act.sequence_cancel({"request_id": "cancel-000002"})
    # ... but B's tick is needed
    with pytest.raises(stack.Refuse) as caught:
        send(no_hands, pause_before_c=True)
    assert caught.value.code == "attest_missing"
    # pausing before B only: C is still the chain's, with all of C's ticks
    with pytest.raises(stack.Refuse) as caught:
        send(no_client, pause_before_b=True)
    assert caught.value.code == "attest_missing"
    assert caught.value.extra["keys"] == "no_client"


@pytest.mark.parametrize(
    "options",
    [
        {"through": "b"},
        {"through": None},
        {"through": ["c"]},
        {"pause_before_b": "yes"},
        {"pause_before_c": 1},
        {"pause_before_c": None},
    ],
)
def test_an_option_that_is_not_one_is_refused_before_anything_is_begun(m, options):
    m.m_ready()
    body = {**m.chain_body("chain-option-1"), **options}
    with pytest.raises(stack.Refuse) as caught:
        m.act.sequence_start(body)
    assert caught.value.code == "sequence_option_invalid"
    assert caught.value.status == 400
    assert m.doc()["sequence"] == {"state": "idle", "kind": None}
    assert m.systemd_runs() == []


# --- a pause: the person's own start of that part, then the chain goes on -----------------------


def test_a_pause_before_b_waits_and_goes_on_by_itself_after_the_persons_b(m):
    press(m, "chain-pause-b1", pause_before_b=True)
    seq = m.run_chain()
    assert (seq["state"], seq["next"], seq["step"]) == ("waiting_confirm", "b", None)
    assert seq["pauses"] == {"b": True, "c": False}
    assert states(seq)["b_probe"] == "pending" and states(seq)["b"] == "pending"
    for _ in range(5):  # B could start; the chain does not
        m.act.advance()
    assert m.systemd_runs("b") == [] and m.systemd_runs("c") == []
    m.start_b()  # the person, with a window of their own
    seq = m.act.advance()
    assert (seq["state"], seq["step"]) == ("running", "c_preflight")
    assert states(seq)["b_probe"] == "skipped" and states(seq)["b"] == "skipped"
    assert a_record(m, "b")["request_id"] == "start-b-0001"  # (not the chain's)
    seq = m.run_chain()
    assert seq["state"] == "done" and m.terminal("c")["state"] == "ready"
    assert a_record(m, "c")["request_id"] == "chain-pause-b1"


def test_a_pause_before_c_starts_b_by_itself_and_waits_for_the_persons_c(m):
    press(m, "chain-pause-c1", pause_before_c=True)
    seq = m.run_chain()
    assert (seq["state"], seq["next"]) == ("waiting_confirm", "c")
    assert m.terminal("b")["state"] == "ready"
    assert a_record(m, "b")["request_id"] == "chain-pause-c1"
    assert states(seq)["b"] == "done" and states(seq)["c_preflight"] == "pending"
    for _ in range(5):
        m.act.advance()
    assert m.systemd_runs("c") == [] and m.oneshots_of("check_fr3_ee_frame") == []
    m.controllers_out, m.controllers_code = CONTROLLERS_OUT, 0
    m.act.preflight_c({"request_id": "pre-c-00000001"})
    m.act.start("c", m.start_body("c"))
    m.c_up()
    seq = m.act.advance()
    assert seq["state"] == "done"
    assert states(seq)["c_preflight"] == "skipped" and states(seq)["c"] == "skipped"


def test_both_pauses_are_the_guided_start_of_old(m):
    press(m, "chain-pause-2", pause_before_b=True, pause_before_c=True)
    seq = m.run_chain()
    assert (seq["state"], seq["next"]) == ("waiting_confirm", "b")
    m.start_b()
    assert m.act.advance()["next"] == "c"
    for _ in range(5):
        m.act.advance()
    assert m.systemd_runs("c") == []


def test_a_pause_that_outlasts_the_clicks_ticks_ends_the_chain_before_it_starts_c(m):
    """The ticks of the click are not good for ever: after a long pause the
    chain does not start C on them; the person starts it (with ticks of their own)."""
    press(m, "chain-late-001", pause_before_b=True)
    m.run_chain()
    wait = m.specs.marker_wait_s
    m.t += (
        stack_act.SEQUENCE_ARM_WINDOW_S
        + wait
        + stack_act.SEQUENCE_A_READY_S
        + stack_act.SEQUENCE_B_READY_S
        + stack_act.SEQUENCE_C_CHECK_S
        + stack_act.SEQUENCE_C_READY_S
        + 10
    )
    m.refresh_health()
    m.act.advance()  # (the optional check of B is made again while waiting)
    m.start_b()
    m.refresh_health()
    seq = m.act.advance()
    assert (seq["state"], seq["step"]) == ("running", "c_preflight")
    m.refresh_health()
    seq = m.act.advance()
    assert (seq["state"], seq["reason"]) == ("cancelled", "expired")
    assert m.systemd_runs("c") == [] and m.oneshots_of("check_fr3_ee_frame") == []
    assert (
        states(seq)["c_preflight"] == "cancelled" and states(seq)["c"] == "not_reached"
    )
    assert m.terminal("a")["state"] == "ready" and m.terminal("b")["state"] == "ready"


# --- the chain ends at the step that fails, and leaves what is up -------------------------------


def to_step(m, step, ready=("a", "b", "c"), **options):
    press(m, **options)
    seq = m.run_chain(ready=ready, stop_at=lambda s: s["step"] == step)
    assert seq["step"] == step, seq
    return seq


def failed_at(m, step, reason, not_reached=()):
    seq = m.act.advance()
    assert (seq["state"], seq["reason"]) == ("failed", reason), seq
    assert states(seq)[step] == "failed"
    for name in not_reached:
        assert states(seq)[name] == "not_reached", name
    assert seq["step"] is None
    return seq


def test_b_that_never_becomes_ready_ends_the_chain_and_leaves_a_up(m):
    press(m)
    m.run_chain(ready=("a",), stop_at=lambda s: s["step"] == "b" and "b" in m.units)
    m.t += m.specs.units["b"].ready_timeout_s + 1
    m.refresh_health()
    failed_at(m, "b", "b_failed", ("c_preflight", "c"))
    assert m.terminal("a")["state"] == "ready"
    assert m.terminal("b")["state"] == "failed" and m.terminal("b")["can_stop"]
    assert m.systemd_runs("c") == [] and not m.stop_calls()
    # ... and nothing more happens
    for _ in range(3):
        assert m.act.advance()["state"] == "failed"


def test_a_b_that_vanishes_while_it_starts_ends_the_chain(m):
    press(m)
    m.run_chain(ready=("a",), stop_at=lambda s: s["step"] == "b" and "b" in m.units)
    del m.units["b"]
    failed_at(m, "b", "b_dead", ("c_preflight", "c"))


def test_a_gripper_stack_that_runs_already_ends_the_chain_with_the_gate(m):
    m.controllers_out, m.controllers_code = CONTROLLERS_OUT, 0
    press(m)
    seq = m.run_chain()
    assert seq["state"] == "failed" and seq["reason"] in ("gate.b_absent", "b_not_seen")
    assert m.systemd_runs("b") == [] and m.terminal("a")["state"] == "ready"


def test_a_check_of_the_gripper_stack_that_gets_no_answer_ends_the_chain(m):
    to_step(m, "b_probe")
    m.t += stack_act.B_PROBE_REFRESH_S + 1  # (the old answer is due again)
    m.refresh_health()
    m.controllers_out, m.controllers_code = "garbage\n", 1
    probes = len(m.oneshots)
    m.act.advance()  # the check is made, in this tick
    # (the graph says nothing, so the controller manager is asked as well)
    assert len(m.oneshots) == probes + 2
    assert len(m.oneshots_of("node list")) == 2  # the early check, and this one
    failed_at(m, "b_probe", "b_not_seen", ("b", "c_preflight", "c"))
    assert m.systemd_runs("b") == []


def test_a_check_of_the_gripper_stack_is_made_when_the_old_one_is_due(m):
    to_step(m, "b_probe")
    m.t += stack_act.B_PROBE_REFRESH_S + 1
    m.refresh_health()
    before = len(m.oneshots)
    m.controllers_out, m.controllers_code = NO_MANAGER, NO_MANAGER_CODE
    assert m.act.advance()["step"] == "b_probe"
    assert len(m.oneshots) == before + 1
    assert m.act.advance()["step"] == "b"
    assert states(m.doc()["sequence"])["b_probe"] == "done"
    assert m.run_chain()["state"] == "done"
    # a fresh one is not asked again: the graph twice (the early check and this
    # one), the controller manager only for the checks before C
    assert len(m.oneshots_of("node list")) == 2
    assert len(m.oneshots_of("list_controllers")) == 1


def test_a_fresh_confirmed_check_of_the_gripper_stack_is_not_made_again(m):
    to_step(m, "b_probe")
    # (a confirmed absence: two answers of the graph, or the manager's own word)
    m.act._b_probe = {**m.act._b_probe, "confirmed": True}
    before = len(m.oneshots)
    assert m.act.advance()["step"] == "b"
    assert len(m.oneshots) == before


def test_the_first_answer_of_the_early_check_is_confirmed_once_before_b(m):
    """The early check gave one answer of the graph (A's start could not wait
    for two); B needs the second, made here when the chain was not given the
    time during A's start-up (A is ready at the first look in this world)."""
    to_step(m, "b_probe")
    assert m.act._b_probe["confirmed"] is False
    before = len(m.oneshots_of("node list"))
    assert m.act.advance()["step"] == "b_probe"
    assert len(m.oneshots_of("node list")) == before + 1  # one: the first is fresh
    assert m.act._b_probe["confirmed"] is True and m.systemd_runs("b") == []
    assert m.act.advance()["step"] == "b"
    assert m.run_chain()["state"] == "done" and len(m.systemd_runs("b")) == 1


# --- the checks before C: one retry when the listing gets no answer -----------------------------


def listing_script(m, answers):
    """The gripper controllers' listing answers ``answers`` in turn (the last
    one stays); the gripper stack's own check, made before B exists, is
    not scripted. Returns the list of listings made after B exists."""
    made = []

    def hook(cmd):
        if "list_controllers" in " ".join(cmd) and "b" in m.units:
            made.append(cmd)
            out = answers[min(len(made), len(answers)) - 1]
            m.controllers_out, m.controllers_code = out

    m.on_oneshot = hook
    return made


GOOD = (CONTROLLERS_OUT, 0)
SILENT = (NO_MANAGER, NO_MANAGER_CODE)
INACTIVE = (CONTROLLERS_OUT.replace(" active", " inactive"), 0)


def test_a_listing_that_gets_no_answer_is_asked_once_more_and_the_chain_goes_on(m):
    made = listing_script(m, [SILENT, GOOD])
    press(m)
    seq = m.run_chain()
    assert seq["state"] == "done", seq
    assert len(made) == 2  # one retry, no more
    assert states(seq)["c_preflight"] == "done"
    assert len(m.oneshots_of("check_fr3_ee_frame")) == 2  # (the checks run again)
    assert m.terminal("c")["state"] == "ready"


def test_a_listing_that_gets_no_answer_twice_ends_the_chain_at_the_checks(m):
    made = listing_script(m, [SILENT])
    press(m)
    seq = m.run_chain()
    assert (seq["state"], seq["reason"]) == ("failed", "c_preflight_failed")
    assert len(made) == 2 and m.systemd_runs("c") == []
    assert states(seq)["c_preflight"] == "failed" and states(seq)["c"] == "not_reached"
    assert m.terminal("a")["state"] == m.terminal("b")["state"] == "ready"


def test_a_listing_that_was_answered_with_a_controller_not_active_is_not_asked_again(m):
    made = listing_script(m, [INACTIVE])
    assert m.act._c_pre is None
    press(m)
    seq = m.run_chain()
    assert (seq["state"], seq["reason"]) == ("failed", "c_preflight_failed")
    assert len(made) == 1
    assert m.act._c_pre["controllers_state"] == "listed"


@pytest.mark.parametrize("what", ["ee frame", "qos"])
def test_a_check_that_answered_no_is_not_asked_again(m, what):
    made = listing_script(m, [GOOD])
    if what == "ee frame":
        m.ee_out, m.ee_code = "  FAIL -- expected franka_hand\n", 1
    else:
        m.qos_text = "x\nreliable\n"
        m.set_control(True)
    press(m)
    seq = m.run_chain()
    assert (seq["state"], seq["reason"]) == ("failed", "c_preflight_failed"), seq
    assert len(made) == 1  # (one run of the three checks)
    assert m.systemd_runs("c") == []


def test_c_is_started_only_with_a_check_made_in_this_chain(m):
    """The check is not skipped for an old one that would still pass."""
    press(m)
    m.run_chain(stop_at=lambda s: s["step"] == "c_preflight")
    assert m.act._c_pre is None
    assert m.act.advance()["step"] == "c_preflight"  # (the op, made in this tick)
    assert m.act._c_pre is not None


# --- C that fails ---------------------------------------------------------------------------


def test_c_that_never_becomes_ready_ends_the_chain_and_leaves_a_and_b_up(m):
    press(m)
    m.run_chain(ready=("a", "b"), stop_at=lambda s: s["step"] == "c" and "c" in m.units)
    m.t += m.specs.units["c"].ready_timeout_s + 1
    m.refresh_health()
    failed_at(m, "c", "c_failed")
    assert {k: m.terminal(k)["state"] for k in "abc"} == {
        "a": "ready",
        "b": "ready",
        "c": "failed",
    }
    assert not m.stop_calls()


def test_a_c_that_vanishes_while_it_starts_ends_the_chain(m):
    press(m)
    m.run_chain(ready=("a", "b"), stop_at=lambda s: s["step"] == "c" and "c" in m.units)
    del m.units["c"]
    failed_at(m, "c", "c_dead")


def test_a_step_that_never_finishes_ends_the_chain(m):
    press(m)
    m.run_chain(ready=("a",), stop_at=lambda s: s["step"] == "b" and "b" in m.units)
    m.t += stack_act.STEP_TIMEOUT_S + 1
    m.refresh_health()
    failed_at(m, "b", "step_timeout", ("c_preflight", "c"))


# --- every gate of the single start, alone, at its step ------------------------------------------


def first_required(m, key):
    return next(
        b["code"] for b in m.terminal(key)["start_blockers"] if b["level"] == "required"
    )


# What the chain does when the gate breaks just before the start: the reason is
# the first blocker the single start would have refused with, except where a
# stage before it is no longer ready (then it is that stage that is lost).
A_OUTCOME = {
    "operation running": "waits",  # (a busy lock or operation is tried again)
    "previous start not closed": "a_dead",  # (the record says A was started: it is gone)
}
B_OUTCOME = {
    "a lost": "waits",  # (A cannot be vouched for: nothing is started, nothing ends)
}
C_OUTCOME = {
    "a gone": "waits",
    "hardware inactive": "waits",  # (A cannot be vouched for either)
    "b gone": "b_lost",
}


@pytest.mark.parametrize("name", sorted(A_BREAKS))
def test_each_gate_of_a_stops_the_chain_before_a_is_started(m, name):
    """The guided start settles some gates by itself in the steps before A
    (``SEQUENCE_SOFT``); at A's own step every one is asked again."""
    to_step(m, "arm")
    A_BREAKS[name][0](m)
    assert A_BREAKS[name][1] in m.blockers("a"), (name, m.blockers("a"))
    expected = A_OUTCOME.get(name) or first_required(m, "a")
    seq = m.act.advance()
    if expected == "waits":
        assert seq["state"] == "running" and seq["step"] == "arm"
    else:
        assert (seq["state"], seq["reason"]) == ("failed", expected), (name, seq)
        assert states(seq)["arm"] == "failed" and states(seq)["b"] == "not_reached"
    assert m.systemd_runs("a") == []


@pytest.mark.parametrize("name", sorted(B_BREAKS))
def test_each_gate_of_b_stops_the_chain_before_b_is_started(m, name):
    to_step(m, "b")
    B_BREAKS[name][0](m)
    expected = B_OUTCOME.get(name)
    if expected is None:
        assert B_BREAKS[name][1] in m.blockers("b")
        expected = first_required(m, "b")
    seq = m.act.advance()
    if expected == "waits":
        assert seq["state"] == "running" and seq["step"] == "b"
    else:
        assert (seq["state"], seq["reason"]) == ("failed", expected), (name, seq)
        assert states(seq)["b"] == "failed" and states(seq)["c"] == "not_reached"
    assert m.systemd_runs("b") == [] and m.systemd_runs("c") == []


@pytest.mark.parametrize("name", sorted(C_BREAKS))
def test_each_gate_of_c_stops_the_chain_before_c_is_started(m, name):
    to_step(m, "c")
    C_BREAKS[name][0](m)
    expected = C_OUTCOME.get(name)
    if expected is None:
        assert C_BREAKS[name][1] in m.blockers("c")
        expected = first_required(m, "c")
    seq = m.act.advance()
    if expected == "waits":
        assert seq["state"] == "running" and seq["step"] == "c"
    else:
        assert (seq["state"], seq["reason"]) == ("failed", expected), (name, seq)
        assert states(seq)["c"] in ("failed", "done") or expected == "b_lost"
    assert m.systemd_runs("c") == []


def test_c_is_never_started_when_a_is_gone_or_b_is_gone(m):
    to_step(m, "c")
    del m.units["a"]
    m.sockets = {"state": "inactive", "detail": ""}
    m.no_manager()
    seq = m.act.advance()
    assert (seq["state"], seq["reason"]) == ("failed", "a_lost")
    assert m.systemd_runs("c") == []


def test_b_is_never_started_when_a_is_gone(m):
    to_step(m, "b")
    del m.units["a"]
    m.sockets = {"state": "inactive", "detail": ""}
    m.no_manager()
    seq = m.act.advance()
    assert (seq["state"], seq["reason"]) == ("failed", "a_lost")
    assert m.systemd_runs("b") == []


def test_a_part_that_cannot_be_vouched_for_for_a_while_is_waited_for(m):
    to_step(m, "c")
    m.t += 30
    m.health(age=60)  # the monitor's file is old: A reads unknown, not lost
    assert m.terminal("a")["state"] == "unknown"
    for _ in range(3):
        seq = m.act.advance()
        assert seq["state"] == "running" and seq["step"] == "c"
    assert m.systemd_runs("c") == []
    m.refresh_health()
    m.health(hardware=True, controller=False, red=False, mode="IDLE")
    assert m.run_chain()["state"] == "done"


# --- pressed again: what is ready is skipped ---------------------------------------------------


def test_pressed_again_after_a_failure_it_starts_only_what_is_missing(m):
    press(m, "chain-again-01")
    m.run_chain(ready=("a",), stop_at=lambda s: s["step"] == "b" and "b" in m.units)
    m.t += m.specs.units["b"].ready_timeout_s + 1
    m.refresh_health()
    failed_at(m, "b", "b_failed")
    # the person fixes the cause: here, stops the failed B (acknowledging it)
    m.act.stop("b", m.stop_body("b"))
    assert m.terminal("b")["state"] in ("idle", "unknown")
    m.refresh_health()
    assert press(m, "chain-again-02") == {"done": "sequence_started"}
    seq = m.doc()["sequence"]
    assert seq["state"] == "running"
    seq = m.run_chain()
    assert seq["state"] == "done", seq
    assert len(m.systemd_runs("a")) == 1  # (A was not started again)
    assert len(m.systemd_runs("b")) == 2 and len(m.systemd_runs("c")) == 1
    assert [states(seq)[s] for s in stack_act.A_STEPS[1:5]] == ["skipped"] * 4
    assert states(seq)["monitor"] == "done" and states(seq)["await_a"] == "done"
    # only M was looked at again: no second marker, no second check of the arm
    assert m.fci_probes == [("10.42.0.157", 1337)]
    assert not m.stop_calls() or len(m.stop_calls()) == 1


def test_pressed_with_everything_up_it_answers_already_started_and_begins_nothing(m):
    press(m)
    assert m.run_chain()["state"] == "done"
    runs = len(m.systemd_runs())
    assert press(m, "chain-again-03") == {"done": "already_started"}
    assert m.doc()["sequence"]["state"] == "done"  # (the finished one is still shown)
    assert len(m.systemd_runs()) == runs


def test_pressed_with_a_up_it_does_not_ask_for_the_confirmations_of_a(m):
    m.start_a()
    ticks = {"hands_clear": True, "estop_hand": True, "no_client": True}
    body = {**m.chain_body("chain-again-04"), "attest": ticks}
    assert m.act.sequence_start(body) == {"done": "sequence_started"}
    seq = m.run_chain()
    assert seq["state"] == "done" and len(m.systemd_runs("a")) == 1


def test_the_guided_start_through_a_still_refuses_an_a_that_is_not_idle(m):
    m.start_a()
    body = m.start_body("a", "seq-old-000009") | m.body(
        "sequence_start",
        "seq-old-000009",
        attest={"desk_done": True, "estop_hand": True},
    )
    assert refused(m.act.sequence_start, body) in (
        "gate.a_absent",
        "gate.arm_sockets_clear",
    )


# --- time: the ticks of a click do not last --------------------------------------------------------


def window_of(m, step):
    ctx = m.act._context(m.sup._read())
    return m.act._window_s(step, ctx)


def test_each_stage_has_a_later_window_than_the_one_before(m):
    m.m_ready()
    wait = m.specs.marker_wait_s
    arm = window_of(m, "arm")
    b = window_of(m, "b")
    c = window_of(m, "c")
    assert arm == stack_act.SEQUENCE_ARM_WINDOW_S + wait
    assert b == arm + stack_act.SEQUENCE_A_READY_S + stack_act.SEQUENCE_B_READY_S
    assert c == b + stack_act.SEQUENCE_C_CHECK_S + stack_act.SEQUENCE_C_READY_S
    assert window_of(m, "b_probe") == b and window_of(m, "c_preflight") == c
    assert window_of(m, "await_a") is None


@pytest.mark.parametrize("step", ["b_probe", "b", "c_preflight", "c"])
def test_a_chain_that_is_late_for_a_stage_does_not_start_it(m, step):
    to_step(m, step)
    # (the click was this long ago; the step itself has been going for a moment)
    m.act._seq["begun_at"] -= window_of(m, step) + 1
    runs = len(m.systemd_runs())
    seq = m.act.advance()
    assert (seq["state"], seq["reason"]) == ("cancelled", "expired")
    assert len(m.systemd_runs()) == runs
    assert states(seq)[step] == "cancelled"


def test_a_chain_in_time_for_every_stage_takes_the_whole_window_it_is_given(m):
    """The named windows are enough for the chain of the fake machine, with room."""
    press(m)
    seq = m.run_chain()
    assert seq["state"] == "done"
    assert m.t - 1800000000.0 < window_of(m, "c")


# --- cancel, "stop whole stack", a restart -------------------------------------------------------


@pytest.mark.parametrize("step", stack_act.SEQUENCE_STEPS)
def test_a_cancel_at_any_step_stops_the_chain_and_stops_nothing(m, step):
    to_step(m, step)
    runs = len(m.systemd_runs())
    assert m.act.sequence_cancel({"request_id": "seq-cancel-001"}) == {
        "done": "sequence_cancelled"
    }
    for _ in range(8):  # the world goes on, the chain does not
        m.chain_world(set())
        seq = m.act.advance()
    assert (seq["state"], seq["reason"]) == ("cancelled", "cancelled")
    assert len(m.systemd_runs()) == runs and not m.stop_calls()
    order = list(stack_act.SEQUENCE_STEPS)
    here = order.index(step)
    got = states(seq)
    assert got[step] == "cancelled"
    assert all(got[s] == "done" for s in order[:here])
    assert all(got[s] == "not_reached" for s in order[here + 1 :])


@pytest.mark.parametrize(
    "step", ["wait", "await_a", "b_probe", "b", "c_preflight", "c"]
)
def test_stop_whole_stack_takes_the_place_of_the_chain_and_it_starts_nothing_more(
    m, step
):
    to_step(m, step)
    m.refresh_health()
    runs = len(m.systemd_runs())
    old = m.act._seq
    if m.terminal("a")["owned"]:
        answer = m.act.sequence_stop(m.stop_all_body("stop-all-chain1"))
        assert answer == {"done": "sequence_stop_started"}
    else:
        assert m.act.sequence_stop(m.stop_all_body("stop-all-chain1")) == {
            "done": "already_stopped"
        }
    assert (old["state"], old["reason"]) == ("cancelled", "superseded")
    shown = m.doc()["sequence"]
    # (nothing of ours to stop: the cancelled start is what the page still shows)
    assert (shown["kind"], shown["reason"]) in (("stop", None), ("start", "superseded"))
    for _ in range(40):
        m.chain_world(set())
        m.refresh_health()
        if m.act.advance()["state"] != "running":
            break
    assert len(m.systemd_runs()) == runs
    assert {k: m.terminal(k)["owned"] for k in "abc"} == {k: False for k in "abc"}


def test_stop_whole_stack_takes_the_place_of_a_paused_chain(m):
    press(m, "chain-pause-s1", pause_before_b=True)
    m.run_chain()
    old = m.act._seq
    assert old["state"] == "waiting_confirm"
    m.act.sequence_stop(m.stop_all_body("stop-all-chain2"))
    assert (old["state"], old["reason"]) == ("cancelled", "superseded")
    for _ in range(20):
        if m.act.advance()["state"] != "running":
            break
    assert m.systemd_runs("b") == [] and m.terminal("a")["owned"] is False


def test_the_chain_does_not_outlive_the_process(m):
    to_step(m, "b")
    runs = len(m.systemd_runs())
    fresh = stack_act.Act(m.sup, m.act_host(), setup=lambda: (m.loaded, m.specs))
    assert fresh._seq == {"state": "idle"}
    for _ in range(6):
        m.chain_world(set())
        assert fresh.advance()["state"] == "idle"
    assert len(m.systemd_runs()) == runs and not m.stop_calls()
    # what was started stays what it was; the press again finishes the job
    m.sup.act = fresh
    m.act = fresh
    press(m, "chain-restart-02")
    assert m.run_chain()["state"] == "done"
    assert len(m.systemd_runs("a")) == 1 and len(m.systemd_runs("b")) == 1


# --- the chain and the person's own hand, a stale check, a lost tick -----------------------------


def test_a_check_that_went_old_between_two_steps_is_made_and_b_goes_on(m):
    to_step(m, "b")
    m.act._b_probe = {**m.act._b_probe, "at": m.t - stack_act.B_PROBE_TTL_S - 1}
    before = len(m.oneshots)
    seq = (
        m.act.advance()
    )  # B's start finds no usable check: it makes one, starts nothing
    assert seq["step"] == "b_probe" and m.systemd_runs("b") == []
    # (nothing fresh goes before it: two answers of the graph)
    assert len(m.oneshots) == before + 2 and not m.act._busy()
    assert states(seq)["b"] == "pending"
    seq = m.run_chain()
    assert seq["state"] == "done" and len(m.systemd_runs("b")) == 1


def test_b_started_by_the_person_while_the_chain_checks_is_not_started_again(m):
    to_step(m, "b_probe", pause_before_b=False)
    m.start_b()
    seq = m.act.advance()
    assert seq["step"] == "b" and states(seq)["b_probe"] == "skipped"
    assert m.run_chain()["state"] == "done"
    assert len(m.systemd_runs("b")) == 1


def test_c_started_by_the_person_while_the_chain_checks_is_not_started_again(m):
    to_step(m, "c_preflight")
    m.controllers_out, m.controllers_code = CONTROLLERS_OUT, 0
    m.act.preflight_c({"request_id": "pre-c-00000001"})
    m.act.start("c", m.start_body("c"))
    m.c_up()
    checks = len(m.oneshots_of("check_fr3_ee_frame"))
    seq = m.act.advance()
    assert seq["step"] == "c" and states(seq)["c_preflight"] == "skipped"
    assert len(m.oneshots_of("check_fr3_ee_frame")) == checks  # (no second check)
    assert m.run_chain()["state"] == "done"
    assert len(m.systemd_runs("c")) == 1


def test_a_part_that_was_up_at_the_request_and_is_gone_is_not_started_on_no_tick(m):
    """B was ready when the button was pressed, so nothing was ticked for
    starting it; if it is gone by the time the chain gets to it, the chain stops
    there instead of starting it on someone else's confirmation."""
    m.start_a()
    m.start_b()
    ticks = {"estop_hand": True, "no_client": True, "hands_clear": True}
    body = {**m.chain_body("chain-notick-001"), "attest": ticks}
    assert m.act.sequence_start(body) == {"done": "sequence_started"}
    assert set(m.act._seq["_attests"]) == {"c"}  # (A and B: nothing to start)
    m.act._seq["_attests"].pop("c")  # (the chain was to start C, with no ticks)
    seq = m.run_chain()
    assert (seq["state"], seq["reason"]) == ("failed", "attest_missing")
    assert m.systemd_runs("c") == []


def test_pressed_with_a_and_b_up_and_a_pause_before_c_nothing_is_asked_for(m):
    m.start_a()
    m.start_b()
    body = {**m.chain_body("chain-nothing-01", pause_before_c=True), "attest": {}}
    assert m.act.sequence_start(body) == {"done": "sequence_started"}
    seq = m.run_chain()
    assert (seq["state"], seq["next"]) == ("waiting_confirm", "c")
    assert len(m.systemd_runs("a")) == 1 and len(m.systemd_runs("b")) == 1


# --- a part that takes its time, and a part the person stops under the chain -----------------------


def test_a_b_that_takes_its_time_to_be_ready_is_waited_for(m):
    press(m)
    m.run_chain(ready=("a",), stop_at=lambda s: s["step"] == "b" and "b" in m.units)
    for _ in range(4):  # B is starting: its lines have not come yet
        seq = m.act.advance()
        assert (seq["state"], seq["step"]) == ("running", "b")
    assert m.terminal("b")["state"] == "starting" and len(m.systemd_runs("b")) == 1
    m.b_up()
    assert m.run_chain()["state"] == "done"
    assert len(m.systemd_runs("b")) == 1


def test_a_c_that_takes_its_time_to_be_ready_is_waited_for(m):
    press(m)
    m.run_chain(ready=("a", "b"), stop_at=lambda s: s["step"] == "c" and "c" in m.units)
    for _ in range(4):
        seq = m.act.advance()
        assert (seq["state"], seq["step"]) == ("running", "c")
    assert m.terminal("c")["state"] == "starting" and len(m.systemd_runs("c")) == 1
    m.c_up()
    assert m.run_chain()["state"] == "done"
    assert len(m.systemd_runs("c")) == 1


def test_a_b_the_person_stops_while_the_chain_waits_for_it_ends_the_chain(m):
    press(m)
    m.run_chain(ready=("a",), stop_at=lambda s: s["step"] == "b" and "b" in m.units)
    m.act.stop("b", m.stop_body("b"))
    assert m.terminal("b")["state"] in ("idle", "unknown")
    failed_at(m, "b", "b_lost", ("c_preflight", "c"))
    assert len(m.systemd_runs("b")) == 1  # (not started again)


def test_a_c_the_person_stops_while_the_chain_waits_for_it_ends_the_chain(m):
    press(m)
    m.run_chain(ready=("a", "b"), stop_at=lambda s: s["step"] == "c" and "c" in m.units)
    m.act.stop("c", m.stop_body("c"))
    failed_at(m, "c", "c_dead")
    assert len(m.systemd_runs("c")) == 1


# --- the words ---------------------------------------------------------------------------------


def test_every_reason_a_sequence_can_end_with_is_listed_for_the_page():
    """Every reason ``_seq_end`` is called with is in ``SEQUENCE_REASONS`` or is
    a code of ``TEXT`` (the page words both); the reasons built from a unit's
    name are the ones listed here."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(stack_act.__file__).read_text())
    literals, built = set(), []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_seq_end"
            and len(node.args) >= 2
        ):
            reason = node.args[1]
            if isinstance(reason, ast.Constant) and isinstance(reason.value, str):
                literals.add(reason.value)
            elif isinstance(reason, ast.JoinedStr):
                built.append(ast.unparse(reason))
    known = set(stack_act.SEQUENCE_REASONS) | set(stack_act.TEXT)
    assert literals <= known, sorted(literals - known)
    # built from a name: a_failed a_dead / monitor_<state> / <unit>_lost / b_, c_
    assert sorted(built) == sorted(
        ["f'a_{a}'", "f'monitor_{m}'", "f'{key}_lost'", "f'b_{b}'", "f'c_{c}'"]
    ), built
    expanded = {
        "a_failed", "a_dead", "a_lost", "b_lost", "b_failed", "b_dead",
        "c_failed", "c_dead",
    }  # fmt: skip
    assert expanded <= set(stack_act.SEQUENCE_REASONS)
    assert "c_preflight_failed" in stack_act.SEQUENCE_REASONS
    assert stack_act.catalog()["sequence_reasons"] == list(stack_act.SEQUENCE_REASONS)


def test_the_catalog_names_the_words_of_the_chain():
    cat = stack_act.catalog()
    assert cat["sequence_throughs"] == ["a", "c"]
    assert cat["sequence_steps"] == list(stack_act.SEQUENCE_STEPS)
    assert set(cat["sequence_step_states"]) >= {"skipped", "not_reached"}
    for code in ("sequence_stopping", "sequence_option_invalid"):
        assert code in cat["blockers"]
        status, text = stack_act.TEXT[code]
        assert status >= 400 and text and "/" not in text
    assert "notice.stop_unfinished" in cat["notices"]
    assert "stop_c_unknown" in cat["blockers"]


DOCUMENTED = (
    "through",
    "pause_before_b",
    "pause_before_c",
    "already_started",
    "not_reached",
    "sequence_stopping",
    "stop_unfinished",
    "b_lost",
    "c_preflight_failed",
    "controllers_state",
    "SEQUENCE_ARM_WINDOW_S",
    "expired",
    "touch_blockers",
    "hands_clear",
    "no_client",
    "attest_missing",
    "b_probe",
    "c_preflight",
)


def test_the_document_names_the_chain_in_both_languages():
    from pathlib import Path

    root = Path(stack_act.__file__).resolve().parents[2]
    doc = (root / "docs" / "MARVEL0_STACK.md").read_text(encoding="utf-8")
    english, chinese = doc.split("\n---\n", 1)
    for term in DOCUMENTED:
        assert term in english, f"{term} is not in the English half"
        assert term in chinese, f"{term} is not in the Chinese half"
