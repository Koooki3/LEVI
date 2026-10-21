"""The units of the arm stack as data: what the page starts for A (the arm
controller), B (the gripper stack) and C (the robot server), the lines that
prove each is ready or has failed, and the one-shot checks it runs beside them.
Nothing here starts anything; ``stack_act.py`` does, through its ``Host``.

* **The commands** are the ones ``setup.md`` section 2 names (and
  ``reviews/stack-start/1-launch-requirements.md`` measures), with the values
  that are machine facts (the checkout, the robot's address, the serial device,
  the reset pose, the server's port) taken from the control file and from
  nowhere else. ``ros2 launch`` gets ``--noninteractive`` written out: with a
  standard input that is ``/dev/null`` it is launch's own default
  (``not sys.stdin.isatty()``, ros2launch/command/launch.py:77), so the flag
  only keeps a changed ``StandardInput=`` from flipping it. In this mode
  launch sends SIGINT on to its children even when it was itself interrupted
  (launch/actions/execute_local.py:448-452), so under ``KillMode=control-group``
  a child of launch gets the unit's SIGINT and launch's: two, where a terminal
  sends one (an interactive launch does not forward). ``control-group`` is kept
  on purpose: it is also what stops children that outlive a launch that died.
  Whether a double SIGINT matters for these nodes is to be seen at S4b.
  The ROS environment is not copied into the unit: the
  command runs behind ``stack_exec.sh <fr3_env.sh> --``, which sources the
  rig's own file and replaces itself with the command.
* **The unit** stops with ``SIGINT`` (the terminal's Ctrl+C) and is given 30 s,
  has no restart, no standard input, the realtime limits the control node asks
  for, and an opt-out from the memory-pressure killer. ``SendSIGKILL=no``: the
  page never kills a control loop. ``ros2 launch`` escalates by itself (SIGTERM
  after 5 s, SIGKILL after 10 s), which systemd cannot prevent; a normal stop
  of A therefore takes 10-11 s, and a unit that is still stopping after
  ``STOPPING_RED_S`` is shown as a problem, not waited for.
* **Ready is read from two places at once**: the lines the processes print to
  the unit's log, and, where the rig has one, the health file or the socket
  table. A unit being active proves nothing (A stays active after a reflex, a
  half-started B stays active without a gripper, a duplicate A exits 0).
  The line patterns carry the source file and line that print them; a test
  greps those sources when the franka_control checkout exists. **None of the
  patterns has been checked against a log of a real unit**: no unit of this
  kind has been started on the real machine.
"""

import hashlib
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from . import deploy, stack

