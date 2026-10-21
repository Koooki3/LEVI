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


def rows(states):
    wn = max(len(n) for n in states)
    wt = max(len(TYPES[n]) for n in states)
    ws = max(len(s) for s in states.values())
    return "".join(
        f"{n:<{wn}} {TYPES[n]:<{wt}}  {s:<{ws}}\n" for n, s in states.items()
    )


kind, rest = sys.argv[1], sys.argv[2:]
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
else:
    mode = rest[0]
    if mode == "ok":
        print(
            "  flange -> EE      0.10340 m\n\n  OK -- Desk is serving the expected 'franka_hand' profile."
        )
    else:
        print("  FAIL -- expected 'franka_hand', measured 'robotiq'.")
        raise SystemExit(1)
