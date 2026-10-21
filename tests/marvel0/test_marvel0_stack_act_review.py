"""The findings of the two independent reviews of the arm-stack actions
(``reviews/control-review-R1-b1-20261021.md``, physical safety and gates, and
``control-review-R2-b1-20261021.md``, real commands and lifecycle), one test
each, on the fake machine of ``stack_act_machine.py`` (L1).

Every test here failed before its fix (the finding id is in its name); none
starts a process or opens a socket (the guard of ``test_marvel0_deploy.py``)."""

import sys
import threading

import pytest
from stack_act_machine import (
    CONTROLLERS_OUT,
    NO_MANAGER,
    OURS,
    SERVER_PORT,
    WAITING_LINE,
    ActMachine,
    ready_lines,
)
from test_marvel0_deploy import forbid_real_machine  # noqa: F401 - the guard
from test_marvel0_stack import _sock

from levi.marvel0 import net, record, stack, stack_act, stack_units

DIED = "[ERROR] [launch]: [ros2_control_node-2]: process has died [pid 4001, exit code -6, cmd 'x']."


@pytest.fixture
def m(tmp_path):
    return ActMachine(tmp_path)


def refused(call, *args):
    with pytest.raises(stack.Refuse) as caught:
        call(*args)
    return caught.value.code


def codes(rows):
    return [row["code"] for row in rows]


def stop_c(m, rid="stop-c-00001", **extra):
    return m.act.stop("c", m.stop_body("c", rid, **extra))


def grip(m, action="open", rid="grip-0000001"):
    return m.act.gripper(
        {
            **m.body(f"gripper_{action}", rid),
            "action": action,
            "attest": {"hands_clear": True},
        }
    )


# --- R1 M1: B and C need the stage before them ready, not merely running -----------------


def test_r1_m1_b_does_not_start_while_a_has_failed_but_is_still_connected(m):
    m.start_a()
    m.emit("a", DIED)  # the arm still shows a control connection
    assert m.terminal("a")["state"] == "failed"
    assert "a_not_ready" in m.blockers("b")
    assert refused(m.act.start, "b", m.start_body("b")) == "a_not_ready"
    assert m.systemd_runs("b") == []


@pytest.mark.parametrize("how", ["unit_failed", "starting"])
def test_r1_m1_b_does_not_start_while_a_is_not_ready(m, how):
    m.ready_for_a()
    m.act.start("a", m.start_body("a"))
    if how == "unit_failed":
        m.a_up()
        m.units["a"]["active"] = "failed"
    else:
        m.sockets = {"state": "active", "detail": "2 control sockets"}
    assert (
        m.terminal("a")["state"]
        == {"unit_failed": "failed", "starting": "starting"}[how]
    )
    assert "a_not_ready" in m.blockers("b")
    assert refused(m.act.start, "b", m.start_body("b")) == "a_not_ready"


def test_r1_m1_the_params_of_the_blocker_name_the_state(m):
    m.start_a()
    m.emit("a", DIED)
    [row] = [b for b in m.terminal("b")["start_blockers"] if b["code"] == "a_not_ready"]
    assert row["params"] == {"state": "failed"} and "failed" in row["text"]


def ready_to_start_c(m):
    m.start_a()
    m.start_b()
    m.controllers_out, m.controllers_code = CONTROLLERS_OUT, 0
    m.act.preflight_c({"request_id": "pre-c-00000001"})
    assert m.blockers("c") == []


def test_r1_m1_c_does_not_start_after_b_has_failed(m):
    ready_to_start_c(m)
    m.emit("b", DIED)  # the checks before C passed up to 300 s ago
    assert m.terminal("b")["state"] == "failed"
    assert "gripper_b_not_ready" in m.blockers("c")
    assert refused(m.act.start, "c", m.start_body("c")) == "gripper_b_not_ready"
    assert m.systemd_runs("c") == []


def test_r1_m1_c_does_not_start_while_b_is_a_unit_that_failed(m):
    ready_to_start_c(m)
    m.units["b"]["active"] = "failed"
    assert refused(m.act.start, "c", m.start_body("c")) == "gripper_b_not_ready"


def test_r1_m1_c_does_not_start_after_a_has_failed(m):
    ready_to_start_c(m)
    m.emit("a", DIED)
    assert refused(m.act.start, "c", m.start_body("c")) == "a_not_ready"


def test_r1_m1_a_gripper_stack_started_by_hand_does_not_unlock_c(m):
    # (foreign B: it is running, but not as a unit of this page)
    m.m_ready()
    m.set_marker_old()
    m.controllers_out, m.controllers_code = CONTROLLERS_OUT, 0
    m.act.preflight_a({"request_id": "pre-a-00000009"})
    m.act.start("a", m.start_body("a"))
    m.a_up()
    assert m.terminal("a")["state"] == "ready"
    assert m.terminal("b")["state"] == "foreign_running"
    assert "gripper_b_not_ready" in m.blockers("c")


def test_r1_m1_the_blockers_are_gone_when_both_are_ready(m):
    ready_to_start_c(m)
    assert "a_not_ready" not in m.blockers("b") + m.blockers("c")
    assert "gripper_b_not_ready" not in m.blockers("c")


