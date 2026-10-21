"""The units of the arm stack as data (``levi/marvel0/stack_units.py``) and the
gripper test's command and verdict (``levi/marvel0/gripper.py``): the argv is
the one ``setup.md`` names, the unit has the properties the design record
lists, the ready and failure rules read the lines the programs print, and the
wrapper is the reviewed file. Nothing here starts a process."""

import hashlib
import os
import re
from pathlib import Path

import pytest
from stack_act_machine import (
    BOLD,
    CONTROLLERS_OUT,
    END,
    GREEN,
    GRIPPER_TYPES,
    NO_MANAGER,
    POSE,
    WAITING_LINE,
    list_controllers_print,
    ready_lines,
)
from test_marvel0_deploy import forbid_real_machine  # noqa: F401 - the guard

from levi.marvel0 import control_site, gripper, stack, stack_units

ROOT = Path(stack.__file__).resolve().parents[2]
SETUP_VALUES = {
    "robot_ip": "10.42.0.157",
    "com_port": "/dev/serial/by-id/usb-FTDI_USB_TO_RS-485_DABBX310-if00-port0",
}


def control(**kw):
    values = {
        "enabled": True,
        "franka_control": "/opt/rig/franka_control",
        "reset_joint_target": tuple(POSE),
        **SETUP_VALUES,
        **kw,
    }
    return control_site.StackControl(**values)


@pytest.fixture
def specs():
    return stack_units.real_specs(control())


def command_of(spec):
    """The command behind the wrapper."""
    assert spec.argv[:2] == ("/bin/bash", str(stack_units.WRAPPER))
    assert spec.argv[3] == "--"
    return spec.argv[4:]


def find_setup():
    for parent in ROOT.parents:
        candidate = parent / "setup.md"
        if candidate.is_file():
            return candidate
    return None


