"""The residual findings of the two verifiers who re-checked the first fix round
of the arm-stack actions (verify-round1: the conformance verifier V1 and the
state-machine verifier V2), one test or group each, on the fake machine of
``stack_act_machine.py`` (L1).

Every test here failed before its fix (the finding is in the group's heading),
except the groups marked "survivor": those pin a gate that the mutation check
found nobody tested (``mutate_stack_act.py`` has a mutant for each). None starts
a process or opens a socket (the guard of ``test_marvel0_deploy.py``)."""

import json
import os

import pytest
from stack_act_machine import (
    CONTROLLERS_OUT,
    NO_MANAGER,
    OURS,
    TIME_WAIT_S,
    WAITING_LINE,
    ActMachine,
)
from test_marvel0_deploy import forbid_real_machine  # noqa: F401 - the guard
from test_marvel0_stack_act import run_sequence, start_sequence

from levi.marvel0 import control_site, gripper, record, stack, stack_act, stack_units

KILL = "--signal=SIGINT"


@pytest.fixture
def m(tmp_path):
    return ActMachine(tmp_path)


def refused(call, *args):
    with pytest.raises(stack.Refuse) as caught:
        call(*args)
    return caught.value.code


def codes(rows):
    return [row["code"] for row in rows]


def grip(m, action="open", rid="grip-0000001"):
    return m.act.gripper(
        {
            **m.body(f"gripper_{action}", rid),
            "action": action,
            "attest": {"hands_clear": True},
        }
    )


def probes(m):
    """The one-shot checks that asked whether a gripper stack runs: the ROS graph
    (the fast question) and, when that proved nothing, B's controller manager."""
    return [
        c
        for c in m.oneshots
        if "list_controllers" in " ".join(c) or " node list " in " ".join(c)
    ]


def probe_request(n=1):
    return {"request_id": f"probe-b-{n:07d}"}


def restarted(m):
    again = stack_act.Act(
        stack.Stack(m.host()), m.act_host(), setup=lambda: (m.loaded, m.specs)
    )
    again.stack.act = again
    return again


def hung_a(m):
    m.start_a()
    m.units["a"]["hangs"] = True
    m.act.stop("a", m.stop_body("a", "stop-a-hang01"))
    assert m.terminal("a")["state"] == "failed"


def a_up_much_later(m):
    """A is ready; the check made before A looked at B, and the person comes
    to B after the check's validity (10 minutes) is over."""
    m.start_a()
    m.t += stack_act.B_PROBE_TTL_S + 1
    m.a_up(lines=False)  # (the monitor keeps writing: A stays ready)
    assert m.terminal("a")["state"] == "ready"


# --- V2 medium: with A up, B could not be started 10 minutes after the check ---------------


def test_an_old_b_check_reads_unknown_and_never_absent(m):
    m.start_a()
    b = m.terminal("b")
    assert (b["state"], b["reason"]) == ("idle", "b.not_running")  # fresh: absent
    m.t += stack_act.B_PROBE_TTL_S + 1
    m.a_up(lines=False)
    b = m.terminal("b")
    assert (b["state"], b["reason"]) == ("unknown", "b.probe_stale")
    # a check dated after "now" (the clock went back) is no fresher
    m.act._b_probe["at"] = m.t + 500
    assert m.terminal("b")["reason"] == "b.probe_stale"


def test_b_reads_not_probed_only_when_no_check_ever_ran(m):
    m.m_ready()
    assert m.terminal("b")["reason"] == "b.not_probed"
    m.preflight_a()
    assert m.terminal("b")["reason"] == "b.not_running"


def test_the_b_check_alone_refreshes_it_with_a_up_and_makes_no_fci_connection(m):
    a_up_much_later(m)
    m.fci_probes.clear()
    m.oneshots.clear()
    # A's own check cannot be made now, which is why the old one was a dead end
    assert refused(m.act.preflight_a, {"request_id": "pre-a-stale001"}) == (
        "probe_arm_present"
    )
    answer = m.act.probe_b(probe_request())
    assert answer == {"done": "probe_started", "kind": "probe_b"}
    assert m.fci_probes == [], "the B check never touches the arm's FCI port"
    b = m.terminal("b")
    assert (b["state"], b["reason"]) == ("idle", "b.not_running")
    op = m.doc()["operation"]
    assert (op["kind"], op["terminal"], op["state"]) == ("probe_b", "b", "done")


def test_the_b_check_is_bounded_as_the_one_before_a_is(m):
    m.m_ready()
    m.nodes_out = ""  # (the graph proves nothing: the controller manager is asked)
    m.act.probe_b(probe_request())
    [_, cmd] = probes(m)
    assert cmd[cmd.index("--") + 1 :][:6] == ["timeout", "-s", "INT", "-k", "3", "20"]
    [run] = [c for c in m.calls if c[0] == "systemd-run" and "probe-b-1" in " ".join(c)]
    limit = next(a for a in run if a.startswith("--property=RuntimeMaxSec="))
    assert int(limit.split("=")[2]) == stack_units.PROBE_RUNTIME_MAX_S