# --- R1 M2: the FCI port is touched only while A is positively absent --------------------


def probe(m, rid="pre-a-00000042"):
    m.fci_probes.clear()
    return refused(m.act.preflight_a, {"request_id": rid}), list(m.fci_probes)


def test_r1_m2_no_fci_probe_while_a_cannot_be_told(m):
    m.m_ready()
    m.sockets = {"state": "unknown", "detail": "arm_guard failed"}
    m.health(age=100)  # nobody writes the health file: M is not running
    assert m.terminal("a")["state"] == "unknown"
    code, probes = probe(m)
    assert code == "probe_arm_present" and probes == []


def test_r1_m2_no_fci_probe_while_systemd_cannot_be_asked(m):
    m.m_ready()
    m.systemd_down = True
    code, probes = probe(m)
    assert code == "probe_arm_present" and probes == []


def test_r1_m2_the_drain_exemption_needs_sockets_that_are_known(m):
    m.start_a()
    m.act.stop("a", m.stop_body("a"))
    m.no_manager()
    m.sockets = {"state": "unknown", "detail": "arm_guard failed"}
    code, probes = probe(m, "pre-a-00000043")
    assert code == "probe_arm_present" and probes == []
    m.sockets = {"state": "active", "detail": "TIME_WAIT"}  # known: lingering
    m.preflight_a("pre-a-00000044")
    assert len(m.fci_probes) == 1


def test_r1_m2_no_fci_probe_while_the_record_of_a_cannot_be_read(m):
    # (nothing else says A is there: no unit, quiet sockets; only the state)
    m.m_ready()
    folder = m.act._folder("a")
    (folder / "record.json").write_text("{not json")
    assert m.terminal("a")["state"] == "unknown"
    code, probes = probe(m)
    assert code == "probe_arm_present" and probes == []


def test_r1_m2_no_fci_probe_while_a_unit_of_ours_exists(m):
    m.start_a()
    m.sockets = {"state": "inactive", "detail": ""}
    m.no_manager()
    code, probes = probe(m)
    assert code == "probe_arm_present" and probes == []


# --- R1 M3: a gripper test that ran out of time is cancelled, not left moving ------------


def test_r1_m3_the_test_is_bounded_with_sigint_and_a_kill_after(m):
    m.start_a()
    m.start_b()
    grip(m)
    [cmd] = [c for c in m.oneshots if "send_goal" in c]
    wrapped = cmd[cmd.index("--") + 1 :]
    assert wrapped[:6] == ["timeout", "-s", "INT", "-k", "5", "20"]


@pytest.mark.parametrize(
    "code, text, verdict",
    [
        (137, "", "timeout_killed"),
        (124, "Sending goal:\nGoal accepted with ID: 01\n", "timeout_killed"),
        (
            124,
            "Goal accepted with ID: 01\nCanceling goal...\nGoal canceled.\n",
            "timeout",
        ),
    ],
)
def test_r1_m3_a_test_that_ran_out_of_time_says_whether_it_was_cancelled(
    m, code, text, verdict
):
    m.start_a()
    m.start_b()
    m.gripper_code, m.gripper_out = code, text
    grip(m)
    last = m.doc()["gripper_test"]["last"]
    assert (last["ok"], last["code"]) == (False, verdict)
    assert m.doc()["operation"]["code"] == verdict


# --- R1 M4 / R2 1: the probe whether a gripper stack runs ends with a defined verdict ----


@pytest.mark.parametrize("status", [124, 137, 1, 2, 143])
def test_r1_m4_the_warning_is_the_answer_whatever_status_ended_the_command(m, status):
    m.m_ready()
    m.controllers_out, m.controllers_code = NO_MANAGER, status
    m.act.preflight_a({"request_id": "pre-a-00000051"})
    assert m.terminal("b")["state"] == "idle"
    assert m.terminal("b")["reason"] == "b.not_running"


@pytest.mark.parametrize("status", [124, 137, 1])
def test_r1_m4_a_command_ended_before_it_could_say_is_unknown_with_its_own_reason(
    m, status
):
    m.m_ready()
    m.controllers_out, m.controllers_code = WAITING_LINE, status
    m.act.preflight_a({"request_id": "pre-a-00000052"})
    assert m.terminal("b")["state"] == "unknown"
    assert m.terminal("b")["reason"] == "b.probe_no_answer"
    # ... and nothing is started on a part of the stack that cannot be seen
    assert "gate.layers_known" in m.blockers("a")


def test_r1_m4_a_runner_that_gave_up_is_unknown_not_absent(m):
    m.m_ready()
    m.controllers_out, m.controllers_code = "did not finish in time", 124
    m.act.preflight_a({"request_id": "pre-a-00000053"})
    assert m.terminal("b")["reason"] == "b.probe_no_answer"


