"""The fast start (L1, the fake machine): what changed so that nothing waits for
nothing.

* The check whether a gripper stack runs asks the ROS graph first (about 3 s)
  and the controller manager (about 20 s) only when the graph proves nothing;
  "absent" needs the health monitor's own node in the list, and nothing that
  cannot be told is ever absent.
* That check is an operation beside the others: it runs while the FCI check
  runs, and the start of A does not wait for it (it waits for its answer, as it
  needs B known, but not for the operation to be cleared).
* The marker is set right after M, the check of B begins with it; a start that
  ends before A gives the marker back.
* The checks before C run at the same time.
* The ticker looks again at once after any change and every step says how long
  it took.

The outputs the fake machine gives are the real commands' (``ros2 node list``
as ``ros2node/verb/list.py`` prints it, see ``stack_act_machine``)."""

import threading
from pathlib import Path

import pytest
from stack_act_machine import (
    CONTROLLERS_OUT,
    DUPLICATE_WARNING,
    GRIPPER_NODES,
    MONITOR_NODE,
    ActMachine,
    node_list_print,
)
from test_marvel0_deploy import forbid_real_machine  # noqa: F401 - the guard
from test_marvel0_stack_act import start_sequence

from levi.marvel0 import control_site, stack, stack_act, stack_api, stack_units

FRANKA = Path("/home/marvel/work/franka_control")


@pytest.fixture
def m(tmp_path):
    return ActMachine(tmp_path)


def refused(call, *args):
    with pytest.raises(stack.Refuse) as caught:
        call(*args)
    return caught.value.code


def probe_request(n=1):
    return {"request_id": f"fast-probe-{n:07d}"}


# --- reading `ros2 node list` -------------------------------------------------------------


def parsed(text, code=0):
    return stack_units.parse_nodes(text, code)


def test_the_graph_with_the_monitor_and_no_gripper_node_is_absent():
    got = parsed(node_list_print([MONITOR_NODE, "/controller_manager"]))
    assert got["state"] == "absent" and got["canary"] is True and got["gripper"] == []


def test_a_gripper_node_in_the_graph_is_present_whatever_else_is_there():
    text = node_list_print([MONITOR_NODE, *GRIPPER_NODES])
    got = parsed(text)
    assert got["state"] == "present"
    assert "/robotiq/controller_manager" in got["gripper"]
    # a half-started stack is somebody's gripper stack too
    assert parsed(node_list_print(["/robotiq/spawner_1234"]))["state"] == "present"
    # ... also when the command was ended before it finished (the list is printed
    # at once, so what was printed is whole)
    assert parsed(text, 124)["state"] == "present"
    assert parsed(text, 1)["state"] == "present"


def test_an_empty_graph_proves_nothing():
    got = parsed("")
    assert got["state"] == "unknown" and got["canary"] is False
    assert parsed("\n\n")["state"] == "unknown"


def test_a_graph_without_the_monitor_proves_nothing_even_with_other_nodes():
    got = parsed(node_list_print(["/controller_manager", "/robot_state_publisher"]))
    assert got["state"] == "unknown", "discovery of the monitor is the proof"


@pytest.mark.parametrize("code", [1, 2, 124, 255])
def test_a_command_that_did_not_end_well_is_not_absent(code):
    assert parsed(node_list_print([MONITOR_NODE]), code)["state"] == "unknown"


def test_what_is_not_a_node_name_is_not_read_as_one():
    text = (
        DUPLICATE_WARNING
        + "\x1b[1m[INFO] [1760000100.000000000] [_ros2cli_77]: spinning\x1b[0m\n"
        + node_list_print([MONITOR_NODE])
    )
    got = parsed(text)
    assert got["nodes"] == [MONITOR_NODE] and got["state"] == "absent"


def test_a_name_that_only_looks_like_the_gripper_namespace_is_not_in_it():
    got = parsed(node_list_print([MONITOR_NODE, "/robotiq_tools/node", "/x/robotiq/y"]))
    assert got["state"] == "absent"
    assert parsed(node_list_print([MONITOR_NODE, "/robotiq"]))["state"] == "present"