def setup_command(text, head):
    """The tokens of the command of ``setup.md`` that starts with ``head``
    (backslash continuations joined)."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(head))
    out = []
    for line in lines[start:]:
        out.append(line.rstrip("\\").strip())
        if not line.rstrip().endswith("\\"):
            break
    return " ".join(out).split()


# --- the commands -----------------------------------------------------------------------


def test_the_commands_are_the_ones_setup_md_names(specs):
    setup = find_setup()
    if setup is None:
        pytest.skip("no setup.md above this checkout")
    text = setup.read_text(encoding="utf-8")
    for key, head in (
        ("a", "ros2 launch serl_franka_controllers_ros2"),
        ("b", "ros2 launch franka_gripper_manager"),
        ("c", "python3 -m fr3_robot_server.franka_server_ros2"),
    ):
        wanted = setup_command(text, head)
        got = list(command_of(specs.units[key]))
        # ``--noninteractive`` is the unit's own (its standard input is null).
        got = [t for t in got if t != "--noninteractive"]
        assert got == wanted, key


def test_a_unit_runs_behind_the_wrapper_with_the_rigs_environment_file(specs):
    for key in "abc":
        argv = specs.units[key].argv
        assert (
            argv[0] == "/bin/bash"
            and argv[2] == "/opt/rig/franka_control/env/fr3_env.sh"
        )
        assert argv[3] == "--"


def test_launch_is_noninteractive_for_a_and_b_only(specs):
    assert "--noninteractive" in command_of(specs.units["a"])
    assert "--noninteractive" in command_of(specs.units["b"])
    assert "--noninteractive" not in command_of(specs.units["c"])


def test_machine_values_come_from_the_control_file_only():
    other = stack_units.real_specs(
        control(robot_ip="10.9.9.9", server_port=5056, com_port="/dev/ttyUSB7")
    )
    a, b, c = (command_of(other.units[k]) for k in "abc")
    assert "robot_ip:=10.9.9.9" in a
    assert "com_port:=/dev/ttyUSB7" in b
    assert "--robot_ip=10.9.9.9" in c and "--flask_port=5056" in c
    assert not any("10.42.0.157" in t for u in other.units.values() for t in u.argv)


def test_c_runs_in_the_server_folder_and_a_and_b_anywhere(specs):
    assert specs.units["c"].cwd == "/opt/rig/franka_control/robot_server"
    assert specs.units["a"].cwd is None and specs.units["b"].cwd is None


def test_a_missing_machine_value_gives_no_specs():
    with pytest.raises(ValueError):
        stack_units.real_specs(control_site.StackControl(enabled=True))


@pytest.mark.parametrize("bad", ["a b", "a$b", "a%b", "a\nb"])
def test_an_unsafe_token_is_refused(bad):
    with pytest.raises(ValueError):
        stack_units.real_specs(control(com_port=f"/dev/{bad}"))


def test_the_unit_names_are_the_ones_the_stack_view_asks_about(specs):
    for key in "abc":
        assert specs.units[key].service == stack.STACK_UNITS[key.upper()]
        assert specs.units[key].unit.startswith("levi-aeri-stack-marvel0-")


# --- the unit -------------------------------------------------------------------------


def run_argv(specs, key, unset=("ROS_DISTRO",)):
    return stack_units.systemd_run_argv(
        specs.units[key], "/state/marvel0/stack/x/logs/x.log", unset
    )


def test_every_long_lived_unit_stops_like_ctrl_c_and_never_restarts(specs):
    for key in "abc":
        argv = run_argv(specs, key)
        for prop in (
            "Type=exec",
            "KillMode=control-group",
            "KillSignal=SIGINT",
            "SendSIGKILL=no",
            "TimeoutStopSec=30",
            "Restart=no",
            "StandardInput=null",
            "ManagedOOMPreference=avoid",
        ):
            assert f"--property={prop}" in argv, (key, prop)
        assert specs.units[key].stop_timeout_s >= 30


def test_the_realtime_limits_are_a_and_b_s_and_the_memory_lock_a_only(specs):
    def limits(key):
        return sorted(p for p in run_argv(specs, key) if "Limit" in p)

    assert limits("a") == [
        "--property=LimitMEMLOCK=infinity",
        "--property=LimitRTPRIO=99",
    ]
    assert limits("b") == ["--property=LimitRTPRIO=99"]
    assert limits("c") == []


def test_no_priority_or_cpu_setting_and_no_collect(specs):
    for key in "abc":
        argv = " ".join(run_argv(specs, key))
        for word in ("Nice=", "CPUAffinity", "CPUQuota", "--collect", "Restart=on"):
            assert word not in argv, (key, word)


def test_the_log_is_appended_by_systemd_and_the_environment_is_clean(specs):
    argv = run_argv(specs, "a", unset=("CONDA_SHLVL", "ROS_DISTRO"))
    log = "/state/marvel0/stack/x/logs/x.log"
    assert f"--property=StandardOutput=append:{log}" in argv
    assert f"--property=StandardError=append:{log}" in argv
    assert "--property=UnsetEnvironment=CONDA_SHLVL ROS_DISTRO" in argv
    assert "--setenv=PYTHONUNBUFFERED=1" in argv
    assert "--setenv=RCUTILS_COLORIZED_OUTPUT=0" in argv
    assert argv[argv.index("--") + 1 :] == list(specs.units["a"].argv)
    assert "--working-directory=/opt/rig/franka_control/robot_server" in run_argv(
        specs, "c"
    )


def test_the_manager_environment_names_to_drop_are_the_policy_units_list():
    text = "PATH=/usr/bin\nROS_DISTRO=jazzy\nAMENT_PREFIX_PATH=/x\nCONDA_SHLVL=1\nHOME=/h\nPYTHONPATH=/p\n"
    assert stack_units.unset_names(text) == [
        "AMENT_PREFIX_PATH",
        "CONDA_SHLVL",
        "PYTHONPATH",
        "ROS_DISTRO",
    ]


def test_a_unit_is_recognised_by_its_id_and_exact_command(specs):
    spec = specs.units["a"]
    rec = {"unit": spec.unit, "exec_argv": list(spec.argv)}
    shown = {
        "Id": spec.service,
        "ExecStart": f"{{ path={spec.argv[0]} ; argv[]={' '.join(spec.argv)} ; ignore_errors=no }}",
    }
    assert stack_units.same_unit(shown, rec)
    assert not stack_units.same_unit({**shown, "Id": "other.service"}, rec)
    assert not stack_units.same_unit(
        {
            **shown,
            "ExecStart": shown["ExecStart"].replace(
                "load_gripper:=false", "load_gripper:=true"
            ),
        },
        rec,
    )
    assert not stack_units.same_unit(None, rec) and not stack_units.same_unit(
        shown, None
    )
    assert not stack_units.same_unit(shown, {"unit": spec.unit})


def test_a_oneshot_waits_for_its_output_and_is_collected():
    argv = stack_units.oneshot_argv(
        "levi-aeri-stack-marvel0-oneshot-x", ["true"], 30, "t"
    )
    for word in ("--pipe", "--wait", "--collect", "--property=RuntimeMaxSec=30"):
        assert word in argv
    assert argv[-2:] == ["--", "true"]
    assert stack_units.ONESHOT_PREFIX.startswith(stack_units.UNIT_PREFIX)


# --- the wrapper ------------------------------------------------------------------------


def test_the_wrapper_is_the_reviewed_file():
    digest = hashlib.sha256(stack_units.WRAPPER.read_bytes()).hexdigest()
    assert stack_units.wrapper_sha256() == digest == stack_units.WRAPPER_SHA256, (
        "stack_exec.sh changed: review it, then update WRAPPER_SHA256 in stack_units.py"
    )


def test_the_wrapper_only_sources_and_execs():
    text = stack_units.WRAPPER.read_text()
    body = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    for word in ("curl", "ssh", "sudo", "kill", "ros2", "/dev/tcp", "eval "):
        assert word not in body, word
    assert 'exec "${cmd[@]}"' in body and "RCUTILS_COLORIZED_OUTPUT=0" in body


# --- reading the lines --------------------------------------------------------------------

HEALTH_OK = {"state": "ok", "hardware_active": True, "controller_active": True}
HEALTH_RED = {"state": "red", "hardware_active": False, "controller_active": False}


def plain(lines):
    return [stack_units.strip_ansi(line) for line in lines]


def test_a_is_ready_with_its_lines_and_the_hardware_active(specs):
    got = stack_units.read_lines(specs.units["a"], ready_lines("a"), HEALTH_OK, None)
    assert got["ready"] and not got["missing"] and got["failure"] is None
    assert [r["key"] for r in got["seen"]] == [
        "fifo",
        "spawners",
        "health_hardware_active",
    ]


def test_a_unit_that_is_active_with_a_dead_health_file_is_not_ready(specs):
    got = stack_units.read_lines(specs.units["a"], ready_lines("a"), HEALTH_RED, None)
    assert not got["ready"]
    assert [r["key"] for r in got["missing"]] == ["health_hardware_active"]
    assert not stack_units.read_lines(specs.units["a"], ready_lines("a"), None, None)[
        "ready"
    ]
    stale = {"state": "offline", "hardware_active": True}
    assert not stack_units.read_lines(specs.units["a"], ready_lines("a"), stale, None)[
        "ready"
    ]


def test_a_needs_priority_98_and_both_spawners(specs):
    lines = ready_lines("a")
    low = [l.replace("priority 98", "priority 97") for l in lines]
    assert not stack_units.read_lines(specs.units["a"], low, HEALTH_OK, None)["ready"]
    one = [l for l in lines if "spawner-5" not in l]
    got = stack_units.read_lines(specs.units["a"], one, HEALTH_OK, None)
    assert [r["key"] for r in got["missing"]] == ["spawners"]


@pytest.mark.parametrize(
    "line, key",
    [
        (
            "A Franka ros2_control_node is already running. Stop it first",
            "duplicate_launch",
        ),
        (
            "Could not enable FIFO RT scheduling policy: with error number <1>",
            "fifo_failed",
        ),
        (
            "[WARN] Unable to lock the memory: 'Operation not permitted'",
            "memlock_failed",
        ),
        (
            "Failed to set the initial state of the component : FrankaHardwareInterface",
            "hardware_failed",
        ),
        (
            "[ERROR] [ros2_control_node-2]: process has died [pid 1, exit code -11]",
            "node_died",
        ),
    ],
)
def test_a_names_its_failures(specs, line, key):
    got = stack_units.read_lines(
        specs.units["a"], [*ready_lines("a"), line], HEALTH_OK, None
    )
    assert got["failure"] == key and not got["ready"]


def test_a_ready_line_printed_twice_is_a_second_process(specs):
    lines = [*ready_lines("a"), ready_lines("a")[1]]
    got = stack_units.read_lines(specs.units["a"], lines, HEALTH_OK, None)
    assert got["duplicate"] == "fifo" and not got["ready"]


def test_b_is_ready_with_a_driver_and_three_controllers_through_the_colour_codes(specs):
    lines = plain(ready_lines("b"))
    got = stack_units.read_lines(specs.units["b"], lines, None, None)
    assert got["ready"] and len(got["seen"]) == 4
    # (the lines carry escape codes in the log; the reader strips them)
    coloured = f"{GREEN}Configured and activated {BOLD}robotiq_gripper_controller{END}"
    assert "\x1b" in coloured
    lines = [l for l in lines if "robotiq_gripper_controller" not in l]
    got = stack_units.read_lines(specs.units["b"], lines, None, None)
    assert [r["key"] for r in got["missing"]] == [
        "controller_robotiq_gripper_controller"
    ]


@pytest.mark.parametrize(
    "line, key",
    [
        (
            "Failed to set the initial state of the component : RobotiqGripperHardwareInterface",
            "hardware_failed",
        ),
        (
            "[spawner-4] [FATAL] Failed loading controller robotiq_gripper_controller",
            "controller_failed",
        ),
        ("[ERROR] [robotiq_gripper_client-7]: process has died [pid 3]", "node_died"),
    ],
)
def test_b_names_its_failures(specs, line, key):
    got = stack_units.read_lines(
        specs.units["b"], [*plain(ready_lines("b")), line], None, None
    )
    assert got["failure"] == key


def test_c_needs_its_lines_a_listener_and_the_controller_active(specs):
    spec = specs.units["c"]
    got = stack_units.read_lines(spec, ready_lines("c"), HEALTH_OK, True)
    assert got["ready"]
    assert not stack_units.read_lines(spec, ready_lines("c"), HEALTH_OK, False)["ready"]
    assert not stack_units.read_lines(spec, ready_lines("c"), HEALTH_OK, None)["ready"]
    no_controller = {**HEALTH_OK, "controller_active": False}
    got = stack_units.read_lines(spec, ready_lines("c"), no_controller, True)
    assert [r["key"] for r in got["missing"]] == ["health_controller_active"]


def test_c_accepts_an_impedance_controller_that_was_already_active(specs):
    lines = [l.replace("started", "already active") for l in ready_lines("c")]
    assert stack_units.read_lines(specs.units["c"], lines, HEALTH_OK, True)["ready"]


def test_c_listens_where_the_control_file_says():
    other = stack_units.real_specs(control(server_port=5056))
    on_5000 = ready_lines("c", 5000)
    assert not stack_units.read_lines(other.units["c"], on_5000, HEALTH_OK, True)[
        "ready"
    ]
    assert stack_units.read_lines(
        other.units["c"], ready_lines("c", 5056), HEALTH_OK, True
    )["ready"]
    assert other.units["c"].listen_port == 5056


@pytest.mark.parametrize(
    "line, key",
    [
        (
            "[WARN] [franka_server]: No fresh robot state; impedance activation deferred",
            "activation_deferred",
        ),
        ("OSError: [Errno 98] Address already in use", "port_taken"),
        ("Traceback (most recent call last):", "traceback"),
    ],
)
def test_c_names_its_failures(specs, line, key):
    got = stack_units.read_lines(
        specs.units["c"], [*ready_lines("c"), line], HEALTH_OK, True
    )
    assert got["failure"] == key and not got["ready"]
    assert stack_units.failure_text(specs.units["c"], key)


def test_every_rule_names_the_source_that_prints_it(specs):
    for spec in specs.units.values():
        for item in (*spec.ready, *spec.fail):
            assert item.source and re.search(
                r"[\w/]+\.\w+|launch|werkzeug|flask|python|socket|hardware", item.source
            )


# Each rule's text is in the source that prints it: checked when the franka_control
# checkout is there (it is outside this repository).
SOURCE_FRAGMENTS = {
    "franka_ros2_ws/src/ros2_control/controller_manager/src/ros2_control_node.cpp": [
        "Successful set up FIFO RT scheduling policy with priority",
        "Could not enable FIFO RT scheduling policy",
        "Unable to lock the memory",
    ],
    "franka_ros2_ws/src/serl_franka_controllers_ros2/launch/impedance.launch.py": [
        "A Franka ros2_control_node is already running",
    ],
    "franka_ros2_ws/src/ros2_robotiq_gripper/robotiq_driver/src/hardware_interface.cpp": [
        "Robotiq Gripper successfully activated!",
    ],
    "franka_ros2_ws/src/ros2_control/controller_manager/controller_manager/spawner.py": [
        "Configured and activated",
    ],
    # The "Failed to set the initial state" rule of A and B (hardware_failed).
    "franka_ros2_ws/src/ros2_control/controller_manager/src/controller_manager.cpp": [
        "Failed to set the initial state of the component",
    ],
    # The print format of the gripper-controller check and of the probe whether a
    # gripper stack runs (list_controllers rows, "no controller", the endless
    # wait for the service and its warning).
    "franka_ros2_ws/src/ros2_control/ros2controlcli/ros2controlcli/verb/list_controllers.py": [
        (
            'f"{state_color}{c.name:<{col_width_name}}{bcolors.ENDC} '
            "{c.type:<{col_width_type}}  "
            '{state_color}{c.state:<{col_width_state}}{bcolors.ENDC}"'
        ),
        'print("No controllers are currently loaded!")',
    ],
    "franka_ros2_ws/src/ros2_control/controller_manager/controller_manager/controller_manager_services.py": [
        "waiting for service {fully_qualified_service_name} to become available...",
        'node.get_logger().warning(f"Could not contact service {fully_qualified_service_name}")',
        "elif not cli.wait_for_service(10.0):",
        "service_timeout=0.0",
        "RCUTILS_COLORIZED_OUTPUT",
    ],
    "robot_server/fr3_robot_server/franka_server_ros2.py": [
        "Impedance controller started",
        "Impedance controller already active",
        "/jointreset mode:",
        "impedance activation deferred",
        "ReliabilityPolicy.BEST_EFFORT",
    ],
}


def test_the_rules_are_in_the_franka_control_sources_when_they_are_there():
    root = None
    for parent in ROOT.parents:
        if (parent / "franka_control" / "env" / "fr3_env.sh").is_file():
            root = parent / "franka_control"
            break
        if (parent / "work" / "franka_control" / "env").is_dir():
            root = parent / "work" / "franka_control"
            break
    if root is None:
        pytest.skip("no franka_control checkout beside this one")
    for rel, fragments in SOURCE_FRAGMENTS.items():
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
        for fragment in fragments:
            assert fragment in text, (rel, fragment)


# --- the one-shot checks' outputs --------------------------------------------------------


def real_rows(state="active", color=False, only=None):
    names = only or list(GRIPPER_TYPES)
    return list_controllers_print(
        [(n, GRIPPER_TYPES[n], state) for n in names], color=color
    )


def test_the_real_three_row_listing_is_parsed():
    # The three rows B's controller manager lists, in the CLI's own format
    # (list_controllers.py:34-36): name, type, state, no brackets.
    text = real_rows()
    assert "joint_state_broadcaster_robotiq joint_state_broadcaster/" in text
    assert "[" not in text
    listed = stack_units.parse_controllers(text)
    assert listed["state"] == "listed"
    assert listed["controllers"] == dict.fromkeys(GRIPPER_TYPES, "active")
    assert stack_units.gripper_controllers_ok(listed)
    assert listed == stack_units.parse_controllers(CONTROLLERS_OUT)


def test_colour_codes_do_not_change_the_listing():
    # (RCUTILS_COLORIZED_OUTPUT=1 puts the codes around name and state)
    coloured = real_rows(color=True)
    assert "\x1b[92m" in coloured
    assert stack_units.parse_controllers(coloured) == stack_units.parse_controllers(
        real_rows()
    )


def test_a_listing_with_a_controller_that_is_not_active_is_not_ok():
    rows = [
        (
            n,
            GRIPPER_TYPES[n],
            "inactive" if n == "robotiq_gripper_controller" else "active",
        )
        for n in GRIPPER_TYPES
    ]
    got = stack_units.parse_controllers(list_controllers_print(rows))
    assert got["controllers"]["robotiq_gripper_controller"] == "inactive"
    assert not stack_units.gripper_controllers_ok(got)
    unconfigured = real_rows("unconfigured")
    assert stack_units.parse_controllers(unconfigured)["state"] == "listed"
    assert not stack_units.gripper_controllers_ok(
        stack_units.parse_controllers(unconfigured)
    )


def test_a_manager_without_controllers_is_listed_empty_not_absent():
    # list_controllers.py:122: the manager answered, nothing is loaded. It is
    # there (a gripper stack exists), and the three controllers are missing.
    got = stack_units.parse_controllers(list_controllers_print([]))
    assert got == {"state": "listed", "controllers": {}}
    assert not stack_units.gripper_controllers_ok(got)


def test_only_a_listing_with_all_three_active_rows_passes():
    partial = real_rows(only=["robotiq_gripper_controller"])
    assert not stack_units.gripper_controllers_ok(
        stack_units.parse_controllers(partial)
    )
    assert not stack_units.gripper_controllers_ok(
        stack_units.parse_controllers(NO_MANAGER)
    )


def test_could_not_contact_service_means_absent_whatever_the_exit_status_was():
    # The command never ends by itself: the warning is the whole answer, and a
    # caller sees it with whatever status ended the command.
    assert NO_MANAGER.count("waiting for service") == 2
    assert stack_units.parse_controllers(NO_MANAGER)["state"] == "absent"
    # (the warning in rclpy's colourless line format and on its own)
    assert (
        stack_units.parse_controllers(
            "Could not contact service /robotiq/controller_manager/list_controllers"
        )["state"]
        == "absent"
    )


def test_a_command_that_was_ended_before_it_could_tell_is_unknown():
    # Only the first "waiting" line: ended before the 10 s warning, so nothing
    # is known (the service may have been about to appear).
    assert stack_units.parse_controllers(WAITING_LINE)["state"] == "unknown"
    assert stack_units.parse_controllers("did not finish in time")["state"] == "unknown"
    assert stack_units.parse_controllers("")["state"] == "unknown"
    assert stack_units.parse_controllers("Traceback ... rclpy")["state"] == "unknown"
    # the warning of another service is not this answer
    assert (
        stack_units.parse_controllers(
            "Could not contact service /controller_manager/list_hardware_components"
        )["state"]
        == "unknown"
    )


def test_log_lines_that_look_like_a_row_are_not_rows():
    noisy = (
        "[INFO] [1760000100.0] [x]: configured and active\n"
        "\tupdate_rate: 500 Hz\n"
        "    robotiq_gripper_controller position_controllers/GripperActionController  active\n"
    )
    assert stack_units.parse_controllers(noisy)["state"] == "unknown"


def test_the_end_effector_check_passes_only_on_its_own_ok_line():
    assert stack_units.ee_frame_ok(f"  {stack_units.EE_OK}\n")
    assert stack_units.ee_frame_ok(f"\x1b[32m{stack_units.EE_OK}\x1b[0m")
    assert not stack_units.ee_frame_ok(
        "  FAIL -- expected 'franka_hand', measured 'robotiq'."
    )
    assert not stack_units.ee_frame_ok("TIMEOUT ... no data")


def test_the_qos_check_is_the_preflight_scripts_grep():
    assert stack_units.qos_ok("x\nreliability=ReliabilityPolicy.BEST_EFFORT,\n")
    assert not stack_units.qos_ok("reliability=ReliabilityPolicy.RELIABLE,\n")


# --- the gripper test ---------------------------------------------------------------------

# (send_goal.py:211-212: sorted yaml keys, only the first line indented)
OUT = """\
Goal accepted with ID: 01