def test_r1_m4_the_probe_is_bounded_from_inside_and_from_outside(m):
    m.m_ready()
    m.nodes_out = ""  # (the graph proves nothing: the controller manager is asked)
    m.act.preflight_a({"request_id": "pre-a-00000054"})
    [cmd] = [c for c in m.oneshots if "list_controllers" in " ".join(c)]
    assert cmd[cmd.index("--") + 1 :][:6] == ["timeout", "-s", "INT", "-k", "3", "20"]
    [run] = [c for c in m.calls if c[0] == "systemd-run" and "probe-b-1" in " ".join(c)]
    limit = int(
        next(a for a in run if a.startswith("--property=RuntimeMaxSec=")).split("=")[2]
    )
    assert limit == stack_units.PROBE_RUNTIME_MAX_S
    assert limit > stack_units.LIST_TIMEOUT_S + stack_units.LIST_KILL_AFTER_S


def test_the_fast_probe_is_bounded_from_inside_and_from_outside(m):
    m.m_ready()
    m.act.preflight_a({"request_id": "pre-a-00000056"})
    # (the button's check is two answers of the graph, the same command twice)
    cmds = [c for c in m.oneshots if " node list " in " ".join(c)]
    assert len(cmds) == 2 and cmds[0] == cmds[1]
    cmd = cmds[0]
    assert cmd[cmd.index("--") + 1 :] == [
        "timeout", "-s", "INT", "-k", "2", "8",
        "ros2", "node", "list", "--no-daemon", "--spin-time", "2.0",
    ]  # fmt: skip
    runs = [
        c for c in m.calls if c[0] == "systemd-run" and "probe-b-nodes" in " ".join(c)
    ]
    assert len(runs) == 2
    [limit] = {
        int(
            next(a for a in run if a.startswith("--property=RuntimeMaxSec=")).split(
                "="
            )[2]
        )
        for run in runs
    }
    assert limit == stack_units.NODES_RUNTIME_MAX_S
    assert limit > stack_units.NODES_TIMEOUT_S + stack_units.NODES_KILL_AFTER_S
    assert not m.oneshots_of("list_controllers"), "the slow question was not needed"


def test_r1_m4_a_present_gripper_stack_is_foreign_running_not_unknown(m):
    # R2 1: B started by hand, listed by its controller manager
    m.m_ready()
    m.controllers_out, m.controllers_code = CONTROLLERS_OUT, 0
    m.act.preflight_a({"request_id": "pre-a-00000055"})
    assert m.terminal("b")["state"] == "foreign_running"
    assert m.terminal("b")["reason"] == "b.foreign_running"


# --- R1 M5: a failed stop is not acknowledged while processes are left -------------------


def hung_a(m):
    m.start_a()
    m.units["a"]["hangs"] = True
    m.act.stop("a", m.stop_body("a", "stop-a-hang01"))
    assert m.terminal("a")["state"] == "failed"
    assert m.doc()["operation"]["code"] == "stop_failed"


def test_r1_m5_a_unit_with_processes_left_is_not_reset(m):
    hung_a(m)
    assert refused(m.act.stop, "a", m.stop_body("a", "stop-a-hang02")) == (
        "stop_left_processes"
    )
    assert not [c for c in m.calls if "reset-failed" in c]
    # the page keeps naming it, and it is still ours to stop
    t = m.terminal("a")
    assert t["state"] == "failed" and t["owned"] and t["can_stop"]
    assert record.read_record(m.state_root / "a")["phase"] == "stop_failed"


def test_r1_m5_the_refusal_names_the_unit_and_does_not_send_anyone_to_a_terminal(m):
    hung_a(m)
    with pytest.raises(stack.Refuse) as caught:
        m.act.stop("a", m.stop_body("a", "stop-a-hang02"))
    text = caught.value.message
    assert "levi-aeri-stack-marvel0-a" in text and "emergency stop" in text
    assert "terminal" not in text.lower() and caught.value.status == 409


def test_r1_m5_it_sends_sigint_again_never_a_kill(m):
    hung_a(m)
    refused(m.act.stop, "a", m.stop_body("a", "stop-a-hang02"))
    assert m.kills == [("levi-aeri-stack-marvel0-a", "--signal=SIGINT")]


def test_r1_m5_once_the_processes_are_gone_it_is_acknowledged(m):
    hung_a(m)
    m.units["a"]["ends_on_kill"] = True
    refused(m.act.stop, "a", m.stop_body("a", "stop-a-hang02"))
    answer = m.act.stop("a", m.stop_body("a", "stop-a-hang03"))
    assert answer["done"] == "acknowledged"
    assert [c for c in m.calls if "reset-failed" in c]
    assert record.read_record(m.state_root / "a")["phase"] == "acknowledged"


def test_r1_m5_a_unit_whose_task_count_cannot_be_read_uses_its_control_group(m):
    hung_a(m)
    m.units["a"]["tasks"] = "[not set]"
    group = "/user.slice/app.slice/levi-aeri-stack-marvel0-a.service"
    m.cgroup_pids[group] = [4321]
    assert refused(m.act.stop, "a", m.stop_body("a", "stop-a-hang02")) == (
        "stop_left_processes"
    )
    m.cgroup_pids[group] = []
    assert m.act.stop("a", m.stop_body("a", "stop-a-hang03"))["done"] == "acknowledged"


