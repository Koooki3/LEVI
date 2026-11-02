"""``integrations/camera_preview/worker.py`` on a **fake** ``pyrealsense2``: no
module of the real library is imported and no device is opened (``main`` is only
run with the import blocked). What is checked: the command line, the pictures as
atomically replaced files at no more than the asked rate, the exit code of every
way the cameras can fail, that each started camera is stopped on the way out,
and that the end of the input stops the worker."""

import importlib.util
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

WORKER = (
    Path(__file__).resolve().parents[2]
    / "integrations"
    / "camera_preview"
    / "worker.py"
)


@pytest.fixture
def worker():
    spec = importlib.util.spec_from_file_location("camera_preview_worker_t", WORKER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


class Color:
    def __init__(self, data):
        self.data = data

    def __bool__(self):
        return True


class Frames:
    def __init__(self, color):
        self.color = color

    def get_color_frame(self):
        return self.color


class Pipeline:
    def __init__(self, world):
        self.world = world
        self.serial = None
        self.running = False
        self.n = 0
        self.began = 0.0

    def start(self, config):
        self.serial = config.serial
        if self.serial in self.world.busy:
            raise RuntimeError("xioctl(VIDIOC_S_FMT) failed: Device or resource busy")
        self.running = True
        self.began = time.monotonic()
        self.world.started.append(self.serial)

    def wait_for_frames(self, timeout_ms=5000):
        # ~100 frames a second per camera, so the rate cap is what limits writes
        time.sleep(0.01)
        silent_after = self.world.silent_after.get(self.serial)
        if silent_after is not None and self.n >= silent_after:
            time.sleep(0.02)
            raise RuntimeError("Frame didn't arrive within 1000")
        self.n += 1
        return Frames(Color(f"{self.serial}:{self.n}".encode()))

    def stop(self):
        self.running = False
        self.world.stopped.append(self.serial)


class Config:
    def __init__(self):
        self.serial = None
        self.stream = None

    def enable_device(self, serial):
        self.serial = serial

    def enable_stream(self, *args):
        self.stream = args


class World:
    """A fake ``pyrealsense2``."""

    camera_info = SimpleNamespace(serial_number="serial")
    stream = SimpleNamespace(color="color")
    format = SimpleNamespace(bgr8="bgr8")

    def __init__(self, present, busy=(), silent_after=None):
        self.present, self.busy = set(present), set(busy)
        self.silent_after = silent_after or {}
        self.started: list = []
        self.stopped: list = []
        self.pipelines: list = []

    def context(self):
        devices = [
            SimpleNamespace(get_info=lambda key, s=s: s) for s in sorted(self.present)
        ]
        return SimpleNamespace(query_devices=lambda: devices)

    def pipeline(self):
        pipeline = Pipeline(self)
        self.pipelines.append(pipeline)
        return pipeline

    def config(self):
        return Config()


def args_for(worker, out, **kw):
    argv = [
        "--out",
        str(out),
        "--camera",
        "side=111111111111",
        "--camera",
        "wrist=222222222222",
    ]
    for key, value in kw.items():
        argv += [f"--{key.replace('_', '-')}", str(value)]
    return worker.parse(argv)


class Encoder:
    def __init__(self):
        self.calls = 0

    def __call__(self, image):
        self.calls += 1
        return b"\xff\xd8\xff" + image + b"\xff\xd9"


def drive(worker, args, world, seconds, **kw):
    """``worker.run`` until a timer sets the stop event ``seconds`` later."""
    stop = kw.pop("stop", None) or threading.Event()
    timer = threading.Timer(seconds, stop.set)
    encode = kw.pop("encode", Encoder())
    say = kw.pop("say", lambda text: None)
    timer.start()
    try:
        code = worker.run(
            args,
            world,
            encode=encode,
            to_image=lambda color: color.data,
            stop=stop,
            say=say,
            **kw,
        )
    finally:
        timer.cancel()
    return code, encode


# --- the command line -----------------------------------------------------------------


def test_the_command_line_names_cameras_by_serial(worker, tmp_path):
    args = args_for(worker, tmp_path)
    assert args.cameras == [("side", "111111111111"), ("wrist", "222222222222")]
    assert (args.width, args.height, args.fps, args.max_fps) == (640, 480, 15, 15.0)


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--out", "/tmp/x"],
        ["--out", "relative", "--camera", "side=123456"],
        ["--out", "/tmp/x", "--camera", "side"],
        ["--out", "/tmp/x", "--camera", "Side=123456"],
        ["--out", "/tmp/x", "--camera", "side=12ab56"],
        ["--out", "/tmp/x", "--camera", "side=123456", "--camera", "side=654321"],
        ["--out", "/tmp/x", "--camera", "side=123456", "--camera", "wrist=123456"],
        ["--out", "/tmp/x", "--camera", "side=123456", "--fps", "0"],
        ["--out", "/tmp/x", "--camera", "side=123456", "--max-fps", "500"],
        ["--out", "/tmp/x", "--camera", "side=123456", "--bogus"],
    ],
)
def test_a_bad_command_line_is_a_usage_error_and_exits_14(worker, argv, capsys):
    with pytest.raises(worker.Usage):
        worker.parse(argv)
    assert worker.main(argv) == worker.EXIT_USAGE == 14


