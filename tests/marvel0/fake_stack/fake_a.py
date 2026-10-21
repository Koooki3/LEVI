"""Stand-in for ``ros2 launch ... impedance.launch.py`` (terminal A).

    fake_a.py --health=PATH --mode=ok|slow|never-ready|crash-after-ready|
                            ignore-sigint|duplicate-text|no-rt-line|rt-failed|
                            crash-clean

Prints the ready lines of the real stack and keeps the health file saying the
hardware is active. Modes: ``slow`` is ready after 1.5 s; ``never-ready``
prints a banner and nothing else; ``crash-after-ready`` exits 1 and
``crash-clean`` exits 0 shortly after it is ready; ``ignore-sigint`` does not
stop on SIGINT; ``duplicate-text`` prints the realtime line twice;
``no-rt-line`` leaves it out; ``rt-failed`` prints the failure instead."""

import os
import resource
import threading
import time

from fake_common import Stopper, arg, heartbeat, say, write_health

mode = arg("mode", "ok")
health = arg("health")
stopper = Stopper(ignore=mode == "ignore-sigint")
say(
    f"[env] FAKE_ROS_SOURCED={os.environ.get('FAKE_ROS_SOURCED')} "
    f"RCUTILS_COLORIZED_OUTPUT={os.environ.get('RCUTILS_COLORIZED_OUTPUT')} "
    f"RTPRIO={resource.getrlimit(resource.RLIMIT_RTPRIO)} "
    f"MEMLOCK={resource.getrlimit(resource.RLIMIT_MEMLOCK)}"
)
say("[INFO] [launch]: All log files can be found below /tmp/fake-ros-log")
if mode == "never-ready":
    stopper.stop.wait()
    raise SystemExit(0)
if mode == "slow":
    time.sleep(1.5)
fifo = (
    "[ros2_control_node-2] [INFO] [1760000001.100] [controller_manager]: "
    "Successful set up FIFO RT scheduling policy with priority 98."
)
if mode == "rt-failed":
    say(
        "[ros2_control_node-2] [WARN] [1760000001.100] [controller_manager]: "
        "Could not enable FIFO RT scheduling policy: with error number <1>(Operation not permitted)."
    )
elif mode != "no-rt-line":
    say(fifo)
if mode == "duplicate-text":
    say(fifo)
say(
    "[INFO] [spawner-4]: process has finished cleanly [pid 4004]",
    "[INFO] [spawner-5]: process has finished cleanly [pid 4005]",
)
write_health(
    health,
    hardware_active=True,
    controller_active=False,
    red_light=False,
    robot_mode_name="IDLE",
)
if mode in ("crash-after-ready", "crash-clean"):
    time.sleep(0.6)
    # (the real monitor notices within half a second)
    # (no controller manager answers any more: the monitor writes null, not
    # false, fr3_health_monitor.py:115-120 and :349-359)
    write_health(health, hardware_active=None, controller_active=None, red_light=True)
    raise SystemExit(1 if mode == "crash-after-ready" else 0)
beat = threading.Thread(
    target=heartbeat,
    args=(health, stopper),
    kwargs={"hardware_active": True},
    daemon=True,
)
beat.start()
while not stopper.stop.wait(0.1):
    pass
time.sleep(0.2)  # (the real launch takes 10 s to let go)
write_health(
    health, hardware_active=None, controller_active=None, red_light=True
)  # (as the monitor would: no manager answers, so null)
