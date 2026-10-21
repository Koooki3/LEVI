"""What a guided stop ("stop whole stack") holds back, and what it says when it
cannot finish (the second review of the stop-all package), on the fake machine
of ``stack_act_machine.py`` (L1):

- nothing is started, set or tested while a guided stop is live
  (``sequence_stopping``): the stop would meet what it is stopping;
- a C that is not the page's and cannot be seen ends the stop at C, saying so
  (``stop_c_unknown``), and does not pass for "nothing to stop";
- the steps a stop did not reach read ``not_reached``;
- a stop that was asked for and is carried by nobody (the page restarted in
  the middle of it, or it failed) leaves a notice built from the unit's
  record (``notice.stop_unfinished``).

Nothing here starts a process or opens a socket (the guard of
``test_marvel0_deploy.py``)."""

import pytest
from stack_act_machine import OURS, ActMachine
from test_marvel0_deploy import forbid_real_machine  # noqa: F401 - the guard

from levi.marvel0 import stack, stack_act


@pytest.fixture
def m(tmp_path):
    return ActMachine(tmp_path)


def refused(call, *args):
    with pytest.raises(stack.Refuse) as caught:
        call(*args)
    return caught.value.code


def codes(rows):
    return [row["code"] for row in rows]


def steps(m):
    return [(i["terminal"], i["state"]) for i in m.doc()["sequence"]["steps"]]


def stop_all(m, rid="stop-all-00001"):
    return m.act.sequence_stop(m.stop_all_body(rid))


def run_stop(m, limit=30):
    for _ in range(limit):
        seq = m.act.advance()
        if seq["state"] != "running":
            return seq
    raise AssertionError("the guided stop did not settle")


# --- nothing is started, set or tested while a guided stop is live -----------------------------


def test_while_a_guided_stop_is_live_no_start_is_offered_or_accepted(m):
    m.start_a()
    assert m.terminal("b")["can_start"] is True  # (without the stop)
    stop_all(m)
    for key in "abc":
        t = m.terminal(key)
        assert t["can_start"] is False
        row = next(b for b in t["start_blockers"] if b["code"] == "sequence_stopping")
        assert row["level"] == "required" and row["text"]
    runs = len(m.systemd_runs())
    for key in "bca":
        body = m.start_body(key, f"start-{key}-stopping")
        assert refused(m.act.start, key, body) == "sequence_stopping"
    assert len(m.systemd_runs()) == runs


def test_while_a_guided_stop_is_live_the_gripper_test_is_off(m):
    m.start_a()
    m.start_b()
    t = m.doc()["gripper_test"]
    assert t["can_run"] is True
    stop_all(m)
    t = m.doc()["gripper_test"]
    assert t["can_run"] is False and "sequence_stopping" in codes(t["blockers"])
    body = {
        **m.body("gripper_open", "grip-stopping-1"),
        "action": "open",
        "attest": {"hands_clear": True},
    }
    m.oneshots.clear()
    assert refused(m.act.gripper, body) == "sequence_stopping"
    assert not m.oneshots


def test_while_a_guided_stop_is_live_the_marker_is_not_set(m):
    m.start_a()
    m.marker_path.unlink()  # (a marker gone, A up: the button would be there)
    assert m.doc()["marker"]["touch_blockers"] == []
    stop_all(m)
    marker = m.doc()["marker"]
    assert codes(marker["touch_blockers"]) == ["sequence_stopping"]
    body = m.body("marker_touch", "mk-stopping-001")
    assert refused(m.act.marker_op, "touch", body) == "sequence_stopping"
    assert not m.marker_path.exists()
    # the stop goes on; when it is over, the marker can be set again
    assert run_stop(m)["state"] == "done"
    assert m.doc()["marker"]["touch_blockers"] == []
    m.act.marker_op("touch", m.body("marker_touch", "mk-stopping-002"))
    assert m.marker_path.exists()


def test_the_way_out_stays_open_while_a_guided_stop_is_live(m):
    m.all_up()
    stop_all(m)
    assert m.terminal("c")["can_stop"] is True
    answer = m.act.stop("c", m.stop_body("c"))  # (a single stop beside it)
    assert answer["done"] == "stopping"
    assert run_stop(m)["state"] == "done"


def test_a_guided_start_does_not_hold_the_starts_back(m):
    m.start_a()
    m.m_ready()
    body = m.chain_body("chain-live-0001", pause_before_b=True, pause_before_c=True)
    assert m.act.sequence_start(body)  # (A is up: the chain waits at once)
    assert m.doc()["sequence"]["kind"] == "start"
    assert "sequence_stopping" not in codes(m.terminal("b")["start_blockers"])


def test_the_hold_is_off_when_the_stop_has_ended_in_any_way(m):
    m.all_up()
    m.stop_fails = {"a"}
    stop_all(m)
    assert run_stop(m)["state"] == "failed"
    assert "sequence_stopping" not in codes(m.terminal("b")["start_blockers"])
    stop_all(m, "stop-all-00002")
    m.act.sequence_cancel({"request_id": "seq-cancel-0001"})
    assert "sequence_stopping" not in codes(m.terminal("b")["start_blockers"])


# --- a C that cannot be seen is not "nothing to stop" ----------------------------------------


def test_a_c_that_is_not_ours_and_cannot_be_told_ends_the_stop_at_c(m):
    m.start_a()
    m.start_b()
    m.table_ok = False  # (the socket table cannot be read)
    assert m.terminal("c")["state"] == "unknown" and not m.terminal("c")["owned"]
    stop_all(m)
    seq = run_stop(m)
    assert (seq["state"], seq["reason"]) == ("failed", "stop_c_unknown")
    assert steps(m) == [("c", "failed"), ("a", "not_reached"), ("b", "not_reached")]
    assert not m.stop_calls()  # (A was not stopped under a C that may be up)
    assert "stop_c_unknown" in stack_act.TEXT


