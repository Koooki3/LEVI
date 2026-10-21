"""The guided stop of the arm stack, "stop whole stack"
(``Act.sequence_stop`` in ``levi/marvel0/stack_act.py``), on the fake machine of
``stack_act_machine.py`` (L1): one request stops C, then A, then B, each by the
single stop's own code and gates and each gone before the next; M stays; it
starts nothing; it is refused while a hosted run is live or a client is
connected to C; a stop that fails ends it at that step and the next press
skips what is stopped; it can be cancelled; it never outlives the process; and
the way out does not depend on anything only a start needs (RIG-04).

Nothing here starts a process or opens a socket (the guard of
``test_marvel0_deploy.py``)."""

import fcntl
import json
import os
import time

import pytest
from stack_act_machine import OURS, SERVER_PORT, ActMachine
from test_marvel0_deploy import forbid_real_machine  # noqa: F401 - the guard
from test_marvel0_stack import _sock
from test_marvel0_stack_act import start_sequence

from levi.marvel0 import net, record, stack, stack_act


@pytest.fixture
def m(tmp_path):
    return ActMachine(tmp_path)


def refused(call, *args):
    with pytest.raises(stack.Refuse) as caught:
        call(*args)
    return caught.value.code


def codes(rows):
    return [row["code"] for row in rows]


def stop_all(m, rid="stop-all-00001", **extra):
    return m.act.sequence_stop(m.stop_all_body(rid, **extra))


def run_stop(m, limit=30):
    """Advance until the sequence stops running."""
    for _ in range(limit):
        seq = m.act.advance()
        if seq["state"] != "running":
            return seq
    raise AssertionError("the guided stop did not settle")


def steps(m):
    return [(i["terminal"], i["state"]) for i in m.doc()["sequence"]["steps"]]


def stopped_units(m):
    return [c[-1] for c in m.stop_calls()]


@pytest.fixture
def up(m):
    m.all_up()
    return m


# --- the order, and what it leaves ------------------------------------------------------


def test_one_request_stops_c_then_a_then_b_and_leaves_the_monitor(up):
    m = up
    assert stop_all(m) == {"done": "sequence_stop_started"}
    seq = m.doc()["sequence"]
    assert (seq["kind"], seq["state"], seq["step"]) == ("stop", "running", "stop_c")
    assert not m.stop_calls()  # (the request itself stops nothing: the ticks do)
    assert run_stop(m)["state"] == "done"
    assert stopped_units(m) == [OURS["c"], OURS["a"], OURS["b"]]
    assert steps(m) == [("c", "done"), ("a", "done"), ("b", "done")]
    t = m.doc()["terminals"]
    assert {k: t[k]["state"] for k in "abc"} == {k: "idle" for k in "abc"}
    assert t["m"]["state"] == "ready"  # M stays
    assert stack.MONITOR_SERVICE not in stopped_units(m)
    assert not m.marker_path.exists()  # (the release follows A's stop, as always)
    assert m.doc()["sequence"]["step"] is None


def test_each_step_waits_for_its_unit_to_be_gone_before_the_next(up, monkeypatch):
    m = up
    held = []
    m.act.host.background = True  # (workers are queued, not run)
    monkeypatch.setattr(
        m.act, "_start_thread", lambda op, work: held.append((op, work))
    )
    monkeypatch.setattr(m.act, "_ensure_ticker", lambda: None)
    stop_all(m)
    m.act.advance()  # C is told to stop; its worker has not run
    for _ in range(3):
        assert m.act.advance()["step"] == "stop_c"
    assert stopped_units(m) == [] and len(held) == 1
    assert steps(m) == [("c", "running"), ("a", "pending"), ("b", "pending")]
    op, work = held.pop()
    m.act._run_work(op, work)  # C is gone
    assert m.act.advance()["step"] == "stop_a"
    assert stopped_units(m) == [OURS["c"]]  # (A is told at the next look)
    m.act.advance()
    assert len(held) == 1 and stopped_units(m) == [OURS["c"]]
    op, work = held.pop()
    m.act._run_work(op, work)
    assert m.act.advance()["step"] == "stop_b"
    m.act.advance()
    op, work = held.pop()
    m.act._run_work(op, work)
    assert m.act.advance()["state"] == "done"
    assert stopped_units(m) == [OURS["c"], OURS["a"], OURS["b"]]


