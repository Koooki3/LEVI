"""A stand-in for ``pyrealsense2`` for the tests that run the REAL
``integrations/camera_preview/worker.py`` as a process (``PYTHONPATH`` points at
this folder; the real library is never imported and no device is opened).

The behaviour comes from the environment of the process:

    STUB_RS_SERIALS   comma list of the serials that are "connected"
    STUB_RS_BUSY      comma list of serials whose ``pipeline.start`` raises
    STUB_RS_STOP_S    seconds every ``pipeline.stop()`` takes (default 0)
    STUB_RS_START_S   seconds every ``pipeline.start()`` takes (default 0)
    STUB_RS_MARK      a file that gets one line when a ``pipeline.start()`` begins
"""

import os
import time
from types import SimpleNamespace


def _list(name):
    return [item for item in os.environ.get(name, "").split(",") if item]


camera_info = SimpleNamespace(serial_number="serial")
stream = SimpleNamespace(color="color")
format = SimpleNamespace(bgr8="bgr8")


class _Color:
    def __init__(self, data):
        self._data = data

    def __bool__(self):
        return True

    def get_data(self):
        return self._data


class _Frames:
    def __init__(self, data):
        self._data = data

    def get_color_frame(self):
        return _Color(self._data)


class config:
    def __init__(self):
        self.serial = None

    def enable_device(self, serial):
        self.serial = serial

    def enable_stream(self, *args):
        pass


class pipeline:
    def __init__(self):
        self.serial = None
        self.n = 0

    def start(self, cfg):
        self.serial = cfg.serial
        mark = os.environ.get("STUB_RS_MARK")
        if mark:
            with open(mark, "a") as handle:
                handle.write(f"{self.serial}\n")
        time.sleep(float(os.environ.get("STUB_RS_START_S", "0")))
        if self.serial in _list("STUB_RS_BUSY"):
            raise RuntimeError("Device or resource busy")

    def wait_for_frames(self, timeout_ms=5000):
        time.sleep(0.02)
        self.n += 1
        return _Frames(f"{self.serial}:{self.n}".encode())

    def stop(self):
        time.sleep(float(os.environ.get("STUB_RS_STOP_S", "0")))


class context:
    def query_devices(self):
        return [
            SimpleNamespace(get_info=lambda key, s=s: s)
            for s in _list("STUB_RS_SERIALS")
        ]