def test_the_node_names_are_the_ones_the_rig_uses_when_its_sources_are_there():
    monitor = FRANKA / "scripts" / "fr3_health_monitor.py"
    launch = (
        FRANKA
        / "robotiq_ws/src/franka_gripper_manager/launch/robotiq_collect.launch.py"
    )
    if not (monitor.is_file() and launch.is_file()):
        pytest.skip("the rig's checkout is not on this machine")
    assert (
        f'rclpy.create_node("{stack_units.MONITOR_NODE.lstrip("/")}")'
        in monitor.read_text()
    )
    text = launch.read_text()
    assert 'name="namespace"' in text and 'default_value="robotiq"' in text
    assert stack_units.GRIPPER_NODE == "/robotiq/controller_manager"
    assert "-c /robotiq/controller_manager" in text  # (the query the launch names)
    assert stack_units.GRIPPER_NAMESPACE == "/robotiq"
    ros = Path("/opt/ros/jazzy/lib/python3.12/site-packages/ros2node/verb/list.py")
    if ros.is_file():
        assert "print(*sorted_names, sep='\\n')" in ros.read_text()


def test_the_command_is_the_graph_listing_without_a_daemon_bounded_by_timeout(m):
    argv = m.specs.nodes_cmd
    tail = list(argv[argv.index("--") + 1 :])
    assert tail == [
        "timeout", "-s", "INT", "-k", "2", "8",
        "ros2", "node", "list", "--no-daemon", "--spin-time", "2.0",
    ]  # fmt: skip
    assert argv[0] == "/bin/bash" and argv[2].endswith("fr3_env.sh")


# --- the check of B: the graph first, the controller manager only when it must -----------------


def test_a_gripper_stack_that_is_not_there_is_seen_absent_by_the_graph_alone(m):
    m.m_ready()
    m.act.probe_b(probe_request())
    assert m.act._b_probe["state"] == "absent" and m.act._b_probe["by"] == "nodes"
    assert m.act._b_probe["graph"] == "absent"
    assert len(m.oneshots_of("node list")) == 1
    assert m.oneshots_of("list_controllers") == [], "the slow question was not asked"
    assert (m.terminal("b")["state"], m.terminal("b")["reason"]) == (
        "idle",
        "b.not_running",
    )


def test_a_gripper_stack_in_the_graph_is_foreign_running_without_the_slow_question(m):
    m.m_ready()
    m.nodes_out = node_list_print([MONITOR_NODE, *GRIPPER_NODES])
    m.act.probe_b(probe_request())
    assert m.act._b_probe["state"] == "present" and m.act._b_probe["by"] == "nodes"
    assert m.terminal("b")["state"] == "foreign_running"
    assert m.oneshots_of("list_controllers") == []


def test_a_graph_that_proves_nothing_asks_the_controller_manager_and_says_absent_only_on_its_word(
    m,
):
    m.m_ready()
    m.nodes_out = node_list_print([])  # (not discovered)
    m.act.probe_b(probe_request())
    assert m.act._b_probe["by"] == "probe" and m.act._b_probe["graph"] == "unknown"
    assert m.act._b_probe["state"] == "absent"  # (the manager's warning said so)
    assert len(m.oneshots_of("list_controllers")) == 1


def test_a_graph_and_a_manager_that_both_say_nothing_are_unknown_never_absent(m):
    m.m_ready()
    m.nodes_out, m.nodes_code = "", 124
    m.controllers_out, m.controllers_code = "did not finish", 124
    m.act.probe_b(probe_request())
    assert m.act._b_probe["state"] == "unknown"
    assert m.terminal("b")["reason"] == "b.probe_no_answer"
    assert m.doc()["operation"]["state"] == "failed"
    assert m.doc()["operation"]["code"] == "b_probe_no_answer"


def test_a_graph_that_failed_with_the_monitor_listed_falls_back_to_the_manager(m):
    m.m_ready()
    m.nodes_out, m.nodes_code = node_list_print([MONITOR_NODE]), 1
    m.controllers_out, m.controllers_code = CONTROLLERS_OUT, 0
    m.act.probe_b(probe_request())
    # (the manager answers: a gripper stack runs, though the graph said otherwise)
    assert m.act._b_probe["state"] == "present" and m.act._b_probe["by"] == "probe"


def test_the_manager_alone_is_used_by_specs_that_have_no_graph_command(m):
    m.m_ready()
    object.__setattr__(m.specs, "nodes_cmd", ())
    m.act.probe_b(probe_request())
    assert m.act._b_probe["by"] == "probe" and m.act._b_probe["graph"] is None
    assert m.oneshots_of("node list") == []