def test_the_sequence_never_starts_or_checks_anything(up):
    m = up
    runs, ones = len(m.systemd_runs()), len(m.oneshots)
    stop_all(m)
    run_stop(m)
    assert len(m.systemd_runs()) == runs and len(m.oneshots) == ones
    assert not any(c[:3] == ["systemctl", "--user", "kill"] for c in m.calls)
    assert m.kills == []


def test_a_part_that_is_already_stopped_is_skipped(m):
    m.start_a()  # no B, no C
    stop_all(m)
    assert run_stop(m)["state"] == "done"
    assert steps(m) == [("c", "skipped"), ("a", "done"), ("b", "skipped")]
    assert stopped_units(m) == [OURS["a"]]


def test_skipped_steps_are_passed_in_one_look(m):
    m.m_ready()
    m.start_a()
    m.start_b()  # no C: C is passed at once and A is told in the same tick
    stop_all(m)
    m.act.advance()
    assert stopped_units(m) == [OURS["a"]]
    assert steps(m)[0] == ("c", "skipped")


def test_a_stop_of_a_or_c_is_the_single_stops_own_with_its_own_notes(up):
    m = up
    stop_all(m)
    m.act.advance()
    m.act.advance()  # C gone
    doc = m.doc()
    assert doc["terminals"]["c"]["stop_note"] == "c.stop_arm_still_held"
    assert {"code": "notice.arm_still_held"} in doc["notices"]
    assert run_stop(m)["state"] == "done"
    assert m.doc()["terminals"]["a"]["stop_note"] == "a.stop_fci_dropped"
    for key in "ca":
        assert a_record(m, key)["phase"] == "stopped"


def a_record(m, key):
    return record.read_record(m.state_root / key)


def test_evidence_that_lingers_after_a_stop_of_a_is_not_a_part_that_is_not_ours(up):
    m = up
    real = m.on_stopped

    def lingers(key):
        # the monitor still shows the hardware and the arm's sockets are still
        # there for a while after the unit is gone (its own delay): A reads as
        # somebody else's
        if key == "c":
            real(key)

    m.on_stopped = lingers
    stop_all(m)
    seq = run_stop(m)
    assert seq["state"] == "done", seq
    assert steps(m) == [("c", "done"), ("a", "done"), ("b", "done")]
    assert m.terminal("a")["state"] == "foreign_running"  # (what the page reads)
    assert stopped_units(m) == [OURS["c"], OURS["a"], OURS["b"]]


def test_a_unit_that_had_already_failed_is_acknowledged_by_the_sequence(up):
    m = up
    m.units["a"]["active"] = "failed"
    m.units["a"]["tasks"] = 0
    stop_all(m)
    seq = run_stop(m)
    assert seq["state"] == "done"
    assert steps(m) == [("c", "done"), ("a", "done"), ("b", "done")]
    assert [c for c in m.calls if "reset-failed" in c] == [
        ["systemctl", "--user", "reset-failed", OURS["a"]]
    ]
    assert stopped_units(m) == [OURS["c"], OURS["b"]]  # (A needed no stop)
    assert a_record(m, "a")["phase"] == "acknowledged"


# --- what refuses it, and what does not ---------------------------------------------------


def test_a_hosted_run_that_is_live_refuses_it_and_nothing_is_stopped(up):
    m = up
    m.run_active = True
    assert refused(stop_all, m) == "run_active"
    doc = m.doc()
    assert doc["checks"]["can_stop_all"] is False
    assert "run_active" in codes(doc["stop_all_blockers"])
    assert doc["sequence"] == {"state": "idle", "kind": None}
    assert not m.stop_calls()
    m.run_active = False
    assert stop_all(m, "stop-all-00002") == {"done": "sequence_stop_started"}


