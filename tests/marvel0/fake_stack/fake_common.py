"""Shared by the stand-in programs of the arm-stack rehearsal (stdlib only: a
unit that systemd starts has no virtual environment). They print the lines the
real programs print, write a fake health file the way the monitor does, and
stop on SIGINT like the real ones, except in the modes that misbehave on
purpose. They never touch a robot, a serial port or a protected port."""

import fcntl
import json
import os
import signal
import sys
import threading
import time

BOLD, GREEN, END = "\x1b[1m", "\x1b[92m", "\x1b[0m"


def say(*lines):
    for line in lines:
        print(line, flush=True)


def write_health(path, **flags):
    """Merge ``flags`` into the fake health file and stamp it now (atomic).

    The stand-ins A and C write the same file (A's heartbeat, C's one write of
    ``controller_active``). The merge reads, changes and replaces, so two
    writers that overlap lose one update: C's flag written between A's read and
    A's replace was gone and never written again, so C ran into its ready
    timeout (reproduced by waiting between the read and the replace; the
    chain test failed the same way in CI run 43615091128). The real monitor is
    the file's only writer, so there is no such race to model: the read and the
    replace are done under an exclusive lock of a file next to it."""
    with open(f"{path}.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(path) as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            data = {"monitor_pid": 4242, "red_light": False, "robot_mode_name": "IDLE"}
        data.update(flags)
        data["updated_at_epoch"] = time.time()
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as handle:
            json.dump(data, handle)
        os.replace(tmp, path)


class Stopper:
    """SIGINT handling: ``stop`` is set (the program ends), or ignored."""

    def __init__(self, ignore=False):
        self.stop = threading.Event()
        self.ignore = ignore
        signal.signal(signal.SIGINT, self._on_int)
        signal.signal(signal.SIGTERM, self._on_term)

    def _on_int(self, *_):
        if not self.ignore:
            self.stop.set()

    def _on_term(self, *_):
        self.stop.set()


def heartbeat(path, stopper, interval=0.2, **flags):
    """Keep the fake health file fresh until told to stop."""
    while not stopper.stop.wait(interval):
        write_health(path, **flags)


def arg(name, default=None):
    """``--name=value`` from the command line."""
    for item in sys.argv[1:]:
        if item.startswith(f"--{name}="):
            return item.split("=", 1)[1]
    return default