def test_a_stop_during_the_check_drops_the_graph_answer(m):
    m.m_ready()
    m.act._begin("probe_b", "b")
    op = m.act._ops[-1]
    op["_superseded"] = True
    assert m.act._probe_b(m.specs, op) is None
    assert m.act._b_probe is None


def test_the_button_for_a_runs_the_fci_check_and_the_check_of_b_together(m):
    m.m_ready()
    m.act.host.parallel = True
    m.act.preflight_a({"request_id": "fast-pre-a-0001"})
    assert len(m.fci_probes) == 1 and len(m.oneshots_of("node list")) == 1
    assert m.act._fci["state"] == "open" and m.act._b_probe["state"] == "absent"
    assert m.terminal("b")["reason"] == "b.not_running"


def test_the_two_checks_of_the_button_really_overlap(m):
    m.m_ready()
    m.act.host.parallel = True
    both = threading.Barrier(2, timeout=5)

    def fci(ip, port):
        both.wait()  # (cannot pass unless the node listing is asked meanwhile)
        return {"state": "open"}

    m.act.host.fci_probe = fci
    m.on_oneshot = lambda cmd: both.wait() if " node list " in " ".join(cmd) else None
    m.act.preflight_a({"request_id": "fast-pre-a-0002"})
    assert m.act._fci["state"] == "open" and m.act._b_probe["state"] == "absent"


# --- the check of B runs beside the others ----------------------------------------------


def test_a_running_check_of_b_does_not_hold_back_the_start_of_a(m):
    m.ready_for_a()
    m.act._begin("probe_b", "b")
    assert "operation_running" not in m.blockers("a")
    assert m.act.start("a", m.start_body("a"))["done"] == "started"


def test_a_running_check_of_b_still_holds_back_b_and_c_and_the_gripper(m):
    m.start_a()
    m.act._begin("probe_b", "b")
    assert "operation_running" in m.blockers("b")
    assert "operation_running" in m.blockers("c")
    assert "operation_running" in [
        b["code"] for b in m.doc()["gripper_test"]["blockers"]
    ]


def test_the_check_of_b_can_begin_beside_the_fci_check_and_not_beside_itself(m):
    m.act._begin("preflight_a")
    m.act._begin("probe_b", "b")
    assert refused(m.act._begin, "probe_b", "b") == "operation_running"
    assert refused(m.act._begin, "preflight_a") == "operation_running"
    assert refused(m.act._begin, "gripper", "b") == "operation_running"


def test_the_fci_check_can_begin_beside_a_running_check_of_b(m):
    m.act._begin("probe_b", "b")
    m.act._begin("preflight_a")
    assert {o["kind"] for o in m.act._ops if o["state"] == "running"} == {
        "probe_b",
        "preflight_a",
    }


def test_a_stop_ends_both_checks(m):
    m.start_a()
    m.act._begin("probe_b", "b")
    m.act._begin("preflight_a")
    m.act._begin("stop_c", "c")
    assert all(
        o["_superseded"] for o in m.act._ops if o["kind"] in ("probe_b", "preflight_a")
    )


# --- the order of the start ---------------------------------------------------------------


def test_the_steps_of_a_are_ordered_marker_first():
    assert stack_act.A_STEPS == (
        "monitor",
        "marker",
        "probe",
        "wait",
        "arm",
        "await_a",
    )
    assert stack_act.SEQUENCE_STEPS[:6] == stack_act.A_STEPS


def test_the_marker_is_set_before_the_check_of_b_and_the_fci_check_begin(m):
    m.m_ready()
    seen = []
    m.on_oneshot = lambda cmd: seen.append(("nodes", m.marker_path.exists()))
    probe = m.act.host.fci_probe
    m.act.host.fci_probe = lambda ip, port: (
        seen.append(("fci", m.marker_path.exists())) or probe(ip, port)
    )
    start_sequence(m)
    for _ in range(8):
        m.act.advance()
    assert seen == [("nodes", True), ("fci", True)], "the marker is first"
    steps = [i["step"] for i in m.doc()["sequence"]["steps"]]
    assert steps == list(stack_act.A_STEPS)


def test_the_check_of_b_is_begun_once_at_the_marker_and_not_by_the_probe_step(m):
    m.m_ready()
    start_sequence(m)
    for _ in range(12):
        m.act.advance()
    assert len(m.oneshots_of("node list")) == 1
    assert m.act._seq["step"] in ("wait", "arm", "await_a")