def test_the_b_check_also_runs_while_a_is_idle(m):
    m.m_ready()
    assert m.terminal("a")["state"] == "idle"
    m.act.probe_b(probe_request())
    assert m.terminal("b")["reason"] == "b.not_running" and m.fci_probes == []


def test_a_gripper_stack_seen_by_the_b_check_is_foreign_running(m):
    m.m_ready()
    m.controllers_out, m.controllers_code = CONTROLLERS_OUT, 0
    m.act.probe_b(probe_request())
    assert m.terminal("b")["state"] == "foreign_running"
    assert m.doc()["operation"]["state"] == "done"  # (a finding, not a failure)


def test_a_b_check_that_got_no_answer_fails_its_operation_and_is_no_answer(m):
    m.m_ready()
    m.controllers_out, m.controllers_code = WAITING_LINE, 124
    m.act.probe_b(probe_request())
    op = m.doc()["operation"]
    assert (op["state"], op["code"]) == ("failed", "b_probe_no_answer")
    assert m.terminal("b")["reason"] == "b.probe_no_answer"


def test_the_b_check_is_refused_while_a_unit_of_b_exists(m):
    m.start_a()
    m.start_b()
    m.oneshots.clear()
    assert refused(m.act.probe_b, probe_request()) == "gate.b_unit_absent"
    assert probes(m) == []


def test_the_b_check_is_refused_when_systemd_cannot_be_asked(m):
    m.m_ready()
    m.systemd_down = True
    assert refused(m.act.probe_b, probe_request()) == "systemd_unreadable"
    assert probes(m) == []


def test_the_b_check_needs_the_product_and_the_control_file(m):
    m.m_ready()
    m.workspace_main = False
    assert refused(m.act.probe_b, probe_request(1)) == "not_product"
    m.workspace_main = True
    m.act._setup = lambda: (control_site.ControlLoad("missing", None), None)
    assert refused(m.act.probe_b, probe_request(2)) == "control_disabled"
    assert probes(m) == []


def test_a_stop_ends_the_b_check_that_runs(m):
    check = m.act._begin("probe_b", "b")
    m.act._begin("stop_c", "c")
    assert check["_superseded"] is True


def test_a_press_on_start_b_with_an_old_check_makes_the_check_and_starts_nothing(m):
    a_up_much_later(m)
    b = m.terminal("b")
    # the button is there to press, and says what the press will do
    assert b["state"] == "unknown" and b["can_start"] is True
    assert [(r["code"], r["level"]) for r in b["start_blockers"]] == [
        ("b_probe_stale", "warn")
    ]
    m.oneshots.clear()
    assert refused(m.act.start, "b", m.start_body("b")) == "b_probe_started"
    assert m.systemd_runs("b") == [], "nothing is started on a B that cannot be seen"
    # (two answers of the graph, as every check a person asks for: nothing fresh
    # went before it, and the FCI check is the one before A)
    assert len(probes(m)) == 2 and len(m.fci_probes) == 1
    assert m.terminal("b")["state"] == "idle"  # the check answered: absent
    # the person presses again: now it starts
    assert m.act.start("b", m.start_body("b", "start-b-0002"))["done"] == "started"
    assert len(m.systemd_runs("b")) == 1


def test_a_press_with_a_b_check_that_got_no_answer_asks_again(m):
    m.start_a()
    m.controllers_out, m.controllers_code = WAITING_LINE, 124
    m.act.probe_b(probe_request())
    assert m.terminal("b")["reason"] == "b.probe_no_answer"
    assert m.terminal("b")["can_start"] is True
    m.controllers_out, m.controllers_code = NO_MANAGER, 124
    assert refused(m.act.start, "b", m.start_body("b")) == "b_probe_started"
    assert m.terminal("b")["state"] == "idle"


def test_a_press_does_not_make_the_check_when_another_layer_cannot_be_seen(m):
    a_up_much_later(m)
    m.table_ok = False  # C cannot be told: the check would not settle it
    assert "gate.layers_known" in m.blockers("b")
    assert "b_probe_stale" not in m.blockers("b")
    assert m.terminal("b")["can_start"] is False
    m.oneshots.clear()
    assert refused(m.act.start, "b", m.start_body("b")) == "gate.layers_known"
    assert probes(m) == []


def test_a_press_still_needs_a_ready_and_the_attestations(m):
    a_up_much_later(m)
    m.oneshots.clear()
    assert refused(m.act.start, "b", m.start_body("b", "start-b-noat1", False)) == (
        "attest_missing"
    )
    assert probes(m) == []
    m.units["a"]["active"] = "failed"
    assert "a_not_ready" in m.blockers("b") and m.terminal("b")["can_start"] is False


def test_a_press_while_a_check_runs_waits_for_it(m):
    a_up_much_later(m)
    m.act._begin("probe_b", "b")
    assert "operation_running" in m.blockers("b")
    assert refused(m.act.start, "b", m.start_body("b")) == "operation_running"