def test_r1_m5_a_vanished_unit_needs_no_check(m):
    m.start_a()
    del m.units["a"]
    assert m.act.stop("a", m.stop_body("a"))["done"] == "acknowledged"
    assert m.kills == []


# --- R2 2: a healthy C is not failed by lines it prints later ----------------------------


def test_r2_2_c_stays_ready_when_startimp_prints_the_line_again(m):
    m.all_up()
    assert m.terminal("c")["state"] == "ready"
    m.emit("c", ready_lines("c")[1])  # "Impedance controller started" again
    assert m.terminal("c")["state"] == "ready"
    assert m.terminal("c")["failure"] is None


def test_r2_2_c_stays_ready_after_a_flask_traceback_of_a_500_answer(m):
    m.all_up()
    assert m.terminal("c")["state"] == "ready"
    m.emit(
        "c",
        "[2026-10-21 10:00:00,000] ERROR in app: Exception on /pose [POST]",
        "Traceback (most recent call last):",
        '  File "flask/app.py", line 1, in wsgi_app',
        "KeyError: 'x'",
    )
    assert m.terminal("c")["state"] == "ready"


def test_r2_2_the_latch_is_in_the_record_and_survives_a_restart_of_the_page(m):
    m.all_up()
    assert m.terminal("c")["state"] == "ready"
    assert record.read_record(m.state_root / "c")["ready_at"] > 0
    m.emit("c", ready_lines("c")[1], "Traceback (most recent call last):")
    again = stack_act.Act(
        stack.Stack(m.host()), m.act_host(), setup=lambda: (m.loaded, m.specs)
    )
    again.stack.act = again
    assert again.stack.view()["act"]["terminals"]["c"]["state"] == "ready"


def test_r2_2_a_start_up_that_repeats_a_line_before_it_is_ready_still_fails(m):
    m.start_a()
    m.start_b()
    m.controllers_out, m.controllers_code = CONTROLLERS_OUT, 0
    m.act.preflight_c({"request_id": "pre-c-00000001"})
    m.act.start("c", m.start_body("c"))
    m.emit("c", *ready_lines("c"), ready_lines("c")[1])
    m.c_up(lines=False)
    t = m.terminal("c")
    assert (t["state"], t["failure"]) == ("failed", "duplicate_text")


def test_r2_2_a_second_run_does_not_inherit_the_latch(m):
    m.all_up()
    assert m.terminal("a")["state"] == "ready"
    assert "ready_at" in record.read_record(m.state_root / "a")
    m.act.stop("c", m.stop_body("c"))
    m.act.stop("b", m.stop_body("b"))
    m.act.stop("a", m.stop_body("a"))
    m.run_no = 2
    m.ready_for_a()
    m.act.start("a", m.start_body("a", "start-a-second1"))
    assert "ready_at" not in record.read_record(m.state_root / "a")
    assert m.terminal("a")["state"] == "starting"


def test_r2_2_a_failure_after_ready_still_shows_when_the_unit_itself_fails(m):
    m.all_up()
    assert m.terminal("a")["state"] == "ready"
    m.units["a"]["active"] = "failed"
    assert m.terminal("a")["state"] == "failed"


# --- R2 3: losing the evidence after ready is "unknown", not "ready_timeout" -------------


def test_r2_3_a_stale_health_file_after_ready_is_unknown_health_lost(m):
    m.all_up()
    assert m.terminal("a")["state"] == "ready" and m.terminal("c")["state"] == "ready"
    m.t += 3600  # an hour later: M restarted, the file is stale
    for key in "ac":
        t = m.terminal(key)
        assert (t["state"], t["reason"], t["failure"]) == (
            "unknown",
            f"{key}.health_lost",
            None,
        ), key
    assert m.terminal("b")["state"] == "ready"  # (B has no health evidence)


def test_r2_3_the_unit_is_ready_again_when_the_file_is_fresh_again(m):
    m.all_up()
    assert m.terminal("a")["state"] == "ready"
    m.t += 3600
    assert m.terminal("a")["state"] == "unknown"
    m.health(hardware=True, controller=True, red=False, mode="MOVE")
    assert m.terminal("a")["state"] == "ready"


def test_r2_3_a_waiting_sequence_does_not_end_for_lost_evidence(m):
    from test_marvel0_stack_act import run_sequence, start_sequence

    start_sequence(m)
    seq = run_sequence(m)
    assert seq["state"] == "waiting_confirm"
    m.t += 120  # (inside the wait) the health file goes stale
    assert m.terminal("a")["state"] == "unknown"
    seq = m.act.advance()
    assert seq["state"] == "waiting_confirm" and seq["reason"] is None
    # a real loss still ends it
    del m.units["a"]
    assert m.act.advance()["reason"] == "a_lost"


def test_r2_3_the_notice_about_the_open_port_stays_while_c_cannot_be_judged(m):
    m.all_up()
    m.terminal("c")
    m.t += 3600
    assert m.terminal("c")["state"] == "unknown"
    assert {"code": "notice.c_open_port"} in m.doc()["notices"]


# --- R2 4: the way out does not wait for read-only work, nor for a broken probe ----------