def test_the_same_c_does_not_hold_back_the_single_stop_of_a(m):
    m.start_a()
    m.table_ok = False
    t = m.terminal("a")
    warn = [b for b in t["stop_blockers"] if b["code"] == "stop_c_unknown"]
    assert warn and warn[0]["level"] == "warn" and t["can_stop"] is True
    assert m.act.stop("a", m.stop_body("a"))["done"] == "stopping"


def test_the_stop_goes_through_when_the_c_can_be_told_again(m):
    m.start_a()
    m.table_ok = False
    stop_all(m)
    assert run_stop(m)["reason"] == "stop_c_unknown"
    m.table_ok = True
    stop_all(m, "stop-all-00002")
    assert run_stop(m)["state"] == "done"
    assert steps(m) == [("c", "skipped"), ("a", "done"), ("b", "skipped")]


def test_a_b_that_is_not_ours_and_not_seen_is_still_passed(m):
    m.start_a()
    m.act._b_probe = None  # B: never looked at (unknown), not ours
    assert m.terminal("b")["state"] == "unknown" and not m.terminal("b")["owned"]
    stop_all(m)
    assert run_stop(m)["state"] == "done"
    assert steps(m) == [("c", "skipped"), ("a", "done"), ("b", "skipped")]


# --- what was not reached reads so -------------------------------------------------------------


def test_the_steps_after_a_failed_one_were_not_reached(m):
    m.all_up()
    m.stop_fails = {"c"}
    stop_all(m)
    seq = run_stop(m)
    assert seq["state"] == "failed"
    assert steps(m) == [("c", "failed"), ("a", "not_reached"), ("b", "not_reached")]
    assert "not_reached" in stack_act.SEQUENCE_STEP_STATES
    assert "not_reached" in stack_act.catalog()["sequence_step_states"]


def test_a_cancelled_stop_has_its_running_step_cancelled_and_the_rest_not_reached(m):
    m.all_up()
    stop_all(m)
    m.act.sequence_cancel({"request_id": "seq-cancel-0002"})
    assert [s for _, s in steps(m)] == ["cancelled", "not_reached", "not_reached"]


def test_a_finished_stop_has_no_step_left_pending(m):
    m.all_up()
    stop_all(m)
    run_stop(m)
    assert {s for _, s in steps(m)} <= {"done", "skipped"}


# --- a stop that was asked for and is carried by nobody -----------------------------------------


def unfinished(m):
    return [
        n["params"]["unit"]
        for n in m.doc()["notices"]
        if n["code"] == "notice.stop_unfinished"
    ]


def test_a_stop_cut_short_by_a_restart_leaves_a_notice_and_the_next_press_goes_on(
    m, monkeypatch
):
    m.all_up()
    held = []
    m.act.host.background = True  # (workers are queued, not run)
    monkeypatch.setattr(m.act, "_start_thread", lambda op, work: held.append(work))
    monkeypatch.setattr(m.act, "_ensure_ticker", lambda: None)
    stop_all(m)
    m.act.advance()  # C is told to stop: the worker is queued ...
    assert unfinished(m) == []  # (a stop is on its way: this process carries it)
    # ... and the page restarts before it runs: nothing carries the stop
    fresh = stack_act.Act(m.sup, m.act_host(), setup=lambda: (m.loaded, m.specs))
    m.sup.act = fresh
    m.act = fresh
    fresh.host.background = False
    assert fresh._seq == {"state": "idle"}
    assert unfinished(m) == ["c"]
    assert {"code": "notice.stop_unfinished", "params": {"unit": "c"}} in m.doc()[
        "notices"
    ]
    assert m.terminal("c")["state"] == "ready"  # (the unit is still there)
    # the person presses again: the stop of C is sent again, the notice goes
    stop_all(m, "stop-all-00002")
    assert run_stop(m)["state"] == "done"
    assert unfinished(m) == []
    assert [c[-1] for c in m.stop_calls()] == [OURS[k] for k in "cab"]


def test_a_stop_that_failed_leaves_the_notice_until_it_is_done(m):
    m.start_a()
    m.stop_fails = {"a"}
    m.act.stop("a", m.stop_body("a"))
    assert unfinished(m) == ["a"]
    m.stop_fails = set()
    m.act.stop("a", m.stop_body("a", "stop-a-again1"))
    assert unfinished(m) == []


def test_there_is_no_notice_for_a_unit_that_is_going_or_gone_or_stopped(m):
    m.all_up()
    m.act.stop("c", m.stop_body("c"))  # (gone)
    assert unfinished(m) == []
    m.act._write_record("b", phase="stopping", stop_requested_at=m.t)
    m.units["b"]["active"] = "deactivating"  # (systemd is on it)
    assert unfinished(m) == []
    m.units["b"]["active"] = "active"
    assert unfinished(m) == ["b"]
    m.act._write_record("b", phase="started")
    assert unfinished(m) == []
    # a record that says "stopping" for a unit that is gone is not a stop to look at
    m.act._write_record("b", phase="stopping", stop_requested_at=m.t)
    del m.units["b"]
    assert not m.terminal("b")["owned"] and unfinished(m) == []


def test_a_stop_that_is_running_in_this_process_is_not_unfinished(m):
    m.all_up()
    m.act._write_record("c", phase="stopping", stop_requested_at=m.t)
    op = m.act._begin("stop_c", "c")
    assert unfinished(m) == []
    m.act._finish(op, True)
    assert unfinished(m) == ["c"]