def test_a_guided_start_that_waits_for_b_keeps_the_check_fresh(m):
    start_sequence(m)
    seq = run_sequence(m)
    assert seq["state"] == "waiting_confirm" and seq["next"] == "b"
    before = len(probes(m))  # (the one made before A: a single answer of the graph)
    # B needs a second answer: the sequence makes it while it waits (one check)
    m.act.advance()
    assert len(probes(m)) == before + 1
    assert m.act._b_probe["confirmed"] is True
    # now it is not due: the check is left alone
    m.act.advance()
    assert len(probes(m)) == before + 1
    before += 1
    # half of the validity later it is made again, by the sequence itself
    m.t += stack_act.B_PROBE_REFRESH_S + 1
    m.a_up(lines=False)
    assert m.act.advance()["state"] == "waiting_confirm"
    assert len(probes(m)) == before + 1
    assert m.terminal("b")["reason"] == "b.not_running"
    m.act.advance()
    assert len(probes(m)) == before + 1, "a fresh check is not made again"
    # a person who comes after most of the wait finds B startable at once
    m.t += 1400
    m.a_up(lines=False)
    m.act.advance()
    assert m.terminal("b")["state"] == "idle" and m.terminal("b")["can_start"]
    assert m.systemd_runs("b") == [], "the sequence never starts B"


def test_a_guided_start_looks_again_sooner_at_a_gripper_stack_it_saw(m):
    start_sequence(m)
    run_sequence(m)
    m.controllers_out, m.controllers_code = CONTROLLERS_OUT, 0
    m.t += stack_act.B_PROBE_REFRESH_S + 1
    m.a_up(lines=False)
    m.act.advance()
    assert m.terminal("b")["state"] == "foreign_running"
    # the person stops that stack in its terminal: a minute later the page knows
    m.controllers_out, m.controllers_code = NO_MANAGER, 124
    n = len(probes(m))
    m.act.advance()
    assert len(probes(m)) == n, "not before the shorter wait"
    m.t += stack_act.B_PROBE_RETRY_S + 1
    m.a_up(lines=False)
    m.act.advance()
    assert m.terminal("b")["state"] == "idle"


def test_a_guided_start_does_not_end_while_another_operation_holds_the_check_back(m):
    start_sequence(m)
    run_sequence(m)
    m.t += stack_act.B_PROBE_TTL_S + 1
    m.a_up(lines=False)
    n = len(probes(m))
    op = m.act._begin("preflight_c")  # another operation is running
    assert m.act.advance()["state"] == "waiting_confirm"
    assert len(probes(m)) == n
    m.act._finish(op, True)
    assert m.act.advance()["state"] == "waiting_confirm"
    # (nothing fresh goes before it: two answers of the graph)
    assert len(probes(m)) == n + 2


def test_a_guided_start_does_not_end_when_the_optional_check_is_refused(m):
    start_sequence(m)
    run_sequence(m)
    m.start_b()  # the person started B between the tick's read and its check
    n = len(probes(m))
    m.act._refresh_b_probe(m.act._seq)  # (refused: a unit of B exists)
    assert len(probes(m)) == n
    assert m.act.advance()["state"] == "waiting_confirm"


# --- V2 medium: the FCI check's own TIME_WAIT socket read as a foreign A ----------------------


def bring_up_to_the_check(m):
    """M, marker, the FCI check, with what the real monitor writes (null while no
    controller manager answers) and the check's own socket in TIME_WAIT."""
    m.fci_time_wait = True
    m.m_ready()
    assert json.loads(m.health_path.read_text())["hardware_active"] is None
    m.set_marker_old()  # (the clock jumps: the file is stale)
    m.no_manager()
    m.preflight_a()
    assert m.sup._read()["sockets"]["state"] == "active"  # arm_guard counts it


def test_the_fake_monitor_writes_null_for_no_manager_and_false_for_one_that_answers(m):
    m.no_manager()
    health = json.loads(m.health_path.read_text())
    assert health["hardware_active"] is None and health["controller_active"] is None
    m.health(hardware=False, controller=False, red=True, mode=None)
    assert json.loads(m.health_path.read_text())["hardware_active"] is False
    m.m_ready()
    m.start_a()
    m.act.stop("a", m.stop_body("a"))
    assert json.loads(m.health_path.read_text())["hardware_active"] is None


def test_the_fci_checks_own_socket_is_not_a_foreign_a(m):
    bring_up_to_the_check(m)
    a = m.terminal("a")
    assert (a["state"], a["reason"]) == ("idle", "a.sockets_draining")
    assert "not_ours" not in m.blockers("a", "stop") and not a["can_stop"]
    assert m.blockers("a", "stop") == []
    # ... and the start still waits for the arm's sockets to be quiet
    assert "gate.arm_sockets_clear" in m.blockers("a")


def test_the_check_can_be_made_again_while_its_own_socket_lingers(m):
    # A person presses the check twice (the first answer was not what they
    # waited for): the first one's TIME_WAIT socket must not forbid the second.
    bring_up_to_the_check(m)
    assert m.terminal("a")["reason"] == "a.sockets_draining"
    m.t += 5
    m.no_manager()
    m.preflight_a("pre-a-again001")
    assert len(m.fci_probes) == 2