def test_r2_4_a_stop_does_not_wait_for_the_checks_before_c(m):
    m.all_up()
    seen = []

    def during_the_check(cmd):
        if not seen and "list_controllers" in " ".join(cmd):
            check = m.act._op  # the check that is running
            seen.append((check, m.act.stop("c", m.stop_body("c", "stop-c-during"))))

    m.on_oneshot = during_the_check
    m.controllers_out, m.controllers_code = CONTROLLERS_OUT, 0
    earlier = m.act._c_pre
    m.act.preflight_c({"request_id": "pre-c-00000077"})
    [(check, answer)] = seen
    assert answer["done"] == "stopping"
    assert [c for c in m.stop_calls() if OURS["c"] in c]
    # the check was superseded: it ends as such and its result is not kept
    assert (check["kind"], check["state"], check["code"]) == (
        "preflight_c",
        "failed",
        "superseded",
    )
    assert m.act._c_pre is earlier


def test_r2_4_stopping_b_ends_a_gripper_test_that_is_running(m):
    m.start_a()
    m.start_b()
    seen = []

    def during_the_test(cmd):
        if "send_goal" in " ".join(cmd) and not seen:
            seen.append(m.act.stop("b", m.stop_body("b", "stop-b-during")))

    m.on_oneshot = during_the_test
    grip(m)
    [answer] = seen
    assert answer["done"] == "stopping"
    ended = [c for c in m.stop_calls() if "oneshot-gripper" in c[-1]]
    assert ended, "the one-shot unit of the test was stopped (SIGINT cancels the goal)"
    last = m.doc()["gripper_test"]["last"]
    assert (last["ok"], last["code"]) == (False, "cancelled")
    assert m.terminal("b")["state"] == "idle"


def test_r2_4_a_stop_of_a_other_unit_leaves_the_gripper_test_alone(m):
    m.all_up()
    seen = []

    def during_the_test(cmd):
        if "send_goal" in " ".join(cmd) and not seen:
            seen.append(m.act.stop("c", m.stop_body("c", "stop-c-during")))

    m.on_oneshot = during_the_test
    grip(m)
    assert seen and not [c for c in m.stop_calls() if "oneshot-gripper" in c[-1]]
    assert m.doc()["gripper_test"]["last"]["code"] == "reached"


def test_r2_4_a_stop_of_the_same_unit_twice_is_one_operation(m):
    m.start_a()
    one = m.act._begin("stop_a", "a")
    assert refused(m.act._begin, "stop_a", "a") == "operation_running"
    assert one["state"] == "running"


def test_r2_4_two_stops_of_different_units_do_not_wait_for_each_other(m):
    one = m.act._begin("stop_c", "c")
    two = m.act._begin("stop_b", "b")
    assert one["state"] == two["state"] == "running"


def test_r2_4_a_start_still_waits_for_every_operation(m):
    m.act._begin("stop_c", "c")
    assert refused(m.act._begin, "preflight_a") == "operation_running"
    assert refused(m.act._begin, "gripper", "b") == "operation_running"


def test_r2_4_a_stop_supersedes_a_check_that_is_running(m):
    check = m.act._begin("preflight_c")
    m.act._begin("stop_c", "c")
    assert check["_superseded"] is True


def test_r2_4_only_the_stop_of_b_supersedes_the_gripper_test(m):
    test = m.act._begin("gripper", "b")
    m.act._begin("stop_c", "c")
    assert test["_superseded"] is False
    m.act._begin("stop_b", "b")
    assert test["_superseded"] is True


def test_r2_4_a_raising_run_probe_never_blocks_a_stop(m):
    m.all_up()

    def broken():
        raise RuntimeError("the run supervisor is broken")

    stack_act.register_run_probe(broken)
    assert stack_act.hosted_run_active() is None
    assert "run_active" in m.blockers("c", "start")  # a start is not allowed ...
    for key in "cba":  # ... the way out is, in its order
        assert m.terminal(key)["can_stop"] is True, key
        assert "run_active" not in m.blockers(key, "stop")
        assert m.act.stop(key, m.stop_body(key, f"stop-{key}-probe1"))["done"] == (
            "stopping"
        )


def test_r2_4_a_raising_probe_does_not_lift_the_rule_about_a_connected_client(m):
    m.all_up()
    stack_act.register_run_probe(lambda: 1 / 0)
    m.socks.append(_sock(40000, net.ESTABLISHED, remote_port=SERVER_PORT))
    assert refused(stop_c, m) == "client_connected"
    answer = stop_c(m, "stop-c-override", client_override=stack_act.CLIENT_OVERRIDE)
    assert answer["done"] == "stopping"


def test_r2_4_a_run_that_is_known_to_be_active_still_blocks_the_stop(m):
    m.all_up()
    m.run_active = True
    assert "run_active" in m.blockers("c", "stop")
    assert refused(stop_c, m) == "run_active"


def test_r1_l3_before_a_probe_is_registered_nothing_starts_but_everything_stops(m):
    m.all_up()
    stack_act._RUN_PROBES[:] = []
    assert stack_act.hosted_run_active() is None
    assert m.act.stop("c", m.stop_body("c", "stop-c-noprobe"))["done"] == "stopping"
    assert "run_active" in m.blockers("c", "start")
    assert m.terminal("a")["can_stop"] is True