def test_a_start_whose_b_is_already_known_makes_no_early_check(m):
    m.m_ready()
    m.act.probe_b(probe_request())  # (a fresh answer: absent)
    m.oneshots.clear()
    start_sequence(m)
    for _ in range(12):
        m.act.advance()
    assert m.oneshots_of("node list") == []


def test_the_early_check_that_got_no_answer_ends_the_start_as_b_not_seen(m):
    m.m_ready()
    m.nodes_out, m.nodes_code = "", 124
    m.controllers_out, m.controllers_code = "nothing", 124
    start_sequence(m)
    seq = {}
    for _ in range(12):
        seq = m.act.advance()
        if seq["state"] != "running":
            break
    assert (seq["state"], seq["reason"]) == ("failed", "b_not_seen")
    assert m.systemd_runs("a") == []
    assert not m.marker_path.exists(), "nothing started: the marker is given back"


def test_a_refused_early_check_is_made_again_where_the_answer_is_needed(m):
    m.m_ready()
    start_sequence(m)
    held = m.act._begin("probe_b", "b")  # (another check of B holds the place)
    for _ in range(4):
        m.act.advance()
    assert m.act._seq["step"] == "probe" and "_early_op" not in m.act._seq
    m.act._finish(held, True)
    for _ in range(3):
        m.act.advance()
    assert len(m.oneshots_of("node list")) == 1
    assert m.act._seq["step"] in ("wait", "arm", "await_a")


# --- the marker of a start that ended before A --------------------------------------------


def run_until_over(m, limit=40):
    seq = {}
    for _ in range(limit):
        seq = m.act.advance()
        if seq["state"] != "running":
            return seq
    raise AssertionError(seq)


def test_a_start_that_ends_at_the_fci_check_gives_the_marker_back(m):
    m.m_ready()
    m.fci = {"state": "closed"}
    start_sequence(m)
    seq = run_until_over(m)
    assert (seq["state"], seq["reason"]) == ("failed", "fci_closed")
    assert not m.marker_path.exists() and m.systemd_runs("a") == []
    assert stack_act.record.read_record(m.state_root / "marker") is not None


def test_a_cancel_at_the_wait_gives_the_marker_back_at_the_next_look(m):
    m.m_ready()
    start_sequence(m)
    for _ in range(8):
        if m.act.advance()["step"] == "wait":
            break
    assert m.marker_path.exists()
    m.act.sequence_cancel({"request_id": "fast-cancel-0001"})
    assert m.marker_path.exists(), "the cancel itself only stops advancing"
    m.act.advance()
    assert not m.marker_path.exists()


def test_a_marker_that_was_there_before_the_start_is_left_alone(m):
    m.m_ready()
    m.act.marker_op("touch", m.body("marker_touch", "fast-touch-00001"))
    m.fci = {"state": "closed"}
    start_sequence(m)
    seq = run_until_over(m)
    assert seq["reason"] == "fci_closed" and m.marker_path.exists()


def test_a_marker_is_not_given_back_when_a_was_started(m):
    m.m_ready()
    start_sequence(m)
    for _ in range(40):
        seq = m.act.advance()
        if seq.get("step") == "wait":
            m.t += 40
            m.no_manager()
        if "a" in m.units:
            break
    assert "a" in m.units
    m.act.sequence_cancel({"request_id": "fast-cancel-0002"})
    m.act.advance()
    assert m.marker_path.exists(), "A runs: its marker stays until A is stopped"


def test_the_marker_stays_when_the_arm_shows_a_control_connection(m):
    m.m_ready()
    m.fci = {"state": "closed"}
    start_sequence(m)
    m.sockets = {"state": "active", "detail": "2 control sockets"}  # (someone's)
    seq = run_until_over(m)
    assert seq["state"] == "failed" and m.marker_path.exists()


def test_the_marker_is_given_back_once(m):
    m.m_ready()
    m.fci = {"state": "closed"}
    start_sequence(m)
    run_until_over(m)
    m.act.marker_op("touch", m.body("marker_touch", "fast-touch-00002"))
    m.act.advance()  # (the sequence is over; the person's new marker is theirs)
    assert m.marker_path.exists()


# --- the checks before C at the same time -----------------------------------------------