def test_the_bring_up_goes_on_from_the_check_to_a(m):
    bring_up_to_the_check(m)
    m.t += TIME_WAIT_S + 1  # the marker wait, in the page's own sequence
    m.no_manager()
    assert (
        m.terminal("a")["state"] == "idle" and m.terminal("a")["reason"] == "a.stopped"
    )
    a = m.terminal("a")
    assert [b["code"] for b in a["start_blockers"] if b["level"] == "required"] == []
    assert a["can_start"] is True
    m.act.start("a", m.start_body("a"))
    m.a_up()
    assert m.terminal("a")["state"] == "ready"


def test_the_guided_start_never_shows_a_foreign_a_while_the_checks_socket_lingers(m):
    m.fci_time_wait = True
    start_sequence(m)
    seen, up = set(), False
    for _ in range(40):
        seq = m.act.advance()
        if seq["state"] != "running":
            break
        seen.add(m.terminal("a")["state"])
        if seq.get("step") == "wait":
            m.t += 25
            m.no_manager()
        if "a" in m.units and not up:
            m.a_up()
            up = True
    assert seq["state"] == "waiting_confirm", seq
    assert "foreign_running" not in seen, seen


def test_the_sockets_of_the_pages_own_stop_are_not_a_foreign_a_with_the_real_health(m):
    m.start_a()
    m.act.stop("a", m.stop_body("a"))
    m.act._fci = None  # (the stop's own mark alone: no check was made lately)
    assert json.loads(m.health_path.read_text())["hardware_active"] is None
    m.sockets = {"state": "active", "detail": "TIME_WAIT"}
    a = m.terminal("a")
    assert (a["state"], a["reason"]) == ("idle", "a.sockets_draining")
    assert a["stop_blockers"] == [] and not a["can_stop"]
    assert m.terminal("a")["stop_note"] is None  # (the note waits for quiet sockets)


@pytest.mark.parametrize("hardware", [None, False])
def test_the_hardware_going_down_after_the_pages_own_stop_is_still_its_leftovers(
    m, hardware
):
    m.start_a()
    m.act.stop("a", m.stop_body("a"))
    m.act._fci = None  # (the stop's own mark alone)
    m.sockets = {"state": "active", "detail": "TIME_WAIT"}
    m.health(hardware=hardware, controller=None, red=True, mode=None)
    assert m.terminal("a")["reason"] == "a.sockets_draining"


def test_an_a_of_somebody_else_that_comes_up_right_after_the_pages_stop_is_foreign(m):
    # The window after the page's own stop is the time in which the arm's sockets
    # are its leftovers. An active hardware in it is not a leftover (the monitor
    # shows a manager that is up, fr3_health_monitor.py:300-340): somebody
    # started an A, and the monitor must be kept and the A left alone.
    m.start_a()
    m.act.stop("a", m.stop_body("a"))
    m.t += 5
    m.hand_started_a()  # a control connection and an active hardware, no unit of ours
    a = m.terminal("a")
    assert (a["state"], a["reason"]) == ("foreign_running", "a.foreign_running")
    assert codes(m.terminal("m")["stop_blockers"]) == ["stack_running"]
    assert refused(m.act.stop, "m", m.body("monitor_stop", "stop-m-foreign2")) == (
        "stack_running"
    )
    assert refused(m.preflight_a, "pre-a-foreign2") == "probe_arm_present"


def test_a_manager_that_answers_after_the_check_is_not_the_checks_own_socket(m):
    # The check's mark alone: a controller manager answering since (the monitor
    # writes false while an A starts) is somebody's A, whatever the clock says.
    bring_up_to_the_check(m)
    assert m.terminal("a")["reason"] == "a.sockets_draining"
    m.health(hardware=False, controller=False, red=True, mode=None)
    assert m.terminal("a")["state"] == "foreign_running"
    m.health(hardware=True, controller=False, red=False, mode="IDLE")
    assert m.terminal("a")["state"] == "foreign_running"
    assert refused(m.preflight_a, "pre-a-draining2") == "probe_arm_present"


def test_leftover_sockets_are_not_leftovers_when_the_monitor_is_not_looking(m):
    bring_up_to_the_check(m)
    m.no_manager()
    m.health(age=60.0, hardware=None, controller=None, red=True, mode=None)
    assert m.terminal("a")["state"] == "foreign_running"


def test_leftover_sockets_after_the_window_are_a_foreign_a_again(m):
    bring_up_to_the_check(m)
    m.sockets = {"state": "active", "detail": "2 control sockets"}  # not TIME_WAIT
    m.t += stack_act.DRAIN_S + 1
    m.no_manager()
    assert m.terminal("a")["state"] == "foreign_running"


def test_sockets_that_cannot_be_read_are_not_leftovers(m):
    m.start_a()
    m.act.stop("a", m.stop_body("a"))
    m.sockets = {"state": "unknown", "detail": "arm_guard failed"}
    a = m.terminal("a")
    assert a["state"] == "unknown" and a["reason"] == "a.unknown"


# --- V2 medium: a stop of A or B before the page's own running C ---------------------------