# --- R1 L1: a stop says what happened only after it happened ---------------------------


def test_r1_l1_the_stop_answer_does_not_claim_a_stop(m):
    m.start_a()
    answer = m.act.stop("a", m.stop_body("a"))
    assert answer["done"] == "stopping" and answer["note"] is None


def test_r1_l1_a_ready_a_carries_no_note_about_a_stop_that_has_not_happened(m):
    m.start_a()
    assert m.terminal("a")["state"] == "ready" and m.terminal("a")["stop_note"] is None


def test_r1_l1_the_note_comes_when_the_unit_is_gone_and_the_arm_is_quiet(m):
    m.start_a()
    m.act.stop("a", m.stop_body("a"))
    assert m.terminal("a")["state"] == "idle"
    assert m.terminal("a")["stop_note"] == "a.stop_fci_dropped"


def test_r1_l1_no_note_while_the_arm_still_shows_a_connection(m):
    m.start_a()
    m.act.stop("a", m.stop_body("a"))
    m.sockets = {"state": "active", "detail": "TIME_WAIT"}
    assert m.terminal("a")["stop_note"] is None


def test_r1_l1_c_says_the_arm_is_still_held_only_after_it_stopped(m):
    m.all_up()
    assert m.terminal("c")["state"] == "ready" and m.terminal("c")["stop_note"] is None
    m.act.stop("c", m.stop_body("c"))
    m.health(hardware=True, controller=True, red=False, mode="MOVE")
    assert m.terminal("c")["state"] == "idle"
    assert m.terminal("c")["stop_note"] == "c.stop_arm_still_held"


# --- R1 L2: a request id is remembered across a restart --------------------------------


def restarted(m):
    again = stack_act.Act(
        stack.Stack(m.host()), m.act_host(), setup=lambda: (m.loaded, m.specs)
    )
    again.stack.act = again
    return again


def test_r1_l2_a_repeated_gripper_request_after_a_restart_does_not_move_it_again(m):
    m.start_a()
    m.start_b()
    grip(m, "open", "grip-0000001")
    sent = len([c for c in m.oneshots if "send_goal" in " ".join(c)])
    again = restarted(m)
    answer = again.gripper(
        {
            **m.body("gripper_open", "grip-0000001"),
            "action": "open",
            "attest": {"hands_clear": True},
        }
    )
    assert answer["repeated"] is True
    assert len([c for c in m.oneshots if "send_goal" in " ".join(c)]) == sent


def test_r1_l2_the_same_id_for_another_request_is_refused_after_a_restart(m):
    m.start_a()
    m.start_b()
    grip(m, "open", "grip-0000001")
    again = restarted(m)
    assert (
        refused(
            again.gripper,
            {
                **m.body("gripper_close", "grip-0000001"),
                "action": "close",
                "attest": {"hands_clear": True},
            },
        )
        == "request_id_used"
    )


def test_r1_l2_the_remembered_answers_are_bounded(m):
    for n in range(40):
        m.act.marker_op("touch", m.body("marker_touch", f"mk-touch-{n:06d}"))
        m.act.marker_op("release", m.body("marker_clear", f"mk-clear-{n:06d}"))
    kept = list((m.state_root / "replay" / "requests").iterdir())
    assert 0 < len(kept) <= stack.ANSWERS_KEPT


# --- R1 L4: stopping A or B with C not seen is allowed, with a warning -------------------


def test_r1_l4_a_stop_of_a_with_an_unreadable_socket_table_carries_a_warning(m):
    m.all_up()
    m.act.stop("c", m.stop_body("c"))
    m.table_ok = False  # C can no longer be told
    assert m.terminal("c")["state"] == "unknown"
    rows = m.terminal("a")["stop_blockers"]
    assert [(r["code"], r["level"]) for r in rows] == [("stop_c_unknown", "warn")]
    assert m.terminal("a")["can_stop"] is True
    assert m.act.stop("a", m.stop_body("a"))["done"] == "stopping"


def test_r1_l4_a_stop_of_a_with_c_running_is_still_refused(m):
    m.all_up()
    assert refused(m.act.stop, "a", m.stop_body("a")) == "stop_c_first"


# --- R1 L5: systemd-run that did not answer in time ------------------------------------


def test_r1_l5_a_timeout_of_systemd_run_is_not_a_failed_start(m):
    m.ready_for_a()
    m.run_codes["systemd-run"] = 124
    with pytest.raises(stack.Refuse) as caught:
        m.act.start("a", m.start_body("a"))
    assert caught.value.code == "start_unconfirmed" and caught.value.status == 504
    assert "may be starting" in caught.value.message
    assert record.read_record(m.state_root / "a")["phase"] == "intent"


def test_r1_l5_another_failure_of_systemd_run_is_still_start_failed(m):
    m.ready_for_a()
    m.run_codes["systemd-run"] = 1
    assert refused(m.act.start, "a", m.start_body("a")) == "start_failed"