Result:
    effort: 0.0
position: {p}
reached_goal: {r}
stalled: {s}

Goal finished with status: SUCCEEDED
"""


def out(reached="true", stalled="false", position="0.0"):
    return OUT.format(p=position, r=reached, s=stalled)


def test_the_goal_is_a_position_with_no_effort_through_the_wrapper(specs):
    for action, position in (("open", "0.0"), ("close", "0.7929")):
        argv = specs.gripper_cmd(stack_units.GRIPPER_POSITIONS[action])
        assert argv[:4] == (
            "/bin/bash",
            str(stack_units.WRAPPER),
            "/opt/rig/franka_control/env/fr3_env.sh",
            "--",
        )
        # SIGINT, not timeout's default SIGTERM: only send_goal's SIGINT handler
        # cancels the goal (ros2action/verb/send_goal.py:146-170).
        assert argv[4:10] == ("timeout", "-s", "INT", "-k", "5", "20")
        assert argv[10:14] == (
            "ros2",
            "action",
            "send_goal",
            "/robotiq/robotiq_gripper_controller/gripper_cmd",
        )
        assert argv[14] == "control_msgs/action/GripperCommand"
        assert argv[15] == f"{{command: {{position: {position}, max_effort: 0.0}}}}"


@pytest.mark.parametrize(
    "code, text, ok, verdict",
    [
        (0, out(), True, "reached"),
        (0, out("false", "true"), False, "stalled"),
        (0, out("false", "false"), False, "not_reached"),
        (
            0,
            "Waiting for an action server to become available...\n",
            False,
            "no_result",
        ),
        (124, "", False, "timeout"),
        (0, "Goal was rejected.\n", False, "rejected"),
        (125, "Failed to start", False, "start_failed"),
        # (ended without a result by a status that is not 0 or timeout's 124:
        # nothing says it cancelled, so the gripper may still be moving)
        (1, "", False, "timeout_killed"),
        (255, "", False, "timeout_killed"),
        (0, "", False, "no_result"),
    ],
)
def test_the_verdict_is_read_from_what_the_command_printed(code, text, ok, verdict):
    got = gripper.judge(code, text)
    assert got["ok"] is ok and got["code"] == verdict
    assert verdict in gripper.RESULT_TEXT


def test_a_stall_is_never_a_pass_even_when_the_goal_is_reached():
    assert gripper.judge(0, out("true", "true"))["ok"] is False


def test_execute_runs_one_oneshot_and_keeps_the_result(specs):
    seen = []

    def run(argv, timeout):
        seen.append((argv, timeout))
        return 0, out(position="0.7929")

    got = gripper.execute(run, specs, "close", 1_800_000_000.0)
    assert got == {
        "action": "close",
        "ok": True,
        "code": "reached",
        "reached": True,
        "stalled": False,
        "at": 1_800_000_000.0,
    }
    [(argv, timeout)] = seen
    assert argv[0:3] == ["systemd-run", "--user", "--quiet"]
    assert "--pipe" in argv and "--wait" in argv
    assert any(
        a.startswith("--unit=levi-aeri-stack-marvel0-oneshot-gripper-") for a in argv
    )
    assert "position: 0.7929, max_effort: 0.0" in argv[-1]
    assert timeout > stack_units.GRIPPER_TIMEOUT_S
    with pytest.raises(ValueError):
        gripper.execute(run, specs, "squeeze", 1.0)


# --- review 2026-10-21 (R1 M3/M4, R2 1/2/3/6/9) --------------------------------------------


def test_the_command_that_never_ends_is_bounded_and_ended_with_sigint(specs):
    # `ros2 control list_controllers` waits for ever for a missing manager
    # (service_timeout=0.0), so every use of it is wrapped.
    argv = specs.controllers_cmd
    assert argv[4:10] == ("timeout", "-s", "INT", "-k", "3", "20")
    assert argv[10:] == (
        "ros2",
        "control",
        "list_controllers",
        "-c",
        "/robotiq/controller_manager",
    )


def test_the_outer_limit_of_a_oneshot_outlasts_the_inner_one():
    # RuntimeMaxSec is only the backstop of `timeout`.
    assert gripper.RUNTIME_MAX_S > (
        stack_units.GRIPPER_TIMEOUT_S + stack_units.GRIPPER_KILL_AFTER_S
    )
    assert stack_units.PROBE_RUNTIME_MAX_S > (
        stack_units.LIST_TIMEOUT_S + stack_units.LIST_KILL_AFTER_S
    )
    # the warning is logged 10 s after the wait starts (and python needs a
    # while to start): the bound must leave room for it
    assert stack_units.LIST_TIMEOUT_S >= 15


def test_a_cancelled_goal_is_a_timeout_that_says_so():
    printed = "Canceling goal...\nGoal canceled.\nResult:\n    reached_goal: false\n    stalled: false\n\nGoal finished with status: CANCELED\n"
    got = gripper.judge(124, printed)
    assert got["ok"] is False and got["code"] == "timeout"
    # the same text without `timeout` involved (systemd's backstop sent SIGINT)
    assert gripper.judge(0, printed)["code"] == "timeout"


@pytest.mark.parametrize(
    "code, text",
    [
        (137, ""),  # timeout had to kill it (a shell's status)
        (-9, ""),  # ... python's
        (
            255,
            "",
        ),  # ... what systemd-run --wait gives for a unit that ended by a signal
        (255, "Waiting for an action server to become available...\n"),
        (2, "Traceback (most recent call last):\n"),
        (
            124,
            "Sending goal:\n  command:\nGoal accepted with ID: 01\n",
        ),  # no cancel seen
        (124, "did not finish in time"),  # the page's own runner gave up
        (1, "Goal accepted with ID: 01\n"),  # ended with a goal accepted, no result
    ],
)
def test_a_goal_that_was_not_seen_cancelled_may_still_be_moving(code, text):
    got = gripper.judge(code, text)
    assert got["ok"] is False and got["code"] == "timeout_killed"
    assert "may still be moving" in gripper.RESULT_TEXT["timeout_killed"]
    assert "cancel" in gripper.RESULT_TEXT["timeout"]


def test_a_wait_that_never_got_a_server_sent_nothing_and_is_a_plain_timeout():
    waiting = "Waiting for an action server to become available...\n"
    assert gripper.judge(124, waiting)["code"] == "timeout"
    assert gripper.judge(0, "Goal was rejected.\n")["code"] == "rejected"


def test_the_unit_name_is_told_before_the_test_runs(specs):
    told = []

    def run(argv, timeout):
        assert told, "the hook comes first"
        return 0, out()

    gripper.execute(run, specs, "open", 1_800_000_000.0, unit_hook=told.append)
    assert told == [gripper.unit_name(1_800_000_000.0)]


def test_once_ready_the_start_up_rules_no_longer_apply(specs):
    # R2 2: /startimp, the reset paths and a Flask 500 print C's start-up lines
    # again (franka_server_ros2.py:318-320, :751-754) or a Traceback.
    spec = specs.units["c"]
    health = {"state": "ok", "controller_active": True}
    healthy = ready_lines("c")
    again = [*healthy, healthy[1], "Traceback (most recent call last):"]
    before = stack_units.read_lines(spec, again, health, True)
    assert before["failure"] == "traceback" and before["duplicate"] == "impedance"
    after = stack_units.read_lines(spec, again, health, True, latched=True)
    assert after["failure"] is None and after["duplicate"] is None
    assert after["ready"] and not after["missing"]
    # the log is not read at all once latched
    assert stack_units.read_lines(spec, [], health, True, latched=True)["ready"]


def test_a_process_that_dies_after_the_start_is_still_a_failure_of_a_and_b(specs):
    # (B has no other evidence: a launch keeps running when one process died)
    died = "[ERROR] [launch]: [ros2_control_node-1]: process has died [pid 5, exit code -6, cmd 'x']."
    for key in "ab":
        late = stack_units.read_lines(
            specs.units[key],
            [died],
            {"state": "ok", "hardware_active": True},
            None,
            latched=True,
        )
        assert late["failure"] == "node_died" and not late["ready"]
        assert [i.key for i in stack_units.late_rules(specs.units[key])] == [
            "node_died"
        ]
        # ... and nothing else of the start-up rules
        other = stack_units.read_lines(
            specs.units[key],
            ["Could not enable FIFO RT scheduling policy"],
            {"state": "ok", "hardware_active": True},
            None,
            latched=True,
        )
        assert other["failure"] is None
    assert stack_units.late_rules(specs.units["c"]) == ()


def test_a_latched_unit_loses_only_its_live_evidence(specs):
    spec = specs.units["c"]
    fresh = {"state": "ok", "controller_active": True}
    stale = {"state": "offline", "controller_active": True}
    lost = stack_units.read_lines(spec, [], stale, True, latched=True)
    assert not lost["ready"] and [m["kind"] for m in lost["missing"]] == ["health"]
    no_listener = stack_units.read_lines(spec, [], fresh, False, latched=True)
    assert [m["key"] for m in no_listener["missing"]] == ["listening"]
    unknown_table = stack_units.read_lines(spec, [], fresh, None, latched=True)
    assert [m["key"] for m in unknown_table["missing"]] == ["listening"]
    # the log rows count as seen
    assert {s["kind"] for s in lost["seen"]} == {"log", "socket"}
    # B has no live evidence: latched, it stays ready
    assert stack_units.read_lines(
        specs.units["b"], [], {"state": "offline"}, None, latched=True
    )["ready"]


def test_the_rule_source_of_the_hardware_failure_is_the_controller_manager(specs):
    for spec in (specs.units["a"], specs.units["b"]):
        item = next(i for i in spec.fail if i.key == "hardware_failed")
        assert "controller_manager.cpp:919" in item.source


def test_a_log_is_cut_in_place_to_its_tail(tmp_path):
    # R2 9: werkzeug logs every request of C's clients for as long as it runs;
    # systemd appends (O_APPEND), so the file can be cut under the writer.
    path = tmp_path / "c.log"
    lines = [f"line {n:06d} " + "x" * 80 for n in range(4000)]
    path.write_text("\n".join(lines) + "\n")
    size = path.stat().st_size
    writer = os.open(path, os.O_WRONLY | os.O_APPEND)
    try:
        assert stack_units.trim_log(path, cap=size + 1, keep=2000) is False
        assert path.stat().st_size == size
        assert stack_units.trim_log(path, cap=size - 1, keep=2000) is True
        kept = path.read_text().splitlines()
        assert 0 < path.stat().st_size <= 2000 + 100
        assert kept[-1] == lines[-1] and kept[0].startswith("line ")
        os.write(writer, b"after the cut\n")  # the writer carries on at the end
    finally:
        os.close(writer)
    assert path.read_text().splitlines()[-1] == "after the cut"


def test_a_link_or_a_missing_log_is_never_cut(tmp_path):
    target = tmp_path / "other.log"
    target.write_text("x" * 100)
    link = tmp_path / "c.log"
    link.symlink_to(target)
    assert stack_units.trim_log(link, cap=1, keep=10) is False
    assert target.read_text() == "x" * 100
    assert stack_units.trim_log(tmp_path / "gone.log", cap=1, keep=10) is False


def test_a_one_shot_unit_does_not_wait_for_what_its_command_leaves_behind():
    """The ros2 CLI starts the ROS 2 daemon as a detached child; the unit then
    stays in "deactivating" for the default 90 s stop timeout and `systemd-run
    --wait --pipe` does not return: a check that succeeded lost its output (the first
    real run of Start whole stack, 21 Oct). The leftovers get one second."""
    argv = stack_units.oneshot_argv(
        "levi-aeri-stack-marvel0-oneshot-x", ["true"], 35, "x"
    )
    assert f"--property=TimeoutStopSec={stack_units.ONESHOT_STOP_S}" in argv
    assert stack_units.ONESHOT_STOP_S <= 2