def test_a_stop_of_a_waits_for_c_when_c_is_ours_and_its_health_went_stale(m):
    m.all_up()
    assert m.terminal("c")["state"] == "ready"  # (latched: the log is not read again)
    m.t += 3600  # M was restarted: the health file is old
    c = m.terminal("c")
    assert (c["state"], c["reason"], c["owned"]) == ("unknown", "c.health_lost", True)
    assert "stop_c_first" in m.blockers("a", "stop")
    assert m.terminal("a")["can_stop"] is False
    assert refused(m.act.stop, "a", m.stop_body("a")) == "stop_c_first"
    assert refused(m.act.stop, "b", m.stop_body("b", "stop-b-stale01")) == (
        "stop_c_first"
    )
    assert m.stop_calls() == []


def test_a_stop_of_a_waits_for_c_when_c_is_ours_and_the_control_file_is_gone(m):
    m.all_up()
    m.act._setup = lambda: (control_site.ControlLoad("missing", None), None)
    c = m.terminal("c")
    assert (c["state"], c["reason"], c["owned"]) == ("unknown", "c.no_specs", True)
    assert refused(m.act.stop, "a", m.stop_body("a")) == "stop_c_first"
    assert m.stop_calls() == []


def test_c_itself_can_still_be_stopped_when_its_health_went_stale(m):
    m.all_up()
    assert m.terminal("c")["state"] == "ready"
    m.t += 3600
    assert m.terminal("c")["can_stop"] is True
    assert m.act.stop("c", m.stop_body("c"))["done"] == "stopping"


def test_a_stop_of_a_carries_a_warning_for_a_c_that_is_not_ours_to_see(m):
    m.all_up()
    m.act.stop("c", m.stop_body("c"))
    m.table_ok = False
    rows = m.terminal("a")["stop_blockers"]
    assert [(r["code"], r["level"], r["params"]) for r in rows] == [
        ("stop_c_unknown", "warn", {"reason": "c.unknown"})
    ]
    assert "c.unknown" in rows[0]["text"] and m.terminal("a")["can_stop"] is True


def test_an_unreadable_record_of_c_does_not_lock_a_and_b_for_good(m):
    # C's own stop is refused for a record it cannot read; if A's stop waited
    # for C, nothing could ever be stopped from the page.
    m.start_a()
    (m.act._folder("c") / "record.json").write_text("{not json")
    c = m.terminal("c")
    assert (c["state"], c["reason"], c["owned"]) == (
        "unknown",
        "c.record_corrupt",
        False,
    )
    assert [(r["code"], r["level"]) for r in m.terminal("a")["stop_blockers"]] == [
        ("stop_c_unknown", "warn")
    ]
    assert m.act.stop("a", m.stop_body("a"))["done"] == "stopping"


# --- V1 low: the status the judge gets for a timeout-killed unit is 255 -----------------------


@pytest.mark.parametrize("code", [255, 137, -9, 1, 2, 130, 143])
def test_a_command_that_ended_without_a_result_by_a_status_but_0_or_124_may_still_move(
    code,
):
    got = gripper.judge(code, "")
    assert got == {
        "ok": False,
        "code": "timeout_killed",
        "reached": None,
        "stalled": None,
    }


def test_the_statuses_that_are_not_a_killed_command_keep_their_verdicts():
    assert gripper.judge(0, "")["code"] == "no_result"
    assert gripper.judge(124, "")["code"] == "timeout"
    assert gripper.judge(gripper.STARTUP_FAILED, "x")["code"] == "start_failed"


def test_a_test_whose_unit_ended_by_a_signal_says_the_gripper_may_still_move(m):
    m.start_a()
    m.start_b()
    m.gripper_code, m.gripper_out = 255, ""  # (what systemd-run --wait gives)
    grip(m)
    last = m.doc()["gripper_test"]["last"]
    assert (last["ok"], last["code"]) == (False, "timeout_killed")
    assert "may still be moving" in gripper.RESULT_TEXT["timeout_killed"]


def test_the_rehearsal_asserts_the_verdict_not_the_number():
    import rehearse_stack

    assert rehearse_stack.gripper is gripper
    # the same two cases its timeout_check feeds through the judge
    assert gripper.judge(255, "")["code"] == "timeout_killed"
    assert gripper.judge(124, "Goal canceled.\n")["code"] == "timeout"


# --- V1 low: B's stop waits for the gripper test's unit, and re-checks ------------------------


def the_unit_of_the_test(m):
    return f"{gripper.unit_name(m.t)}.service"


def during_the_test(m, seen):
    def hook(cmd):
        if "send_goal" in " ".join(cmd) and not seen:
            seen.append(m.act.stop("b", m.stop_body("b", "stop-b-during")))

    m.on_oneshot = hook


def index_of(m, *prefix, last=None):
    for i, c in enumerate(m.calls):
        if c[: len(prefix)] == list(prefix) and (last is None or c[-1] == last):
            return i
    return None


