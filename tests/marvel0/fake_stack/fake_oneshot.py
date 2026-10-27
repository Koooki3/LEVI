"""Stand-in for the three one-shot commands (``ros2 action send_goal``,
``ros2 control list_controllers`` and ``check_fr3_ee_frame.py``).

    fake_oneshot.py gripper POSITION MODE   (MODE: ok | stalled | silent | hang)
    fake_oneshot.py controllers MODE ALIVE  (MODE: ok | inactive | none | silent |
                                             hang | flaky-once; the controllers are listed only
                                             while the file ALIVE exists: the
                                             gripper stack is up. Without it the
                                             command does what the real one does
                                             with no controller manager: it logs
                                             on stderr and is ended from outside
                                             (`timeout`: status 124, here after a
                                             moment). ``silent`` is ended before
                                             its warning; ``hang`` is not ended by
                                             anything but the caller's backstop;
                                             ``none`` is a manager with nothing
                                             loaded; ``flaky-once`` gets no answer
                                             the first time it is asked while the
                                             gripper stack is up, as the first real
                                             run did, and answers after that)
    fake_oneshot.py ee MODE                 (MODE: ok | fail)
    fake_oneshot.py nodes MODE ALIVE        (the stand-in of `ros2 node list --no-daemon
                                             --spin-time T`: it prints the node names, one per
                                             line, sorted, after the `delay=` below:
                                             the monitor's node always (MODE ok), the gripper
                                             stack's nodes while the file ALIVE exists.
                                             MODE ``blind``: the graph was not discovered
                                             (nothing printed); ``hang``: never ends by
                                             itself, only the caller's backstop ends it)

Any of them takes a last argument ``delay=SECONDS``: it waits that long before it
does anything (what the real command spends starting up and discovering).

The output is the real programs': the rows of ``list_controllers`` are the
f-string of franka_ros2_ws/src/ros2_control/ros2controlcli/ros2controlcli/verb/
list_controllers.py:34-36, the wait and its warning are
controller_manager_services.py:140-150."""

import os
import signal
import sys
import time

TYPES = {
    "joint_state_broadcaster_robotiq": "joint_state_broadcaster/JointStateBroadcaster",
    "robotiq_gripper_controller": "position_controllers/GripperActionController",
    "robotiq_activation_controller": "robotiq_controllers/RobotiqActivationController",
}
SERVICE = "/robotiq/controller_manager/list_controllers"
MONITOR_NODE = "/fr3_health_monitor"
GRIPPER_NODES = (
    "/robotiq/controller_manager",
    "/robotiq/joint_state_broadcaster_robotiq",
    "/robotiq/robot_state_publisher",
    "/robotiq/robotiq_activation_controller",
    "/robotiq/robotiq_gripper_controller",
)


def rows(states):
    wn = max(len(n) for n in states)
    wt = max(len(TYPES[n]) for n in states)
    ws = max(len(s) for s in states.values())
    return "".join(
        f"{n:<{wn}} {TYPES[n]:<{wt}}  {s:<{ws}}\n" for n, s in states.items()
    )


kind, rest = sys.argv[1], sys.argv[2:]
delay = 0.0
for item in list(rest):
    if item.startswith("delay="):
        delay = float(item.split("=", 1)[1])
        rest.remove(item)
    elif item.startswith("owner="):  # the test folder, for leftover_standins only
        rest.remove(item)
time.sleep(delay)
if kind == "gripper":
    position, mode = rest
    if mode == "hang":
        time.sleep(60)
    if mode == "silent":
        print("Waiting for an action server to become available...")
        raise SystemExit(1)
    stalled = "true" if mode == "stalled" else "false"
    reached = "false" if mode == "stalled" else "true"
    # (send_goal.py:140-212: sorted yaml keys, only the first line indented)
    print(
        "Waiting for an action server to become available...\n"
        f"Sending goal:\n     command:\n  max_effort: 0.0\n  position: {position}\n\n"
        "Goal accepted with ID: 0123\n\nResult:\n"
        f"    effort: 0.0\nposition: {position}\n"
        f"reached_goal: {reached}\nstalled: {stalled}\n\n"
        "Goal finished with status: SUCCEEDED"
    )
elif kind == "controllers":
    mode, alive = rest
    listed = os.path.exists(alive)
    if mode == "flaky-once" and listed and not os.path.exists(alive + ".asked"):
        open(alive + ".asked", "w").close()
        listed = False  # (no answer this once)
    if not listed:
        # (the real command is ended by SIGINT from `timeout` or from systemd)
        signal.signal(signal.SIGINT, lambda *args: sys.exit(2))
        waiting = (
            f"[INFO] [{time.time():.9f}] [_ros2cli_{os.getpid()}]: waiting for "
            f"service {SERVICE} to become available..."
        )
        print(waiting, file=sys.stderr, flush=True)
        if mode != "silent":
            print(
                f"[WARN] [{time.time():.9f}] [_ros2cli_{os.getpid()}]: "
                f"Could not contact service {SERVICE}",
                file=sys.stderr,
                flush=True,
            )
        if mode == "hang":
            time.sleep(3600)
        time.sleep(0.3)  # (the bound of `timeout`, much shorter)
        raise SystemExit(124)
    if mode == "none":
        print("No controllers are currently loaded!")
    else:
        print(
            rows(
                {
                    name: (
                        "inactive"
                        if mode == "inactive" and name.startswith("robotiq_g")
                        else "active"
                    )
                    for name in TYPES
                }
            ),
            end="",
        )
elif kind == "nodes":
    mode, alive = rest
    if mode == "hang":
        time.sleep(3600)
    names = [] if mode == "blind" else [MONITOR_NODE]
    if os.path.exists(alive) and mode != "blind":
        names += GRIPPER_NODES
    # (ros2node/verb/list.py: `print(*sorted_names, sep='\n')`; nothing for none)
    if names:
        print(*sorted(names), sep="\n")
else:
    mode = rest[0]
    if mode == "ok":
        print(
            "  flange -> EE      0.10340 m\n\n  OK -- Desk is serving the expected 'franka_hand' profile."
        )
    else:
        print("  FAIL -- expected 'franka_hand', measured 'robotiq'.")
        raise SystemExit(1)
