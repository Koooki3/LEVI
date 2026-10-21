"""Stand-in for ``python3 -m fr3_robot_server.franka_server_ros2`` (terminal C).

    fake_c.py --flask_port=5056 --health=PATH --mode=ok|slow|never-ready|
                 crash-after-ready|ignore-sigint|deferred|port-taken

It binds the loopback port it is given (5056 in the rehearsals: 5000 is the
real server's and no test touches it) and serves nothing: nothing connects to
it. Like the real server it leaves ``controller_active`` true in the health
file when it exits."""

import socket
import time

from fake_common import Stopper, arg, say, write_health

mode = arg("mode", "ok")
port = int(arg("flask_port", "5056"))
health = arg("health")
assert port not in (5000, 5001, 5100, 7470, 8000), "a protected port"
stopper = Stopper(ignore=mode == "ignore-sigint")
say("[INFO] [1760000003.0] [franka_server]: /jointreset mode: cartesian")
if mode == "never-ready":
    stopper.stop.wait()
    raise SystemExit(0)
if mode == "slow":
    time.sleep(1.5)
if mode == "deferred":
    say(
        "[WARN] [1760000004.0] [franka_server]: No fresh robot state; impedance activation deferred"
    )
else:
    say("[INFO] [1760000007.0] [franka_server]: Impedance controller started")
    write_health(health, controller_active=True, robot_mode_name="MOVE")
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    server.bind(("127.0.0.1", port))
except OSError as exc:
    say(f"OSError: [Errno 98] Address already in use ({exc})")
    raise SystemExit(1) from exc
server.listen(1)
say(f" * Running on http://127.0.0.1:{port}")
if mode == "crash-after-ready":
    time.sleep(0.6)
    raise SystemExit(1)
while not stopper.stop.wait(0.1):
    pass
server.close()