def test_stopping_b_waits_until_the_gripper_tests_unit_is_gone(m):
    m.start_a()
    m.start_b()
    m.oneshot_linger = 3  # the unit takes three more looks to go
    seen = []
    during_the_test(m, seen)
    grip(m)
    assert seen and seen[0]["done"] == "stopping"
    unit = the_unit_of_the_test(m)
    stop_unit = index_of(m, "systemctl", "--user", "stop", last=unit)
    stop_b = index_of(m, "systemctl", "--user", "stop", last=OURS["b"])
    shows = [
        i
        for i, c in enumerate(m.calls)
        if c[:4] == ["systemctl", "--user", "show", unit]
    ]
    assert stop_unit is not None and stop_b is not None and stop_unit < stop_b
    assert len(shows) == 5, "the stop looked until the unit was gone"
    assert all(i < stop_b for i in shows), "B was stopped after the unit was gone"
    assert m.slept.count(stack_act.GRIPPER_END_POLL_S) == 3
    last = m.doc()["gripper_test"]["last"]
    assert (last["ok"], last["code"]) == (False, "cancelled")
    assert m.terminal("b")["state"] == "idle"


def test_a_gripper_unit_that_will_not_go_does_not_hold_back_the_stop_of_b(m):
    m.start_a()
    m.start_b()
    m.oneshot_linger = 10_000
    seen = []
    during_the_test(m, seen)
    grip(m)
    unit = the_unit_of_the_test(m)
    polls = int(stack_act.GRIPPER_END_WAIT_S / stack_act.GRIPPER_END_POLL_S)
    shows = [c for c in m.calls if c[:4] == ["systemctl", "--user", "show", unit]]
    assert len(shows) == polls, "bounded by GRIPPER_END_WAIT_S"
    assert m.slept.count(stack_act.GRIPPER_END_POLL_S) == polls - 1
    assert index_of(m, "systemctl", "--user", "stop", last=OURS["b"]) is not None
    assert m.terminal("b")["state"] == "idle"
    assert stack_act.GRIPPER_END_WAIT_S > stack_units.GRIPPER_KILL_AFTER_S


def test_stopping_b_waits_for_a_gripper_unit_that_systemd_has_not_created_yet(m):
    m.start_a()
    m.start_b()
    unit = the_unit_of_the_test(m)
    seen = []

    def before_the_unit_exists(argv):
        # the hook runs after the page told the unit's name and before systemd
        # creates the unit: a stop of B lands exactly there
        if any("oneshot-gripper" in a for a in argv) and not seen:
            seen.append(m.act.stop("b", m.stop_body("b", "stop-b-race001")))

    def systemd_creates_it_meanwhile(seconds):
        m.slept.append(seconds)
        m.oneshot_units.setdefault(
            unit.removesuffix(".service"), {"stopped": False, "shows_left": 0}
        )

    m.on_systemd_run = before_the_unit_exists
    m.act.host.sleep = systemd_creates_it_meanwhile
    grip(m)
    assert seen and seen[0]["done"] == "stopping"
    stop_unit = index_of(m, "systemctl", "--user", "stop", last=unit)
    stop_b = index_of(m, "systemctl", "--user", "stop", last=OURS["b"])
    first_look = index_of(m, "systemctl", "--user", "show", unit)
    assert first_look is not None and first_look < stop_unit < stop_b
    assert m.slept, "the stop waited for the unit to appear"
    assert m.doc()["gripper_test"]["last"]["code"] == "cancelled"


def test_a_stop_of_b_with_no_gripper_test_does_not_look_at_any_oneshot(m):
    m.start_a()
    m.start_b()
    before = len(m.calls)
    m.act.stop("b", m.stop_body("b"))
    assert not [c for c in m.calls[before:] if "oneshot" in " ".join(c)]
    assert m.slept == []


# --- V1/V2 low: the lows -----------------------------------------------------------------------


def test_a_failed_unit_with_a_control_group_that_cannot_be_read_and_no_main_pid_is_not_reset(
    m,
):
    hung_a(m)
    group = "/user.slice/app.slice/levi-aeri-stack-marvel0-a.service"
    m.units["a"].update(tasks="[not set]", pid=0)
    m.cgroup_pids[group] = None  # unreadable
    assert refused(m.act.stop, "a", m.stop_body("a", "stop-a-hang02")) == (
        "stop_left_processes"
    )
    assert not [c for c in m.calls if "reset-failed" in c]
    assert m.kills == [("levi-aeri-stack-marvel0-a", KILL)]


def test_a_positive_main_pid_counts_when_nothing_else_can_be_read(m):
    hung_a(m)
    group = "/user.slice/app.slice/levi-aeri-stack-marvel0-a.service"
    m.units["a"].update(tasks="[not set]", pid=4321)
    m.cgroup_pids[group] = None
    assert refused(m.act.stop, "a", m.stop_body("a", "stop-a-hang02")) == (
        "stop_left_processes"
    )


def test_a_clean_failed_unit_that_has_no_control_group_any_more_is_acknowledged(m):
    # systemd lets go of the group of an empty unit: no ControlGroup, no task
    # count, MainPID 0. Nothing is left, and refusing here would be a unit that
    # can never be cleared.
    hung_a(m)
    m.units["a"].update(tasks="[not set]", pid=0, cgroup="")
    assert m.act.stop("a", m.stop_body("a", "stop-a-hang02"))["done"] == "acknowledged"
    assert [c for c in m.calls if "reset-failed" in c]