def test_the_two_checks_before_c_overlap_when_the_host_allows_it(m):
    m.start_a()
    m.start_b()
    m.controllers_out, m.controllers_code = CONTROLLERS_OUT, 0
    m.act.host.parallel = True
    both = threading.Barrier(2, timeout=5)
    m.on_oneshot = lambda cmd: (
        both.wait()
        if "list_controllers" in " ".join(cmd) or "check_fr3_ee_frame" in " ".join(cmd)
        else None
    )
    m.act.preflight_c({"request_id": "fast-pre-c-0001"})
    pre = m.act._c_pre
    assert pre["ok"] is True and pre["controllers"] and pre["ee_frame"]


def test_without_parallel_the_checks_before_c_run_in_the_old_order(m):
    m.start_a()
    m.start_b()
    m.controllers_out, m.controllers_code = CONTROLLERS_OUT, 0
    order = []
    m.on_oneshot = lambda cmd: order.append(
        "listing" if "list_controllers" in " ".join(cmd) else "frame"
    )
    m.act.preflight_c({"request_id": "fast-pre-c-0002"})
    assert order == ["listing", "frame"] and m.act._c_pre["ok"] is True


def test_a_check_before_c_that_raises_ends_the_operation_failed(m):
    m.start_a()
    m.start_b()
    m.act.host.parallel = True

    def boom(cmd):
        if "check_fr3_ee_frame" in " ".join(cmd):
            raise RuntimeError("no ros")

    m.on_oneshot = boom
    m.act.preflight_c({"request_id": "fast-pre-c-0003"})
    assert m.doc()["operation"]["state"] == "failed"
    assert m.doc()["operation"]["code"] == "RuntimeError"


# --- the ticker --------------------------------------------------------------------------


class Loop:
    """``Act._tick_loop`` with a scripted ``advance`` and a pause that is
    recorded instead of waited for."""

    def __init__(self, m, script):
        self.m, self.script, self.pauses = m, list(script), []
        m.act.advance = self.advance
        m.act._wait_tick = self.pauses.append

    def advance(self):
        state, changes = self.script.pop(0)
        self.m.act._progress += changes
        return {"state": state}


def test_the_ticker_pauses_a_quarter_of_a_second_while_nothing_changes(m):
    loop = Loop(m, [("running", 0), ("running", 0), ("done", 0)])
    m.act._tick_loop()
    assert loop.pauses == [stack_act.SEQUENCE_TICK_S] * 2
    assert stack_act.SEQUENCE_TICK_S == 0.25


def test_the_ticker_looks_again_at_once_after_a_change(m):
    loop = Loop(m, [("running", 1), ("running", 1), ("running", 0), ("done", 0)])
    m.act._tick_loop()
    assert loop.pauses == [stack_act.SEQUENCE_TICK_S]  # (only after the quiet look)


def test_the_ticker_does_not_spin_on_changes_that_never_stop(m):
    burst = stack_act.SEQUENCE_BURST
    loop = Loop(m, [("running", 1)] * (2 * burst + 3) + [("done", 0)])
    m.act._tick_loop()
    # (a pause after every `burst` looks in a row, however much changed)
    assert loop.pauses == [stack_act.SEQUENCE_TICK_S] * 2


def test_the_ticker_looks_slowly_while_the_sequence_waits_for_the_person(m):
    loop = Loop(m, [("waiting_confirm", 0), ("waiting_confirm", 0), ("done", 0)])
    m.act._tick_loop()
    assert loop.pauses == [stack_act.SEQUENCE_TICK_WAITING_S] * 2


def test_the_ticker_lets_go_when_the_sequence_is_over(m):
    Loop(m, [("done", 0)])
    m.act._ticker = threading.current_thread()
    m.act._tick_loop()
    assert m.act._ticker is None


def test_a_change_wakes_the_ticker_and_counts_up(m):
    before = m.act._progress
    assert not m.act._wake.is_set()
    m.act._poke()
    assert m.act._progress == before + 1 and m.act._wake.is_set()


def test_the_pause_ends_when_the_page_announces_a_change(m):
    done = threading.Event()
    threading.Timer(0.05, m.act._poke).start()
    waited = []

    def wait():
        import time

        began = time.monotonic()
        m.act._wait_tick(10.0)
        waited.append(time.monotonic() - began)
        done.set()

    threading.Thread(target=wait, daemon=True).start()
    assert done.wait(5)
    assert waited[0] < 5, "the pause ran its course instead of ending at the change"


def test_an_operation_that_ends_announces_it_once(m):
    told = []
    m.act.on_change = lambda: told.append(1)
    op = m.act._begin("probe_b", "b")
    m.act._finish(op, True)
    m.act._finish(op, False, "late")  # (once: the second changes nothing)
    assert told == [1]
    assert op["state"] == "done" and op["code"] is None


