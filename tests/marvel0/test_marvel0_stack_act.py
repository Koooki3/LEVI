"""Start and stop of the arm stack from the marvel0 page (``levi/marvel0/
stack_act.py``), on the fake machine of ``stack_act_machine.py`` (L1): which
gate refuses what, in which order, with what record; what ready and failed mean;
the order of a stop; the marker; the checks before A and C; the gripper test;
the guided sequence; and what the document says.

Nothing here starts a process or opens a socket: ``systemctl`` and
``systemd-run`` are scripted, the real argv is built and recorded and never
run (the guard of ``test_marvel0_deploy.py`` makes any process, socket or
``/proc`` read fail the test)."""

import fcntl
import json
import os
import stat
from pathlib import Path

import pytest
from stack_act_machine import (
    CONTROLLERS_OUT,
    OURS,
    SERVER_PORT,
    ActMachine,
    ready_lines,
)
from test_marvel0_deploy import forbid_real_machine  # noqa: F401 - the guard
from test_marvel0_stack import _sock

from levi.marvel0 import net, record, stack, stack_act, stack_units


@pytest.fixture
def m(tmp_path):
    return ActMachine(tmp_path)


def refused(call, *args):
    """The code of the refusal ``call(*args)`` raises."""
    with pytest.raises(stack.Refuse) as caught:
        call(*args)
    return caught.value.code


def codes(rows):
    return [row["code"] for row in rows]


def a_record(m, key="a"):
    return record.read_record(m.state_root / key)


# --- who may act -------------------------------------------------------------------------


def test_a_worktree_instance_refuses_every_write_and_runs_nothing(tmp_path):
    m = ActMachine(tmp_path, product=False)
    doc = m.sup.view()
    assert doc["profile"] == "fake" and doc["act"]["enabled"] is True
    for key in "abc":
        assert "gate.product" in codes(doc["act"]["terminals"][key]["start_blockers"])
    calls = [
        (m.act.start, "a", m.start_body("a")),
        (m.act.start, "b", m.start_body("b")),
        (m.act.start, "c", m.start_body("c")),
        (m.act.stop, "a", m.stop_body("a")),
        (m.act.stop, "c", m.stop_body("c")),
        (m.act.marker_op, "touch", m.body("marker_touch", "mk-touch-000001")),
        (m.act.marker_op, "adopt", m.body("marker_adopt", "mk-adopt-000001")),
        (m.act.preflight_a, {"request_id": "pre-a-00000001"}),
        (m.act.preflight_c, {"request_id": "pre-c-00000001"}),
        (m.act.gripper, {**m.body("gripper_open", "grip-0000001"), "action": "open"}),
        (m.act.sequence_start, m.start_body("a", "seq-00000001")),
    ]
    for call, *args in calls:
        assert refused(call, *args) == "not_product", call
    assert not m.systemd_runs() and not m.stop_calls() and not m.oneshots
    assert not m.state_root.exists() and not m.marker_path.exists()
    assert not (m.hub / stack.LOCK_FILE).exists()


def test_without_the_control_file_switched_on_nothing_starts(tmp_path):
    m = ActMachine(tmp_path, enabled=False)
    m.m_ready()
    doc = m.doc()
    assert doc["enabled"] is False and doc["control"]["stack_enabled"] is False
    for key in "abc":
        assert codes(doc["terminals"][key]["start_blockers"]) == ["control_disabled"]
        assert doc["terminals"][key]["can_start"] is False
    assert doc["gripper_test"]["can_run"] is False
    for call, *args in [
        (m.act.start, "a", m.start_body("a")),
        (m.act.marker_op, "touch", m.body("marker_touch", "mk-touch-000001")),
        (m.act.preflight_a, {"request_id": "pre-a-00000001"}),
        (m.act.sequence_start, m.start_body("a", "seq-00000001")),
        (m.act.gripper, {**m.body("gripper_open", "grip-0000001"), "action": "open"}),
    ]:
        assert refused(call, *args) == "control_disabled", call
    assert stack_act.TEXT["control_disabled"][0] == 403
    assert not m.systemd_runs() and not m.marker_path.exists()


def test_a_missing_control_file_leaves_the_rest_of_the_document_alone(tmp_path):
    m = ActMachine(tmp_path)
    m.act._setup = lambda: (stack_act.control_site.ControlLoad("missing", None), None)
    doc = m.sup.view()
    assert doc["act"]["control"]["code"] == "control.missing"
    assert doc["terminals"]["M"] and doc["gates"]["A"] and doc["profile"] == "real"


def test_a_stop_needs_no_control_file(m):
    m.start_a()
    m.act._setup = lambda: (stack_act.control_site.ControlLoad("missing", None), None)
    doc = m.doc()
    assert doc["enabled"] is False
    assert doc["terminals"]["a"]["can_stop"] is True
    answer = m.act.stop("a", m.stop_body("a"))
    assert answer["done"] == "stopping" and len(m.stop_calls()) == 1


def test_the_document_is_the_stack_s_act_part_and_only_when_attached(tmp_path):
    m = ActMachine(tmp_path)
    assert m.sup.view()["act"]["schema"] == stack_act.SCHEMA
    bare = stack.Stack(m.host())
    assert bare.view()["act"] is None


# --- starting A --------------------------------------------------------------------------


def test_a_start_runs_one_unit_with_the_designed_properties(m):
    m.ready_for_a()
    answer = m.act.start("a", m.start_body("a"))
    assert answer == {"done": "started", "terminal": "a", "unit": OURS["a"]}
    [argv] = m.systemd_runs("a")
    spec = m.specs.units["a"]
    assert argv[:3] == ["systemd-run", "--user", "--quiet"]
    assert argv[argv.index("--") + 1 :] == list(spec.argv)
    assert "--unit=levi-aeri-stack-marvel0-a" in argv
    for prop in (
        "KillSignal=SIGINT",
        "SendSIGKILL=no",
        "TimeoutStopSec=30",
        "Restart=no",
    ):
        assert f"--property={prop}" in argv
    assert "--property=UnsetEnvironment=CONDA_SHLVL ROS_DISTRO" in argv
    assert not any("--collect" in a for a in argv)
    # Nothing else was started or touched.
    assert m.systemd_runs("b") == [] and m.systemd_runs("c") == []


def test_the_record_is_written_before_the_unit_exists(m):
    m.ready_for_a()
    seen = {}

    def before(argv):
        seen["record"] = a_record(m)
        seen["units"] = dict(m.units)

    m.on_systemd_run = before
    m.act.start("a", m.start_body("a"))
    assert seen["units"] == {} and seen["record"]["phase"] == "intent"
    assert seen["record"]["exec_argv"] == list(m.specs.units["a"].argv)
    after = a_record(m)
    assert after["phase"] == "started" and after["attest"] == {
        "desk_done": True,
        "estop_hand": True,
    }
    assert after["request_id"] == "start-a-0001"


def test_the_folder_and_the_log_are_private(m):
    m.start_a()
    folder = m.state_root / "a"
    assert stat.S_IMODE(os.lstat(folder).st_mode) == 0o700
    assert stat.S_IMODE(os.lstat(folder / "logs").st_mode) == 0o700
    [log] = list((folder / "logs").iterdir())
    assert stat.S_IMODE(os.lstat(log).st_mode) == 0o600
    assert log.name.startswith("a-") and log.suffix == ".log"
    assert a_record(m)["log"] == str(log)


def test_the_machine_wide_lock_is_taken_and_says_who(m):
    m.ready_for_a()
    m.act.start("a", m.start_body("a"))
    note = json.loads((m.hub / stack.LOCK_FILE).read_text())
    assert note["owner"] == "marvel0-page" and note["action"] == "arm_start"