def run_to_await_a(m):
    start_sequence(m)
    for _ in range(40):
        seq = m.act.advance()
        if seq.get("step") == "await_a":
            return seq
        if seq.get("step") == "wait":
            m.t += 40
            m.no_manager()
    raise AssertionError("the sequence never reached await_a")


def test_a_stop_of_a_while_the_sequence_waits_for_it_ends_the_sequence(m):
    run_to_await_a(m)
    assert m.act.stop("a", m.stop_body("a"))["done"] == "stopping"
    seq = m.act.advance()
    assert (seq["state"], seq["reason"]) == ("failed", "a_lost")
    # nothing holds the next guided start back
    assert start_sequence(m, "seq-00000009") == {"done": "sequence_started"}


def test_the_press_of_a_stop_ends_the_sequence_before_the_unit_is_gone(m):
    run_to_await_a(m)
    m.act._write_record("a", phase="stopping", stop_requested_at=m.t)  # (the press)
    assert m.terminal("a")["state"] == "starting"  # (the unit is still there)
    seq = m.act.advance()
    assert (seq["state"], seq["reason"]) == ("failed", "a_lost")


def test_a_sequence_that_is_starting_a_is_not_ended_by_an_a_that_is_still_coming_up(m):
    run_to_await_a(m)
    assert m.terminal("a")["state"] == "starting"
    assert m.act.advance()["state"] == "running"


def stopped_during_the_restart(m):
    """The page died between the press of a stop and the end of it: the record
    says ``stopping``; the unit is gone."""
    m.start_a()
    m.act._write_record("a", phase="stopping", stop_requested_at=m.t)
    del m.units["a"]
    m.on_stopped("a")
    assert m.marker_path.exists()


def test_a_page_that_restarts_after_a_stop_it_did_not_finish_releases_the_marker(m):
    stopped_during_the_restart(m)
    restarted(m).resume()
    assert not m.marker_path.exists()
    rec = record.read_record(m.state_root / "a")
    assert rec["phase"] == "stopped" and isinstance(rec["stopped_at"], float)
    assert m.terminal("a")["stop_note"] == "a.stop_fci_dropped"


def test_a_page_that_restarts_during_a_stop_that_has_not_ended_leaves_everything(m):
    m.start_a()
    m.act._write_record("a", phase="stopping", stop_requested_at=m.t)
    # the unit is still there (deactivating): the stop is not over
    m.units["a"]["active"] = "deactivating"
    restarted(m).resume()
    assert m.marker_path.exists()
    assert record.read_record(m.state_root / "a")["phase"] == "stopping"


def test_a_page_that_restarts_cannot_close_a_record_when_systemd_cannot_be_asked(m):
    stopped_during_the_restart(m)
    m.systemd_down = True
    restarted(m).resume()
    assert m.marker_path.exists()
    assert record.read_record(m.state_root / "a")["phase"] == "stopping"


def test_a_marker_that_is_not_ours_is_left_by_the_resume_of_a_cut_short_stop(m):
    stopped_during_the_restart(m)
    os.utime(m.marker_path, (1, 1))  # a person's marker now
    restarted(m).resume()
    assert m.marker_path.exists()


# --- survivors of the mutation check ---------------------------------------------------------


@pytest.mark.parametrize("units_a", [None, True])
def test_the_fci_check_needs_the_unit_of_a_known_absent_not_only_a_idle(m, units_a):
    m.m_ready()
    r = m.sup._read()
    assert r["units"]["A"] is False and m.act._context(r).states["a"]["state"] == "idle"
    r["units"]["A"] = units_a  # (the unit list says what the unit's own show did not)
    assert refused(m.act._do_preflight_a, r) == "probe_arm_present"
    assert m.fci_probes == []


def test_a_unit_whose_task_count_cannot_be_asked_at_all_is_not_acknowledged(m):
    hung_a(m)
    m.show_failing = {"TasksCurrent"}  # (only the question about processes fails)
    assert refused(m.act.stop, "a", m.stop_body("a", "stop-a-hang02")) == (
        "stop_left_processes"
    )
    assert not [c for c in m.calls if "reset-failed" in c]
    assert m.kills == [("levi-aeri-stack-marvel0-a", KILL)]
    assert record.read_record(m.state_root / "a")["phase"] == "stop_failed"


def test_the_ready_latch_is_written_only_to_the_run_it_was_read_for(m):
    m.ready_for_a()
    m.act.start("a", m.start_body("a"))
    old = m.act._record("a")
    # a newer start replaced the record (another start time, even if the log
    # name were the same): the latch of the run read before must not mark it
    record.update_record(m.act._folder("a"), started_at=old["started_at"] + 5)
    m.act._latch("a", old, m.t)
    assert "ready_at" not in record.read_record(m.state_root / "a")
    # the run it was read for is marked
    current = m.act._record("a")
    m.act._latch("a", current, m.t)
    assert record.read_record(m.state_root / "a")["ready_at"] == round(m.t, 3)