def test_a_listener_that_raises_is_logged_and_never_the_operations_failure(m):
    def boom():
        raise RuntimeError("cache gone")

    m.act.on_change = boom
    op = m.act._begin("probe_b", "b")
    m.act._finish(op, True)
    assert op["state"] == "done"


def test_the_product_wires_the_change_to_the_pages_cached_read(m):
    from levi.marvel0 import stack_act_api

    made = stack.Stack(m.host())
    stack_act_api.attach(made)
    assert made.act.on_change is stack_api.invalidate
    stack_api._VIEW[:] = [(0.0, {"cached": True})]
    op = made.act._begin("probe_b", "b")
    made.act._finish(op, True)
    assert stack_api._VIEW == [], "the cached read of the stack was forgotten"


def test_the_pages_cache_lives_two_seconds_for_idle_reads():
    assert stack_api.CACHE_S == 2.0


# --- how long each step took ---------------------------------------------------------------


def test_a_step_keeps_its_clock_and_adds_up_when_it_runs_again(m):
    steps = m.act._new_steps(["x", "y"], lambda name: "a")
    assert steps[0]["state"] == "running" and steps[0]["at"] == m.t
    assert steps[1]["at"] is None and steps[1]["took_s"] is None
    m.t += 2.5
    m.act._item_state(steps[0], "done")
    assert steps[0]["took_s"] == 2.5
    m.t += 1
    m.act._item_state(steps[0], "running")  # (again: the check was asked again)
    m.t += 0.75
    m.act._item_state(steps[0], "done")
    assert steps[0]["took_s"] == 3.25
    m.act._item_state(steps[1], "skipped")  # (never ran)
    assert steps[1]["took_s"] is None


def test_the_sequence_adds_up_what_its_steps_took(m):
    m.m_ready()
    start_sequence(m)
    seq = {}
    for _ in range(40):
        seq = m.act.advance()
        if seq["state"] != "running":
            break
        m.t += 0.5
        if seq.get("step") == "wait":
            m.t += 40
            m.no_manager()
        if "a" in m.units and m.sockets["state"] != "active":
            m.a_up()
    assert seq["state"] == "waiting_confirm"
    took = [i["took_s"] for i in seq["steps"]]
    assert all(isinstance(t, float) for t in took)
    assert sum(took) > 40  # (the wait)


def test_a_failed_step_has_its_time_and_the_ones_after_it_have_none(m):
    m.m_ready()
    m.fci = {"state": "closed"}
    start_sequence(m)
    seq = run_until_over(m)
    got = {i["step"]: (i["state"], i["took_s"]) for i in seq["steps"]}
    assert got["probe"][0] == "failed" and got["probe"][1] is not None
    assert got["wait"] == ("not_reached", None)
    assert seq["took_s"] == sum(
        i["took_s"] for i in seq["steps"] if i["took_s"] is not None
    )


def test_a_running_sequence_says_how_long_it_has_been_going(m):
    m.m_ready()
    start_sequence(m)
    m.act.advance()
    m.t += 7
    assert m.doc()["sequence"]["elapsed_s"] == pytest.approx(7, abs=0.2)


# --- the control file's minimum --------------------------------------------------------------


def test_the_minimum_age_of_the_marker_defaults_to_three_seconds():
    assert control_site.DEFAULT_MARKER_WAIT_S == 3.0
    assert control_site.MARKER_WAIT_FLOOR_S == 2.0
    assert control_site.LEGACY_MARKER_WAIT_S == 45.0
    site = control_site.StackControl()
    assert site.marker_wait_s == 3.0


@pytest.mark.parametrize(
    "value, ok", [(1.9, False), (2, True), (3, True), (601, False)]
)
def test_the_minimum_age_must_be_between_two_and_six_hundred_seconds(value, ok):
    checked = control_site.STACK_CHECKS["marker_wait_s"]
    if ok:
        assert checked(value) == float(value)
    else:
        with pytest.raises(control_site._Bad):
            checked(value)


def test_the_template_names_the_new_range():
    assert "marker_wait_s      = 3 " in control_site.TEMPLATE
    assert "2 to 600" in control_site.TEMPLATE


# --- the heavy jobs behind the page: what a failure to ask does ---------------------------------