def test_main_without_the_camera_library_exits_12_and_opens_nothing(
    worker, monkeypatch, tmp_path
):
    # (a None entry makes ``import pyrealsense2`` raise, installed or not)
    monkeypatch.setitem(sys.modules, "pyrealsense2", None)
    code = worker.main(["--out", str(tmp_path), "--camera", "side=123456"])
    assert code == worker.EXIT_UNAVAILABLE == 12


# --- the pictures ---------------------------------------------------------------------


def test_each_camera_gets_its_own_picture_file(worker, tmp_path):
    world = World({"111111111111", "222222222222"})
    code, _ = drive(worker, args_for(worker, tmp_path), world, 0.4)
    assert code == worker.EXIT_OK == 0
    side = (tmp_path / "side.jpg").read_bytes()
    wrist = (tmp_path / "wrist.jpg").read_bytes()
    assert side.startswith(b"\xff\xd8\xff111111111111:") and side.endswith(b"\xff\xd9")
    assert wrist.startswith(b"\xff\xd8\xff222222222222:") and wrist.endswith(
        b"\xff\xd9"
    )
    assert sorted(p.name for p in tmp_path.iterdir()) == ["side.jpg", "wrist.jpg"]


def test_the_mode_of_a_picture_file_is_private(worker, tmp_path):
    drive(
        worker, args_for(worker, tmp_path), World({"111111111111", "222222222222"}), 0.2
    )
    assert (tmp_path / "side.jpg").stat().st_mode & 0o777 == 0o600


def test_pictures_are_written_at_most_at_the_asked_rate(worker, tmp_path):
    world = World({"111111111111", "222222222222"})  # ~100 frames a second each
    code, encode = drive(worker, args_for(worker, tmp_path, max_fps=10), world, 1.0)
    assert code == 0
    # two cameras at 10 a second for one second, with some slack either way
    assert 8 <= encode.calls <= 26, encode.calls
    assert max(p.n for p in world.pipelines) > 40  # many more frames arrived


def test_the_streams_are_asked_for_at_the_size_and_rate_of_the_command(
    worker, tmp_path
):
    world = World({"111111111111", "222222222222"})
    seen = []
    original = Config.enable_stream
    Config.enable_stream = lambda self, *a: seen.append(a) or original(self, *a)
    try:
        drive(
            worker, args_for(worker, tmp_path, width=320, height=240, fps=6), world, 0.1
        )
    finally:
        Config.enable_stream = original
    assert seen == [("color", 320, 240, "bgr8", 6)] * 2


