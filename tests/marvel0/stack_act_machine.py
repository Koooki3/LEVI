"""L1 of the stack actions: a fake machine for ``levi/marvel0/stack_act.py``.

It extends ``test_marvel0_stack.Machine`` (the S4-0 double: temporary folders,
a fake ``systemctl``, the socket tables and ``arm_guard`` as values) with what
starting A, B and C needs: a control file, the real ``Specs`` (the real argv,
which no test ever runs), units that ``systemd-run`` registers and
``systemctl stop`` removes, their logs, the one-shot checks' outputs and a
clock moved by hand. Nothing is started, signalled or connected to.

``ready_lines`` are the lines a healthy unit prints, in the form the real
programs print them (escape codes of the controller spawner included), so the
tests read the same text the page will."""

import dataclasses
import fnmatch
import json
import os
import re
from pathlib import Path

from test_marvel0_stack import NOW, Machine, _sock

from levi.marvel0 import control_site, net, stack, stack_act, stack_units

POSE = [-0.8063, -0.0623, 0.9130, -2.4036, 0.0580, 2.3011, 1.5844]
SERVER_PORT = 5000
OURS = {
    "a": stack.STACK_UNITS["A"],
    "b": stack.STACK_UNITS["B"],
    "c": stack.STACK_UNITS["C"],
}
KEY_OF = {v.removesuffix(".service"): k for k, v in OURS.items()}
BOLD, GREEN, END = "\x1b[1m", "\x1b[92m", "\x1b[0m"


def ready_lines(key, port=SERVER_PORT) -> list:
    """What a healthy unit of ``key`` prints once it is ready."""
    if key == "a":
        return [
            "[INFO] [launch]: All log files can be found below /home/x/.ros/log/1",
            (
                "[ros2_control_node-2] [INFO] [1760000001.100] [controller_manager]: "
                "Successful set up FIFO RT scheduling policy with priority 98."
            ),
            "[INFO] [spawner-4]: process has finished cleanly [pid 4004]",
            "[INFO] [spawner-5]: process has finished cleanly [pid 4005]",
        ]
    if key == "b":
        names = stack_units.GRIPPER_CONTROLLERS
        return [
            (
                "[ros2_control_node-1] [INFO] [1760000001.200] "
                "[robotiq_controller_manager]: Robotiq Gripper successfully activated!"
            ),
            *(
                f"[spawner-{i}] [INFO] [1760000002.0] [spawner_{n}]: "
                f"{GREEN}Configured and activated {BOLD}{n}{END}"
                for i, n in enumerate(names, 3)
            ),
        ]
    return [
        "[INFO] [1760000003.0] [franka_server]: /jointreset mode: cartesian",
        "[INFO] [1760000007.0] [franka_server]: Impedance controller started",
        f" * Running on http://127.0.0.1:{port}",
    ]


# What `ros2 action send_goal` prints (ros2action/verb/send_goal.py:135-212).
# `message_to_yaml` is yaml.dump with sorted keys (rosidl_runtime_py/convert.py:
# 92-96), and send_goal indents only the first line of it ('Result:\n    {}'),
# so the keys after the first are at column 0.
GRIPPER_OUT = """\
Waiting for an action server to become available...
Sending goal:
     command:
  max_effort: 0.0
  position: {position}

Goal accepted with ID: 0123456789abcdef

Result:
    effort: 0.0
position: {position}
reached_goal: {reached}
stalled: {stalled}

Goal finished with status: SUCCEEDED
"""
# What ``ros2 control list_controllers -c /robotiq/controller_manager`` prints.
# The row is the f-string of franka_ros2_ws/src/ros2_control/ros2controlcli/
# ros2controlcli/verb/list_controllers.py:34-36 (name and type padded to the widest
# of their column, two spaces before the state, no brackets; the widths are
# taken at :126-128); the colour codes are empty unless RCUTILS_COLORIZED_OUTPUT=1
# (controller_manager_services.py:46-54).
# The types are the ones robotiq_controllers.yaml (franka_gripper_manager/config)
# configures. Never write a row by hand: build it here.
GRIPPER_TYPES = {
    "joint_state_broadcaster_robotiq": "joint_state_broadcaster/JointStateBroadcaster",
    "robotiq_gripper_controller": "position_controllers/GripperActionController",
    "robotiq_activation_controller": "robotiq_controllers/RobotiqActivationController",
}


