"""Stand-in for ``ros2 launch ... robotiq_collect.launch.py`` (terminal B).

    fake_b.py --mode=ok|slow|never-ready|crash-after-ready|ignore-sigint|
                      half-started

``half-started`` prints the driver line and the first controller and goes
quiet: the unit stays active and the gripper is not ready (the real stack can
do this after 3 of 171 starts)."""

import os
import resource
import time

from fake_common import BOLD, END, GREEN, Stopper, arg, say

mode = arg("mode", "ok")
alive = arg("alive")  # exists while the gripper's controllers would be listed
stopper = Stopper(ignore=mode == "ignore-sigint")
say(
    f"[env] FAKE_ROS_SOURCED={os.environ.get('FAKE_ROS_SOURCED')} "
    f"RCUTILS_COLORIZED_OUTPUT={os.environ.get('RCUTILS_COLORIZED_OUTPUT')} "
    f"RTPRIO={resource.getrlimit(resource.RLIMIT_RTPRIO)} "
    f"MEMLOCK={resource.getrlimit(resource.RLIMIT_MEMLOCK)}"
)
if mode == "never-ready":
    say("[INFO] [launch]: All log files can be found below /tmp/fake-ros-log")
    stopper.stop.wait()
    raise SystemExit(0)
if mode == "slow":
    time.sleep(1.5)
say(
    "[ros2_control_node-1] [INFO] [1760000001.200] [robotiq_controller_manager]: "
    "Robotiq Gripper successfully activated!"
)
names = (
    "joint_state_broadcaster_robotiq",
    "robotiq_gripper_controller",
    "robotiq_activation_controller",
)
for number, name in enumerate(names, 3):
    say(
        f"[spawner-{number}] [INFO] [1760000002.0] [spawner_{name}]: "
        f"{GREEN}Configured and activated {BOLD}{name}{END}"
    )
    if mode == "half-started":
        break
if alive and mode != "half-started":
    open(alive, "w").close()
try:
    if mode == "crash-after-ready":
        time.sleep(0.6)
        raise SystemExit(1)
    while not stopper.stop.wait(0.1):
        pass
    time.sleep(0.1)
finally:
    if alive and os.path.exists(alive):
        os.unlink(alive)