def test_a_client_connected_to_c_refuses_it_and_the_override_is_not_part_of_it(up):
    m = up
    m.socks.append(_sock(40000, net.ESTABLISHED, remote_port=SERVER_PORT))
    assert refused(stop_all, m) == "stop_all_client_connected"
    # the single stop's phrase does not lift it (the field is not even part of it)
    over = m.stop_all_body("stop-all-00002", client_override=stack_act.CLIENT_OVERRIDE)
    assert refused(m.act.sequence_stop, over) == "stop_all_client_connected"
    doc = m.doc()
    assert doc["checks"]["can_stop_all"] is False
    row = next(b for b in doc["stop_all_blockers"] if b["level"] == "required")
    assert row["code"] == "stop_all_client_connected" and row["params"] == {
        "port": SERVER_PORT
    }
    assert "override" not in row["text"].split("Stopping the whole stack")[0]
    assert not m.stop_calls()
    # C's own stop, with its phrase, is still there
    assert m.terminal("c")["can_stop_with_override"] is True


def test_an_unreadable_socket_table_counts_as_a_client(up):
    m = up
    m.table_ok = False
    assert refused(stop_all, m) == "stop_all_client_connected"


def test_a_client_that_connects_after_the_press_stops_the_sequence_at_c(up):
    m = up
    stop_all(m)
    m.socks.append(_sock(40000, net.ESTABLISHED, remote_port=SERVER_PORT))
    seq = m.act.advance()
    assert seq["state"] == "failed" and seq["reason"] == "stop_all_client_connected"
    assert steps(m)[0] == ("c", "failed")
    assert not m.stop_calls()
    # the client leaves; the press again goes through
    m.socks.pop()
    stop_all(m, "stop-all-00002")
    assert run_stop(m)["state"] == "done"


def test_a_client_is_not_asked_about_when_c_is_not_running(m):
    m.start_a()
    m.socks.append(_sock(40000, net.ESTABLISHED, remote_port=SERVER_PORT))
    stop_all(m)
    assert run_stop(m)["state"] == "done"


def test_the_phrase_is_the_one_in_the_document_and_must_be_exact(up):
    m = up
    assert stack.PHRASES["sequence_stop"] == "stop whole stack"
    assert m.doc()["phrases"]["sequence_stop"] == "stop whole stack"
    bad = {**m.stop_all_body(), "confirm": "stop arm stack"}
    assert refused(m.act.sequence_stop, bad) == "confirm_required"
    assert refused(m.act.sequence_stop, {**bad, "confirm": ""}) == "confirm_required"
    assert not m.stop_calls()
    unknown = {**m.stop_all_body("stop-all-00003"), "attest": {"make_tea": True}}
    assert refused(m.act.sequence_stop, unknown) == "attest_invalid"
    # a stop ticks nothing: the known keys, or none, pass
    ok = {**m.stop_all_body("stop-all-00004"), "attest": {"hands_clear": True}}
    assert m.act.sequence_stop(ok) == {"done": "sequence_stop_started"}


def test_a_request_id_is_answered_again_and_never_done_twice(up):
    m = up
    first = stop_all(m)
    again = stop_all(m)
    assert again == {**first, "repeated": True}
    other = {**m.stop_all_body("stop-all-00001"), "attest": {"hands_clear": True}}
    assert refused(m.act.sequence_stop, other) == "request_id_used"
    run_stop(m)
    assert stopped_units(m) == [OURS["c"], OURS["a"], OURS["b"]]


def test_one_guided_stop_at_a_time(up):
    m = up
    stop_all(m)
    assert refused(stop_all, m, "stop-all-00002") == "sequence_active"
    doc = m.doc()
    assert doc["checks"]["can_stop_all"] is False
    assert codes(doc["stop_all_blockers"]) == ["sequence_active"]
    run_stop(m)
    assert m.doc()["checks"]["can_stop_all"] is False  # (nothing is left to stop)
    assert codes(m.doc()["stop_all_blockers"]) == ["nothing_to_stop"]