def list_controllers_print(rows, color=False) -> str:
    """The output of the command for ``rows`` ``[(name, type, state)]``, built
    with the CLI's own format string (see above)."""
    if not rows:
        return "No controllers are currently loaded!\n"  # list_controllers.py:122
    tints = {"active": "\x1b[92m", "inactive": "\x1b[96m", "unconfigured": "\x1b[93m"}
    end = "\x1b[0m" if color else ""
    wn = max(len(n) for n, _, _ in rows)
    wt = max(len(t) for _, t, _ in rows)
    ws = max(len(s) for _, _, s in rows)
    out = ""
    for name, kind, state in rows:
        tint = tints.get(state, "") if color else ""
        out += f"{tint}{name:<{wn}}{end} {kind:<{wt}}  {tint}{state:<{ws}}{end}\n"
    return out


CONTROLLERS_OUT = list_controllers_print(
    [(n, t, "active") for n, t in GRIPPER_TYPES.items()]
)
# With no controller manager the command never ends by itself: rclpy logs this
# line when the wait starts and again every 10 s, and "Could not contact
# service" after the first 10 s (controller_manager_services.py:140-150, with
# service_timeout 0.0 from list_controllers(); the command has no timeout
# option, ros2controlcli/api/__init__.py:71-89). What a caller gets is what
# was printed before ``timeout`` ended the command (exit status 124).
WAITING_LINE = (
    "[INFO] [1760000100.000000000] [_ros2cli_4242]: waiting for service "
    "/robotiq/controller_manager/list_controllers to become available...\n"
)
NO_MANAGER = (
    WAITING_LINE
    + "[WARN] [1760000110.000000000] [_ros2cli_4242]: Could not contact service "
    "/robotiq/controller_manager/list_controllers\n" + WAITING_LINE
)
NO_MANAGER_CODE = 124  # (``timeout`` ended the command)
# What ``ros2 node list`` prints: the fully qualified names, sorted, one per
# line (ros2node/verb/list.py main(): `print(*sorted_names, sep='\n')`). The
# gripper stack of robotiq_collect.launch.py is these nodes, all in the
# namespace `robotiq` (the control node is `/robotiq/controller_manager`, the
# controllers it loads are lifecycle nodes named after them); the health
# monitor's own node is `/fr3_health_monitor` (fr3_health_monitor.py:285). The
# duplicate-name warning goes to standard error (list.py), which a
# `systemd-run --pipe` unit returns with the rest.
MONITOR_NODE = "/fr3_health_monitor"
GRIPPER_NODES = (
    "/robotiq/controller_manager",
    "/robotiq/joint_state_broadcaster_robotiq",
    "/robotiq/robot_state_publisher",
    "/robotiq/robotiq_activation_controller",
    "/robotiq/robotiq_gripper_controller",
)


def scope_print(name, load, active, tasks) -> str:
    """What `systemctl --user show --property=Id,LoadState,ActiveState,TasksCurrent
    <scope>` prints for one scope, in the order systemd prints it (captured on the
    machine, 21 Oct: a loaded scope that holds no process says `TasksCurrent=0`, a
    scope that is not there says `TasksCurrent=[not set]` and
    `LoadState=not-found`; with several units there is one block each, a blank
    line between them)."""
    return f"TasksCurrent={tasks}\nId={name}\nLoadState={load}\nActiveState={active}\n"


