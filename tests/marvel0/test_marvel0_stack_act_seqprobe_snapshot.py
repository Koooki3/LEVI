"""A check of B that ends right after the tick's snapshot was read (MARVEL0-SEQPROBE-STALE-SNAPSHOT-1115).

``advance`` reads one context, then the step handler reads the operation's state
*now*. When the ticker thread is held up between the two, the check of B can end
(and record its answer) in between: the handler then sees "operation done" next
to a context in which B is still unknown. That is not "B was not seen": the next
tick has a newer context and decides. Only a check that failed, or one that is
done and still leaves B unknown in a context made after it ended, ends the start
as ``b_not_seen``. Nothing here widens a gate: while B is unknown the sequence
never goes on to ``wait``/``arm``/``b``.

The hold-up is injected without any sleep: ``_step_running`` is wrapped so that
the operation ends after ``advance`` has read its context and before the handler
runs. L1 fake machine, nothing is started."""

import pytest
from stack_act_machine import ActMachine
from test_marvel0_deploy import forbid_real_machine  # noqa: F401 - the guard
from test_marvel0_stack_act import start_sequence
from test_marvel0_stack_act_chain import to_step

ABSENT = {"state": "absent", "by": "nodes", "graph": "absent", "confirmed": True}


@pytest.fixture
def m(tmp_path):
    return ActMachine(tmp_path)


def end_after_snapshot(m, op, ok, record=None):
    """The next ``advance`` reads its context, and only then ``op`` ends (and
    ``record`` becomes the answer about B): the stall of the ticker thread."""
    act = m.act
    real = act._step_running

    def stalled(seq, ctx):
        act._step_running = real  # (once)
        if record is not None:
            act._b_probe = {"at": act.now(), **record}
        act._finish(op, ok, None if ok else "b_probe_no_answer")
        return real(seq, ctx)

    act._step_running = stalled


def at_probe_with_running_check(m):
    """A start at its ``probe`` step whose early check of B has not ended (the
    place is held, then the held operation is the sequence's early check)."""
    m.m_ready()
    held = m.act._begin("probe_b", "b")
    start_sequence(m)
    for _ in range(4):
        m.act.advance()
    seq = m.act._seq
    assert seq["step"] == "probe" and "_early_op" not in seq
    seq["_early_op"] = held
    assert m.act.advance()["step"] == "probe"  # (running: waited for)
    assert held["state"] == "running" and not m.act._b_probe
    return held


def step_names(seq):
    return {i["step"]: i["state"] for i in seq["steps"]}


def test_a_check_that_ends_after_the_snapshot_does_not_end_the_start(m):
    held = at_probe_with_running_check(m)
    end_after_snapshot(m, held, True, ABSENT)
    shown = m.act.advance()  # (op done, the snapshot says B unknown)
    assert (shown["state"], shown["step"]) == ("running", "probe"), shown
    assert shown["reason"] is None
    assert m.systemd_runs("a") == []
    shown = m.act.advance()  # (a context made after the check: B is known)
    assert (shown["state"], shown["step"]) == ("running", "wait"), shown
    assert m.act._b_probe["state"] == "absent"


def test_a_check_that_is_done_and_leaves_b_unknown_still_ends_the_start(m):
    held = at_probe_with_running_check(m)
    end_after_snapshot(m, held, True)  # (done, no answer recorded: B never appears)
    first = m.act.advance()
    assert (first["state"], first["step"]) == ("running", "probe"), first
    second = m.act.advance()  # (the newer context still says unknown)
    assert (second["state"], second["reason"]) == ("failed", "b_not_seen"), second
    names = step_names(second)
    assert names["probe"] == "failed"
    assert {names[s] for s in ("wait", "arm", "await_a")} == {"not_reached"}
    assert m.systemd_runs("a") == [], "nothing was started"
    assert not m.marker_path.exists(), "the marker is given back"


def test_a_check_that_is_done_for_good_waits_one_tick_and_no_more(m):
    held = at_probe_with_running_check(m)
    m.act._finish(held, True)  # (ended before the next snapshot: not stale)
    assert m.act._b_probe is None
    seen = []
    for _ in range(4):
        shown = m.act.advance()
        seen.append((shown["state"], shown["step"]))
        if shown["state"] != "running":
            break
    assert seen == [("running", "probe"), ("failed", None)], seen


def test_a_check_that_failed_after_the_snapshot_ends_the_start_at_once(m):
    held = at_probe_with_running_check(m)
    end_after_snapshot(m, held, False)
    shown = m.act.advance()
    assert (shown["state"], shown["reason"]) == ("failed", "b_not_seen"), shown
    assert m.systemd_runs("a") == []


def test_a_check_that_failed_before_the_snapshot_ends_the_start_at_once(m):
    held = at_probe_with_running_check(m)
    m.act._finish(held, False, "b_probe_no_answer")
    shown = m.act.advance()
    assert (shown["state"], shown["reason"]) == ("failed", "b_not_seen"), shown


def at_b_probe_with_running_check(m):
    """A guided start at ``b_probe`` with A up, B not, and a check of B (the one
    that ``_seq_b_probe`` waits for) that has not ended."""
    to_step(m, "b_probe", ready=("a",))
    live = m.act._seq
    held = m.act._begin("probe_b", "b")
    live["_probe_op"] = held
    assert m.act.advance()["step"] == "b_probe"  # (running: waited for)
    return held


def test_b_probe_check_that_ends_after_the_snapshot_goes_on_to_b_on_the_next_context(
    m,
):
    held = at_b_probe_with_running_check(m)
    end_after_snapshot(m, held, True, ABSENT)
    shown = m.act.advance()
    # (the step after it reads its own context: nothing is started in this tick)
    assert (shown["state"], shown["step"]) == ("running", "b"), shown
    assert m.systemd_runs("b") == []


def test_b_probe_check_that_failed_after_the_snapshot_ends_the_chain(m):
    held = at_b_probe_with_running_check(m)
    end_after_snapshot(m, held, False)
    shown = m.act.advance()
    assert (shown["state"], shown["reason"]) == ("failed", "b_not_seen"), shown
    assert m.systemd_runs("b") == [] and m.terminal("a")["state"] == "ready"


def test_b_probe_check_that_is_still_running_is_waited_for(m):
    at_b_probe_with_running_check(m)
    for _ in range(3):
        assert m.act.advance()["step"] == "b_probe"
    assert m.systemd_runs("b") == []