def test_it_is_not_the_product_it_does_not_act(up):
    m = up
    m.workspace_main = False
    assert refused(stop_all, m) == "gate.product"
    assert "gate.product" in codes(m.doc()["stop_all_blockers"])
    assert not m.stop_calls()


def test_systemd_that_cannot_be_read_refuses_it_never_reads_as_stopped(up):
    m = up
    m.systemd_down = True
    assert refused(stop_all, m) == "systemd_unreadable"
    assert "systemd_unreadable" in codes(m.doc()["stop_all_blockers"])
    assert m.doc()["sequence"]["state"] == "idle"


def test_a_record_that_cannot_be_read_refuses_it(up):
    m = up
    (m.state_root / "b" / "record.json").write_text("{not json")
    assert refused(stop_all, m) == "record_corrupt"
    assert "record_corrupt" in codes(m.doc()["stop_all_blockers"])
    assert not m.stop_calls()


def test_nothing_running_is_already_stopped_and_the_button_is_off(m):
    m.m_ready()
    doc = m.doc()
    assert doc["checks"]["can_stop_all"] is False
    assert codes(doc["stop_all_blockers"]) == ["nothing_to_stop"]
    assert stop_all(m) == {"done": "already_stopped"}
    assert m.doc()["sequence"] == {"state": "idle", "kind": None}
    assert not m.stop_calls()


def test_the_button_is_on_when_anything_of_ours_is_there(m):
    m.start_a()
    doc = m.doc()
    assert doc["checks"]["can_stop_all"] is True and doc["stop_all_blockers"] == []
    assert all(
        set(b) == {"code", "level", "text", "params"} for b in doc["stop_all_blockers"]
    )


# --- the way out does not depend on the way in (RIG-04) ----------------------------------


def test_a_run_probe_that_raises_refuses_a_start_but_not_the_stop(up):
    m = up

    def broken():
        raise RuntimeError("the run supervisor is broken")

    stack_act.register_run_probe(broken)
    assert stack_act.hosted_run_active() is None
    assert "run_active" not in codes(m.doc()["stop_all_blockers"])
    assert m.doc()["checks"]["can_stop_all"] is True
    stop_all(m)
    assert run_stop(m)["state"] == "done"
    assert stopped_units(m) == [OURS["c"], OURS["a"], OURS["b"]]
    # (and a start of the guided kind is refused by the same probe)
    m.m_ready()
    assert refused(start_sequence, m, "seq-after-0001") in ("run_active",)


def test_no_probe_registered_at_all_does_not_refuse_the_stop(up):
    m = up
    stack_act._RUN_PROBES.clear()
    stop_all(m)
    assert run_stop(m)["state"] == "done"


def test_a_stale_health_file_does_not_block_it(up):
    m = up
    m.doc()  # (the first look latches what is ready)
    m.t += 3600  # M was restarted long ago: the file is old, C cannot be vouched for
    assert m.terminal("c")["reason"] == "c.health_lost"
    stop_all(m)
    assert run_stop(m)["state"] == "done"
    assert stopped_units(m) == [OURS["c"], OURS["a"], OURS["b"]]


def test_a_check_that_is_running_does_not_block_it_and_is_ended(up):
    m = up
    check = m.act._begin("probe_b", "b")
    stop_all(m)
    assert run_stop(m)["state"] == "done"
    assert check["_superseded"] is True  # (its result is dropped)


def test_the_gripper_test_running_does_not_block_the_stop_of_b(up):
    m = up
    test = m.act._begin("gripper", "b")
    test["_unit"] = None
    stop_all(m)
    assert run_stop(m)["state"] == "done"
    assert test["_superseded"] is True


def test_no_control_file_is_needed(up):
    m = up
    m.set_control(False)
    doc = m.doc()
    assert doc["enabled"] is False and doc["checks"]["can_stop_all"] is True
    stop_all(m)
    assert run_stop(m)["state"] == "done"
    assert stopped_units(m) == [OURS["c"], OURS["a"], OURS["b"]]