def scopes_print(scopes, shown) -> str:
    """The output of one `systemctl --user show` of ``scopes``, from ``shown``
    (name -> (load, active, tasks); a scope not in it is not there). A glob in
    ``scopes`` is expanded by systemd itself (21 Oct, systemd 255): every loaded
    unit that matches gets a block, and a glob that matches nothing prints
    nothing and exits 0."""
    names = []
    for name in scopes:
        if "*" in name:
            names += [n for n in shown if fnmatch.fnmatchcase(n, name)]
        else:
            names.append(name)
    return "\n".join(
        scope_print(name, *shown.get(name, ("not-found", "inactive", "[not set]")))
        for name in names
    )


DUPLICATE_WARNING = (
    "WARNING: Be aware that there are nodes in the graph that share an exact "
    "name, which can have unintended side effects.\n"
)


def node_list_print(names) -> str:
    """The output of `ros2 node list` for ``names`` (built the way list.py
    prints them; nothing printed for an empty graph)."""
    names = sorted(names)
    return "\n".join(names) + "\n" if names else ""


# The kernel keeps the side that closed a TCP connection in TIME_WAIT for this
# long (net.ipv4.tcp_fin_timeout does not apply to it): the FCI check's own
# socket to the arm, which ``arm_guard`` counts as a control connection.
TIME_WAIT_S = 60.0
EE_OUT = f"  flange -> EE      0.10340 m\n\n  {stack_units.EE_OK}\n"