# --- round 2: what the second fixer's tests did not reach ---------------------------------------
#
# (a) V2 finding 2, second half: after the page's own stop of A (and after its FCI
#     check) the arm's sockets linger in TIME_WAIT. A read "idle, sockets draining",
#     but the monitor's stop still read ``terms["A"]`` of the passive layer
#     (stack.py:683-684: any active socket is "A running") and refused ``stack_running``
#     for the minute that follows, so "stop everything" ended with a refusal that
#     no row could explain.
# (b) The check of B alone had no way to be asked for: with A up and no guided start
#     waiting, a gripper stack seen once (``foreign_running``) and stopped by hand kept
#     being seen for the whole validity (10 minutes).


def lingering(m, detail="1 socket in TIME_WAIT"):
    """The arm's sockets after the page's own stop of A: A's side closed first, so
    the kernel holds the connection in TIME_WAIT; the monitor shows no manager."""
    m.sockets = {"state": "active", "detail": detail}
    m.t += 5
    m.no_manager()


def test_the_monitor_can_be_stopped_while_the_sockets_of_the_pages_own_stop_linger(m):
    m.start_a()
    m.act.stop("a", m.stop_body("a"))
    lingering(m)
    assert m.terminal("a")["reason"] == "a.sockets_draining"
    mon = m.terminal("m")
    assert mon["stop_blockers"] == [] and mon["can_stop"] is True
    stopped = m.act.stop("m", m.body("monitor_stop", "stop-m-linger01"))
    assert stopped["done"] == "monitor_stopped"


def test_the_monitor_can_be_stopped_while_the_fci_checks_own_socket_lingers(m):
    bring_up_to_the_check(m)
    assert m.terminal("a")["reason"] == "a.sockets_draining"
    assert m.terminal("m")["can_stop"] is True
    assert m.act.stop("m", m.body("monitor_stop", "stop-m-linger02"))["done"] == (
        "monitor_stopped"
    )


def test_the_monitor_is_still_kept_while_an_a_of_somebody_else_holds_the_arm(m):
    m.m_ready()
    m.hand_started_a()  # a control connection and an active hardware, no unit of ours
    assert m.terminal("a")["state"] == "foreign_running"
    assert codes(m.terminal("m")["stop_blockers"]) == ["stack_running"]
    body = m.body("monitor_stop", "stop-m-foreign1")
    assert refused(m.act.stop, "m", body) == "stack_running"


def test_the_monitor_is_kept_when_the_sockets_outlive_the_drain_window(m):
    m.start_a()
    m.act.stop("a", m.stop_body("a"))
    lingering(m, "2 control sockets")  # not TIME_WAIT: somebody connected since
    m.t += stack_act.DRAIN_S + 1
    m.no_manager()
    assert m.terminal("a")["state"] == "foreign_running"
    body = m.body("monitor_stop", "stop-m-window01")
    assert refused(m.act.stop, "m", body) == "stack_running"


def test_the_monitors_stop_blockers_name_each_code_once(m):
    m.start_a()
    assert codes(m.terminal("m")["stop_blockers"]) == ["stack_running"]
    m.start_b()
    assert codes(m.terminal("m")["stop_blockers"]) == ["stack_running", "b_running"]


def test_a_start_of_a_still_waits_for_the_lingering_sockets(m):
    # the draining reading must not open the start: the arm's sockets decide that
    m.start_a()
    m.act.stop("a", m.stop_body("a"))
    lingering(m)
    needed = [b["code"] for b in m.terminal("a")["start_blockers"]]
    assert "gate.arm_sockets_clear" in needed and m.terminal("a")["can_start"] is False


def test_the_document_says_when_the_b_check_can_be_made(m):
    m.m_ready()
    assert m.doc()["checks"]["can_probe_b"] is True  # A idle: allowed
    m.start_a()
    assert m.doc()["checks"]["can_probe_b"] is True  # A up: allowed too
    op = m.act._begin("probe_b", "b")
    assert m.doc()["checks"]["can_probe_b"] is False  # one is running
    m.act._finish(op, True)
    assert m.doc()["checks"]["can_probe_b"] is True
    m.start_b()
    assert m.doc()["checks"]["can_probe_b"] is False  # a unit of ours is B
    m.systemd_down = True
    assert m.doc()["checks"]["can_probe_b"] is False


def test_the_b_check_cannot_be_made_without_the_control_file_or_off_the_product(
    tmp_path,
):
    off = ActMachine(tmp_path / "off", enabled=False)
    assert off.doc()["checks"]["can_probe_b"] is False
    elsewhere = ActMachine(tmp_path / "elsewhere")
    elsewhere.m_ready()
    elsewhere.workspace_main = False
    assert elsewhere.doc()["checks"]["can_probe_b"] is False


def test_a_gripper_stack_that_was_stopped_by_hand_is_seen_gone_by_a_check_alone(m):
    m.start_a()
    m.controllers_out, m.controllers_code = CONTROLLERS_OUT, 0
    m.act.probe_b(probe_request(1))
    assert m.terminal("b")["state"] == "foreign_running"
    assert m.doc()["checks"]["can_probe_b"] is True  # (the button is there)
    m.controllers_out, m.controllers_code = NO_MANAGER, 124  # the person stopped it
    m.act.probe_b(probe_request(2))
    assert (m.terminal("b")["state"], m.terminal("b")["can_start"]) == ("idle", True)