def test_a_reader_never_sees_half_a_file(worker, tmp_path):
    target = tmp_path / "side.jpg"
    stop = threading.Event()
    seen = []

    def read():
        while not stop.is_set():
            try:
                seen.append(target.read_bytes())
            except OSError:
                pass

    thread = threading.Thread(target=read)
    thread.start()
    try:
        for n in range(300):
            worker.write_atomic(target, (b"%d" % n).ljust(50_000, b"x"))
    finally:
        stop.set()
        thread.join()
    assert seen and all(len(data) == 50_000 for data in seen)
    assert [p.name for p in tmp_path.iterdir()] == ["side.jpg"]


# --- the ways the cameras fail --------------------------------------------------------


def test_a_serial_that_is_not_connected_exits_11_and_starts_no_camera(worker, tmp_path):
    world = World({"111111111111"})  # the wrist camera is missing
    code, _ = drive(worker, args_for(worker, tmp_path), world, 0.2)
    assert code == worker.EXIT_NOT_FOUND == 11
    assert world.started == [] and world.pipelines == []


def test_a_camera_another_program_holds_exits_10(worker, tmp_path):
    world = World({"111111111111", "222222222222"}, busy={"111111111111"})
    code, _ = drive(worker, args_for(worker, tmp_path), world, 0.2)
    assert code == worker.EXIT_BUSY == 10
    assert world.started == [] and not list(tmp_path.glob("*.jpg"))


def test_when_the_second_camera_is_busy_the_first_is_let_go(worker, tmp_path):
    world = World({"111111111111", "222222222222"}, busy={"222222222222"})
    code, _ = drive(worker, args_for(worker, tmp_path), world, 0.3)
    assert code == 10
    assert world.started == ["111111111111"]
    assert world.stopped == ["111111111111"]  # released on the way out


def test_every_started_camera_is_stopped_when_it_is_told_to_stop(worker, tmp_path):
    world = World({"111111111111", "222222222222"})
    code, _ = drive(worker, args_for(worker, tmp_path), world, 0.2)
    assert code == 0
    assert sorted(world.stopped) == ["111111111111", "222222222222"]
    assert not any(p.running for p in world.pipelines)


def test_cameras_that_never_send_a_picture_exit_13(worker, tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "FIRST_FRAME_S", 0.3)
    monkeypatch.setattr(worker, "SILENT_S", 100.0)
    world = World(
        {"111111111111", "222222222222"},
        silent_after={"111111111111": 0, "222222222222": 0},
    )
    code, _ = drive(worker, args_for(worker, tmp_path), world, 3.0)
    assert code == worker.EXIT_NO_FRAMES == 13
    assert sorted(world.stopped) == ["111111111111", "222222222222"]


def test_a_camera_that_stops_sending_exits_13(worker, tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "SILENT_S", 0.3)
    world = World({"111111111111", "222222222222"}, silent_after={"222222222222": 5})
    code, _ = drive(worker, args_for(worker, tmp_path), world, 5.0)
    assert code == 13
    assert sorted(world.stopped) == ["111111111111", "222222222222"]


def test_a_picture_that_cannot_be_encoded_ends_the_preview(worker, tmp_path):
    def broken(image):
        raise ValueError("no picture")

    code, _ = drive(
        worker,
        args_for(worker, tmp_path),
        World({"111111111111", "222222222222"}),
        3.0,
        encode=broken,
    )
    assert code == 13


# --- a stop that arrives early -----------------------------------------------------------


def test_a_stop_that_came_before_anything_was_opened_opens_nothing(worker, tmp_path):
    world = World({"111111111111", "222222222222"})
    stop = threading.Event()
    stop.set()
    code, _ = drive(worker, args_for(worker, tmp_path), world, 5.0, stop=stop)
    assert code == worker.EXIT_OK
    assert world.pipelines == [] and world.started == []