def test_a_failure_to_ask_about_heavy_jobs_is_unknown_and_the_fixed_wait_applies(m):
    m.m_ready()
    m.set_marker_old(aged_s=10.0)

    def boom(log, run, now):
        raise RuntimeError("no systemd")

    m.act.host.heavy_jobs = boom
    marker = m.doc()["marker"]
    assert (marker["heavy"]["state"], marker["heavy"]["why"]) == (
        "unknown",
        "heavy_failed",
    )
    assert marker["wait_s"] == control_site.LEGACY_MARKER_WAIT_S
    # an answer that is not one is no answer either
    m.act.host.heavy_jobs = lambda log, run, now: {"state": "all clear"}
    assert m.doc()["marker"]["heavy"]["state"] == "unknown"


# --- the wait: two clean looks, quiet sockets -------------------------------------------------


def to_wait(m, passes=4):
    m.m_ready()
    start_sequence(m)
    for _ in range(passes):
        m.act.advance()


def test_the_wait_wants_two_clean_looks_in_a_row_after_a_job(m):
    m.m_ready()
    start_sequence(m)
    for _ in range(3):  # (monitor, marker, probe: the looks so far were clean)
        m.act.advance()
    m.heavy = {
        "state": "running",
        "count": 1,
        "kinds": ["pytest-dir"],
        "empty": 0,
        "why": None,
    }
    for _ in range(4):
        seq = m.act.advance()
    assert seq["step"] == "wait" and m.systemd_runs("a") == []
    m.heavy = {"state": "clear", "count": 0, "kinds": [], "empty": 0, "why": None}
    assert m.act.advance()["step"] == "wait", "one clean look is not enough"
    assert m.systemd_runs("a") == []
    assert m.act.advance()["step"] == "arm"  # (the second look)
    m.act.advance()
    assert len(m.systemd_runs("a")) == 1


def test_the_wait_goes_on_while_the_arms_sockets_are_not_quiet(m):
    to_wait(m)
    while m.act._seq["step"] != "wait":
        m.act.advance()
    m.sockets = {"state": "active", "detail": "2 control sockets"}  # (someone's)
    for _ in range(5):
        seq = m.act.advance()
    assert seq["step"] == "wait" and m.systemd_runs("a") == []
    assert "arm_sockets" in m.doc()["sequence"]["wait"]["holds"]
    m.sockets = {"state": "inactive", "detail": "no socket to the arm"}
    for _ in range(3):
        m.act.advance()
    assert len(m.systemd_runs("a")) == 1


# --- the order, more ---------------------------------------------------------------------------


def test_the_check_of_b_begins_in_the_pass_that_sets_the_marker(m):
    m.m_ready()
    start_sequence(m)
    m.act.advance()  # monitor is ready: on to the marker
    assert not m.marker_path.exists() and m.oneshots_of("node list") == []
    m.act.advance()  # the marker is set, and the check of B begins with it
    assert m.marker_path.exists() and len(m.oneshots_of("node list")) == 1
    assert m.act._seq["step"] == "marker"


def test_a_marker_that_is_there_already_also_begins_the_check_in_its_pass(m):
    m.m_ready()
    m.act.marker_op("touch", m.body("marker_touch", "fast-touch-00003"))
    start_sequence(m)
    m.act.advance()
    m.act.advance()  # the marker is there and ours: the check begins, on to the probe
    assert len(m.oneshots_of("node list")) == 1 and m.act._seq["step"] == "probe"


def test_the_fci_check_is_made_while_a_check_of_b_still_runs(m):
    m.m_ready()
    start_sequence(m)
    m.act._begin("probe_b", "b")  # (a check of B is running, and stays so)
    for _ in range(4):
        m.act.advance()
    assert len(m.fci_probes) == 1, "the FCI check did not wait for the check of B"
    assert m.act._seq["step"] == "probe"  # (B is not known yet: it waits for that)


def test_a_marker_stays_when_a_was_started_and_then_vanished(m):
    m.m_ready()
    start_sequence(m)
    for _ in range(40):
        seq = m.act.advance()
        if seq["state"] != "running" or "a" in m.units:
            break
    assert "a" in m.units
    m.units.pop("a")  # (it ended by itself: the record says it was started)
    seq = m.act.advance()
    assert (seq["state"], seq["reason"]) == ("failed", "a_dead")
    assert m.marker_path.exists(), "A was started: its stop releases the marker"


