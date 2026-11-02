"""The camera preview worker of the marvel0 page (``levi/marvel0/preview.py``).

LEVI starts this file, in a process group of its own, when a person asks the
Run card for a camera preview and no client holds the cameras. It opens the two
RealSense colour streams **by serial**, keeps the newest picture of each as a
JPEG file in ``--out`` (``<name>.jpg``, written to a temporary name and renamed,
so a reader sees a whole file or the previous one) and runs until it is told to
stop. It never connects to the robot, a port or LEVI; it reads no site file
(LEVI passes the serials, which change when the owner swaps a camera) and
writes only into ``--out``.

Why a worker of its own: ``pyrealsense2`` is not in LEVI's environment, and a
RealSense pipeline is held by one process at a time, so the preview must be
something LEVI can stop (and see stopped) before it starts a client. LEVI runs it
as ``nice -n 19 uv run --no-sync --with pyrealsense2==<pin> python worker.py ...``
from the openpi checkout (the way the AERI terminal D gets the same module), so
the numpy and OpenCV of that environment encode the pictures.

Stops: ``SIGTERM``, ``SIGINT``, or the end of its standard input (LEVI holds the
other end of a pipe; when LEVI dies the pipe closes and the cameras are let go
about 1.3 s later, with no LEVI left to ask: measured 2026-11-02 with both D405s,
from the close of the pipe to the exit of the process; the time of every
``pipeline.stop()`` and of the whole exit is written to standard error, so each
run leaves its own measurement). Each camera is stopped before the process
exits, which is what "released" means for the next client. A stop that arrives
while the cameras are being opened is checked before each one is opened, so the
rest are left alone. The pipe is read with ``os.read`` on the descriptor, not
through ``sys.stdin``: a thread blocked in the buffered reader holds its lock, and
the interpreter then aborts at exit ("_enter_buffered_busy", exit code -6) after
a stop by signal, losing the exit codes below. The file ends with ``os._exit`` of
the code, after standard error is flushed, for the same reason.

Exit codes (``levi/marvel0/preview.py`` ``EXIT_FAILURES`` words them; a test
keeps the two files in step):

    0   stopped as asked
    10  a camera did not start (another program holds it, or it is unplugged
        half way): "cameras busy"
    11  a serial is not among the connected devices: "camera not found"
    12  ``pyrealsense2``, numpy or OpenCV could not be imported
    13  the cameras started and sent no picture (or stopped sending)
    14  a bad command line

The pictures are at most ``--max-fps`` a second per camera (JPEG encoding is
the work that costs CPU), 640x480 by default. Importing this file imports only
the standard library; the camera and image libraries are imported by ``main``.
"""

import argparse
import contextlib
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path

EXIT_OK = 0
EXIT_BUSY = 10
EXIT_NOT_FOUND = 11
EXIT_UNAVAILABLE = 12
EXIT_NO_FRAMES = 13
EXIT_USAGE = 14

NAME = re.compile(r"[a-z]{1,16}")
SERIAL = re.compile(r"[0-9]{6,16}")
# A camera that has sent nothing for this long is not sending any more.
SILENT_S = 5.0
FIRST_FRAME_S = 15.0
WAIT_MS = 1000  # one wait for a frameset; a timeout is not yet a failure