def test_a_held_lock_refuses_without_waiting(m):
    m.ready_for_a()
    fd = os.open(m.hub / stack.LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert refused(m.act.start, "a", m.start_body("a")) == "stack_busy"
    finally:
        os.close(fd)
    assert not m.systemd_runs("a")


def test_a_repeated_request_is_answered_and_not_done_twice(m):
    m.ready_for_a()
    first = m.act.start("a", m.start_body("a"))
    again = m.act.start("a", m.start_body("a"))
    assert again == {**first, "repeated": True}
    assert len(m.systemd_runs("a")) == 1
    # The same id for another action is a different request.
    assert (
        refused(m.act.stop, "a", m.stop_body("a", "start-a-0001")) == "request_id_used"
    )


def test_the_phrase_must_be_typed_exactly(m):
    m.ready_for_a()
    body = {**m.start_body("a"), "confirm": "start arm"}
    assert refused(m.act.start, "a", body) == "confirm_required"
    assert not m.systemd_runs("a")


@pytest.mark.parametrize(
    "key, attest, code",
    [
        ("a", {}, "attest_missing"),
        ("a", {"desk_done": True}, "attest_missing"),
        ("a", {"desk_done": True, "estop_hand": False}, "attest_missing"),
        ("a", {"desk_done": True, "estop_hand": True, "extra": True}, "attest_invalid"),
    ],
)
def test_the_attestations_are_all_needed_and_known(m, key, attest, code):
    m.ready_for_a()
    body = {**m.start_body("a"), "attest": attest}
    assert refused(m.act.start, "a", body) == code
    assert not m.systemd_runs("a")


def test_a_linger_warning_does_not_refuse(m):
    m.linger = "no"
    m.ready_for_a()
    t = m.terminal("a")
    assert t["can_start"] is True
    assert [b["level"] for b in t["start_blockers"] if b["code"] == "gate.linger"] == [
        "warn"
    ]
    m.act.start("a", m.start_body("a"))


def break_nothing(m):
    pass


A_BREAKS = {
    "no monitor": (
        lambda m: (setattr(m, "monitor", None), m.health_path.unlink()),
        "gate.monitor_fresh",
    ),
    "stale monitor": (lambda m: m.health(age=60.0), "gate.monitor_fresh"),
    "layer unknown": (lambda m: setattr(m.act, "_b_probe", None), "gate.layers_known"),
    "a by hand": (lambda m: m.hand_started_a(), "gate.a_absent"),
    "a unit exists": (
        lambda m: m.units.update(
            a={
                "active": "active",
                "sub": "running",
                "result": "success",
                "argv": ["x"],
                "pid": 1,
                "log": m.tmp / "l",
                "opts": [],
            }
        ),
        "gate.a_unit_absent",
    ),
    "no marker": (lambda m: m.marker_path.unlink(), "gate.marker_set"),
    "young marker": (
        lambda m: setattr(m, "t", os.lstat(m.marker_path).st_ctime + 10),
        "marker_young",
    ),
    "arm sockets": (
        lambda m: setattr(m, "sockets", {"state": "active", "detail": ""}),
        "gate.arm_sockets_clear",
    ),
    "unreadable sockets": (
        lambda m: setattr(m, "sockets", {"state": "unknown", "detail": ""}),
        "gate.arm_sockets_clear",
    ),
    "rt low": (lambda m: setattr(m, "rtprio", "0"), "gate.rt_limits"),
    "memlock low": (lambda m: setattr(m, "memlock", "65536"), "gate.rt_limits"),
    "fci closed": (
        lambda m: (
            setattr(m, "fci", {"state": "closed"}),
            m.preflight_a("pre-a-closed01"),
        ),
        "gate.fci_probe",
    ),
    "fci never checked": (lambda m: setattr(m.act, "_fci", None), "gate.fci_probe"),
    "fci stale": (
        lambda m: setattr(m.act, "_fci", {"at": m.t - 1000, "state": "open"}),
        "gate.fci_probe",
    ),
    "health file elsewhere": (
        lambda m: m.set_control(franka_control=str(m.tmp / "other")),
        "control_health_file",
    ),
    "operation running": (lambda m: m.act._begin("preflight_c"), "operation_running"),
    "run active": (lambda m: setattr(m, "run_active", True), "run_active"),
    "arm lock held": (lambda m: m.hold_arm_lock(), "arm_lock_held"),
    "previous start not closed": (
        lambda m: record.write_record(
            m.act._folder("a"), {"phase": "started", "unit": "u", "exec_argv": ["x"]}
        ),
        "record_open",
    ),
}


@pytest.mark.parametrize("name", sorted(A_BREAKS))
def test_each_gate_of_a_refuses_alone(m, name):
    breaker, code = A_BREAKS[name]
    m.ready_for_a()
    assert m.terminal("a")["can_start"], codes(m.terminal("a")["start_blockers"])
    breaker(m)
    assert code in m.blockers("a"), (name, m.blockers("a"))
    assert m.terminal("a")["can_start"] is False
    with pytest.raises(stack.Refuse):
        m.act.start(
            "a", m.start_body("a", f"start-a-{name[:8]}".replace(" ", "-") + "0001")
        )
    assert not m.systemd_runs("a")


def test_the_first_blocker_is_the_refusals_code(m):
    m.ready_for_a()
    m.rtprio = "0"
    assert refused(m.act.start, "a", m.start_body("a")) == "gate.rt_limits"


def test_a_wrapper_that_is_not_the_reviewed_file_is_not_started(m, monkeypatch):
    m.ready_for_a()
    monkeypatch.setattr(stack_units, "WRAPPER_SHA256", "0" * 64)
    assert refused(m.act.start, "a", m.start_body("a")) == "wrapper_changed"
    assert not m.systemd_runs("a")


def test_a_failing_systemd_run_leaves_a_closed_record_and_a_refusal(m):
    m.ready_for_a()
    m.run_codes["systemd-run"] = 1
    with pytest.raises(stack.Refuse) as caught:
        m.act.start("a", m.start_body("a"))
    assert caught.value.code == "start_failed" and caught.value.status == 502
    assert a_record(m)["phase"] == "start_failed"
    m.run_codes.clear()
    assert m.terminal("a")["state"] == "idle"
    assert m.terminal("a")["can_start"] is True


def test_a_timed_out_systemd_run_may_have_started_the_unit(m):
    m.ready_for_a()
    m.run_codes["systemd-run"] = 124
    # (not "failed": the unit may be there; the person is told to look)
    assert refused(m.act.start, "a", m.start_body("a")) == "start_unconfirmed"
    assert a_record(m)["phase"] == "intent"
    assert m.terminal("a")["state"] == "dead"
    assert "record_open" in m.blockers("a")


def test_a_start_is_refused_while_the_manager_environment_cannot_be_read(m):
    m.ready_for_a()
    m.stop_codes["noop"] = 0
    real = m.run

    def run(argv, timeout):
        if argv[:3] == ["systemctl", "--user", "show-environment"]:
            return 1, ""
        return real(argv, timeout)

    m.run = run
    m.act.host.base.run = stack.guarded(run)
    assert refused(m.act.start, "a", m.start_body("a")) == "systemd_unreadable"
    assert not m.systemd_runs("a")


# --- B and C -------------------------------------------------------------------------------


def test_b_starts_only_after_a_is_ready_with_clear_hands(m):
    m.ready_for_a()
    assert "gate.a_running" in m.blockers("b")
    m.start_a()
    assert m.terminal("b")["can_start"] is True
    body = {**m.start_body("b"), "attest": {}}
    assert refused(m.act.start, "b", body) == "attest_missing"
    assert not m.systemd_runs("b")
    assert m.act.start("b", m.start_body("b")) == {
        "done": "started",
        "terminal": "b",
        "unit": OURS["b"],
    }
    [argv] = m.systemd_runs("b")
    assert (
        "--property=LimitRTPRIO=99" in argv
        and "--property=LimitMEMLOCK=infinity" not in argv
    )


B_BREAKS = {
    "serial device missing": (lambda m: m.serial.unlink(), "serial_missing"),
    "b unit exists": (
        lambda m: m.units.update(
            b={
                "active": "active",
                "sub": "running",
                "result": "success",
                "argv": ["x"],
                "pid": 1,
                "log": m.tmp / "l",
                "opts": [],
            }
        ),
        "gate.b_unit_absent",
    ),
    "gripper stack started by hand": (
        lambda m: setattr(
            m.act, "_b_probe", {"at": m.t, "state": "present", "by": "probe"}
        ),
        "gate.b_absent",
    ),
    # (B never looked at, or looked at too long ago, is not here: it does not
    # refuse a press, the press makes the check. See test_marvel0_stack_act_residual.py.)
    "a lost": (
        lambda m: m.sockets.update(state="inactive") or m.no_manager(),
        "gate.a_running",
    ),
    "arm lock held": (lambda m: m.hold_arm_lock(), "arm_lock_held"),
    "run active": (lambda m: setattr(m, "run_active", True), "run_active"),
}


@pytest.mark.parametrize("name", sorted(B_BREAKS))
def test_each_gate_of_b_refuses_alone(m, name):
    breaker, code = B_BREAKS[name]
    m.start_a()
    assert m.terminal("b")["can_start"], codes(m.terminal("b")["start_blockers"])
    breaker(m)
    assert code in m.blockers("b"), (name, m.blockers("b"))
    assert m.terminal("b")["can_start"] is False
    with pytest.raises(stack.Refuse):
        m.act.start("b", m.start_body("b"))
    assert not m.systemd_runs("b")


def up_to_c(m):
    m.start_a()
    m.start_b()
    m.controllers_out, m.controllers_code = CONTROLLERS_OUT, 0
    m.act.preflight_c({"request_id": "pre-c-00000001"})


def test_c_needs_a_b_the_three_checks_and_a_ready_arm(m):
    m.start_a()
    assert "gate.a_running" not in m.blockers("c")
    assert "gate.b_running" in m.blockers("c") and "c_preflight" in m.blockers("c")
    m.start_b()
    m.controllers_out, m.controllers_code = CONTROLLERS_OUT, 0
    m.act.preflight_c({"request_id": "pre-c-00000001"})
    t = m.terminal("c")
    assert t["can_start"] is True, codes(t["start_blockers"])
    for attest in (
        {},
        {"estop_hand": True, "hands_clear": True},
        {"estop_hand": True, "no_client": True},
    ):
        assert (
            refused(m.act.start, "c", {**m.start_body("c"), "attest": attest})
            == "attest_missing"
        )
    assert not m.systemd_runs("c")
    answer = m.act.start("c", m.start_body("c"))
    assert answer["done"] == "started"
    [argv] = m.systemd_runs("c")
    assert "--working-directory=" + str(m.franka / "robot_server") in argv
    assert not any("Limit" in a for a in argv)


C_BREAKS = {
    "a gone": (
        lambda m: (
            m.sockets.update(state="inactive"),
            m.no_manager(),
        ),
        "gate.a_running",
    ),
    "b gone": (lambda m: m.units.pop("b"), "gate.b_running"),
    "no checks": (lambda m: setattr(m.act, "_c_pre", None), "c_preflight"),
    "old checks": (
        lambda m: setattr(m.act, "_c_pre", {**m.act._c_pre, "at": m.t - 1000}),
        "c_preflight",
    ),
    "failed checks": (
        lambda m: setattr(m.act, "_c_pre", {**m.act._c_pre, "ok": False}),
        "c_preflight",
    ),
    "red light": (
        lambda m: m.health(hardware=True, controller=False, red=True, mode="REFLEX"),
        "health_not_ok",
    ),
    "hardware inactive": (
        lambda m: m.health(hardware=False, controller=False, mode="IDLE"),
        "health_not_ok",
    ),
    "robot mode unknown": (
        lambda m: m.health(hardware=True, controller=False, mode="GUIDING"),
        "health_not_ok",
    ),
    "port taken": (
        lambda m: m.socks.append(_sock(SERVER_PORT, net.LISTEN)),
        "port_taken",
    ),
    "client on the policy server": (
        lambda m: m.socks.append(_sock(8000, net.ESTABLISHED, remote_port=40000)),
        "gate.no_client",
    ),
    "table unreadable": (lambda m: setattr(m, "table_ok", False), "port_taken"),
    "arm lock held": (lambda m: m.hold_arm_lock(), "arm_lock_held"),
}


@pytest.mark.parametrize("name", sorted(C_BREAKS))
def test_each_gate_of_c_refuses_alone(m, name):
    up_to_c(m)
    assert m.terminal("c")["can_start"], codes(m.terminal("c")["start_blockers"])
    C_BREAKS[name][0](m)
    assert C_BREAKS[name][1] in m.blockers("c"), (name, m.blockers("c"))
    assert m.terminal("c")["can_start"] is False
    with pytest.raises(stack.Refuse):
        m.act.start("c", m.start_body("c"))
    assert not m.systemd_runs("c")


# --- what ready, failed and dead mean ------------------------------------------------------


def started_a(m):
    m.ready_for_a()
    m.act.start("a", m.start_body("a"))


def a_state(m):
    t = m.terminal("a")
    return t["state"], t["failure"]


def test_a_unit_that_is_active_proves_nothing(m):
    started_a(m)
    assert a_state(m) == ("starting", None)
    t = m.terminal("a")
    assert [r["key"] for r in t["readiness"]["missing"]] == [
        "fifo",
        "spawners",
        "health_hardware_active",
    ]
    assert t["readiness"]["seen"] == []
    assert t["can_stop"] is True and t["can_start"] is False


def test_ready_needs_the_lines_and_the_health_file_together(m):
    started_a(m)
    m.emit("a", *ready_lines("a"))
    assert a_state(m) == ("starting", None)  # lines alone are not enough
    m.health(hardware=True, controller=False, mode="IDLE")
    assert a_state(m) == ("ready", None)
    m.health(hardware=False, controller=False, mode="REFLEX", red=True)
    # the hardware went away: not ready, and not "did not become ready" either
    # (it had been): the evidence is lost (R2 3)
    assert a_state(m) == ("unknown", None)
    assert m.terminal("a")["reason"] == "a.health_lost"


def test_progress_lists_what_was_seen_and_what_is_missing(m):
    started_a(m)
    m.emit("a", *ready_lines("a")[:2])
    m.t += 12
    m.no_manager()
    r = m.terminal("a")["readiness"]
    assert [x["key"] for x in r["seen"]] == ["fifo"] and r["waited_s"] == 12.0
    assert [x["key"] for x in r["missing"]] == ["spawners", "health_hardware_active"]
    assert r["timeout_s"] == m.specs.units["a"].ready_timeout_s
    assert all(x["text"] for x in r["seen"] + r["missing"])


def test_a_unit_never_ready_is_failed_after_its_timeout(m):
    started_a(m)
    m.t += m.specs.units["a"].ready_timeout_s + 1
    m.no_manager()
    assert a_state(m) == ("failed", "ready_timeout")
    assert "record_open" in m.blockers("a") and m.terminal("a")["can_start"] is False
    assert m.terminal("a")["can_stop"] is True


def test_a_failure_line_fails_a_unit_that_is_still_active(m):
    started_a(m)
    m.emit(
        "a", *ready_lines("a"), "Could not enable FIFO RT scheduling policy: error 1"
    )
    m.health(hardware=True, controller=False, mode="IDLE")
    assert a_state(m) == ("failed", "fifo_failed")


def test_a_ready_line_twice_is_a_duplicate_not_a_second_ready(m):
    started_a(m)
    m.emit("a", *ready_lines("a"), ready_lines("a")[1])
    m.health(hardware=True, controller=False, mode="IDLE")
    assert a_state(m) == ("failed", "duplicate_text")


def test_a_unit_that_vanishes_after_it_was_ready_is_dead_not_idle(m):
    m.start_a()
    del m.units["a"]
    assert a_state(m) == ("dead", None)
    assert m.terminal("a")["reason"] == "a.unit_gone"
    assert "record_open" in m.blockers("a")


def test_a_failed_unit_is_failed_and_waits_to_be_cleared(m):
    m.start_a()
    m.units["a"]["active"] = "failed"
    assert a_state(m) == ("failed", "unit_failed")
    assert m.terminal("a")["can_start"] is False and m.terminal("a")["can_stop"] is True


def test_a_unit_that_is_stopping_is_stopping(m):
    m.start_a()
    m.units["a"]["active"] = "deactivating"
    assert a_state(m)[0] == "stopping"
    assert m.terminal("a")["can_stop"] is False


def test_a_unit_of_our_name_with_another_command_is_foreign_and_never_stopped(m):
    m.start_a()
    m.units["a"]["argv"] = ["/usr/bin/sleep", "1000"]
    assert a_state(m)[0] == "foreign_running"
    assert m.terminal("a")["reason"] == "a.unit_foreign"
    assert refused(m.act.stop, "a", m.stop_body("a")) == "unit_foreign"
    assert not m.stop_calls()


def test_an_a_started_in_a_terminal_is_seen_and_cannot_be_stopped_from_here(m):
    m.m_ready()
    m.hand_started_a()
    assert a_state(m)[0] == "foreign_running"
    assert refused(m.act.stop, "a", m.stop_body("a")) == "not_ours"
    assert not m.stop_calls()


def test_a_corrupt_record_is_unknown_and_blocks(m):
    m.start_a()
    (m.state_root / "a" / "record.json").write_text("{not json")
    assert (
        a_state(m)[0] == "unknown" and m.terminal("a")["reason"] == "a.record_corrupt"
    )
    assert m.terminal("a")["can_start"] is False
    assert refused(m.act.stop, "a", m.stop_body("a")) == "record_corrupt"


def test_unreadable_systemd_is_unknown_never_idle(m):
    m.systemd_down = True
    t = m.sup.view()["act"]["terminals"]
    assert {t[k]["state"] for k in "abc"} == {"unknown"}


def test_c_is_ready_with_a_listener_and_the_controller_active(m):
    up_to_c(m)
    m.act.start("c", m.start_body("c"))
    m.emit("c", *ready_lines("c"))
    assert m.terminal("c")["state"] == "starting"
    m.c_up(lines=False)
    assert m.terminal("c")["state"] == "ready"
    m.socks = [s for s in m.socks if s.local_port != SERVER_PORT]
    # nobody listens any more: no longer vouched for (it had been ready)
    assert m.terminal("c")["state"] == "unknown"
    assert m.terminal("c")["reason"] == "c.health_lost"


# --- stopping -----------------------------------------------------------------------------


def test_the_order_of_a_stop_is_client_c_a_b_then_m(m):
    m.all_up()
    m.ours()
    assert codes(m.terminal("a")["stop_blockers"]) == ["stop_c_first"]
    assert codes(m.terminal("b")["stop_blockers"]) == ["stop_c_first"]
    assert "stack_running" in codes(m.terminal("m")["stop_blockers"])
    for key in "ab":
        assert refused(m.act.stop, key, m.stop_body(key)) == "stop_c_first"
    assert refused(m.act.stop, "m", m.body("monitor_stop", "stop-m-000000")) in (
        "stack_running",
        "b_running",
    )
    assert not m.stop_calls()
    m.act.stop("c", m.stop_body("c"))
    m.act.stop("a", m.stop_body("a", "stop-a-000001"))
    assert (
        refused(
            m.act.stop,
            "m",
            m.body("monitor_stop", "stop-m-000001"),
        )
        == "b_running"
    )
    m.act.stop("b", m.stop_body("b", "stop-b-000001"))
    assert (
        m.act.stop("m", m.body("monitor_stop", "stop-m-000002"))["done"]
        == "monitor_stopped"
    )
    assert [c[-1] for c in m.stop_calls()] == [
        OURS["c"],
        OURS["a"],
        OURS["b"],
        stack.MONITOR_SERVICE,
    ]


def test_a_stop_names_only_the_recorded_unit_and_sends_no_signal(m):
    m.all_up()
    answer = m.act.stop("c", m.stop_body("c"))
    assert answer["done"] == "stopping" and answer["unit"] == OURS["c"]
    assert m.stop_calls() == [["systemctl", "--user", "stop", OURS["c"]]]
    assert not any(c[0] in ("kill", "pkill") for c in m.calls)
    rec = a_record(m, "c")
    assert rec["phase"] == "stopped" and rec["stopped_at"] is not None


def test_stopping_c_says_the_arm_is_still_held(m):
    m.all_up()
    answer = m.act.stop("c", m.stop_body("c"))
    # (said once C is stopped, never while the stop may still fail)
    assert answer["note"] is None
    doc = m.doc()
    note = doc["terminals"]["c"]["stop_note"]
    assert note == "c.stop_arm_still_held"
    assert "impedance control" in stack_act.STOP_NOTE_TEXT[note]
    assert {"code": "notice.arm_still_held"} in doc["notices"]
    assert doc["terminals"]["c"]["state"] == "idle"
    assert doc["terminals"]["a"]["can_stop"] is True


def test_a_client_on_c_refuses_the_stop_unless_the_phrase_overrides(m):
    m.all_up()
    m.socks.append(_sock(40000, net.ESTABLISHED, remote_port=SERVER_PORT))
    t = m.terminal("c")
    assert codes(t["stop_blockers"]) == ["client_connected"] and t["can_stop"] is False
    assert (
        t["can_stop_with_override"] is True
        and t["override"] == stack_act.CLIENT_OVERRIDE
    )
    assert refused(m.act.stop, "c", m.stop_body("c")) == "client_connected"
    assert (
        refused(
            m.act.stop, "c", m.stop_body("c", "stop-c-wrong001", client_override="yes")
        )
        == "confirm_required"
    )
    assert not m.stop_calls()
    answer = m.act.stop(
        "c",
        m.stop_body("c", "stop-c-override1", client_override=stack_act.CLIENT_OVERRIDE),
    )
    assert answer["done"] == "stopping" and len(m.stop_calls()) == 1


def test_an_unreadable_socket_table_counts_as_a_client(m):
    m.all_up()
    m.table_ok = False
    assert refused(m.act.stop, "c", m.stop_body("c")) == "client_connected"


def test_the_override_phrase_does_not_lift_any_other_rule(m):
    m.all_up()
    m.run_active = True
    body = m.stop_body("c", client_override=stack_act.CLIENT_OVERRIDE)
    assert refused(m.act.stop, "c", body) == "run_active"
    assert not m.stop_calls()


def test_a_hosted_run_stops_before_anything_else(m):
    m.all_up()
    m.run_active = True
    for key in "abc":
        assert "run_active" in codes(m.terminal(key)["stop_blockers"])


def test_stopping_what_is_not_there_is_already_stopped(m):
    m.m_ready()
    assert m.act.stop("a", m.stop_body("a")) == {
        "done": "already_stopped",
        "terminal": "a",
    }
    assert not m.stop_calls()


def test_a_vanished_unit_is_acknowledged_not_stopped(m):
    m.start_a()
    del m.units["a"]
    m.on_stopped("a")  # the process died with its unit: the arm is quiet
    answer = m.act.stop("a", m.stop_body("a"))
    assert answer == {"done": "acknowledged", "terminal": "a"}
    assert not m.stop_calls()
    assert a_record(m)["phase"] == "acknowledged"
    assert a_state(m)[0] == "idle"


def test_a_failed_unit_is_reset_and_the_record_closed(m):
    m.start_a()
    m.units["a"]["active"] = "failed"
    assert m.act.stop("a", m.stop_body("a"))["done"] == "acknowledged"
    assert [c for c in m.calls if "reset-failed" in c] == [
        ["systemctl", "--user", "reset-failed", OURS["a"]]
    ]
    assert not m.stop_calls()


def test_a_stop_that_fails_is_reported_and_leaves_the_unit_alone(m):
    m.start_a()
    m.stop_codes["stop"] = 1
    m.act.stop("a", m.stop_body("a"))
    op = m.doc()["operation"]
    assert op["state"] == "failed" and op["code"] == "stop_failed"
    assert a_state(m)[0] == "ready"


def test_a_unit_that_hangs_on_stop_is_failed_and_the_page_says_so(m):
    m.start_a()
    m.units["a"]["hangs"] = True
    m.act.stop("a", m.stop_body("a"))
    assert m.doc()["operation"]["code"] == "stop_failed"
    assert a_state(m)[0] == "failed"
    assert len(m.systemd_runs("a")) == 1  # nothing started again


def test_stopping_too_long_is_a_notice(m):
    m.start_a()
    m.units["a"]["active"] = "deactivating"
    rec = a_record(m)
    record.write_record(m.state_root / "a", {**rec, "stop_requested_at": m.t - 40})
    assert {"code": "notice.stopping_too_long", "params": {"unit": "a"}} in m.doc()[
        "notices"
    ]


def test_a_second_operation_waits_for_the_first(m):
    m.all_up()
    m.act._begin("preflight_a")
    # a start waits for it; a stop does not (it ends the check: R2 4)
    assert refused(m.act._begin, "preflight_c") == "operation_running"
    assert "operation_running" in m.blockers("b")
    assert "operation_running" not in codes(m.terminal("c")["stop_blockers"])
    assert m.act.stop("c", m.stop_body("c"))["done"] == "stopping"


def test_b_is_known_to_be_absent_after_we_stopped_it(m):
    m.all_up()
    m.act.stop("c", m.stop_body("c"))
    m.act.stop("a", m.stop_body("a", "stop-a-000001"))
    m.act._b_probe = None
    m.act.stop("b", m.stop_body("b", "stop-b-000001"))
    assert m.terminal("b")["state"] == "idle"


# --- the marker ---------------------------------------------------------------------------


def touch(m, rid="mk-touch-000001"):
    return m.act.marker_op("touch", m.body("marker_touch", rid))


def test_the_marker_is_created_exclusively_and_known_to_be_ours(m):
    assert touch(m) == {"done": "marker_set"}
    assert m.marker_path.exists() and m.doc()["marker"]["ours"] is True
    assert refused(touch, m, "mk-touch-000002") == "marker_exists"
    os.utime(m.marker_path, (1, 1))  # someone touched it: not what we made
    assert m.doc()["marker"]["ours"] is False


def test_a_marker_that_is_a_link_is_not_ours_to_create_over(m):
    target = m.tmp / "elsewhere"
    target.write_text("")
    m.marker_path.symlink_to(target)
    assert refused(touch, m) == "marker_unusable"
    assert target.exists()


def test_a_hand_made_marker_is_adopted_by_one_request_and_then_removable(m):
    m.marker_path.write_text("")
    assert m.doc()["marker"]["ours"] is False
    assert (
        refused(m.act.marker_op, "release", m.body("marker_clear", "mk-clear-000001"))
        == "marker_not_ours"
    )
    assert m.marker_path.exists()
    assert m.act.marker_op("adopt", m.body("marker_adopt", "mk-adopt-000001")) == {
        "done": "marker_adopted"
    }
    assert m.doc()["marker"]["ours"] is True
    assert m.act.marker_op("release", m.body("marker_clear", "mk-clear-000002")) == {
        "done": "marker_cleared"
    }
    assert not m.marker_path.exists()


def test_there_is_nothing_to_adopt_or_release_without_a_marker(m):
    assert (
        refused(m.act.marker_op, "adopt", m.body("marker_adopt", "mk-adopt-000001"))
        == "marker_absent"
    )
    assert (
        refused(m.act.marker_op, "release", m.body("marker_clear", "mk-clear-000001"))
        == "marker_absent"
    )


def test_the_marker_stays_while_a_may_run(m):
    m.start_a()
    assert (
        refused(m.act.marker_op, "release", m.body("marker_clear", "mk-clear-000001"))
        == "marker_a_unit"
    )
    assert m.marker_path.exists()


def test_stopping_a_releases_our_marker_once_the_arm_is_quiet(m):
    m.start_a()
    assert m.marker_path.exists() and m.doc()["marker"]["ours"]
    m.act.stop("a", m.stop_body("a"))
    assert not m.marker_path.exists()
    assert m.slept == []


def test_stopping_a_waits_for_the_arms_sockets_and_gives_up(m):
    m.start_a()
    m.on_stopped = lambda key: m.units.pop(key, None) and None  # sockets stay
    m.act.stop("a", m.stop_body("a"))
    assert m.marker_path.exists()
    assert len(m.slept) == stack_act.RELEASE_POLLS
    assert set(m.slept) == {stack_act.RELEASE_POLL_S}


def test_stopping_a_leaves_a_marker_that_is_not_ours(m):
    m.start_a()
    os.utime(m.marker_path, (1, 1))
    m.act.stop("a", m.stop_body("a"))
    assert m.marker_path.exists()


def test_the_sockets_left_by_a_stop_are_not_someone_elses_a(m):
    m.start_a()
    m.act.stop("a", m.stop_body("a"))
    m.sockets = {"state": "active", "detail": "TIME_WAIT"}
    m.no_manager()
    t = m.terminal("a")
    assert t["state"] == "idle" and t["reason"] == "a.sockets_draining"
    assert "gate.arm_sockets_clear" in m.blockers("a")
    m.t += stack_act.DRAIN_S + 1
    m.no_manager()
    assert m.terminal("a")["state"] == "foreign_running"


# --- the checks before A and C ---------------------------------------------------------------


def test_the_fci_check_is_one_connection_to_the_arm_and_only_that(m):
    m.m_ready()
    assert m.preflight_a() == {"done": "preflight_started", "kind": "preflight_a"}
    assert m.fci_probes == [("10.42.0.157", stack_units.FCI_PORT)]
    assert stack_units.FCI_PORT == 1337
    checks = m.doc()["checks"]
    assert checks["fci"]["state"] == "open" and checks["fci"]["fresh"] is True
    assert m.doc()["operation"]["state"] == "done"


def test_a_closed_fci_port_fails_the_check_and_the_gate(m):
    m.m_ready()
    m.fci = {"state": "unreachable"}
    m.preflight_a()
    assert m.doc()["operation"]["code"] == "fci_unreachable"
    assert "gate.fci_probe" in m.blockers("a")


def test_the_fci_check_is_never_made_while_a_exists(m):
    m.start_a()
    m.fci_probes.clear()
    assert refused(m.preflight_a, "pre-a-second01") == "probe_arm_present"
    assert m.fci_probes == []
    m.act.stop("a", m.stop_body("a"))
    m.hand_started_a()
    assert refused(m.preflight_a, "pre-a-third001") == "probe_arm_present"
    assert m.fci_probes == []


def test_the_fci_check_may_follow_a_stop_while_the_sockets_drain(m):
    m.start_a()
    m.act.stop("a", m.stop_body("a"))
    m.sockets = {"state": "active", "detail": "TIME_WAIT"}
    m.no_manager()
    m.fci_probes.clear()
    m.preflight_a("pre-a-drain001")
    assert len(m.fci_probes) == 1


@pytest.mark.parametrize(
    "out, code, state",
    [
        (CONTROLLERS_OUT, 0, "foreign_running"),
        (
            "Could not contact service /robotiq/controller_manager/list_controllers\n",
            1,
            "idle",
        ),
        ("rclpy: something else broke\n", 1, "unknown"),
        ("", 124, "unknown"),
    ],
)
def test_a_gripper_stack_started_by_hand_is_seen_by_asking_its_controller_manager(
    m, out, code, state
):
    m.m_ready()
    m.controllers_out, m.controllers_code = out, code
    m.act.preflight_a({"request_id": "pre-a-00000009"})
    assert m.terminal("b")["state"] == state


def test_b_is_not_probed_when_a_unit_of_ours_is_b(m):
    m.start_a()
    m.start_b()
    m.oneshots.clear()
    m.act.stop("c", m.stop_body("c"))
    m.act.stop("b", m.stop_body("b"))
    assert m.oneshots == []


def test_the_checks_before_c_pass_or_fail_one_by_one(m):
    m.start_a()
    m.start_b()
    m.controllers_out, m.controllers_code = CONTROLLERS_OUT, 0
    m.act.preflight_c({"request_id": "pre-c-00000001"})
    pre = m.doc()["checks"]["c_preflight"]
    assert (pre["ok"], pre["qos"], pre["controllers"], pre["ee_frame"]) == (
        True,
        True,
        True,
        True,
    )
    [first, second] = m.oneshots[-2:]
    assert "list_controllers" in " ".join(
        first
    ) and "check_fr3_ee_frame.py" in " ".join(second)
    assert second[second.index("--expect") :] == [
        "--expect",
        "franka_hand",
        "--gripper-state-topic",
        "/robotiq/joint_states",
    ]
    # one at a time
    server = m.franka / "robot_server" / "fr3_robot_server" / "franka_server_ros2.py"
    server.write_text("reliability=ReliabilityPolicy.RELIABLE,\n")
    m.act.preflight_c({"request_id": "pre-c-00000002"})
    assert m.doc()["checks"]["c_preflight"]["qos"] is False
    assert m.doc()["operation"]["code"] == "c_preflight_failed"
    server.write_text(m.qos_text)
    m.controllers_out = CONTROLLERS_OUT.replace("active", "inactive", 1)
    m.act.preflight_c({"request_id": "pre-c-00000003"})
    assert m.doc()["checks"]["c_preflight"]["controllers"] is False
    m.controllers_out = CONTROLLERS_OUT
    m.ee_out = "  FAIL -- expected 'franka_hand', measured 'robotiq'.\n"
    m.act.preflight_c({"request_id": "pre-c-00000004"})
    assert m.doc()["checks"]["c_preflight"]["ee_frame"] is False
    assert "c_preflight" in m.blockers("c")


def test_the_checks_before_c_need_a_and_b_ready(m):
    m.start_a()
    m.oneshots.clear()
    assert (
        refused(m.act.preflight_c, {"request_id": "pre-c-00000001"}) == "gate.b_running"
    )
    assert not m.oneshots


# --- the gripper test -----------------------------------------------------------------------


def grip(m, action="open", rid=None, attest=True):
    return m.act.gripper(
        {
            **m.body(f"gripper_{action}", rid or f"grip-{action}-0001"),
            "action": action,
            "attest": {"hands_clear": True} if attest else {},
        }
    )


def test_the_gripper_test_needs_b_ready_and_nothing_else(m):
    m.start_a()
    m.start_b()
    m.oneshots.clear()
    assert m.doc()["gripper_test"]["can_run"] is True
    assert grip(m, "open") == {"done": "gripper_test_started", "action": "open"}
    last = m.doc()["gripper_test"]["last"]
    assert last["ok"] is True and last["code"] == "reached" and last["action"] == "open"
    [cmd] = m.oneshots
    assert "position: 0.0, max_effort: 0.0" in cmd[-1]
    grip(m, "close")
    assert "position: 0.7929, max_effort: 0.0" in m.oneshots[-1][-1]
    assert m.doc()["gripper_test"]["last"]["action"] == "close"
    # C is not needed and not involved.
    assert m.systemd_runs("c") == []


def test_the_gripper_test_reports_a_stall_and_a_missing_result(m):
    m.start_a()
    m.start_b()
    m.gripper_reached, m.gripper_stalled = False, True
    grip(m, "close")
    last = m.doc()["gripper_test"]["last"]
    assert (last["ok"], last["code"], last["stalled"]) == (False, "stalled", True)
    assert m.doc()["operation"]["state"] == "failed"
    m.gripper_code, m.gripper_stalled, m.gripper_reached = 124, False, False
    grip(m, "open", "grip-open-0002")
    assert m.doc()["gripper_test"]["last"]["code"] == "timeout"
    m.gripper_code = 0
    m.gripper_out = "Waiting for an action server to become available...\n"
    grip(m, "open", "grip-open-0003")
    assert m.doc()["gripper_test"]["last"]["code"] == "no_result"
    assert m.doc()["gripper_test"]["last"]["ok"] is False


G_BREAKS = {
    "b not ready": (lambda m: m.units.pop("b"), "gripper_b_not_ready"),
    "run active": (lambda m: setattr(m, "run_active", True), "run_active"),
    "arm lock held": (lambda m: m.hold_arm_lock(), "arm_lock_held"),
    "operation running": (lambda m: m.act._begin("preflight_c"), "operation_running"),
}


@pytest.mark.parametrize("name", sorted(G_BREAKS))
def test_each_gate_of_the_gripper_test_refuses_alone(m, name):
    m.start_a()
    m.start_b()
    m.oneshots.clear()
    G_BREAKS[name][0](m)
    t = m.doc()["gripper_test"]
    assert t["can_run"] is False and G_BREAKS[name][1] in codes(t["blockers"])
    assert refused(grip, m, "open") != ""
    assert not m.oneshots


def test_the_gripper_test_needs_clear_hands_and_the_exact_phrase(m):
    m.start_a()
    m.start_b()
    m.oneshots.clear()
    assert refused(grip, m, "open", None, False) == "attest_missing"
    body = {
        **m.body("gripper_close", "grip-bad-00001"),
        "action": "open",
        "attest": {"hands_clear": True},
    }
    assert refused(m.act.gripper, body) == "confirm_required"
    assert refused(m.act.gripper, {**body, "action": "squeeze"}) == "attest_invalid"
    assert not m.oneshots


def test_the_gripper_test_with_a_repeated_request_runs_once(m):
    m.start_a()
    m.start_b()
    m.oneshots.clear()
    grip(m, "open")
    again = grip(m, "open")
    assert again["repeated"] is True and len(m.oneshots) == 1


# --- the guided sequence ----------------------------------------------------------------------


def start_sequence(m, rid="seq-00000001"):
    return m.act.sequence_start(
        m.start_body("a", rid)
        | m.body("sequence_start", rid, attest={"desk_done": True, "estop_hand": True})
    )


def run_sequence(m, limit=40):
    """Advance until it stops running; the unit prints its lines when it exists."""
    up = False
    for _ in range(limit):
        seq = m.act.advance()
        if seq["state"] != "running":
            return seq
        if seq.get("step") == "wait":
            m.t += 40
            m.no_manager()
        if "a" in m.units and not up:
            m.a_up()
            up = True
    raise AssertionError("the sequence did not settle")


def test_the_sequence_runs_m_the_marker_the_wait_and_a_then_stops_for_b(m):
    assert start_sequence(m) == {"done": "sequence_started"}
    seq = run_sequence(m)
    assert seq["state"] == "waiting_confirm" and seq["next"] == "b"
    assert len(m.systemd_runs("a")) == 1
    assert m.systemd_runs("b") == [] and m.systemd_runs("c") == []
    assert m.marker_path.exists() and m.doc()["marker"]["ours"]
    started = [
        next(a for a in c if a.startswith("--unit=")).split("=", 1)[1]
        for c in m.systemd_runs()
    ]
    assert started == [stack.MONITOR_UNIT, OURS["a"].removesuffix(".service")]
    assert m.fci_probes == [("10.42.0.157", 1337)]
    assert m.doc()["sequence"]["next"] == "b"


def test_the_sequence_never_starts_b_or_c_even_when_they_could_start(m):
    start_sequence(m)
    run_sequence(m)
    for _ in range(5):
        m.act.advance()
    assert m.systemd_runs("b") == [] and m.systemd_runs("c") == []
    assert m.terminal("b")["can_start"] is True


def test_the_sequence_follows_the_person_s_own_starts(m):
    start_sequence(m)
    run_sequence(m)
    m.start_b()
    assert m.act.advance()["next"] == "c"
    m.controllers_out, m.controllers_code = CONTROLLERS_OUT, 0
    m.act.preflight_c({"request_id": "pre-c-00000001"})
    m.act.start("c", m.start_body("c"))
    m.c_up()
    assert m.act.advance()["state"] == "done"


def test_a_sequence_that_cannot_see_the_fci_port_stops_there(m):
    m.fci = {"state": "closed"}
    start_sequence(m)
    seq = run_sequence(m)
    assert seq["state"] == "failed" and seq["reason"] == "fci_closed"
    assert m.systemd_runs("a") == [] and not m.marker_path.exists()


def test_a_sequence_does_not_take_over_a_marker_it_did_not_make(m):
    m.marker_path.write_text("")
    start_sequence(m)
    seq = run_sequence(m)
    assert seq["state"] == "failed" and seq["reason"] == "marker_not_ours"
    assert m.systemd_runs("a") == []


def test_a_sequence_that_fails_to_start_a_stops_and_does_not_retry(m):
    start_sequence(m)

    def fail_a(argv):
        if any("stack-marvel0-a" in a for a in argv):
            m.run_codes["systemd-run"] = 1

    m.on_systemd_run = fail_a
    seq = run_sequence(m)
    assert seq["state"] == "failed" and seq["reason"] == "start_failed"
    for _ in range(3):
        m.act.advance()
    assert len(m.systemd_runs("a")) == 1


def test_a_sequence_whose_a_fails_reports_it(m):
    start_sequence(m)
    seq = {}
    for _ in range(40):
        seq = m.act.advance()
        if seq.get("step") == "wait":
            m.t += 40
            m.no_manager()
        if "a" in m.units:
            m.emit("a", "Could not enable FIFO RT scheduling policy: error 1")
        if seq["state"] != "running":
            break
    assert seq["state"] == "failed" and seq["reason"] in ("a_failed",)


def test_a_sequence_needs_the_attestations_a_idle_and_is_one_at_a_time(m):
    bad = m.start_body("a", "seq-00000002") | m.body(
        "sequence_start", "seq-00000002", attest={}
    )
    assert refused(m.act.sequence_start, bad) == "attest_missing"
    m.hand_started_a()
    m.m_ready()
    assert refused(start_sequence, m, "seq-00000003") in (
        "gate.a_absent",
        "gate.arm_sockets_clear",
    )
    m.sockets = {"state": "inactive", "detail": ""}
    m.no_manager()
    start_sequence(m, "seq-00000004")
    assert refused(start_sequence, m, "seq-00000005") == "sequence_active"


def test_a_sequence_is_cancelled_without_stopping_anything(m):
    start_sequence(m)
    run_sequence(m)
    runs = len(m.systemd_runs())
    assert m.act.sequence_cancel({"request_id": "seq-cancel-01"}) == {
        "done": "sequence_cancelled"
    }
    assert m.doc()["sequence"]["state"] == "cancelled"
    assert m.act.advance()["state"] == "cancelled"
    assert not m.stop_calls() and len(m.systemd_runs()) == runs
    assert (
        refused(m.act.sequence_cancel, {"request_id": "seq-cancel-02"})
        == "sequence_idle"
    )


def test_a_sequence_left_waiting_expires(m):
    start_sequence(m)
    run_sequence(m)
    m.t += stack_act.SEQUENCE_WAIT_S + 1
    m.health(hardware=True, controller=False, red=False, mode="IDLE")
    seq = m.act.advance()
    assert seq["state"] == "cancelled" and seq["reason"] == "expired"


def test_a_sequence_that_loses_a_stops_waiting(m):
    start_sequence(m)
    run_sequence(m)
    m.sockets = {"state": "inactive", "detail": ""}
    m.no_manager()
    del m.units["a"]
    seq = m.act.advance()
    assert seq["state"] == "failed" and seq["reason"] == "a_lost"


def test_a_busy_lock_delays_a_step_and_is_not_a_failure(m):
    start_sequence(m)
    fd = os.open(m.hub / stack.LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert m.act.advance()["state"] == "running"
    finally:
        os.close(fd)
    assert run_sequence(m)["state"] == "waiting_confirm"


def test_nothing_resumes_after_a_restart(m, tmp_path):
    start_sequence(m)
    run_sequence(m)
    runs = len(m.systemd_runs())
    fresh = stack_act.Act(m.sup, m.act_host(), setup=lambda: (m.loaded, m.specs))
    assert fresh._seq == {"state": "idle"}
    for _ in range(5):
        assert fresh.advance()["state"] == "idle"
    assert len(m.systemd_runs()) == runs and not m.stop_calls()
    # Even with M ready, the marker set and old, the FCI answered: still nothing.
    assert (
        refused(fresh.sequence_cancel, {"request_id": "seq-cancel-09"})
        == "sequence_idle"
    )


# --- the document --------------------------------------------------------------------------


def test_the_document_names_no_path_and_is_plain_json(m):
    m.all_up()
    grip(m, "open")
    text = json.dumps(m.sup.view())
    assert str(m.tmp) not in text and "/home/" not in text and "/opt/rig" not in text
    assert "10.42.0.157" not in json.dumps(m.doc())


def test_every_terminal_says_what_the_button_needs(m):
    m.m_ready()
    doc = m.doc()
    assert doc["order"] == ["m", "a", "b", "c"]
    assert doc["terminals"]["a"]["attest"]["start"] == ["desk_done", "estop_hand"]
    assert doc["terminals"]["b"]["attest"]["start"] == ["hands_clear"]
    assert doc["terminals"]["c"]["attest"]["start"] == [
        "estop_hand",
        "hands_clear",
        "no_client",
    ]
    assert doc["terminals"]["a"]["confirm"] == {
        "start": "start arm stack",
        "stop": "stop arm stack",
    }
    assert doc["terminals"]["c"]["confirm"]["stop"] == "stop robot server"
    assert (
        doc["phrases"]["client_override"] == "stop robot server with client connected"
    )
    for key in "abc":
        t = doc["terminals"][key]
        assert t["state"] in stack_act.STATES and t["hazard"]["text"]
        for b in t["start_blockers"] + t["stop_blockers"]:
            assert set(b) == {"code", "level", "text", "params"} and b["text"]
    assert (
        "emergency stop" in doc["estop"]["text"] and "mistakes" in doc["scope"]["text"]
    )


def test_nothing_on_the_page_is_called_an_emergency_stop():
    texts = [t for _, t in stack_act.TEXT.values()] + [
        stack_act.ESTOP_TEXT,
        stack_act.SCOPE_TEXT,
    ]
    texts += [t for _, t in stack_act.HAZARDS.values()] + list(
        stack_act.STOP_NOTE_TEXT.values()
    )
    for text in texts:
        lowered = text.lower()
        assert "e-stop button" not in lowered
        if "emergency stop" in lowered:
            assert "physical" in lowered or "nothing on this page" in lowered


def test_every_code_in_the_tables_is_a_http_error_and_holds_no_path():
    for code, (status, text) in stack_act.TEXT.items():
        assert 400 <= status < 600 and text, code
        assert "/" not in text.replace("1-2 mm", "").replace("1 kHz", ""), code
    # (the two codes this module words for its own units; the rest are shared)
    shared = set(stack.REFUSALS) & set(stack_act.TEXT)
    assert shared == {"start_failed", "stop_failed"}


def test_every_blocker_code_the_machine_can_produce_has_a_sentence(tmp_path):
    seen = set()
    scenes = [
        lambda m: None,
        lambda m: m.m_ready(),
        lambda m: m.hand_started_a(),
        lambda m: m.start_a(),
        lambda m: m.all_up(),
        lambda m: (
            m.start_a(),
            m.units.__setitem__("a", {**m.units["a"], "argv": ["x"]}),
        ),
    ]
    for n, scene in enumerate(scenes):
        m = ActMachine(tmp_path / f"scene{n}")
        scene(m)
        doc = m.doc()
        for t in doc["terminals"].values():
            seen |= {b["code"] for b in t["start_blockers"] + t["stop_blockers"]}
        seen |= {b["code"] for b in doc["gripper_test"]["blockers"]}
    assert len(seen) > 10
    for code in seen:
        assert code in stack_act.TEXT or code in stack.REFUSALS, code


def test_the_unit_names_are_listed_as_busy_by_the_status_part_and_the_monitor_is_not():
    from fnmatch import fnmatch

    from levi.marvel0 import status

    for unit in stack.STACK_UNITS.values():
        assert fnmatch(unit, status.UNIT_GLOB) and status.UNIT_NAME.fullmatch(unit)
    assert not fnmatch(stack.MONITOR_SERVICE, status.UNIT_GLOB)
    text = "".join(
        f"{u} loaded active running x\n"
        for u in (*stack.STACK_UNITS.values(), stack.MONITOR_SERVICE)
    )
    assert [u["name"] for u in status.parse_units(text)] == sorted(
        stack.STACK_UNITS.values()
    )


def test_the_probe_of_the_arm_never_connects_to_a_protected_port():
    for port in stack_act.PROTECTED_PORTS:
        with pytest.raises(ValueError):
            stack_act.tcp_probe("10.42.0.157", port)


def test_a_hosted_run_is_asked_through_one_registered_function(m):
    stack_act.register_run_probe(lambda: True)
    assert stack_act.hosted_run_active() is True

    def broken():
        raise RuntimeError("x")

    stack_act.register_run_probe(broken)
    assert stack_act.hosted_run_active() is None
    stack_act._RUN_PROBES.clear()
    # (until a probe is registered a run may be active: a start is refused)
    assert stack_act.hosted_run_active() is None


def test_the_records_folder_must_not_be_a_link(m):
    m.ready_for_a()
    (m.tmp / "elsewhere").mkdir()
    m.state_root.rename(m.tmp / "moved")
    m.state_root.symlink_to(m.tmp / "elsewhere")
    assert refused(m.act.start, "a", m.start_body("a")) == "record_unusable"
    assert not m.systemd_runs("a")
    assert not Path(m.tmp / "elsewhere" / "a").exists()


# --- found in review --------------------------------------------------------------------------


def test_a_log_path_outside_the_pages_own_folder_is_never_read(m):
    m.start_a()
    secret = m.tmp / "secret.txt"
    secret.write_text("TOP SECRET\nsecond\n")
    folder = m.state_root / "a"
    record.write_record(folder, {**record.read_record(folder), "log": str(secret)})
    assert m.act.log("a", 10)["lines"] == []
    assert "TOP SECRET" not in json.dumps(m.sup.view())
    # (only the health file's leg is seen: not one line of the other file)
    seen = m.terminal("a")["readiness"]["seen"]
    assert [x["key"] for x in seen] == ["health_hardware_active"]


def test_a_view_writes_nothing(tmp_path):
    m = ActMachine(tmp_path)
    m.m_ready()
    m.sup.view()
    assert not m.state_root.exists() and not m.marker_path.exists()
    m.start_a()
    m.sup.view()  # (the first look at a ready unit writes its ready latch, once)
    before = sorted(
        (p, p.stat().st_mtime_ns) for p in m.state_root.rglob("*") if p.is_file()
    )
    for _ in range(3):
        m.sup.view()
    assert before == sorted(
        (p, p.stat().st_mtime_ns) for p in m.state_root.rglob("*") if p.is_file()
    )


def test_the_gripper_test_is_refused_while_a_client_is_connected_to_c(m):
    m.start_a()
    m.start_b()
    m.socks.append(_sock(40000, net.ESTABLISHED, remote_port=SERVER_PORT))
    assert "client_connected" in codes(m.doc()["gripper_test"]["blockers"])
    m.oneshots.clear()
    assert refused(grip, m, "open") == "client_connected"
    assert not m.oneshots
    m.socks.clear()
    m.table_ok = False  # a table that cannot be read is no proof of "no client"
    assert "client_connected" in codes(m.doc()["gripper_test"]["blockers"])


def test_an_automatic_step_that_never_finishes_ends_the_sequence(m):
    start_sequence(m)
    for _ in range(40):
        if m.act.advance().get("step") == "wait":
            break
    m.sockets = {"state": "active", "detail": "a stray control connection"}
    m.t += stack_act.STEP_TIMEOUT_S + 1
    m.no_manager()
    seq = m.act.advance()
    assert seq["state"] == "failed" and seq["reason"] == "step_timeout"
    assert m.systemd_runs("a") == []


def test_the_sequence_view_hides_the_ticked_confirmations_and_the_request_id(m):
    start_sequence(m)
    seq = m.doc()["sequence"]
    assert "attest" not in seq and "request_id" not in seq
    assert set(seq) >= {"state", "step", "since"}
    assert "desk_done" not in json.dumps(m.doc()["sequence"])


def test_the_sequence_records_its_request_id_with_a(m):
    start_sequence(m, "seq-recorded-1")
    run_sequence(m)
    assert a_record(m)["request_id"] == "seq-recorded-1"
    assert a_record(m)["attest"] == {"desk_done": True, "estop_hand": True}


def test_a_document_that_cannot_be_built_says_so_and_logs_it(m, monkeypatch, caplog):
    def broken(r):
        raise RuntimeError("boom")

    monkeypatch.setattr(m.act, "_document", broken)
    with caplog.at_level("ERROR"):
        doc = m.sup.view()["act"]
    assert doc == {
        "schema": stack_act.SCHEMA,
        "enabled": False,
        "error": "RuntimeError",
    }
    assert "could not be built" in caplog.text
    assert m.sup.view()["terminals"]["M"]  # the rest of the view is still there


def test_the_catalog_lists_every_code_the_page_has_to_word():
    cat = stack_act.catalog()
    for code in cat["blockers"]:
        assert code in stack_act.TEXT or code in stack.REFUSALS, code
    assert set(cat["states"]) == set(stack_act.STATES)
    assert set(cat["hazards"]) == {"hazard.a", "hazard.b", "hazard.c"}
    assert set(cat["stop_notes"]) == set(stack_act.STOP_NOTE_TEXT)
    assert {
        "fifo_failed",
        "ready_timeout",
        "duplicate_text",
        "unit_failed",
        "traceback",
    } <= set(cat["failures"])
    assert {
        "fifo",
        "spawners",
        "health_hardware_active",
        "listening",
        "serving",
    } <= set(cat["readiness"])
    assert {"reached", "stalled", "timeout"} <= set(cat["gripper_results"])
    assert set(cat["attest"]) == set(stack_act.ATTEST_KEYS)
    assert set(cat["phrases"]) <= set(stack.PHRASES)


def test_every_notice_and_note_the_machine_can_show_is_in_the_catalog(tmp_path):
    cat = stack_act.catalog()
    seen_notices, seen_notes = set(), set()
    for n, scene in enumerate(
        [
            lambda m: m.all_up(),
            lambda m: (m.all_up(), m.act.stop("c", m.stop_body("c"))),
        ]
    ):
        m = ActMachine(tmp_path / f"s{n}")
        scene(m)
        doc = m.doc()
        seen_notices |= {x["code"] for x in doc["notices"]}
        seen_notes |= {
            t["stop_note"] for t in doc["terminals"].values() if t["stop_note"]
        }
    assert seen_notices <= set(cat["notices"]) and seen_notices
    assert seen_notes <= set(cat["stop_notes"]) and seen_notes


def test_leftover_sockets_are_only_leftovers_while_the_monitor_says_the_hardware_is_off(
    m,
):
    m.start_a()
    m.act.stop("a", m.stop_body("a"))
    m.sockets = {"state": "active", "detail": "TIME_WAIT"}
    m.no_manager()
    assert m.terminal("a")["reason"] == "a.sockets_draining"
    for health in (
        {"age": 60.0, "hardware": False},  # the monitor is not looking
        {"hardware": True},  # it says a controller is up
    ):
        m.health(controller=False, red=False, mode="IDLE", **health)
        assert m.terminal("a")["state"] == "foreign_running", health
        assert refused(m.preflight_a, "pre-a-draining1") == "probe_arm_present"


def test_the_gripper_test_is_off_when_the_control_file_goes_away_even_with_b_ready(m):
    m.start_a()
    m.start_b()
    assert m.doc()["gripper_test"]["can_run"] is True
    m.act._setup = lambda: (stack_act.control_site.ControlLoad("missing", None), None)
    t = m.doc()["gripper_test"]
    assert t["can_run"] is False and codes(t["blockers"]) == ["control_disabled"]
    assert refused(grip, m, "open") == "control_disabled"


def test_two_operations_never_run_at_once(m):
    first = m.act._begin("probe")
    assert refused(m.act._begin, "gripper") == "operation_running"
    assert refused(m.act._begin, "preflight_a") == "operation_running"
    m.act._finish(first, True)
    assert m.act._begin("gripper")["state"] == "running"


def test_the_marker_is_created_exclusively_even_against_a_stale_read(m):
    m.m_ready()
    stale = m.sup._read()  # (absent)
    assert stale["marker"]["state"] == "absent"
    m.marker_path.write_text("someone else's")  # made between the read and the create
    assert refused(m.act._do_touch, stale) == "marker_exists"
    assert m.marker_path.read_text() == "someone else's"
    assert not m.act._marker_is_ours()


def test_the_health_file_is_the_control_files_by_name_not_by_spelling(m):
    m.ready_for_a()
    spelled = str(m.health_path.parent / ".." / "run" / "fr3_health.json")
    m.act.host.base.health_file = lambda: (spelled, 3.0)
    assert "control_health_file" not in m.blockers("a")
    m.act.host.base.health_file = lambda: (str(m.tmp / "run" / "fr3_health.json"), 3.0)
    assert "control_health_file" in m.blockers("a")


def test_an_operation_whose_thread_cannot_start_ends_failed_not_running_for_ever(
    m, monkeypatch
):
    m.start_a()
    m.act.host.background = True

    def no_thread(*args, **kwargs):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(stack_act.threading, "Thread", no_thread)
    m.act.stop("a", m.stop_body("a"))
    op = m.doc()["operation"]
    assert (op["state"], op["code"]) == ("failed", "no_thread")
    monkeypatch.undo()
    m.act.host.background = False
    assert (
        m.act._begin("next")["state"] == "running"
    )  # the next operation is not blocked


def test_a_failing_deferred_call_does_not_fail_the_request_that_asked_for_it(m, caplog):
    m.m_ready()

    def broken():
        raise RuntimeError("boom")

    def act(r):
        m.act._defer(broken)
        return {"done": "x"}

    with caplog.at_level("ERROR"):
        assert m.act._write(
            {"request_id": "req-deferred-01"}, "preflight_a", act, phrase=False
        ) == {"done": "x"}
    assert "deferred call" in caplog.text