def test_a_start_that_goes_on_to_b_refreshes_an_old_answer_during_a_start_up(m):
    m.m_ready()
    m.act.probe_b(probe_request())
    m.t += (
        stack_act.B_PROBE_REFRESH_S + 1
    )  # (older than the refresh, younger than the validity)
    m.refresh_health()
    m.oneshots.clear()
    m.act.sequence_start(m.chain_body("chain-refresh-001"))
    for _ in range(3):
        m.act.advance()
    assert len(m.oneshots_of("node list")) == 1, "begun with the marker"
    seq = m.run_chain()
    assert seq["state"] == "done"
    assert len(m.oneshots_of("node list")) == 1, "not asked again between A and B"
    assert states_of(seq)["b_probe"] == "done"


def states_of(seq):
    return {i["step"]: i["state"] for i in seq["steps"]}


def test_a_start_that_stops_at_a_leaves_an_answer_that_is_good_enough_alone(m):
    m.m_ready()
    m.act.probe_b(probe_request())
    m.t += stack_act.B_PROBE_REFRESH_S + 1
    m.refresh_health()
    m.oneshots.clear()
    start_sequence(m)  # (through A only)
    for _ in range(10):
        m.act.advance()
    assert m.oneshots_of("node list") == []


def test_a_refusal_of_the_check_of_b_that_will_not_go_away_ends_the_start_with_its_code(
    m,
):
    m.m_ready()

    def refuse(r):
        raise stack_act.ActRefuse("systemd_unreadable")

    m.act._do_probe_b = refuse
    start_sequence(m)
    seq = run_until_over(m)
    assert (seq["state"], seq["reason"]) == ("failed", "systemd_unreadable")
    assert m.systemd_runs("a") == [] and not m.marker_path.exists()


def test_a_busy_lock_for_the_check_of_b_is_tried_again_not_the_end(m):
    m.m_ready()
    real = m.act._do_probe_b
    calls = []

    def busy_then_fine(r):
        calls.append(1)
        if len(calls) < 3:
            raise stack_act.ActRefuse("operation_running")
        return real(r)

    m.act._do_probe_b = busy_then_fine
    start_sequence(m)
    for _ in range(12):
        m.act.advance()
    assert len(calls) == 3 and len(m.oneshots_of("node list")) == 1
    assert m.act._seq["step"] in ("wait", "arm", "await_a")


# --- the catalogue ------------------------------------------------------------------------


def test_the_catalogue_names_the_new_words_and_every_hold_the_document_can_say(m):
    cat = stack_act.catalog()
    assert "heavy_jobs_running" in cat["blockers"]
    assert "heavy_job_still_running" in cat["sequence_reasons"]
    assert cat["sequence_holds"] == [
        "marker_absent",
        "marker_young",
        "heavy_jobs_running",
        "heavy_jobs_unknown",
        "arm_sockets",
    ]
    assert cat["heavy_states"] == ["clear", "running", "unknown"]
    assert cat["b_probe_by"] == ["nodes", "probe", "stop"]
    # every way the document says what the wait is for is in the catalogue
    seen = set()
    for heavy in (
        {"state": "running", "count": 1, "kinds": [], "empty": 0, "why": None},
        {"state": "unknown", "count": 0, "kinds": [], "empty": 0, "why": "x"},
        {"state": "clear", "count": 0, "kinds": [], "empty": 0, "why": None},
    ):
        for sockets in ("active", "inactive"):
            m.heavy = heavy
            m.sockets = {"state": sockets, "detail": ""}
            m.m_ready()
            m.set_marker_old(aged_s=0.5) if not m.marker_path.exists() else None
            ctx = m.act._context(m.sup._read())
            seen |= set(m.act._wait_view(ctx, {"step": "wait"})["holds"])
    assert seen <= set(cat["sequence_holds"])
    m.marker_path.unlink()
    ctx = m.act._context(m.sup._read())
    assert "marker_absent" in m.act._wait_view(ctx, {"step": "wait"})["holds"]


def test_the_ways_a_check_of_b_is_answered_are_the_catalogues(m):
    m.m_ready()
    m.act.probe_b(probe_request())
    assert m.act._b_probe["by"] in stack_act.B_PROBE_BY
    m.nodes_out = ""
    m.act.probe_b(probe_request(2))
    assert m.act._b_probe["by"] in stack_act.B_PROBE_BY
    m.start_a()
    m.start_b()
    m.act.stop("b", m.stop_body("b"))
    assert m.act._b_probe["by"] in stack_act.B_PROBE_BY