# --- R1 L6: the heavy-jobs sentence says what the marker does ---------------------------


def test_r1_l6_the_hazard_does_not_promise_to_pause_every_heavy_job(m):
    text = stack_act.HAZARDS["a"][1]
    assert "Heavy jobs on this computer" not in text
    assert "honour the marker" in text


# --- R1 section 2 (invariants with no code behind them) ---------------------------------


def test_r1_s2_2_a_guided_start_that_is_too_old_does_not_start_a(m):
    from test_marvel0_stack_act import start_sequence

    start_sequence(m)
    for _ in range(10):  # monitor, probe, marker
        if m.act.advance()["step"] == "wait":
            break
    assert m.act._seq["step"] == "wait"
    m.t += stack_act.SEQUENCE_ARM_WINDOW_S + 70
    m.no_manager()
    seq = m.act.advance()
    assert seq["state"] == "cancelled" and seq["reason"] == "expired"
    assert m.systemd_runs("a") == []


def test_r1_s2_2_the_window_is_not_part_of_what_the_page_shows(m):
    from test_marvel0_stack_act import start_sequence

    start_sequence(m)
    assert set(m.doc()["sequence"]) <= {
        "state",
        "kind",
        "step",
        "next",
        "reason",
        "since",
        "step_at",
        "remaining_s",
        "through",
        "pauses",
        "steps",
        "reason_params",
        "took_s",
        "elapsed_s",
        "wait",
    }


def test_r1_s2_3_a_product_unit_with_another_workspace_acts_on_nothing(m):
    m.m_ready()
    m.workspace_main = False
    for key in "abc":
        assert "gate.product" in m.blockers(key)
    assert refused(m.act.start, "a", m.start_body("a")) == "gate.product"
    assert "gate.product" in m.blockers("a", "stop")


def test_r1_s2_3_the_workspace_is_the_main_checkouts_state_folder():
    from pathlib import Path

    from levi import paths
    from levi.marvel0 import site

    host = stack_act.production_host()
    ok = host.workspace_ok()
    same = (
        Path(paths.workspace()).resolve() == (site.main_checkout() / ".state").resolve()
    )
    assert ok is same


def test_r1_s2_4_the_same_request_id_with_another_body_is_not_the_same_request(m):
    m.ready_for_a()
    body = m.start_body("a", "start-a-reused01")
    m.act.start("a", body)
    other = {
        **body,
        "attest": {"desk_done": True, "estop_hand": True, "hands_clear": True},
    }
    assert refused(m.act.start, "a", other) == "request_id_used"


def test_r1_s2_7_the_unit_names_are_the_reviewed_constants():
    # (the rehearsal changes them in its own process; the product never does)
    assert stack.STACK_UNITS == {
        "A": "levi-aeri-stack-marvel0-a.service",
        "B": "levi-aeri-stack-marvel0-b.service",
        "C": "levi-aeri-stack-marvel0-c.service",
    }
    assert stack_units.ONESHOT_PREFIX == "levi-aeri-stack-marvel0-oneshot-"


# --- R2 7, 8: records under their lock; a stop that succeeded stays succeeded ------------


def test_r2_7_the_record_is_updated_under_its_lock(m, monkeypatch):
    m.start_a()
    seen = []
    real = record.update_record

    def spy(directory, **fields):
        seen.append(sorted(fields))
        return real(directory, **fields)

    monkeypatch.setattr(record, "update_record", spy)
    m.act.stop("a", m.stop_body("a"))
    assert seen, "the stop wrote its record through update_record"


def test_r2_8_an_error_after_a_good_stop_does_not_rewrite_the_operation(m, monkeypatch):
    m.start_a()

    def boom():
        raise OSError("the marker cannot be examined")

    monkeypatch.setattr(m.act, "_release_after_stop", boom)
    m.act.stop("a", m.stop_body("a"))
    op = m.doc()["operation"]
    assert (op["state"], op["code"]) == ("done", "a.stopped")


def test_r2_8_an_operation_is_finished_once(m):
    op = m.act._begin("preflight_a")
    m.act._finish(op, True, "x")
    m.act._finish(op, False, "OSError")
    assert (op["state"], op["code"]) == ("done", "x")


def test_r2_8_a_page_that_starts_finishes_a_marker_release_the_last_one_left(m):
    m.start_a()
    m.act.stop("a", m.stop_body("a"))
    # the marker was ours and A is gone, but the release loop died with the page
    m.marker_path.write_text("")
    m.act._remember_marker("created")
    m.sockets = {"state": "inactive", "detail": ""}
    assert m.marker_path.exists()
    restarted(m).resume()
    assert not m.marker_path.exists()


def test_r2_8_the_page_resumes_when_the_actions_are_attached(m, monkeypatch):
    from levi.marvel0 import stack_act_api

    calls = []
    monkeypatch.setattr(stack_act.Act, "resume", lambda self: calls.append(self))
    fresh = stack.Stack(m.host())
    attached = stack_act_api.attach(fresh)
    assert calls == [attached] and stack_act_api.attach(fresh) is attached
    assert calls == [attached]  # (once)