WRAPPER = Path(__file__).with_name("stack_exec.sh")
# The sha256 of ``stack_exec.sh`` the page agrees to start units behind (a
# test keeps the two equal: edit the file, update the number, review both).
WRAPPER_SHA256 = "01fe4bab02a3831071efa4ca06e80c84e7c4cff1305be48157fadeb41a2df1d8"
ENV_FILE = ("env", "fr3_env.sh")
UNIT_PREFIX = "levi-aeri-stack-marvel0-"
ORDER = ("m", "a", "b", "c")
KEYS = ("a", "b", "c")
STOP_TIMEOUT_S = 30
STOPPING_RED_S = 15.0  # a unit still deactivating after this is a problem
START_TIMEOUT_S = 15.0  # systemd-run itself (Type=exec returns after the exec)
FCI_PORT = 1337  # libfranka's command port (preflight.sh:19), not 1000
FIFO_PRIORITY = 98
# The gripper: position in radians (0 open, 0.7929 closed), no effort limit
# (franka_control/tests/gripper_test.sh; the force is a launch argument).
GRIPPER_ACTION = "/robotiq/robotiq_gripper_controller/gripper_cmd"
GRIPPER_POSITIONS = {"open": 0.0, "close": 0.7929}
# The goal is cancelled only by SIGINT (ros2action/verb/send_goal.py:146-170
# installs the handler that calls cancel_goal_async; `timeout`'s default
# SIGTERM ends the program with the goal still running), so the test runs under
# `timeout -s INT -k <kill> <seconds>`: SIGINT at 20 s, SIGKILL 5 s later if the
# program ignored it. `timeout` itself exits 124 after a cancel and dies by
# SIGKILL after the kill (a shell sees 137, python -9); what the page is given is
# the status of `systemd-run --pipe --wait`, which (systemd's run.c as the review
# of 2026-10-21 read it; not measured until the rehearsal's timeout_check runs)
# reports a unit that ended by a signal as 255 (EXIT_EXCEPTION). So
# ``gripper.judge`` takes every status but 0 and 124, without a result, as
# "killed or not known".
GRIPPER_TIMEOUT_S = 20
GRIPPER_KILL_AFTER_S = 5
# `ros2 control list_controllers` never ends when the controller manager is
# missing (it waits for ever and warns every 10 s,
# controller_manager_services.py:140-150) and has no timeout option, so it runs
# under `timeout` too. The warning that proves "no manager" comes 10 s after
# the wait starts, and python takes a few seconds to start: 20 s leaves room.
LIST_TIMEOUT_S = 20
LIST_KILL_AFTER_S = 3
ONESHOT_STOP_S = (
    1  # seconds a one-shot's leftover processes get after its main one exits
)
# systemd's RuntimeMaxSec is only the backstop behind `timeout`.
PROBE_RUNTIME_MAX_S = LIST_TIMEOUT_S + LIST_KILL_AFTER_S + 12
# The fast check whether a gripper stack runs asks the ROS graph instead of the
# service: `ros2 node list --no-daemon --spin-time T` starts its own node (no
# daemon: a daemon the one-shot spawned would die with the unit and cost a
# start-up each time), waits T s for discovery and prints one node name per
# line (ros2node/verb/list.py main(): `print(*sorted_names, sep='\n')`;
# ros2cli/node/direct.py: the spin time). It ends by itself after about T + 2 s,
# so the `timeout` here is only a backstop. A gripper stack is the nodes of
# `robotiq_collect.launch.py`, all in the namespace `robotiq` (the control node
# is `/robotiq/controller_manager`: ros2_control_node.cpp:43, launch :107).
# "No such node" proves nothing by itself (a graph that has not been discovered
# yet is empty too), so the answer only counts when the health monitor's own
# node is in the list (``MONITOR_NODE``: fr3_health_monitor.py:285, the node of
# the unit the page starts first): the graph is then known to have been
# discovered. Without it the answer is inconclusive and the slow question
# (``list_controllers``) is asked.
NODES_SPIN_S = 2.0
NODES_TIMEOUT_S = 8
NODES_KILL_AFTER_S = 2
NODES_RUNTIME_MAX_S = NODES_TIMEOUT_S + NODES_KILL_AFTER_S + 6
GRIPPER_NAMESPACE = "/robotiq"
GRIPPER_NODE = "/robotiq/controller_manager"
MONITOR_NODE = "/fr3_health_monitor"
GRIPPER_CONTROLLER_MANAGER = "/robotiq/controller_manager"
GRIPPER_CONTROLLERS = (
    "joint_state_broadcaster_robotiq",
    "robotiq_gripper_controller",
    "robotiq_activation_controller",
)
EE_PROFILE = "franka_hand"
EE_OK = f"OK -- Desk is serving the expected '{EE_PROFILE}' profile."
QOS_OK = "reliability=ReliabilityPolicy.BEST_EFFORT,"  # preflight.sh's grep
QOS_FILE = ("robot_server", "fr3_robot_server", "franka_server_ros2.py")
EE_SCRIPT = ("tools", "check_fr3_ee_frame.py")
# One-shot units (probes, the gripper test) are named so that every part of
# LEVI that lists ``levi-aeri-*`` sees them while they run.
ONESHOT_PREFIX = UNIT_PREFIX + "oneshot-"

# systemd expands % and $ in a command; a control character is never wanted.
_ARG_UNSAFE = re.compile(r"[$%\x00-\x1f\x7f]")
# A long-lived unit is recognised again from ``systemctl show``, which prints
# its argv joined by spaces: no token of it may hold white space.
_UNIT_UNSAFE = re.compile(r"[$%\s\x00-\x1f\x7f]")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