class Usage(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # argparse would print and exit(2)
        raise Usage(message)


def parse(argv) -> argparse.Namespace:
    parser = _Parser(prog="camera-preview-worker", add_help=False)
    parser.add_argument("--out", required=True)
    parser.add_argument("--camera", action="append", default=[], metavar="NAME=SERIAL")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--fps", type=int, default=15)
    parser.add_argument("--max-fps", type=float, default=15.0)
    parser.add_argument("--quality", type=int, default=80)
    args = parser.parse_args(argv)
    args.cameras = []
    for item in args.camera:
        name, _, serial = item.partition("=")
        if not NAME.fullmatch(name) or not SERIAL.fullmatch(serial):
            raise Usage("a camera is NAME=SERIAL (lower-case name, digits)")
        if any(name == other for other, _ in args.cameras):
            raise Usage("a camera name twice")
        args.cameras.append((name, serial))
    if not args.cameras or len({s for _, s in args.cameras}) != len(args.cameras):
        raise Usage("one or more cameras, each with its own serial")
    if not (
        1 <= args.width <= 1920
        and 1 <= args.height <= 1080
        and 1 <= args.fps <= 60
        and 0 < args.max_fps <= 60
        and 1 <= args.quality <= 100
    ):
        raise Usage("size, rate or quality out of range")
    if not os.path.isabs(args.out):
        raise Usage("--out is an absolute folder")
    return args


def write_atomic(path: Path, data: bytes) -> None:
    """``path`` becomes ``data`` in one rename: a reader never sees half a file."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    os.replace(temporary, path)


class Reader(threading.Thread):
    """One camera: wait for framesets, encode at most ``max_fps`` a second."""

    def __init__(self, name, pipeline, args, out, stop, encode, to_image, clock):
        super().__init__(name=f"preview-{name}", daemon=True)
        self.camera, self.pipeline, self.out = name, pipeline, Path(out)
        self.period = 1.0 / args.max_fps
        self.stop_event, self.encode, self.to_image = stop, encode, to_image
        self.clock = clock
        self.written = 0
        self.last_frame = clock()  # the start counts as a sign of life
        self.failed = False

    def run(self) -> None:
        last_write = -1e9
        target = self.out / f"{self.camera}.jpg"
        while not self.stop_event.is_set():
            try:
                frames = self.pipeline.wait_for_frames(WAIT_MS)
            except RuntimeError:  # a timeout, or a hiccup of the device
                if self.clock() - self.last_frame > SILENT_S:
                    self.failed = True
                    return
                continue
            color = frames.get_color_frame()
            if not color:
                continue
            now = self.clock()
            self.last_frame = now
            if now - last_write < self.period:
                continue
            try:
                write_atomic(target, self.encode(self.to_image(color)))
            except Exception:  # noqa: BLE001 - a picture that cannot be made ends the preview
                self.failed = True
                return
            last_write = now
            self.written += 1


def devices(rs) -> set:
    """The serials of the connected RealSense devices."""
    found = set()
    for device in rs.context().query_devices():
        with contextlib.suppress(RuntimeError):
            found.add(device.get_info(rs.camera_info.serial_number))
    return found


def run(
    args,
    rs,
    *,
    encode,
    to_image,
    stop,
    clock=time.monotonic,
    sleep=time.sleep,
    say=lambda text: print(text, file=sys.stderr, flush=True),
) -> int:
    """Open the cameras, keep their pictures current until ``stop`` (an object
    with ``is_set`` and ``set``: ``Stop``, or a ``threading.Event``) is set, let
    them go. The exit code (see the module text)."""
    out = Path(args.out)
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    if stop.is_set():  # asked to stop before anything was opened
        return EXIT_OK
    try:
        present = devices(rs)
    except RuntimeError as exc:
        say(f"cannot list the devices ({type(exc).__name__})")
        return EXIT_BUSY
    missing = [name for name, serial in args.cameras if serial not in present]
    if missing:
        say(f"camera not found: {', '.join(missing)}")
        return EXIT_NOT_FOUND
    started: list = []  # (name, pipeline)
    readers: list = []
    try:
        for name, serial in args.cameras:
            if stop.is_set():  # a stop that arrives half way opens no more
                say(f"stop requested: camera {name} left closed")
                break
            pipeline = rs.pipeline()
            config = rs.config()
            config.enable_device(serial)
            config.enable_stream(
                rs.stream.color, args.width, args.height, rs.format.bgr8, args.fps
            )
            try:
                pipeline.start(config)
            except RuntimeError as exc:
                # Listed and still not startable: another process holds it.
                say(f"camera {name} did not start ({type(exc).__name__})")
                return EXIT_BUSY
            started.append((name, pipeline))
            say(f"camera {name} started")
            reader = Reader(name, pipeline, args, out, stop, encode, to_image, clock)
            readers.append(reader)
            reader.start()
        began = clock()
        while not stop.is_set():
            if any(r.failed for r in readers):
                say("a camera stopped sending pictures")
                return EXIT_NO_FRAMES
            if clock() - began > FIRST_FRAME_S and any(r.written == 0 for r in readers):
                say("a camera sent no picture")
                return EXIT_NO_FRAMES
            sleep(0.05)
        return EXIT_OK
    finally:
        stop.set()
        shutting = clock()
        for reader in readers:
            reader.join(WAIT_MS / 1000 + 1)
        joined = clock()
        for name, pipeline in started:
            began = clock()
            with contextlib.suppress(Exception):
                pipeline.stop()
            say(f"camera {name} stopped in {clock() - began:.2f} s")
        if started:
            say(
                f"readers joined in {joined - shutting:.2f} s, "
                f"cameras released {clock() - shutting:.2f} s after the stop"
            )


def watch_stdin(stop, fd: int = 0) -> None:
    """Set ``stop`` when LEVI's end of our standard input closes. Reads the
    descriptor itself: ``sys.stdin.buffer.read`` would keep the buffered
    reader's lock for as long as it blocks, and the interpreter aborts at exit
    when it cannot take that lock."""

    def wait() -> None:
        with contextlib.suppress(OSError, ValueError):
            while os.read(fd, 1024):
                pass
        stop.set()

    threading.Thread(target=wait, name="preview-stdin", daemon=True).start()


class Stop:
    """The stop flag, which remembers when it was first set (for the log). It
    has no lock, unlike ``threading.Event`` (whose ``set`` takes a lock that is
    not reentrant): the signal handlers run in the main thread, possibly one
    inside the other and inside ``run``'s own ``set``, and a handler that
    interrupted a ``set`` and then called ``set`` would wait for ever for the
    lock its own interrupted frame holds. Nobody waits on it; everyone polls
    ``is_set``."""

    def __init__(self):
        self._flag = False
        self.since: float | None = None

    def set(self) -> None:
        if self.since is None:
            self.since = time.monotonic()
        self._flag = True

    def is_set(self) -> bool:
        return self._flag


def main(argv=None) -> int:
    try:
        args = parse(sys.argv[1:] if argv is None else argv)
    except Usage as exc:
        print(f"usage: {exc}", file=sys.stderr, flush=True)
        return EXIT_USAGE
    try:
        import cv2
        import numpy
        import pyrealsense2 as rs
    except ImportError as exc:
        print(f"cannot import {exc.name}", file=sys.stderr, flush=True)
        return EXIT_UNAVAILABLE
    cv2.setNumThreads(1)
    quality = [int(cv2.IMWRITE_JPEG_QUALITY), args.quality]

    def encode(image) -> bytes:
        done, buffer = cv2.imencode(".jpg", image, quality)
        if not done:
            raise ValueError("the picture could not be encoded")
        return buffer.tobytes()

    def to_image(color):
        return numpy.asanyarray(color.get_data())

    stop = Stop()
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: stop.set())
    watch_stdin(stop)
    code = run(args, rs, encode=encode, to_image=to_image, stop=stop)
    if stop.since is not None:
        print(
            f"exit {code} {time.monotonic() - stop.since:.2f} s after the stop",
            file=sys.stderr,
            flush=True,
        )
    return code


if __name__ == "__main__":
    _code = main()
    # Not sys.exit: whatever thread still sits in a read must not decide the
    # exit code (see the module text).
    for _stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            _stream.flush()
    os._exit(_code)