class ActMachine(Machine):
    # Off unless a test asks: with it, the FCI check leaves its own socket in
    # TIME_WAIT for TIME_WAIT_S of the machine's clock (the arm's sockets then
    # read "active" with no A anywhere), as the kernel does.
    fci_time_wait = False
    time_wait_until = 0.0

    def __init__(self, tmp_path, *, product=True, enabled=True):
        super().__init__(tmp_path, product=product)
        self.t = NOW
        self.units = {}  # "a"/"b"/"c" -> what systemd shows
        self.oneshots = []
        self.serial = tmp_path / "serial-gripper"
        self.serial.write_text("")
        self.arm_lock = tmp_path / "arm.lock"
        self.arm_lock_pids = []
        self.state_root = tmp_path / "state" / "marvel0" / "stack"
        self.fci = {"state": "open"}
        self.fci_probes = []
        self.gripper_reached, self.gripper_stalled, self.gripper_code = True, False, 0
        self.gripper_out = None  # a fixed output instead of the scripted one
        self.controllers_out, self.controllers_code = NO_MANAGER, NO_MANAGER_CODE
        # The ROS graph as `ros2 node list` sees it. None: derived from
        # ``controllers_out`` (a manager that answers is the gripper stack's
        # nodes, one that does not is a graph with only the monitor's node, a
        # command that says nothing is a graph that was not discovered), so a
        # test that says what the controller manager answers says it for both.
        self.nodes_out, self.nodes_code = None, 0
        self.ee_out, self.ee_code = EE_OUT, 0
        # What heavy_jobs_running says (the decision itself is tested with the
        # real function on fixture logs; here the sequence's use of it is).
        self.heavy = {
            "state": "clear",
            "count": 0,
            "kinds": [],
            "empty": 0,
            "why": None,
        }
        self.heavy_asked = 0
        # The heavy-gate scopes systemd shows: name -> (LoadState, ActiveState,
        # TasksCurrent as printed). A scope that is not listed is not there.
        self.scopes = {}
        self.scope_failing = False
        self.stop_codes = {}
        # Units whose `systemctl stop` fails and leaves them as they were (a
        # stop that failed): "a"/"b"/"c". The guided stop's tests use it.
        self.stop_fails = set()
        # Units whose `systemctl stop` answers yes and leaves them there (a
        # unit another program starts again, or systemd that is slow to let go).
        self.stop_keeps = set()
        self.m_health_on_start = True
        self.qos_text = f"x\n{stack_units.QOS_OK}\ny\n"
        self.slept = []
        self.run_active = False
        self.run_no = 1  # a test that starts the stack twice counts it up (request ids)
        self.workspace_main = True  # the product's workspace is the main checkout's
        self.cgroup_pids = {}  # control group -> pids (None: unreadable)
        self.kills = []  # (unit, signal) of every `systemctl kill`
        self.show_failing = (
            set()
        )  # a `systemctl show` naming one of these properties fails
        # One-shot units (`systemd-run --pipe --wait`) that exist while their
        # command runs: name -> {"stopped": bool, "shows_left": int}. After a
        # `systemctl stop` of one, `oneshot_linger` more shows still find it
        # (a unit that takes its time to go).
        self.oneshot_units = {}
        self.oneshot_linger = 0
        self.on_systemd_run = None  # called with the argv just before a unit starts
        self.on_oneshot = (
            None  # called with the command of a one-shot just before it "runs"
        )
        self.set_control(enabled)
        self.sup = stack.Stack(self.host())
        self.act = stack_act.Act(
            self.sup, self.act_host(), setup=lambda: (self.loaded, self.specs)
        )
        self.sup.act = self.act
        stack_act.register_run_probe(lambda: self.run_active)

    # -- the control file and the specs
    def set_control(self, enabled=True, **override):
        values = {
            "enabled": enabled,
            "franka_control": str(self.franka),
            "robot_ip": "10.42.0.157",
            "com_port": str(self.serial),
            "server_port": SERVER_PORT,
            "reset_joint_target": POSE,
            **override,
        }
        # (built directly: the file's schema wants a /dev/ serial device)
        values["reset_joint_target"] = tuple(values["reset_joint_target"])
        site = control_site.ControlSite(
            control_site.StackControl(**values), control_site.ClientControl()
        )
        self.loaded = control_site.ControlLoad("ok", site)
        self.specs = stack_units.real_specs(site.stack) if enabled else None
        qos = self.franka / "robot_server" / "fr3_robot_server"
        qos.mkdir(parents=True, exist_ok=True)
        (qos / "franka_server_ros2.py").write_text(self.qos_text)

    def act_host(self):
        return stack_act.ActHost(
            base=self.host(),
            state_root=lambda: self.state_root,
            fci_probe=self._probe,
            arm_lock=str(self.arm_lock),
            path_exists=lambda p: Path(p).exists(),
            workspace_ok=lambda: self.workspace_main,
            cgroup_procs=lambda group: self.cgroup_pids.get(group),
            background=False,
            parallel=False,
            heavy_jobs=self._heavy_jobs,
            sleep=self.slept.append,
        )

    def _heavy_jobs(self, log, run, now):
        self.heavy_asked += 1
        return dict(self.heavy)

    def _probe(self, ip, port):
        self.fci_probes.append((ip, port))
        if self.fci_time_wait and self.fci.get("state") == "open":
            self.time_wait_until = self.t + TIME_WAIT_S
        return dict(self.fci)

    def _arm_sockets(self):
        shown = dict(self.sockets)
        if shown["state"] == "inactive" and self.t < self.time_wait_until:
            return {"state": "active", "detail": "1 socket in TIME_WAIT"}
        return shown

    def host(self):
        built = super().host()
        return dataclasses.replace(
            built, now=lambda: self.t, arm_sockets=self._arm_sockets
        )

    # -- the health file (relative to the clock)
    def health(
        self,
        age=0.1,
        pid=4242,
        hardware=True,
        controller=True,
        red=False,
        mode="MOVE",
    ):
        self.health_path.write_text(
            json.dumps(
                {
                    "updated_at_epoch": self.t - age,
                    "monitor_pid": pid,
                    "hardware_active": hardware,
                    "controller_active": controller,
                    "robot_mode_name": mode,
                    "red_light": red,
                }
            )
        )

    def no_manager(self):
        """The health file while no controller manager answers (no A anywhere):
        what the real monitor writes (fr3_health_monitor.py:115-120 gives
        ``hardware_active`` None when no Franka component was listed or the
        service did not answer, and :349-359 clears the answers it no longer
        trusts; ``controller_active`` follows the same way, the mode is unknown
        and the light is red for the unreachable manager). ``False`` is a
        manager that answers with the hardware not active: A starting."""
        self.health(hardware=None, controller=None, red=True, mode=None)

    # -- the fake systemd
    def run(self, argv, timeout_s):
        if self.systemd_down and argv[0] in ("systemctl", "systemd-run"):
            self.calls.append(list(argv))
            return 1, "Failed to connect to bus"
        if argv[0] == "systemd-run":
            return self._systemd_run(argv)
        if argv[:3] == ["systemctl", "--user", "show-environment"]:
            self.calls.append(list(argv))
            return 0, "PATH=/usr/bin\nHOME=/home/x\nROS_DISTRO=jazzy\nCONDA_SHLVL=1\n"
        if (
            argv[:3] == ["systemctl", "--user", "show"]
            and len(argv) >= 5
            and argv[3].startswith("--property=Id,LoadState,ActiveState,TasksCurrent")
        ):
            self.calls.append(list(argv))
            if self.scope_failing:
                return 1, "Failed to connect to bus"
            return 0, scopes_print(argv[4:], self.scopes)
        if (
            argv[:3] == ["systemctl", "--user", "show"]
            and len(argv) == 5
            and argv[3].removesuffix(".service") in KEY_OF
        ):
            self.calls.append(list(argv))
            if any(prop in argv[4] for prop in self.show_failing):
                return 1, "Failed to get properties: Connection timed out"
            return 0, self._show(argv[3])
        if (
            len(argv) >= 4
            and argv[:2] == ["systemctl", "--user"]
            and argv[2] in ("show", "stop")
            and argv[3].startswith(stack_units.ONESHOT_PREFIX)
        ):
            self.calls.append(list(argv))
            return self._oneshot_verb(argv)
        if (
            len(argv) >= 4
            and argv[:2] == ["systemctl", "--user"]
            and argv[2] in ("stop", "reset-failed")
            and argv[3].removesuffix(".service") in KEY_OF
        ):
            return self._verb(argv)
        if argv[:3] == ["systemctl", "--user", "kill"]:
            self.calls.append(list(argv))
            unit = argv[-1].removesuffix(".service")
            self.kills.append((unit, argv[3]))
            key = KEY_OF.get(unit)
            if key in self.units and self.units[key].get("ends_on_kill"):
                self.units[key]["tasks"] = 0  # the processes ended on the second SIGINT
            return 0, ""
        return super().run(argv, timeout_s)

    def _show(self, unit) -> str:
        key = KEY_OF[unit.removesuffix(".service")]
        name = OURS[key]
        info = self.units.get(key)
        if info is None:
            return (
                f"Id={name}\nLoadState=not-found\nActiveState=inactive\nSubState=dead\n"
            )
        text = " ".join(info["argv"])
        return (
            f"Id={name}\nLoadState=loaded\nActiveState={info['active']}\n"
            f"SubState={info['sub']}\nResult={info['result']}\n"
            f"ExecStart={{ path={info['argv'][0]} ; argv[]={text} ; ignore_errors=no ; "
            "start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }\n"
            f"MainPID={info['pid']}\nNRestarts=0\n"
            f"TasksCurrent={info.get('tasks', 1 if info['active'] != 'failed' else 0)}\n"
            f"ControlGroup={info.get('cgroup', f'/user.slice/app.slice/{name}')}\n"
        )

    def _oneshot_verb(self, argv):
        name = argv[3].removesuffix(".service")
        entry = self.oneshot_units.get(name)
        if argv[2] == "stop":
            if entry is not None:
                entry["stopped"] = True
                entry["shows_left"] = self.oneshot_linger
            return self.stop_codes.get("oneshot", 0), ""
        gone = entry is None or (entry["stopped"] and entry["shows_left"] <= 0)
        if entry is not None and entry["stopped"] and entry["shows_left"] > 0:
            entry["shows_left"] -= 1
        if gone:
            return 0, f"Id={name}.service\nLoadState=not-found\nActiveState=inactive\n"
        active = "deactivating" if entry["stopped"] else "active"
        return 0, f"Id={name}.service\nLoadState=loaded\nActiveState={active}\n"

    def _verb(self, argv):
        self.calls.append(list(argv))
        key = KEY_OF[argv[3].removesuffix(".service")]
        code = self.stop_codes.get(argv[2], 0)
        if code == 0:
            if argv[2] == "stop" and key in self.stop_fails:
                return 1, "Failed to stop unit: Connection timed out"
            if argv[2] == "stop" and key in self.stop_keeps:
                return 0, ""
            if argv[2] == "stop" and self.units.get(key, {}).get("hangs"):
                self.units[key]["active"] = "failed"
                self.units[key]["tasks"] = 3  # (SendSIGKILL=no: they are still there)
                return 1, "timed out"
            self.units.pop(key, None)
            self.on_stopped(key)
        return code, ""

    def on_stopped(self, key):
        """What stopping a unit does to the rest of the picture."""
        if key == "a":
            self.sockets = {"state": "inactive", "detail": "no socket to the arm"}
            self.no_manager()
        if key == "c":
            self.socks = [s for s in self.socks if s.local_port != SERVER_PORT]

    def _systemd_run(self, argv):
        if self.on_systemd_run is not None:
            self.on_systemd_run(argv)
        self.calls.append(list(argv))
        cut = argv.index("--")
        opts, cmd = argv[:cut], argv[cut + 1 :]
        unit = next(o.split("=", 1)[1] for o in opts if o.startswith("--unit="))
        if "--pipe" in opts:
            if unit in self.oneshot_units:
                # (what systemd-run prints for a name that is in use)
                return (
                    1,
                    (
                        f"Failed to start transient service unit: Unit {unit}.service "
                        "was already loaded or has a fragment file."
                    ),
                )
            self.oneshot_units[unit] = {"stopped": False, "shows_left": 0}
            try:
                return self._oneshot(cmd)
            finally:
                self.oneshot_units.pop(unit, None)
        code = self.run_codes.get("systemd-run", 0)
        if code != 0:
            return (
                code,
                "Failed to start transient service unit: property not supported",
            )
        if unit == stack.MONITOR_UNIT:
            self.ours()
            if self.m_health_on_start:
                self.no_manager()
            return 0, ""
        key = KEY_OF[unit]
        log = next(
            o.split("append:", 1)[1] for o in opts if "StandardOutput=append:" in o
        )
        self.units[key] = {
            "active": "active",
            "sub": "running",
            "result": "success",
            "argv": cmd,
            "pid": 5000 + len(self.units),
            "log": Path(log),
            "opts": opts,
        }
        return 0, ""

    def _oneshot(self, cmd):
        self.oneshots.append(cmd)
        if self.on_oneshot is not None:
            self.on_oneshot(cmd)
        text = " ".join(cmd)
        if "send_goal" in text and self.gripper_out is not None:
            return self.gripper_code, self.gripper_out
        if "send_goal" in text:
            position = re.search(r"position: ([0-9.]+)", text).group(1)
            out = GRIPPER_OUT.format(
                position=position,
                reached=str(self.gripper_reached).lower(),
                stalled=str(self.gripper_stalled).lower(),
            )
            return self.gripper_code, out
        if " node list " in f" {text} ":
            return self._nodes()
        if "list_controllers" in text:
            return self.controllers_code, self.controllers_out
        if "check_fr3_ee_frame" in text:
            return self.ee_code, self.ee_out
        raise AssertionError(f"unexpected one-shot {cmd}")

    def _nodes(self):
        if self.nodes_out is not None:
            return self.nodes_code, self.nodes_out
        listing = stack_units.parse_controllers(self.controllers_out)["state"]
        if listing == "listed":
            return 0, node_list_print([MONITOR_NODE, *GRIPPER_NODES])
        if listing == "absent":
            return 0, node_list_print([MONITOR_NODE])
        return 0, ""  # (nothing discovered: the graph proves nothing)

    # -- what a unit prints, and what the rest of the machine shows then
    def emit(self, key, *lines):
        with open(self.units[key]["log"], "a") as handle:
            handle.writelines(line + "\n" for line in lines)

    def a_up(self, lines=True):
        """A prints its ready lines (unless ``lines`` is False: the test has
        printed them itself) and the arm shows a control connection."""
        if lines:
            self.emit("a", *ready_lines("a"))
        self.sockets = {"state": "active", "detail": "2 control sockets"}
        self.health(hardware=True, controller=False, red=False, mode="IDLE")

    def b_up(self, lines=True):
        if lines:
            self.emit("b", *ready_lines("b"))

    def c_up(self, lines=True):
        if lines:
            self.emit("c", *ready_lines("c", SERVER_PORT))
        self.socks = [*self.socks, _sock(SERVER_PORT, net.LISTEN)]
        self.health(hardware=True, controller=True, red=False, mode="MOVE")

    # -- the steps a person takes, as the page would send them
    def m_ready(self):
        self.ours()
        self.no_manager()

    def set_marker_old(self, aged_s=None):
        """Set the marker and make it old enough. The clock is moved past the
        file's real ``ctime`` (which cannot be set back); with ``aged_s`` the
        clock stays and the file's mtime is set instead (a sample builds with
        ``marker_view`` reading mtime for both ages)."""
        self.act.marker_op(
            "touch", self.body("marker_touch", f"mk-touch-{self.run_no:06d}")
        )
        if aged_s is None:
            self.t = os.lstat(self.marker_path).st_ctime + 3600
            return
        stamp = self.t - aged_s
        os.utime(self.marker_path, (stamp, stamp))
        self.act._remember_marker("created")

    def preflight_a(self, rid=None):
        self.controllers_out, self.controllers_code = NO_MANAGER, NO_MANAGER_CODE
        rid = rid or f"pre-a-{self.run_no:08d}"
        return self.act.preflight_a({"request_id": rid})

    def ready_for_a(self, aged_s=None):
        """M ready, FCI answered, no gripper stack, a marker old enough."""
        self.m_ready()
        self.set_marker_old(aged_s)
        self.no_manager()
        self.preflight_a()

    def body(self, action, rid, **extra):
        return {"request_id": rid, "confirm": stack.PHRASES[action], **extra}

    def start_body(self, key, rid=None, attest=True):
        need = stack_act.ATTEST_NEEDED[key]
        return self.body(
            stack_act.ACTIONS[("start", key)],
            rid or f"start-{key}-0001",
            attest={k: True for k in need} if attest else {},
        )

    def stop_all_body(self, rid="stop-all-00001", **extra):
        """What "stop whole stack" sends: the phrase and no ticks."""
        return self.body("sequence_stop", rid, attest={}, **extra)

    def stop_body(self, key, rid=None, **extra):
        return self.body(
            stack_act.ACTIONS[("stop", key)], rid or f"stop-{key}-00001", **extra
        )

    def start_a(self, aged_s=None):
        self.ready_for_a(aged_s)
        out = self.act.start("a", self.start_body("a"))
        self.a_up()
        return out

    def start_b(self):
        """A person's start of B. When the only answer about a gripper stack is
        the first, unconfirmed "absent" of the graph (the guided start's early
        check), the first press makes the second check and starts nothing
        (``b_probe_started``): the person presses again, as the page says."""
        try:
            out = self.act.start("b", self.start_body("b"))
        except stack_act.ActRefuse as refusal:
            if refusal.code != "b_probe_started":
                raise
            out = self.act.start("b", self.start_body("b", "start-b-0002"))
        self.b_up()
        return out

    def start_c(self):
        self.controllers_out, self.controllers_code = CONTROLLERS_OUT, 0
        self.act.preflight_c({"request_id": "pre-c-00000001"})
        out = self.act.start("c", self.start_body("c"))
        self.c_up()
        return out

    def all_up(self, aged_s=None):
        self.start_a(aged_s)
        self.start_b()
        self.start_c()

    def chain_world(self, up, ready=("a", "b", "c")):
        """What the machine does around a running guided start: each unit of
        ``ready`` prints its ready lines once it exists (``up``, a set, holds
        what has been done), the clock moves past the marker's wait, and the
        controller manager answers once B exists (before that it does not: no
        gripper stack runs)."""
        for key, call in (("a", self.a_up), ("b", self.b_up), ("c", self.c_up)):
            if key in ready and key in self.units and key not in up:
                call()
                up.add(key)
        if "b" in self.units:
            self.controllers_out, self.controllers_code = CONTROLLERS_OUT, 0

    def run_chain(self, limit=120, ready=("a", "b", "c"), stop_at=None):
        """Advance the guided start until it stops running (or ``stop_at(seq)``
        says so), with the machine answering as a healthy one would
        (``chain_world``). The returned dict is the last ``advance``."""
        up = set()
        seq = {}
        for _ in range(limit):
            seq = self.act.advance()
            if seq["state"] != "running" or (stop_at and stop_at(seq)):
                return seq
            if seq.get("step") == "wait":
                self.t += 4  # (the marker's minimum age is 3 s)
                self.no_manager()
            self.chain_world(up, ready)
        raise AssertionError(f"the guided start did not settle: {seq}")

    def refresh_health(self):
        """The monitor keeps writing: the health file is as old as the clock
        says, with the values the machine showed (a jump of the clock does not
        leave it stale)."""
        health = self.act._context(self.sup._read()).r["health"]
        self.health(
            hardware=health.get("hardware_active"),
            controller=health.get("controller_active"),
            red=health["state"] == "red",
            mode=health.get("robot_mode_name"),
        )

    def chain_body(self, rid="chain-0000001", **extra):
        """What "start whole stack" sends: the window's one request, with every
        confirmation ticked (each stage takes the keys it needs)."""
        every = {k: True for k in stack_act.ATTEST_KEYS}
        return self.body(
            "sequence_start", rid, attest=every, **{"through": "c", **extra}
        )

    def hold_arm_lock(self):
        """The AERI arm lock is held by someone (a line in the fake lock table)."""
        self.arm_lock.write_text("")
        st = os.stat(self.arm_lock)
        self.locks.write_text(
            f"1: FLOCK  ADVISORY  WRITE 4321 "
            f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino} 0 EOF\n"
        )

    def hand_started_a(self):
        """A started in a terminal: no unit of ours, a control connection."""
        self.sockets = {"state": "active", "detail": "2 control sockets"}
        self.health(hardware=True, controller=False, red=False, mode="IDLE")

    def doc(self):
        return self.sup.view()["act"]

    def terminal(self, key):
        return self.doc()["terminals"][key]

    def blockers(self, key, which="start"):
        return [b["code"] for b in self.terminal(key)[f"{which}_blockers"]]

    def systemd_runs(self, key=None):
        runs = [c for c in self.calls if c[0] == "systemd-run" and "--pipe" not in c]
        if key is None:
            return runs
        return [c for c in runs if f"--unit={OURS[key].removesuffix('.service')}" in c]

    def oneshots_of(self, word):
        """The one-shot commands run so far whose command line holds ``word``."""
        return [c for c in self.oneshots if word in " ".join(c)]

    def stop_calls(self):
        return [c for c in self.calls if c[:3] == ["systemctl", "--user", "stop"]]
