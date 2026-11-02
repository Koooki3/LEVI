"""The REAL ``integrations/camera_preview/worker.py`` (its ``main``, as a process
in a group of its own with a pipe on its standard input that stays open, the way
``levi/marvel0/preview.py`` starts it) over stand-ins for ``pyrealsense2``,
OpenCV and numpy (``camera_stubs/``, found through ``PYTHONPATH``). No camera,
device or port is touched, and no real library is imported.

What this file is for: a stop that LEVI starts (``Manager.stop``, the hand-back
``hold_off``, a bare SIGTERM) must end the worker with exit code 0 well before
the SIGKILL point, and the worker's own failure codes (busy 10, not found 11, no
frames 13) must reach the page as they are. The thread that reads the pipe once
made the interpreter abort at exit (exit code -6, "_enter_buffered_busy") on
every stop by signal and on every failure, and only the unit tests that call
``main`` in-process and the fake worker of the other files could not see it.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import scaled

from levi.marvel0 import preview

HERE = Path(__file__).resolve().parent
WORKER = HERE.parents[1] / "integrations" / "camera_preview" / "worker.py"
STUBS = HERE / "camera_stubs"
SERIALS = {"side": "111111111111", "wrist": "222222222222"}
SITE = SimpleNamespace(
    cameras=SimpleNamespace(view=SERIALS["side"], wrist=SERIALS["wrist"]),
    openpi_checkout="/nonexistent",
)


def wait_for(condition, seconds=15.0, what="the condition"):
    deadline = time.monotonic() + scaled(seconds)
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


class Rig:
    """A manager over the real worker file and the stand-in libraries."""

    def __init__(self, tmp_path, **stub):
        self.tmp = Path(tmp_path)
        self.out = self.tmp / "out"
        self.out.mkdir()
        self.mark = self.tmp / "mark"
        self.env = {
            "PATH": os.environ["PATH"],
            "PYTHONPATH": str(STUBS),
            "PYTHONDONTWRITEBYTECODE": "1",
            "STUB_RS_SERIALS": ",".join(SERIALS.values()),
            "STUB_RS_MARK": str(self.mark),
            **{k: str(v) for k, v in stub.items()},
        }
        self.spawned: list = []

        def command(site, out):
            argv = [
                sys.executable,
                "-B",
                str(WORKER),
                "--out",
                str(out),
                *[x for n, s in SERIALS.items() for x in ("--camera", f"{n}={s}")],
                "--max-fps",
                "15",
            ]
            return argv, self.env, str(self.tmp)

        def spawn(argv, **kw):
            proc = subprocess.Popen(argv, **kw)
            self.spawned.append(proc)
            return proc

        self.mgr = preview.Manager(
            preview.Host(
                spawn=spawn,
                command=command,
                directory=lambda: self.out,
                blockers=list,
                watch=lambda: (False, False),
                profile=lambda: "real",
                site_loader=lambda: SITE,
                children_state=self.tmp / "children",
            )
        )

    @property
    def proc(self):
        return self.spawned[-1]

    def log(self) -> str:
        return (self.out / preview.LOG_NAME).read_text()

    def on(self):
        self.mgr.start()
        wait_for(
            lambda: (self.mgr.tick(), self.mgr.view()["state"] == "on")[1],
            what="the preview to be on",
        )

    def failed(self) -> dict:
        self.mgr.start()
        wait_for(
            lambda: (self.mgr.tick(), self.mgr.view()["state"] == "failed")[1],
            what="the preview to fail",
        )
        return self.mgr.view()

    def close(self):
        self.mgr.stop("requested")


@pytest.fixture
def rig(tmp_path):
    made: list = []

    def build(**stub):
        r = Rig(tmp_path, **stub)
        made.append(r)
        return r

    yield build
    for r in made:
        r.close()


# --- a stop that LEVI starts ends the worker cleanly --------------------------------------


def _stop(r):
    assert r.mgr.stop("requested") is True


def _hand_back(r):
    r.mgr.hold_off()
    r.mgr.release_hold()


@pytest.mark.parametrize("how", [_stop, _hand_back], ids=["stop", "hand-back"])
@pytest.mark.parametrize("delay", [0, 0.5], ids=["instant stop", "0.5 s per camera"])
def test_a_stop_by_levi_ends_the_real_worker_with_exit_code_0_well_before_the_kill(
    rig, how, delay
):
    r = rig(STUB_RS_STOP_S=delay)
    r.on()
    proc = r.proc
    began = time.monotonic()
    how(r)
    elapsed = time.monotonic() - began
    assert proc.returncode == 0, r.log()
    assert elapsed < scaled(preview.GRACE_S / 2), elapsed
    assert r.mgr.view()["killed"] is False
    log = r.log()
    assert "Fatal Python error" not in log and "_enter_buffered_busy" not in log
    assert log.count("stopped in") == 2  # every camera's own stop, timed
    assert "cameras released" in log and "after the stop" in log


def test_a_bare_sigterm_with_the_pipe_still_open_is_a_clean_exit_too(rig):
    """What ``children.stop_owned`` and a killed service's ``reclaim`` send:
    SIGTERM, with LEVI's end of the pipe still open (or gone)."""
    r = rig()
    r.on()
    proc = r.proc
    assert proc.stdin is not None and not proc.stdin.closed
    os.killpg(proc.pid, signal.SIGTERM)
    wait_for(lambda: proc.poll() is not None, what="the exit")
    assert proc.returncode == 0, r.log()
    r.mgr.tick()


def test_a_stop_that_arrives_while_the_cameras_open_leaves_the_second_one_closed(rig):
    r = rig(STUB_RS_START_S=1.0)
    r.mgr.start()
    # (the first camera is being opened, so the worker has its handlers)
    wait_for(lambda: r.mark.exists() and r.mark.read_text().strip(), what="a start")
    began = time.monotonic()
    r.mgr.hold_off()
    r.mgr.release_hold()
    assert time.monotonic() - began < scaled(preview.GRACE_S / 2)
    assert r.proc.returncode == 0, r.log()
    assert r.mark.read_text().split() == [SERIALS["side"]]  # the wrist was not opened
    assert "left closed" in r.log()


# --- the worker's own failure codes reach the page as they are ----------------------------


def test_cameras_another_program_holds_are_cameras_busy_not_worker_failed(rig):
    r = rig(STUB_RS_BUSY=SERIALS["wrist"])
    view = r.failed()
    assert r.proc.returncode == 10, r.log()
    assert view["reason"]["code"] == "cameras_busy"
    assert "Fatal Python error" not in r.log()


def test_a_camera_that_is_not_connected_is_cameras_not_found(rig):
    r = rig(STUB_RS_SERIALS=SERIALS["side"])
    view = r.failed()
    assert r.proc.returncode == 11, r.log()
    assert view["reason"]["code"] == "cameras_not_found"


def test_cameras_that_send_no_picture_are_no_frames(rig):
    r = rig(STUB_CV2_FAIL=1)  # every picture fails to encode: the reader gives up
    view = r.failed()
    assert r.proc.returncode == 13, r.log()
    assert view["reason"]["code"] == "no_frames"


@pytest.mark.parametrize("serials", [SERIALS["side"], ""], ids=["one", "none"])
def test_the_failure_codes_do_not_depend_on_the_pipe_being_closed(rig, serials):
    """With the pipe open, as LEVI always has it, the worker returned -6."""
    r = rig(STUB_RS_SERIALS=serials)
    r.failed()
    assert r.proc.stdin is not None
    assert r.proc.returncode == 11