def test_r2_8_a_page_that_starts_leaves_a_marker_it_does_not_own(m):
    m.start_a()
    m.act.stop("a", m.stop_body("a"))
    m.marker_path.write_text("")  # a person's marker
    restarted(m).resume()
    assert m.marker_path.exists()


# --- R2 9: a ready unit's log is kept to a size ------------------------------------------


def test_r2_9_a_ready_units_log_is_cut_when_it_grows_past_the_cap(m, monkeypatch):
    m.all_up()
    log = m.units["c"]["log"]
    m.terminal("c")  # latches
    monkeypatch.setattr(stack_units, "LOG_CAP_BYTES", 2000)
    monkeypatch.setattr(stack_units, "LOG_KEEP_BYTES", 500)
    m.emit("c", *[f"127.0.0.1 - GET /state {n}" for n in range(200)])
    assert log.stat().st_size > 2000
    m.t += stack_act.LOG_TRIM_EVERY_S + 1
    m.health(hardware=True, controller=True, red=False, mode="MOVE")
    assert m.terminal("c")["state"] == "ready"
    assert log.stat().st_size <= 600


def test_r2_9_a_unit_that_is_not_ready_yet_keeps_its_whole_log(m, monkeypatch):
    m.ready_for_a()
    m.act.start("a", m.start_body("a"))
    log = m.units["a"]["log"]
    monkeypatch.setattr(stack_units, "LOG_CAP_BYTES", 100)
    m.emit("a", *[f"line {n}" for n in range(100)])
    size = log.stat().st_size
    m.terminal("a")
    assert log.stat().st_size == size


# --- R2 10: reading the document does not wait for the guided start ---------------------


def test_r2_10_the_document_is_built_while_the_sequence_starts_a(m):
    from test_marvel0_stack_act import start_sequence

    start_sequence(m)
    inside, release, done = threading.Event(), threading.Event(), []

    def hold(argv):
        if any("stack-marvel0-a" in a for a in argv):
            inside.set()
            release.wait(10)

    m.on_systemd_run = hold

    def drive():
        for _ in range(12):
            if m.act.advance().get("step") == "wait":
                m.t += 40
                m.no_manager()
            if inside.is_set():
                return

    worker = threading.Thread(target=drive, daemon=True)
    worker.start()
    assert inside.wait(10), "the sequence reached the start of A"
    reader = threading.Thread(target=lambda: done.append(m.sup.view()), daemon=True)
    reader.start()
    reader.join(3)
    try:
        assert done, "the document waited for the sequence's systemd-run"
    finally:
        release.set()
        worker.join(5)


def test_r2_10_a_cancel_during_a_step_is_not_undone_by_it(m):
    from test_marvel0_stack_act import start_sequence

    start_sequence(m)
    seq = m.act._seq
    m.act._seq_end("cancelled", "cancelled")
    m.act._goto(seq, "probe")
    m.act._seq_end("failed", "boom")
    assert (seq["state"], seq["reason"], seq["step"]) == (
        "cancelled",
        "cancelled",
        None,
    )


# --- R2 11: the duplicate launch of A is told why it vanished ---------------------------


def test_r2_11_a_duplicate_launch_that_exited_cleanly_says_why_it_vanished(m):
    m.ready_for_a()
    m.act.start("a", m.start_body("a"))
    m.emit(
        "a",
        "[INFO] [launch.user]: A Franka ros2_control_node is already running. "
        "Stop it first (Ctrl+C in its terminal A) before starting a new arm controller.",
    )
    del m.units["a"]  # exit 0: the transient unit is collected
    t = m.terminal("a")
    assert (t["state"], t["reason"], t["failure"]) == (
        "dead",
        "a.unit_gone",
        "duplicate_launch",
    )


def test_r2_11_a_unit_that_vanished_without_a_known_line_has_no_failure(m):
    m.ready_for_a()
    m.act.start("a", m.start_body("a"))
    m.a_up()
    del m.units["a"]
    t = m.terminal("a")
    assert (t["state"], t["failure"]) == ("dead", None)


# --- R1 W5: the rehearsal does not run beside a robot process ----------------------------


def test_r1_w5_the_rehearsal_refuses_when_a_robot_process_exists(monkeypatch, capsys):
    import rehearse_stack

    called = []
    monkeypatch.setattr(sys, "argv", ["rehearse_stack.py"])
    monkeypatch.setattr(
        rehearse_stack, "passive_robot_check", lambda: ["ros2_control_node --ros-args"]
    )
    monkeypatch.setattr(rehearse_stack, "Rehearsal", lambda work: called.append(work))
    monkeypatch.setattr(
        rehearse_stack.tempfile, "mkdtemp", lambda **kw: called.append(kw)
    )
    assert rehearse_stack.main() == 2
    assert called == [], "nothing was set up or started"
    assert "REFUSED" in capsys.readouterr().out


def test_r1_w5_the_robot_pattern_is_the_one_the_demo_check_uses():
    import rehearse_stack

    for word in (
        "ros2_control_node",
        "franka_server",
        "enpire_franka",
        "serve_policy",
        "run_robotiq_client",
    ):
        assert word in rehearse_stack.ROBOT_PATTERN