# --- the lines that prove a state -----------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """A line of the unit's log. ``source`` names the file and line that print
    it; ``min_count`` lines are needed, and ``max_count`` (when set) is the most
    a healthy unit prints: more means a second process wrote it."""

    key: str
    text: str  # what the page says is expected (English)
    pattern: re.Pattern
    source: str
    min_count: int = 1
    max_count: int | None = None
    # A failure rule that still counts after the unit has been ready: a process
    # of a launch that died. The other failure rules are start-up rules.
    late: bool = False


def rule(key, text, pattern, source, min_count=1, max_count=None, late=False) -> Rule:
    return Rule(key, text, re.compile(pattern), source, min_count, max_count, late)


# A. ros2_control_node asks for FIFO 98 and mlockall; the spawners finish after
# switching their controllers; the health file then shows the hardware active.
A_READY = (
    rule(
        "fifo",
        f"The control node got realtime priority {FIFO_PRIORITY}",
        rf"Successful set up FIFO RT scheduling policy with priority {FIFO_PRIORITY}\b",
        "franka_ros2_ws/src/ros2_control/controller_manager/src/ros2_control_node.cpp:131",
        max_count=1,
    ),
    rule(
        "spawners",
        "Both controller spawners finished",
        r"\[spawner-\d+\]: process has finished cleanly",
        "launch/actions/execute_local.py:594-595 for the spawners of franka.launch.py:161 and :181",
        min_count=2,
    ),
)
A_FAIL = (
    rule(
        "duplicate_launch",
        "Another arm controller is already running",
        r"A Franka ros2_control_node is already running",
        "franka_ros2_ws/src/serl_franka_controllers_ros2/launch/impedance.launch.py:36",
    ),
    rule(
        "fifo_failed",
        "The control node could not get realtime priority",
        r"Could not enable FIFO RT scheduling policy",
        "franka_ros2_ws/src/ros2_control/controller_manager/src/ros2_control_node.cpp:124",
    ),
    rule(
        "memlock_failed",
        "The control node could not lock its memory",
        r"Unable to lock the memory",
        "franka_ros2_ws/src/ros2_control/controller_manager/src/ros2_control_node.cpp:70",
    ),
    rule(
        "hardware_failed",
        "The hardware interface could not be initialised",
        r"Failed to set the initial state of the component",
        "franka_ros2_ws/src/ros2_control/controller_manager/src/controller_manager.cpp:919 and :925",
    ),
    rule(
        "node_died",
        "A launched process died",
        r"\]: process has died",
        "launch/actions/execute_local.py:594-596 (a non-zero exit code)",
        late=True,
    ),
)
# B. The Robotiq driver activates (full open and close), then three controllers.
B_READY = (
    rule(
        "driver",
        "The gripper driver activated",
        r"Robotiq Gripper successfully activated!",
        "franka_control/franka_ros2_ws/src/ros2_robotiq_gripper/robotiq_driver/src/hardware_interface.cpp:252",
        max_count=1,
    ),
    *(
        rule(
            f"controller_{name}",
            f"Controller {name} is configured and active",
            rf"Configured and activated {re.escape(name)}\b",
            "franka_ros2_ws/src/ros2_control/controller_manager/controller_manager/spawner.py:571",
            max_count=1,
        )
        for name in GRIPPER_CONTROLLERS
    ),
)
B_FAIL = (
    rule(
        "hardware_failed",
        "The gripper driver could not be initialised",
        r"Failed to set the initial state of the component",
        "franka_ros2_ws/src/ros2_control/controller_manager/src/controller_manager.cpp:919 and :925",
    ),
    rule(
        "controller_failed",
        "A gripper controller could not be loaded",
        r"Failed loading controller|Failed to activate controller",
        "spawner.py:566 and the controller manager",
    ),
    rule(
        "node_died",
        "A launched process died",
        r"\]: process has died",
        "launch/actions/execute_local.py:594-596 (a non-zero exit code)",
        late=True,
    ),
)
# C. The server activates the impedance controller (or finds it active), says
# its reset mode, and Flask says where it listens. The port is the control
# file's, so that rule is built per specs (``c_ready``).
C_FAIL = (
    rule(
        "activation_deferred",
        "The server runs without the impedance controller (no fresh robot state)",
        r"impedance activation deferred",
        "robot_server/fr3_robot_server/franka_server_ros2.py:729",
    ),
    rule(
        "port_taken",
        "The server's port is taken",
        r"Address already in use|\[Errno 98\]",
        "werkzeug / socket",
    ),
    rule(
        "traceback",
        "The server raised an exception",
        r"^Traceback \(most recent call last\)",
        "python",
    ),
)


def c_ready(port) -> tuple:
    return (
        rule(
            "impedance",
            "The impedance controller is active",
            r"Impedance controller (?:started|already active)",
            "robot_server/fr3_robot_server/franka_server_ros2.py:204 and :197",
            max_count=1,
        ),
        rule(
            "reset_mode",
            "The reset mode is announced",
            r"/jointreset mode: cartesian",
            "robot_server/fr3_robot_server/franka_server_ros2.py:708",
            max_count=1,
        ),
        rule(
            "serving",
            f"The server listens on port {port}",
            rf"Running on http://127\.0\.0\.1:{int(port)}\b",
            "flask / werkzeug (webapp.run at franka_server_ros2.py:945)",
            max_count=1,
        ),
    )


@dataclass(frozen=True)
class UnitSpec:
    """One long-lived unit. ``argv`` is the whole ExecStart (the wrapper and
    the rig's environment file included for the real thing)."""

    key: str  # a, b or c
    unit: str  # without ".service"
    title: str
    argv: tuple
    cwd: str | None
    ready: tuple
    fail: tuple
    health_flag: str | None  # a health file flag that must be true when ready
    listen_port: int | None  # a LISTEN row that must exist when ready
    limits: tuple  # LimitRTPRIO=, LimitMEMLOCK= properties
    ready_timeout_s: float = 90.0
    stop_timeout_s: int = STOP_TIMEOUT_S

    @property
    def service(self) -> str:
        return self.unit + ".service"


@dataclass(frozen=True)
class Specs:
    """Everything the page may run, for one profile: ``real`` (the rig's own
    commands) or ``fake`` (stand-in scripts, only ever built by the tests and
    the rehearsal). There is no way to ask production code for the fake set."""

    profile: str
    units: dict
    wrapper: str
    env_file: str
    robot_ip: str
    server_port: int
    marker_wait_s: float
    franka_control: str
    com_port: str
    qos_file: str | None
    # Commands of the one-shot checks (full ExecStart each).
    gripper_cmd: object = None  # (position) -> argv
    controllers_cmd: tuple = ()
    ee_cmd: tuple = ()
    nodes_cmd: tuple = ()  # the fast check of the ROS graph (``parse_nodes``)
    extra: dict = field(default_factory=dict)


def check_argv(argv, long_lived=True) -> tuple:
    """``argv`` as a tuple, or ValueError when a token holds a character
    systemd would expand (or, for a long-lived unit, split on)."""
    bad = _UNIT_UNSAFE if long_lived else _ARG_UNSAFE
    tokens = tuple(str(a) for a in argv)
    if not tokens or any(bad.search(t) for t in tokens):
        raise ValueError("a token of the command has an unsafe character")
    return tokens


def wrapper_sha256(path=WRAPPER) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _behind(wrapper, env_file, command) -> tuple:
    return ("/bin/bash", str(wrapper), str(env_file), "--", *command)


def _bounded(seconds, kill_after, command) -> tuple:
    """``command`` under ``timeout -s INT -k <kill_after> <seconds>``: SIGINT
    when the time is up (what Ctrl+C does; a SIGTERM would not cancel a
    ``ros2 action send_goal``), SIGKILL ``kill_after`` s later if it is
    ignored. ``timeout`` exits 124 when SIGINT ended it and dies by SIGKILL after
    the kill (137 to a shell, -9 to python, 255 through ``systemd-run --wait``)."""
    return (
        "timeout",
        "-s",
        "INT",
        "-k",
        str(int(kill_after)),
        str(int(seconds)),
        *command,
    )


def _pose_text(pose) -> str:
    return ",".join(f"{value:.4f}" for value in pose)


def real_specs(control, wrapper=WRAPPER) -> Specs:
    """The rig's own commands for a valid ``control_site.StackControl`` with
    the stack switched on. Raises ValueError when a machine value is missing
    or a token is unsafe."""
    needed = (control.franka_control, control.robot_ip, control.com_port)
    if not all(needed) or len(control.reset_joint_target) != 7:
        raise ValueError("the control file lacks the machine values")
    root = Path(control.franka_control)
    env_file = root.joinpath(*ENV_FILE)
    port = control.server_port

    def unit(key):
        return stack.STACK_UNITS[key.upper()].removesuffix(".service")

    a_cmd = (
        "ros2",
        "launch",
        "--noninteractive",
        "serl_franka_controllers_ros2",
        "impedance.launch.py",
        "robot_type:=fr3",
        f"robot_ip:={control.robot_ip}",
        "load_gripper:=false",
    )
    b_cmd = (
        "ros2",
        "launch",
        "--noninteractive",
        "franka_gripper_manager",
        "robotiq_collect.launch.py",
        f"com_port:={control.com_port}",
    )
    c_cmd = (
        "python3",
        "-m",
        "fr3_robot_server.franka_server_ros2",
        f"--robot_ip={control.robot_ip}",
        "--robot_type=fr3",
        "--gripper_type=Robotiq",
        "--robotiq_namespace=robotiq",
        "--flask_url=127.0.0.1",
        f"--flask_port={port}",
        f"--reset_joint_target={_pose_text(control.reset_joint_target)}",
        "--reset_mode=cartesian",
    )
    units = {
        "a": UnitSpec(
            "a",
            unit("a"),
            "FR3 arm ROS 2 stack (terminal A)",
            check_argv(_behind(wrapper, env_file, a_cmd)),
            None,
            A_READY,
            A_FAIL,
            "hardware_active",
            None,
            ("LimitRTPRIO=99", "LimitMEMLOCK=infinity"),
        ),
        "b": UnitSpec(
            "b",
            unit("b"),
            "FR3 Robotiq gripper stack (terminal B)",
            check_argv(_behind(wrapper, env_file, b_cmd)),
            None,
            B_READY,
            B_FAIL,
            None,
            None,
            ("LimitRTPRIO=99",),
            ready_timeout_s=60.0,
        ),
        "c": UnitSpec(
            "c",
            unit("c"),
            "FR3 robot server (terminal C)",
            check_argv(_behind(wrapper, env_file, c_cmd)),
            str(root / "robot_server"),
            c_ready(port),
            C_FAIL,
            "controller_active",
            port,
            (),
            ready_timeout_s=60.0,
        ),
    }
    ee = (
        "python3",
        str(root.joinpath(*EE_SCRIPT)),
        "--expect",
        EE_PROFILE,
        "--gripper-state-topic",
        "/robotiq/joint_states",
    )
    controllers = _bounded(
        LIST_TIMEOUT_S,
        LIST_KILL_AFTER_S,
        (
            "ros2",
            "control",
            "list_controllers",
            "-c",
            GRIPPER_CONTROLLER_MANAGER,
        ),
    )

    nodes = _bounded(
        NODES_TIMEOUT_S,
        NODES_KILL_AFTER_S,
        ("ros2", "node", "list", "--no-daemon", "--spin-time", str(NODES_SPIN_S)),
    )

    def gripper(position):
        goal = f"{{command: {{position: {float(position)!r}, max_effort: 0.0}}}}"
        return check_argv(
            _behind(
                wrapper,
                env_file,
                _bounded(
                    GRIPPER_TIMEOUT_S,
                    GRIPPER_KILL_AFTER_S,
                    (
                        "ros2",
                        "action",
                        "send_goal",
                        GRIPPER_ACTION,
                        "control_msgs/action/GripperCommand",
                        goal,
                    ),
                ),
            ),
            long_lived=False,
        )

    return Specs(
        profile="real",
        units=units,
        wrapper=str(wrapper),
        env_file=str(env_file),
        robot_ip=control.robot_ip,
        server_port=port,
        marker_wait_s=control.marker_wait_s,
        franka_control=str(root),
        com_port=control.com_port,
        qos_file=str(root.joinpath(*QOS_FILE)),
        gripper_cmd=gripper,
        controllers_cmd=check_argv(
            _behind(wrapper, env_file, controllers), long_lived=False
        ),
        ee_cmd=check_argv(_behind(wrapper, env_file, ee), long_lived=False),
        nodes_cmd=check_argv(_behind(wrapper, env_file, nodes), long_lived=False),
    )


# --- the unit ---------------------------------------------------------------------------


def unit_properties(spec) -> list:
    """The ``--property=`` arguments of a long-lived unit (the ones the
    rehearsal and the tests read back)."""
    return [
        "--property=Type=exec",
        "--property=KillMode=control-group",
        "--property=KillSignal=SIGINT",
        "--property=SendSIGKILL=no",
        f"--property=TimeoutStopSec={spec.stop_timeout_s}",
        "--property=Restart=no",
        "--property=StandardInput=null",
        "--property=ManagedOOMPreference=avoid",
        *(f"--property={limit}" for limit in spec.limits),
    ]


def systemd_run_argv(spec, log_path, unset=()) -> list:
    """The ``systemd-run --user`` command that starts ``spec``. No ``--collect``:
    a unit that failed stays listed (as ``failed``) until the page resets it,
    so a crash is never mistaken for a unit that was never started. Nice,
    CPU affinity and quotas are not set (the rig's terminals set none, and the
    network card's interrupts share the control loop's cores)."""
    run = [
        "systemd-run",
        "--user",
        "--quiet",
        f"--unit={spec.unit}",
        f"--description={spec.title}, managed by the marvel0 page",
        *unit_properties(spec),
    ]
    if spec.cwd:
        run.append(f"--working-directory={spec.cwd}")
    run += [
        f"--property=StandardOutput=append:{log_path}",
        f"--property=StandardError=append:{log_path}",
    ]
    if unset:
        run.append("--property=UnsetEnvironment=" + " ".join(unset))
    run += ["--setenv=PYTHONUNBUFFERED=1", "--setenv=RCUTILS_COLORIZED_OUTPUT=0"]
    run.append("--")
    run += list(spec.argv)
    return run


def unset_names(manager_env_text) -> list:
    """The names the user manager's environment holds that a stack unit must
    not inherit (ROS, conda, a venv): the wrapper sources the rig's file on
    top of a clean slate. The same list the policy server's unit uses."""
    env = deploy._manager_env(manager_env_text)
    return sorted(name for name in env if deploy.UNSET_NAMES.fullmatch(name))


def oneshot_argv(unit, argv, runtime_max_s, description) -> list:
    """``systemd-run`` for a short check whose output the page reads back
    (``--pipe --wait``): standard input is not a terminal, the unit is
    collected, and ``RuntimeMaxSec`` ends it if the command hangs."""
    return [
        "systemd-run",
        "--user",
        "--quiet",
        "--pipe",
        "--wait",
        "--collect",
        f"--unit={unit}",
        f"--description={description}, managed by the marvel0 page",
        f"--property=RuntimeMaxSec={int(runtime_max_s)}",
        "--property=KillSignal=SIGINT",
        # What the command leaves behind in the unit's control group is ended
        # ONE second after its main process exits. The ros2 CLI starts the ROS 2
        # daemon (a detached child) when none runs; the unit then waited for it
        # (state "deactivating / stop-sigterm", the default stop timeout is 90 s),
        # `systemd-run --wait --pipe` did not return, the page's 45 s timeout hit
        # and the output of a check that had succeeded was lost (first real run
        # of Start whole stack, 21 Oct 22:26: C's controller check "refused" twice).
        f"--property=TimeoutStopSec={ONESHOT_STOP_S}",
        "--setenv=PYTHONUNBUFFERED=1",
        "--setenv=RCUTILS_COLORIZED_OUTPUT=0",
        "--",
        *argv,
    ]


def same_unit(shown, record) -> bool:
    """Is ``shown`` (``stack.show_unit``'s answer) the unit that ``record``
    started: the same ``Id`` and exactly the same command?"""
    if not shown or not record:
        return False
    unit, wanted = record.get("unit"), record.get("exec_argv")
    if not unit or not wanted:
        return False
    return shown.get("Id") == f"{unit}.service" and (
        stack.exec_argv(shown.get("ExecStart", "")) == list(wanted)
    )


# --- reading the log ---------------------------------------------------------------------


def log_lines(path) -> list:
    """The printable lines of a unit's log, escape codes removed. A log that
    fits in head and tail is read whole (so no line is counted twice)."""
    try:
        size = Path(path).lstat().st_size
    except OSError:
        return []
    whole = deploy.HEAD_BYTES + deploy.TAIL_BYTES
    if size <= whole:
        return deploy.read_head(path, whole)
    return deploy.read_head(path) + deploy.read_tail(path)


def strip_ansi(text) -> str:
    return _ANSI.sub("", text)


def read_lines(spec, lines, health, listening, latched=False) -> dict:
    """What a unit's log (``lines``), the health file and the socket table say
    about ``spec``: ``seen`` and ``missing`` (each ``{"key", "kind", "text"}``,
    in the order of the rules), ``failure`` (the first matching failure rule's
    key, or None), ``duplicate`` (the key of a ready line printed more often
    than a healthy unit prints it) and ``ready``.

    ``health`` is ``stack.read_health``'s answer (or None) and ``listening``
    whether a LISTEN row exists on the unit's port (or None when the table
    cannot be read).

    ``latched``: the unit has been ready before. The log's ready lines and
    failure rules are start-up rules (the server prints "Impedance controller
    started" again on every /startimp and /jointreset, Flask logs a Traceback
    for any request that answers 500): they are not applied again. What can
    still be missing is the live evidence (health file, socket table), and
    what can still fail is a ``late`` rule (a process of a launch died: B has no
    other evidence), read from ``lines`` (``late_rules`` says whether the log is
    worth reading)."""

    def count(item):
        return sum(1 for line in lines if item.pattern.search(line))

    seen, missing, duplicate = [], [], None
    for item in spec.ready:
        row = {"key": item.key, "kind": "log", "text": item.text}
        if latched:
            seen.append(row)
            continue
        n = count(item)
        (seen if n >= item.min_count else missing).append(row)
        if item.max_count is not None and n > item.max_count and duplicate is None:
            duplicate = item.key
    if spec.health_flag:
        fresh = health is not None and health.get("state") in ("ok", "red")
        flag = health.get(spec.health_flag) if fresh else None
        row = {
            "key": f"health_{spec.health_flag}",
            "kind": "health",
            "text": f"The health file shows {spec.health_flag.replace('_', ' ')}",
        }
        (seen if flag is True else missing).append(row)
    if spec.listen_port:
        row = {
            "key": "listening",
            "kind": "socket",
            "text": f"Something listens on port {spec.listen_port}",
        }
        (seen if listening is True else missing).append(row)
    failure = next(
        (
            item.key
            for item in spec.fail
            if (item.late or not latched)
            and any(item.pattern.search(line) for line in lines)
        ),
        None,
    )
    return {
        "seen": seen,
        "missing": missing,
        "failure": failure,
        "duplicate": duplicate,
        "ready": not missing and failure is None and duplicate is None,
    }


# A unit's log is appended by systemd for as long as it runs; werkzeug writes
# one line per HTTP request of C's clients, which is about 10 MB an hour with a
# polling client. Past ``LOG_CAP_BYTES`` the file is cut in place to its last
# ``LOG_KEEP_BYTES`` (the writer opened it O_APPEND, so it carries on at the
# end). A few lines written while it is cut are lost: this is a log to look at,
# not a record. Only a unit that has been ready is cut: its start-up lines are
# no longer read.
LOG_CAP_BYTES = 8 * 1024 * 1024
LOG_KEEP_BYTES = 512 * 1024


def trim_log(path, cap=LOG_CAP_BYTES, keep=LOG_KEEP_BYTES) -> bool:
    """Cut the regular file ``path`` to its last ``keep`` bytes (from a line
    start) when it is larger than ``cap``. True when it was cut. A link, a
    missing file or any error cuts nothing."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size <= cap:
            return False
        start = max(0, info.st_size - keep)
        data = os.pread(fd, info.st_size - start, start)
        newline = data.find(b"\n")
        if start and newline >= 0:
            data = data[newline + 1 :]
        os.ftruncate(fd, 0)
        os.write(fd, data)  # (O_APPEND: after anything written meanwhile)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def late_rules(spec) -> tuple:
    """The failure rules of ``spec`` that still count once it has been ready."""
    return tuple(item for item in spec.fail if item.late)


def failure_text(spec, key) -> str | None:
    for item in (*spec.fail, *spec.ready):
        if item.key == key:
            return item.text
    return None


# --- the one-shot checks' outputs --------------------------------------------------------

# One row of ``ros2 control list_controllers``: name, type, state, separated by
# white space, no brackets. The CLI prints
#   f"{color}{name:<w}{end} {type:<w}  {color}{state:<w}{end}"
# (franka_ros2_ws/src/ros2_control/ros2controlcli/ros2controlcli/verb/
# list_controllers.py:34-36; the colour codes are empty unless
# RCUTILS_COLORIZED_OUTPUT=1, controller_manager_services.py:46-64, and are
# stripped here anyway). A row starts at column 0: the indented lines of
# ``--verbose`` and log lines are not rows, and the state is one of the
# lifecycle states.
_CONTROLLER_ROW = re.compile(
    r"^(\S+) +(\S+) +(active|inactive|unconfigured|finalized)\s*$"
)
# The manager answered with an empty list (list_controllers.py:122).
_NONE_LOADED = re.compile(r"^No controllers are currently loaded!\s*$", re.MULTILINE)
# The command never ends when the controller manager is missing: it logs
# "waiting for service ..." and, every 10 s, this warning
# (controller_manager_services.py:140-150; list_controllers() waits for ever,
# service_timeout=0.0). The warning is proof that the service did not appear
# for 10 s; "waiting for service" alone is not (it is logged first, whatever
# comes next).
_NO_SERVICE = re.compile(r"Could not contact service \S*/list_controllers\b")


def parse_controllers(text) -> dict:
    """``{"state": listed|absent|unknown, "controllers": {name: state}}`` of
    ``ros2 control list_controllers``. ``listed``: the manager answered (an
    empty list included: it exists, nothing is loaded). ``absent``: the command
    logged that it could not contact the service, however it ended (it has to
    be ended by ``timeout``: it never ends by itself). Anything else, a command
    ended before it could say, is ``unknown``, never absent."""
    plain = strip_ansi(text)
    rows = {}
    for line in plain.splitlines():
        match = _CONTROLLER_ROW.match(line.rstrip())
        if match:
            rows[match.group(1)] = match.group(3)
    if rows:
        return {"state": "listed", "controllers": rows}
    if _NONE_LOADED.search(plain):
        return {"state": "listed", "controllers": {}}
    if _NO_SERVICE.search(plain):
        return {"state": "absent", "controllers": {}}
    return {"state": "unknown", "controllers": {}}


# One line of `ros2 node list`: a fully qualified node name. Everything else
# the command or the library prints (a log line, the duplicate-name warning on
# standard error) is not a node.
_NODE_NAME = re.compile(r"^/[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+)*$")


def parse_nodes(text, code=0) -> dict:
    """``{"state": present|absent|unknown, "nodes", "gripper", "canary"}`` of
    ``ros2 node list`` (``code``: how the command ended).

    ``present``: a node of the gripper stack's namespace is listed (the control
    node, or any other: a half-started stack is somebody's gripper stack too).
    ``absent``: nothing of it is listed, the command ended well (0), **and** the
    health monitor's node is listed, which shows that discovery had run. Anything
    else is ``unknown``, never absent: an empty list (nothing discovered), a
    list without the monitor, a command that ended by ``timeout`` or failed."""
    plain = strip_ansi(text)
    nodes = sorted(
        {line.strip() for line in plain.splitlines() if _NODE_NAME.match(line.strip())}
    )
    gripper = [
        n
        for n in nodes
        if n == GRIPPER_NAMESPACE or n.startswith(GRIPPER_NAMESPACE + "/")
    ]
    canary = MONITOR_NODE in nodes
    if gripper:
        state = "present"
    elif code == 0 and canary:
        state = "absent"
    else:
        state = "unknown"
    return {"state": state, "nodes": nodes, "gripper": gripper, "canary": canary}


def gripper_controllers_ok(parsed) -> bool:
    rows = parsed.get("controllers") or {}
    return parsed.get("state") == "listed" and all(
        rows.get(name) == "active" for name in GRIPPER_CONTROLLERS
    )


def ee_frame_ok(text) -> bool:
    return EE_OK in strip_ansi(text)


def qos_ok(source_text) -> bool:
    """Does the robot server still subscribe best-effort (``preflight.sh``'s
    own grep)? A reliable subscription starves the control loop."""
    return QOS_OK in source_text