def test_a_busy_machine_wide_lock_delays_a_step_and_is_not_a_failure(up):
    m = up
    stop_all(m)
    fd = os.open(m.hub / stack.LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for _ in range(3):
            seq = m.act.advance()
            assert seq["state"] == "running" and seq["step"] == "stop_c"
        assert not m.stop_calls()
    finally:
        os.close(fd)
    assert run_stop(m)["state"] == "done"


def test_a_stop_of_c_that_a_person_pressed_is_waited_for_not_repeated(up):
    m = up
    stop_all(m)
    m.units["c"]["active"] = "deactivating"  # the person's own stop is on its way
    m.act._write_record("c", phase="stopping", stop_requested_at=m.t)
    for _ in range(3):
        seq = m.act.advance()
        assert seq["step"] == "stop_c"
    assert not m.stop_calls()
    del m.units["c"]
    m.on_stopped("c")
    m.act._write_record("c", phase="stopped", stopped_at=m.t)
    seq = m.act.advance()
    assert seq["step"] == "stop_a"
    assert steps(m)[0] == ("c", "skipped")


# --- a stop that fails ends it there; pressing again goes on -----------------------------


@pytest.mark.parametrize("failing", ["c", "a", "b"])
def test_a_stop_that_fails_ends_the_sequence_at_that_step_and_the_next_press_goes_on(
    up, failing
):
    m = up
    m.stop_fails = {failing}
    stop_all(m)
    seq = run_stop(m)
    assert seq["state"] == "failed" and seq["reason"] == "stop_failed"
    order = ["c", "a", "b"]
    here = order.index(failing)
    expected = [(k, "done") for k in order[:here]] + [(failing, "failed")]
    expected += [(k, "not_reached") for k in order[here + 1 :]]
    assert steps(m) == expected
    # nothing after the failed step was touched
    assert stopped_units(m) == [OURS[k] for k in order[: here + 1]]
    assert m.terminal(failing)["state"] == "ready"
    assert m.doc()["checks"]["can_stop_all"] is True
    # the same press again: what is stopped is skipped, the failed one is tried again
    m.stop_fails = set()
    m.stop_calls_before = len(m.stop_calls())
    stop_all(m, "stop-all-00002")
    assert run_stop(m)["state"] == "done"
    expected = [(k, "skipped") for k in order[:here]] + [
        (k, "done") for k in order[here:]
    ]
    assert steps(m) == expected
    assert {m.terminal(k)["state"] for k in order} == {"idle"}


def test_a_unit_that_hangs_on_its_stop_ends_it_and_is_never_killed(up):
    m = up
    m.units["a"]["hangs"] = True
    stop_all(m)
    seq = run_stop(m)
    assert seq["state"] == "failed" and seq["reason"] == "stop_failed"
    assert m.terminal("a")["state"] == "failed"
    assert m.kills == [] and stopped_units(m) == [OURS["c"], OURS["a"]]
    # pressed again: C is skipped, A (failed, processes left) is sent SIGINT once
    # more and refused, B is not touched
    stop_all(m, "stop-all-00002")
    seq = run_stop(m)
    assert seq["state"] == "failed" and seq["reason"] == "stop_left_processes"
    assert steps(m) == [("c", "skipped"), ("a", "failed"), ("b", "not_reached")]
    assert m.kills == [(OURS["a"].removesuffix(".service"), "--signal=SIGINT")]
    # the processes are gone: the same press acknowledges A and goes on
    m.units["a"]["tasks"] = 0
    m.on_stopped("a")
    stop_all(m, "stop-all-00003")
    assert run_stop(m)["state"] == "done"
    assert steps(m) == [("c", "skipped"), ("a", "done"), ("b", "done")]


def test_a_part_the_page_did_not_start_stops_the_sequence_at_its_step(m):
    m.m_ready()
    m.start_a()
    m.act._b_probe = {"at": m.t, "state": "present", "by": "probe"}  # B by hand
    assert m.terminal("b")["reason"] == "b.foreign_running"
    stop_all(m)
    seq = run_stop(m)
    assert seq["state"] == "failed" and seq["reason"] == "not_ours"
    assert steps(m) == [("c", "skipped"), ("a", "done"), ("b", "failed")]
    assert stopped_units(m) == [OURS["a"]]  # (A went; the foreign B is left alone)


def test_an_a_started_by_hand_is_not_ours_and_the_sequence_says_so(m):
    m.m_ready()
    m.hand_started_a()
    assert m.terminal("a")["state"] == "foreign_running"
    # nothing of ours is there: the answer is the honest one
    assert codes(m.doc()["stop_all_blockers"]) == ["nothing_to_stop"]
    assert stop_all(m) == {"done": "already_stopped"}
    assert not m.stop_calls()


def test_a_step_that_never_ends_fails_with_step_timeout(up, monkeypatch):
    m = up
    m.act.host.background = True
    monkeypatch.setattr(m.act, "_start_thread", lambda op, work: None)
    monkeypatch.setattr(m.act, "_ensure_ticker", lambda: None)
    stop_all(m)
    assert m.act.advance()["step"] == "stop_c"
    m.t += stack_act.stack_units.STOP_TIMEOUT_S + stack_act.STOP_STEP_SLACK_S - 1
    assert m.act.advance()["state"] == "running"
    m.t += 2
    seq = m.act.advance()
    assert seq["state"] == "failed" and seq["reason"] == "step_timeout"


def test_a_run_that_starts_meanwhile_stops_it_before_the_next_unit(up):
    m = up
    stop_all(m)
    m.act.advance()
    m.act.advance()  # C is gone
    m.run_active = True
    seq = m.act.advance()
    assert seq["state"] == "failed" and seq["reason"] == "run_active"
    assert stopped_units(m) == [OURS["c"]]


def test_c_that_comes_back_before_a_is_told_stops_it_at_a(up, monkeypatch):
    m = up
    stop_all(m)
    m.act.advance()
    m.act.advance()  # C is gone
    # (the page refuses a start while a guided stop is live, so a C that comes
    # back is one that something else started: here the refusal is lifted)
    monkeypatch.setattr(m.act, "_stopping", lambda: False)
    m.act.start("c", m.start_body("c", "start-c-again1"))  # started again
    m.c_up()
    seq = m.act.advance()
    assert seq["state"] == "failed" and seq["reason"] == "stop_c_first"


def test_systemd_that_stops_answering_meanwhile_is_not_a_part_that_is_stopped(up):
    m = up
    stop_all(m)
    m.systemd_down = True
    seq = m.act.advance()
    assert seq["state"] == "failed" and seq["reason"] == "systemd_unreadable"
    assert steps(m)[0] == ("c", "failed")
    assert not m.stop_calls()


def test_a_unit_that_is_still_there_after_the_stop_is_waited_for_not_passed(up):
    m = up
    m.stop_keeps = {"c"}
    stop_all(m)
    for _ in range(4):
        seq = m.act.advance()
        assert seq["state"] == "running" and seq["step"] == "stop_c"
    assert stopped_units(m) == [OURS["c"]]  # (told once; A is not told)
    m.units.pop("c")  # it goes at last
    m.on_stopped("c")
    assert m.act.advance()["step"] == "stop_a"
    m.stop_keeps = set()
    assert run_stop(m)["state"] == "done"
    assert steps(m) == [("c", "done"), ("a", "done"), ("b", "done")]


def test_a_unit_that_vanished_with_its_record_open_is_acknowledged(m):
    m.start_a()
    del m.units["a"]
    m.on_stopped("a")  # the process died with its unit
    assert m.terminal("a")["state"] == "dead"
    assert codes(m.doc()["stop_all_blockers"]) == []
    assert m.doc()["checks"]["can_stop_all"] is True
    stop_all(m)
    assert run_stop(m)["state"] == "done"
    assert steps(m) == [("c", "skipped"), ("a", "done"), ("b", "skipped")]
    assert a_record(m, "a")["phase"] == "acknowledged"
    assert not m.stop_calls()


def wait_for_the_end(m, limit_s=20.0):
    end = time.monotonic() + limit_s
    while time.monotonic() < end:
        seq = m.doc()["sequence"]
        if seq["state"] != "running":
            return seq
        time.sleep(0.02)
    raise AssertionError(f"the guided stop did not settle: {m.doc()['sequence']}")


def test_with_threads_the_ticker_and_the_workers_bring_it_to_the_end_and_again(up):
    m = up
    m.act.host.background = True  # (the production way: a ticker, a thread per stop)
    m.act.host.sleep = lambda s: time.sleep(0.01)
    m.stop_fails = {"a"}
    stop_all(m)
    seq = wait_for_the_end(m)
    assert (seq["state"], seq["reason"]) == ("failed", "stop_failed")
    assert stopped_units(m) == [OURS["c"], OURS["a"]]
    m.stop_fails = set()
    stop_all(m, "stop-all-00002")  # (pressed again: the ticker is started again)
    seq = wait_for_the_end(m)
    assert seq["state"] == "done"
    assert steps(m) == [("c", "skipped"), ("a", "done"), ("b", "done")]
    assert stopped_units(m) == [OURS["c"], OURS["a"], OURS["a"], OURS["b"]]


# --- cancel, restart, and the other sequence --------------------------------------------


def test_it_is_cancelled_without_stopping_anything_more(up):
    m = up
    stop_all(m)
    m.act.advance()  # C is told
    sent = stopped_units(m)
    assert m.act.sequence_cancel({"request_id": "seq-cancel-0001"}) == {
        "done": "sequence_cancelled"
    }
    seq = m.doc()["sequence"]
    assert (seq["state"], seq["kind"], seq["reason"]) == (
        "cancelled",
        "stop",
        "cancelled",
    )
    assert [i["state"] for i in seq["steps"]] == [
        "cancelled",
        "not_reached",
        "not_reached",
    ]
    for _ in range(4):
        assert m.act.advance()["state"] == "cancelled"
    assert stopped_units(m) == sent  # (what was sent stays sent; nothing more)
    assert m.terminal("a")["state"] == "ready" and m.terminal("b")["state"] == "ready"
    assert m.doc()["checks"]["can_stop_all"] is True
    assert refused(m.act.sequence_cancel, {"request_id": "seq-cancel-0002"}) == (
        "sequence_idle"
    )


def test_nothing_resumes_after_a_restart_and_the_next_press_skips_what_is_gone(up):
    m = up
    stop_all(m)
    m.act.advance()
    m.act.advance()  # C gone, A next
    fresh = stack_act.Act(m.sup, m.act_host(), setup=lambda: (m.loaded, m.specs))
    assert fresh._seq == {"state": "idle"}
    sent = stopped_units(m)
    for _ in range(5):
        assert fresh.advance()["state"] == "idle"
    assert stopped_units(m) == sent
    assert refused(fresh.sequence_cancel, {"request_id": "seq-cancel-0009"}) == (
        "sequence_idle"
    )
    m.sup.act = m.act = fresh
    stop_all(m, "stop-all-00002")
    assert run_stop(m)["state"] == "done"
    assert steps(m) == [("c", "skipped"), ("a", "done"), ("b", "done")]


def test_it_ends_a_guided_start_that_would_start_what_it_stops(m):
    start_sequence(m)
    assert m.doc()["sequence"]["kind"] == "start"
    m.start_a()  # (A is up, though the start sequence has not got there)
    stop_all(m)
    seq = m.doc()["sequence"]
    assert (seq["kind"], seq["state"]) == ("stop", "running")
    assert run_stop(m)["state"] == "done"
    assert m.systemd_runs("a") and len(m.systemd_runs("a")) == 1  # (not started again)


def test_a_guided_start_waiting_for_b_is_superseded_even_with_nothing_to_stop(m):
    start_sequence(m)
    old = m.act._seq
    assert stop_all(m) == {"done": "already_stopped"}
    assert old["state"] == "cancelled" and old["reason"] == "superseded"
    assert m.doc()["sequence"]["reason"] == "superseded"
    for _ in range(4):
        m.act.advance()
    assert m.systemd_runs("a") == []  # (the start never went on)


def test_a_step_of_an_old_sequence_cannot_end_the_new_one(m):
    start_sequence(m)
    old = m.act._seq
    m.start_a()
    stop_all(m)
    new = m.act._seq
    assert new is not old and new["kind"] == "stop"
    assert old["state"] == "cancelled"
    old["state"] = "running"  # (as if its step were still in flight)
    m.act._seq_end("failed", "a_lost", old)  # (the old start's step, late)
    assert old["state"] == "failed"
    assert m.act._seq is new and new["state"] == "running"
    m.act._seq_end("failed", "x")  # (no sequence named: the current one)
    assert new["state"] == "failed"


def test_a_guided_start_is_refused_while_a_guided_stop_runs(up):
    m = up
    stop_all(m)
    assert refused(start_sequence, m, "seq-start-0001") == "sequence_active"


# --- what the document says --------------------------------------------------------------


def test_the_document_says_what_the_sequence_is_and_hides_the_bookkeeping(up):
    m = up
    stop_all(m)
    seq = m.doc()["sequence"]
    assert set(seq) == {
        "state",
        "kind",
        "step",
        "next",
        "reason",
        "since",
        "step_at",
        "steps",
        "reason_params",
        "took_s",
        "elapsed_s",
    }
    assert [i["step"] for i in seq["steps"]] == list(stack_act.SEQUENCE_STOP_STEPS)
    assert all(i["state"] in stack_act.SEQUENCE_STEP_STATES for i in seq["steps"])
    text = json.dumps(seq)
    assert "stop-all-00001" not in text and not any(k.startswith("_") for k in seq)
    assert str(m.tmp) not in json.dumps(m.sup.view())
    # the copy handed out is not the live one
    seq["steps"][0]["state"] = "done"
    assert m.doc()["sequence"]["steps"][0]["state"] == "running"


def test_the_start_sequence_says_it_is_one_and_idle_says_nothing(m):
    assert m.doc()["sequence"] == {"state": "idle", "kind": None}
    start_sequence(m)
    seq = m.doc()["sequence"]
    assert seq["kind"] == "start"
    # (the steps of a start are A's: the chain has its own, see the chain tests)
    assert [i["step"] for i in seq["steps"]] == list(stack_act.A_STEPS)


def test_the_catalog_names_the_new_words_and_every_one_has_a_sentence():
    cat = stack_act.catalog()
    assert cat["sequence_kinds"] == ["start", "stop"]
    assert cat["sequence_stop_steps"] == ["stop_c", "stop_a", "stop_b"]
    assert set(cat["sequence_step_states"]) == {
        "pending",
        "running",
        "done",
        "skipped",
        "failed",
        "cancelled",
        "not_reached",
    }
    assert "sequence_stop" in cat["phrases"] and "superseded" in cat["sequence_reasons"]
    for code in ("nothing_to_stop", "stop_all_client_connected", "sequence_active"):
        assert code in cat["blockers"]
        status, text = stack_act.TEXT[code]
        assert status >= 400 and text and "/" not in text.replace("start/stop", "")
    assert set(stack_act.STOP_ALL_RENAMED.values()) <= set(stack_act.TEXT)
    assert stack_act.STOP_ORDER == ("c", "a", "b")


def test_a_unit_of_our_name_with_another_command_stops_the_sequence_with_the_single_stops_code(
    up,
):
    m = up
    m.units["a"]["argv"] = ["/usr/bin/other", "--thing"]
    assert m.terminal("a")["reason"] == "a.unit_foreign"
    stop_all(m)
    seq = run_stop(m)
    assert seq["state"] == "failed" and seq["reason"] == "unit_foreign"
    assert stopped_units(m) == [OURS["c"]]  # (A is not touched, nor is B)
    assert steps(m) == [("c", "done"), ("a", "failed"), ("b", "not_reached")]