def test_a_stop_that_arrives_while_the_first_camera_opens_leaves_the_second_closed(
    worker, tmp_path
):
    world = World({"111111111111", "222222222222"})
    stop = threading.Event()
    original = Pipeline.start

    def start(self, config):
        original(self, config)
        stop.set()  # the stop arrives while the first camera is being opened

    Pipeline.start = start
    try:
        code, _ = drive(worker, args_for(worker, tmp_path), world, 5.0, stop=stop)
    finally:
        Pipeline.start = original
    assert code == worker.EXIT_OK
    assert world.started == ["111111111111"]
    assert world.stopped == ["111111111111"]  # (and the one it opened is let go)


def test_every_camera_stop_and_the_whole_exit_are_written_to_the_log(worker, tmp_path):
    said: list = []
    world = World({"111111111111", "222222222222"})
    code, _ = drive(worker, args_for(worker, tmp_path), world, 0.2, say=said.append)
    assert code == 0
    assert [t for t in said if "stopped in" in t and "camera side" in t]
    assert [t for t in said if "stopped in" in t and "camera wrist" in t]
    assert [t for t in said if "cameras released" in t and "after the stop" in t]


# --- the end of the input ---------------------------------------------------------------


def test_the_end_of_the_input_sets_the_stop_event(worker):
    read, write = os.pipe()
    os.write(write, b"anything LEVI might send")
    os.close(write)
    stop = threading.Event()
    worker.watch_stdin(stop, read)
    assert stop.wait(2.0)
    os.close(read)


def test_input_that_stays_open_does_not_stop_it(worker):
    read, write = os.pipe()
    stop = threading.Event()
    worker.watch_stdin(stop, read)
    assert not stop.wait(0.3)
    os.close(write)  # the other end goes away
    assert stop.wait(2.0)
    os.close(read)


def test_the_lifeline_is_read_without_the_buffered_readers_lock(worker):
    """A thread blocked in ``sys.stdin.buffer.read`` keeps its lock and the
    interpreter aborts at exit after a stop by signal (exit code -6)."""
    source = WORKER.read_text()
    assert "sys.stdin" not in source.replace("``sys.stdin", "")
    assert "os.read(fd, 1024)" in source
    assert "os._exit(" in source


STOP_SIGNAL_SCRIPT = """
import importlib.util, os, signal, sys
spec = importlib.util.spec_from_file_location("w", sys.argv[1])
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)

def attempt(n):
    stop = w.Stop()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())  # as main() installs it
    seen = [0]
    def tracer(frame, event, arg):
        seen[0] += 1
        if seen[0] == n:  # SIGTERM to ourselves at the n-th step of the call
            os.kill(os.getpid(), signal.SIGTERM)
        return tracer
    sys.settrace(tracer)
    stop.set()
    sys.settrace(None)
    return stop.is_set()

assert all(attempt(n) for n in range(1, 60))
print("ok")
"""


def test_a_signal_that_arrives_anywhere_inside_a_set_cannot_deadlock_the_stop_flag(
    worker,
):
    """The handler of SIGTERM runs in the main thread, possibly inside another
    ``set``. A ``threading.Event`` takes a lock that is not reentrant there: the
    handler's own ``set`` would wait for ever for the lock its interrupted frame
    holds, and no camera would be stopped. A trace function raises the signal at
    every step of ``set`` in turn (a flag that hangs fails the timeout)."""
    import subprocess

    done = subprocess.run(
        [sys.executable, "-B", "-c", STOP_SIGNAL_SCRIPT, str(WORKER)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ok"


def test_the_stop_flag_has_no_lock(worker):
    assert not isinstance(worker.Stop(), threading.Event)
    assert not hasattr(worker.Stop(), "wait")


def test_the_stop_event_remembers_when_it_was_first_set(worker):
    stop = worker.Stop()
    assert stop.since is None and not stop.is_set()
    stop.set()
    first = stop.since
    assert first is not None and stop.is_set()
    time.sleep(0.01)
    stop.set()
    assert stop.since == first
