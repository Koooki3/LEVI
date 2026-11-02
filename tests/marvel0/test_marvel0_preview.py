"""``levi/marvel0/preview.py``: the camera preview of the marvel0 page, without a
camera. The worker is ``fake_camera_worker.py`` (a real process in its own
group with a pipe on its standard input, no ``pyrealsense2``, no device) or, where
only the clock matters, a scripted process object; the supervisors the start
rules read are stubs. Nothing here opens a camera, a socket or a port, and the
real worker is never started.

Parts: what may start it (every refusal, unreadable = refuse), the lifecycle of
a real fake worker (frames, stop, failure codes, the SIGKILL after SIGTERM, the
record in ``levi/children.py``), the clock rules (idle, lifetime, stale pictures,
a client appearing, hand-back) on a scripted process, the command of the real
worker, and the words of every code in both catalogues."""

import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import scaled
from fastapi import HTTPException
from site_sample import make_openpi_tree, make_site

from levi import children
from levi.marvel0 import aeri_prep, aeri_run_api, deploy_api, preview, run_api
from levi.workspace import Workspace

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
FAKE = HERE / "fake_camera_worker.py"
WORKER = ROOT / "integrations" / "camera_preview" / "worker.py"
SITE = SimpleNamespace(
    cameras=SimpleNamespace(view="111111111111", wrist="222222222222"),
    openpi_checkout="/nonexistent",
)


# --- what may start it ------------------------------------------------------------------


class RunSup:
    def __init__(self, active=None, boom=False):
        self._active, self._boom = active, boom

    def adopt(self):
        if self._boom:
            raise OSError("record unreadable")

    def active(self):
        if self._boom:
            raise OSError("record unreadable")
        return self._active


class DeploySup:
    def __init__(self, status):
        self._status = status

    def status(self):
        if isinstance(self._status, Exception):
            raise self._status
        return self._status


def status(established=0, arm="ok", net="ok", **over):
    """The deploy document as far as the preview reads it."""
    body = {
        "state": "ready",
        "listener": {"established": established},
        "checks": [
            *([{"code": "arm_lock", "level": arm}] if arm else []),
            *([{"code": "net_table", "level": net}] if net else []),
        ],
    }
    return {**body, **over}


@pytest.fixture
def world(monkeypatch):
    """The three supervisors the rules read, all quiet; a test changes one."""
    w = SimpleNamespace(run=RunSup(), aeri=RunSup(), deploy=DeploySup(status()))
    monkeypatch.setattr(run_api, "supervisor", lambda: w.run)
    monkeypatch.setattr(aeri_run_api, "supervisor", lambda: w.aeri)
    monkeypatch.setattr(deploy_api, "supervisor", lambda: w.deploy)
    return w


def test_nothing_in_the_way_means_the_cameras_are_free(world):
    assert preview.blockers() == []


@pytest.mark.parametrize(
    "change, expected",
    [
        (
            lambda w: setattr(w, "run", RunSup(active={"kind": "marvel0-client"})),
            "run_active",
        ),
        (lambda w: setattr(w, "aeri", RunSup(active={"kind": "aeri"})), "aeri_active"),
        (
            lambda w: setattr(w, "deploy", DeploySup(status(established=1))),
            "client_connected",
        ),
        (lambda w: setattr(w, "deploy", DeploySup(status(arm="refuse"))), "arm_in_use"),
    ],
    ids=["hosted run", "AERI run", "client connected", "arm lock held"],
)
def test_each_reason_the_cameras_may_be_in_use_is_reported_alone(
    world, change, expected
):
    change(world)
    assert preview.blockers() == [expected]


@pytest.mark.parametrize(
    "change",
    [
        lambda w: setattr(w, "run", RunSup(boom=True)),
        lambda w: setattr(w, "aeri", RunSup(boom=True)),
        lambda w: setattr(w, "deploy", DeploySup(RuntimeError("status"))),
        lambda w: setattr(w, "deploy", DeploySup(None)),
        lambda w: setattr(w, "deploy", DeploySup(status(established=None))),
        lambda w: setattr(w, "deploy", DeploySup(status(established=True))),
        lambda w: setattr(w, "deploy", DeploySup(status(established="0"))),
        lambda w: setattr(w, "deploy", DeploySup(status(arm="unknown"))),
        lambda w: setattr(w, "deploy", DeploySup(status(arm=None))),
        lambda w: setattr(w, "deploy", DeploySup(status(listener=None))),
        # the deploy counts 0 connections when it cannot read the socket table
        lambda w: setattr(w, "deploy", DeploySup(status(state="unknown"))),
        lambda w: setattr(w, "deploy", DeploySup(status(state=None))),
        lambda w: setattr(w, "deploy", DeploySup(status(net="unknown"))),
        lambda w: setattr(w, "deploy", DeploySup(status(net="refuse"))),
        lambda w: setattr(w, "deploy", DeploySup(status(net=None))),
    ],
    ids=[
        "run supervisor raises",
        "AERI supervisor raises",
        "status raises",
        "status is no document",
        "no connection count",
        "a bool is no count",
        "a string is no count",
        "arm lock unknown",
        "no arm lock row",
        "no listener",
        "deploy state unknown",
        "deploy state missing",
        "socket table unreadable",
        "socket table row not ok",
        "no socket table row",
    ],
)
def test_what_cannot_be_read_counts_as_in_use(world, change):
    change(world)
    assert "state_unreadable" in preview.blockers()


def test_an_unreadable_socket_table_is_not_zero_clients(world):
    """The deploy status then says 0 established; the preview must not take
    that for "nobody is connected"."""
    world.deploy = DeploySup(status(established=0, state="unknown", net="unknown"))
    assert preview.blockers() == ["state_unreadable"]


def test_a_server_that_is_starting_is_no_reason_by_itself(world):
    world.deploy = DeploySup(status(state="starting"))
    assert preview.blockers() == []


def test_the_watcher_reads_what_cannot_be_read_as_unreadable_not_as_free(
    world, monkeypatch
):
    world.run = RunSup(boom=True)
    assert preview.blockers_safely() == ["state_unreadable"]

    def boom():
        raise RuntimeError("anything")

    monkeypatch.setattr(preview, "blockers", boom)
    assert preview.blockers_safely() == ["state_unreadable"]
    assert preview.watch() == (False, True)


def test_the_watcher_keeps_the_site_files_412_apart(world):
    world.deploy = DeploySup(HTTPException(412, {"code": "site_missing"}))
    assert preview.blockers_safely() == []
    world.deploy = DeploySup(HTTPException(500, {"code": "other"}))
    assert preview.blockers_safely() == ["state_unreadable"]


@pytest.mark.parametrize(
    "found, expected",
    [
        (["run_active"], (True, False)),
        (["aeri_active"], (True, False)),
        (["client_connected"], (True, False)),
        (["arm_in_use"], (True, False)),
        (["state_unreadable"], (False, True)),
        (["state_unreadable", "arm_in_use"], (True, True)),
        ([], (False, False)),
    ],
)
def test_the_watcher_stops_for_everything_a_start_refuses_for(
    monkeypatch, found, expected
):
    monkeypatch.setattr(preview, "blockers", lambda: list(found))
    assert preview.watch() == expected
    assert preview.client_signal() is expected[0]


def test_the_product_profile_is_read_as_the_run_reads_it(monkeypatch):
    from levi.marvel0 import stack

    unit = f"0::/user.slice/user-1000.slice/user@1000.service/app.slice/{stack.PRODUCT_UNIT}"
    monkeypatch.setattr(stack, "_read_self_cgroup", lambda: unit)
    monkeypatch.setattr(stack, "_in_main_checkout", lambda: True)
    assert preview.product_profile() == "real"
    monkeypatch.setattr(stack, "_in_main_checkout", lambda: False)
    assert preview.product_profile() == "fake"
    monkeypatch.setattr(stack, "_in_main_checkout", lambda: True)
    monkeypatch.setattr(stack, "_read_self_cgroup", lambda: "0::/user.slice/other")
    assert preview.product_profile() == "fake"
    monkeypatch.setattr(stack, "_read_self_cgroup", lambda: None)
    assert preview.product_profile() == "fake"


def test_the_site_file_is_the_one_refusal_that_is_not_ours(world):
    world.deploy = DeploySup(HTTPException(412, {"code": "site_missing"}))
    with pytest.raises(HTTPException) as caught:
        preview.blockers()
    assert caught.value.status_code == 412


def test_all_reasons_together_come_in_a_fixed_order(world):
    world.run = RunSup(active={"kind": "x"})
    world.aeri = RunSup(active={"kind": "y"})
    world.deploy = DeploySup(status(established=2, arm="refuse"))
    assert preview.blockers() == [
        "run_active",
        "aeri_active",
        "client_connected",
        "arm_in_use",
    ]


# --- a scripted process: the clock rules ----------------------------------------------


class Clock:
    """Monotonic time moved by hand; ``sleep`` moves it."""

    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds

    def advance(self, seconds):
        self.t += seconds


class Scripted:
    """A worker process the test controls."""

    count = 0

    def __init__(self, ends_on=signal.SIGTERM, events=None):
        Scripted.count += 1
        self.pid = 3_000_000 + Scripted.count
        self.code = None
        self.ends_on = ends_on
        log = events if events is not None else []
        self.stdin = SimpleNamespace(close=lambda: log.append("stdin_closed"))

    def poll(self):
        return self.code


class Rig:
    """A manager over scripted processes and a folder of pictures."""

    def __init__(self, tmp_path, ends_on=signal.SIGTERM):
        self.tmp = Path(tmp_path)
        self.out = self.tmp / "out"
        self.out.mkdir()
        self.clock = Clock()
        self.procs: list[Scripted] = []
        self.signals: list = []
        self.blocked: list = []
        self.client = False
        self.unreadable = False
        self.real = True
        self.events: list = []  # the stdin close and the signals, in order
        self.ends_on = ends_on
        self.host = preview.Host(
            spawn=self.spawn,
            command=lambda site, out: (["worker", "--out", str(out)], {}, str(out)),
            killpg=self.killpg,
            directory=lambda: self.out,
            blockers=lambda: list(self.blocked),
            watch=lambda: (self.client, self.unreadable),
            profile=lambda: "real" if self.real else "fake",
            site_loader=lambda: SITE,
            monotonic=self.clock.monotonic,
            sleep=self.clock.sleep,
            children_state=self.tmp / "children",
        )
        self.mgr = preview.Manager(self.host)

    def spawn(self, argv, **kw):
        proc = Scripted(self.ends_on, self.events)
        self.procs.append(proc)
        self.spawn_kw = kw
        return proc

    def killpg(self, pid, number):
        self.signals.append((pid, number))
        self.events.append(number)
        proc = next(p for p in self.procs if p.pid == pid)
        if proc.ends_on is not None and number in (proc.ends_on, signal.SIGKILL):
            proc.code = 0 if number == signal.SIGTERM else -9

    def pictures(self, age=0.0):
        for camera in preview.CAMERAS:
            path = self.out / f"{camera}.jpg"
            path.write_bytes(b"\xff\xd8\xff\xe0" + camera.encode() + b"\xff\xd9")
            stamp = time.time() - age
            os.utime(path, (stamp, stamp))

    def running(self):
        """Started, with pictures, and ticked into ``on``."""
        self.mgr.start()
        self.pictures()
        self.mgr.tick()
        assert self.mgr.view()["state"] == "on"


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_start_spawns_one_worker_in_a_group_of_its_own_with_a_pipe(rig):
    view = rig.mgr.start()
    assert view["state"] == "starting" and len(rig.procs) == 1
    kw = rig.spawn_kw
    assert kw["start_new_session"] is True and kw["stdin"] == subprocess.PIPE
    assert kw["stdout"] == subprocess.DEVNULL and kw["close_fds"] is True
    assert kw["cwd"] == str(rig.out)


def test_asking_again_while_it_runs_starts_nothing_more(rig):
    rig.mgr.start()
    assert rig.mgr.start()["state"] == "starting"
    assert len(rig.procs) == 1


def test_it_is_on_once_both_pictures_are_there(rig):
    rig.mgr.start()
    rig.pictures()
    (rig.out / "wrist.jpg").unlink()
    rig.mgr.tick()
    assert rig.mgr.view()["state"] == "starting"
    rig.pictures()
    rig.mgr.tick()
    view = rig.mgr.view()
    assert view["state"] == "on" and view["may_start"] is False
    assert set(view["frames"]) == {"side", "wrist"}
    assert all(f["age_ms"] is not None for f in view["frames"].values())
    assert view["limits"] == {"idle_s": 20.0, "max_s": 600.0}


def test_no_picture_in_time_is_a_failure_and_the_worker_is_stopped(rig):
    rig.mgr.start()
    rig.clock.advance(preview.START_TIMEOUT_S + 1)
    rig.mgr.tick()
    view = rig.mgr.view()
    assert view["state"] == "failed" and view["reason"]["code"] == "no_frames"
    assert rig.procs[0].code is not None
    assert rig.signals[0] == (rig.procs[0].pid, signal.SIGTERM)


def test_idle_for_twenty_seconds_without_a_frame_request_stops_it(rig):
    rig.running()
    rig.clock.advance(preview.IDLE_S - 1)
    rig.mgr.tick()
    assert rig.mgr.view()["state"] == "on"
    rig.clock.advance(2)
    rig.mgr.tick()
    view = rig.mgr.view()
    assert view["state"] == "off" and view["stopped_by"] == "idle"
    assert rig.procs[0].code == 0


def test_a_frame_request_is_a_sign_of_life(rig):
    rig.running()
    for _ in range(5):
        rig.clock.advance(preview.IDLE_S - 2)
        rig.mgr.frame("side")
        rig.mgr.tick()
        rig.pictures()
    assert rig.mgr.view()["state"] == "on"
    rig.clock.advance(5)
    assert rig.mgr.view()["remaining"]["idle_s"] == pytest.approx(15.0, abs=0.2)


def test_the_longest_a_preview_lasts_is_ten_minutes(rig):
    rig.running()
    # asked for all the time, so the idle rule never fires
    while rig.clock.t - rig.mgr._started < preview.MAX_S - 10:
        rig.clock.advance(10)
        rig.mgr.frame("wrist")
        rig.pictures()
        rig.mgr.tick()
        assert rig.mgr.view()["state"] == "on"
    rig.clock.advance(11)
    rig.mgr.frame("wrist")
    rig.pictures()
    rig.mgr.tick()
    view = rig.mgr.view()
    assert view["state"] == "off" and view["stopped_by"] == "max_lifetime"
    assert rig.procs[0].code is not None


def test_pictures_that_stop_coming_are_a_failure(rig):
    rig.running()
    rig.clock.advance(preview.STALE_S - 1)
    rig.mgr.frame("side")
    rig.pictures(age=preview.STALE_S + 5)  # old files: the worker is not writing
    rig.mgr.tick()
    assert rig.mgr.view()["state"] == "on"
    rig.clock.advance(2)
    rig.mgr.frame("side")
    rig.mgr.tick()
    view = rig.mgr.view()
    assert view["state"] == "failed" and view["reason"]["code"] == "no_frames"


def test_a_client_that_appears_stops_it(rig):
    rig.running()
    rig.client = True
    rig.clock.advance(preview.PROBE_S + 0.1)
    rig.mgr.frame("side")
    rig.mgr.tick()
    view = rig.mgr.view()
    assert view["state"] == "off" and view["stopped_by"] == "client_start"


def test_a_client_that_appears_while_it_starts_stops_it_at_the_first_probe(rig):
    rig.mgr.start()  # (no picture yet: the worker is still opening the cameras)
    rig.client = True
    rig.clock.advance(preview.PROBE_S - 0.1)
    rig.mgr.tick()
    assert rig.mgr.view()["state"] == "starting" and rig.signals == []
    rig.clock.advance(0.2)
    rig.mgr.tick()
    view = rig.mgr.view()
    assert view["state"] == "off" and view["stopped_by"] == "client_start"
    assert rig.signals[0] == (rig.procs[0].pid, signal.SIGTERM)


@pytest.mark.parametrize("phase", ["starting", "on"])
def test_the_arm_lock_stops_a_preview_through_the_real_watch(rig, monkeypatch, phase):
    """``watch`` over ``blockers``: a terminal D takes the arm lock before it
    connects to anything."""
    rig.host.watch = preview.watch
    monkeypatch.setattr(preview, "blockers", list)
    if phase == "on":
        rig.running()
    else:
        rig.mgr.start()
    rig.clock.advance(preview.PROBE_S + 0.1)
    monkeypatch.setattr(preview, "blockers", lambda: ["arm_in_use"])
    rig.mgr.tick()
    view = rig.mgr.view()
    assert view["state"] == "off" and view["stopped_by"] == "client_start"


def test_a_state_that_cannot_be_read_twice_in_a_row_stops_it_once_does_not(rig):
    rig.running()
    rig.unreadable = True
    rig.clock.advance(preview.PROBE_S + 0.1)
    rig.mgr.frame("side")
    rig.mgr.tick()
    assert rig.mgr.view()["state"] == "on"  # one probe: it may be a hiccup
    rig.unreadable = False
    rig.clock.advance(preview.PROBE_S + 0.1)
    rig.mgr.frame("side")
    rig.pictures()
    rig.mgr.tick()
    rig.unreadable = True  # a read that works in between starts the count again
    rig.clock.advance(preview.PROBE_S + 0.1)
    rig.mgr.frame("side")
    rig.pictures()
    rig.mgr.tick()
    assert rig.mgr.view()["state"] == "on"
    rig.clock.advance(preview.PROBE_S + 0.1)
    rig.mgr.frame("side")
    rig.pictures()
    rig.mgr.tick()
    view = rig.mgr.view()
    assert view["state"] == "off" and view["stopped_by"] == "client_start"


def test_the_stop_button_stops_it_and_removes_the_pictures(rig):
    rig.running()
    assert rig.mgr.stop("requested") is True
    view = rig.mgr.view()
    assert view["state"] == "off" and view["stopped_by"] == "requested"
    assert not list(rig.out.glob("*.jpg"))
    with pytest.raises(preview.NoFrame) as caught:
        rig.mgr.frame("side")
    assert caught.value.reason == "preview_off"
    assert rig.mgr.stop("requested") is True  # idempotent


def test_a_stop_that_the_worker_does_not_obey_is_not_a_success(tmp_path):
    rig = Rig(tmp_path, ends_on=None)  # ignores SIGTERM and SIGKILL alike
    rig.running()
    began = rig.clock.t
    assert rig.mgr.stop("requested") is False
    assert rig.clock.t - began == pytest.approx(preview.HANDBACK_S, abs=0.1)
    numbers = [n for _, n in rig.signals]
    assert numbers[0] == signal.SIGTERM and signal.SIGKILL in numbers
    assert rig.mgr.view()["state"] == "stopping"
    with pytest.raises(preview.Refused) as caught:
        rig.mgr.start()
    assert caught.value.code == "preview_stopping"


def test_a_stop_closes_the_pipe_first_then_asks_and_never_kills_a_worker_that_leaves(
    rig,
):
    rig.running()
    began = rig.clock.t
    assert rig.mgr.stop("requested") is True
    assert rig.events[:2] == ["stdin_closed", signal.SIGTERM]  # (_finish closes again)
    assert rig.clock.t == began  # (it ended at once)
    assert rig.mgr.view()["killed"] is False


def test_the_kill_comes_only_after_the_grace_and_is_logged_shown_and_settled(
    tmp_path, caplog
):
    rig = Rig(tmp_path, ends_on=signal.SIGKILL)  # ignores SIGTERM, dies of KILL
    rig.running()
    assert preview.GRACE_S == 5.0 and preview.HANDBACK_S == 6.0
    began = rig.clock.t
    with caplog.at_level("WARNING", logger="levi.marvel0.preview"):
        assert rig.mgr.stop("requested") is True
    assert rig.events[:3] == ["stdin_closed", signal.SIGTERM, signal.SIGKILL]
    # SIGKILL after the grace (not after two thirds of the time), then a
    # half second for the kernel to let the devices go
    assert rig.clock.t - began == pytest.approx(
        preview.GRACE_S + preview.SETTLE_S, abs=0.1
    )
    assert "SIGKILL" in caplog.text
    view = rig.mgr.view()
    assert view["state"] == "off" and view["killed"] is True
    rig.mgr.start()  # the next preview starts clean
    assert rig.mgr.view()["killed"] is False


def test_a_shorter_time_keeps_a_second_for_the_kill(tmp_path):
    rig = Rig(tmp_path, ends_on=signal.SIGKILL)
    rig.running()
    began = rig.clock.t
    assert rig.mgr.stop("requested", timeout=3.0) is True
    kill_at = next(i for i, n in enumerate(rig.events) if n == signal.SIGKILL)
    assert kill_at == 2
    assert rig.clock.t - began == pytest.approx(2.0 + preview.SETTLE_S, abs=0.1)


# --- handing the cameras to a client -------------------------------------------------


def test_handback_stops_the_preview_and_waits_for_the_exit(rig):
    rig.running()
    rig.mgr.hold_off()
    assert rig.procs[0].code is not None  # the process is gone, not just asked
    view = rig.mgr.view()  # off, and not offered while the client is starting
    assert view["state"] == "refused" and view["reason"]["code"] == "client_starting"
    assert view["stopped_by"] == "client_start"
    rig.mgr.release_hold()
    assert rig.mgr.view()["state"] == "off"


def test_no_preview_starts_while_a_client_is_being_started(rig):
    rig.mgr.hold_off()
    with pytest.raises(preview.Refused) as caught:
        rig.mgr.start()
    assert caught.value.code == "client_starting"
    view = rig.mgr.view()
    assert view["state"] == "refused" and view["reason"]["code"] == "client_starting"
    rig.mgr.release_hold()
    assert rig.mgr.start()["state"] == "starting"
    assert len(rig.procs) == 1


def test_holds_nest(rig):
    rig.mgr.hold_off()
    rig.mgr.hold_off()
    rig.mgr.release_hold()
    with pytest.raises(preview.Refused):
        rig.mgr.start()
    rig.mgr.release_hold()
    rig.mgr.start()


def test_a_worker_that_will_not_exit_fails_the_handback_and_keeps_no_hold(tmp_path):
    rig = Rig(tmp_path, ends_on=None)
    rig.running()
    with pytest.raises(preview.HandbackFailed):
        rig.mgr.hold_off(timeout=1.5)
    assert rig.mgr._held == 0


def test_handback_with_nothing_running_is_free_and_instant(rig):
    began = rig.clock.t
    rig.mgr.hold_off()
    rig.mgr.release_hold()
    assert rig.clock.t == began and rig.signals == []


def test_the_context_manager_holds_for_its_block_only(rig, monkeypatch):
    monkeypatch.setattr(preview, "manager", lambda: rig.mgr)
    rig.running()
    with preview.handback():
        assert rig.procs[0].code is not None
        with pytest.raises(preview.Refused):
            rig.mgr.start()
    assert rig.mgr._held == 0
    rig.mgr.start()


def test_the_context_manager_releases_the_hold_when_the_block_fails(rig, monkeypatch):
    monkeypatch.setattr(preview, "manager", lambda: rig.mgr)
    with pytest.raises(RuntimeError), preview.handback():
        raise RuntimeError("the launch failed")
    assert rig.mgr._held == 0


def test_the_service_stop_lets_the_cameras_go(rig, monkeypatch):
    monkeypatch.setattr(preview, "_MANAGER", [rig.mgr])
    rig.running()
    preview.shutdown()
    assert rig.procs[0].code is not None
    assert rig.mgr.view()["stopped_by"] == "service"


def test_shutdown_without_a_preview_does_nothing(monkeypatch):
    monkeypatch.setattr(preview, "_MANAGER", [])
    preview.shutdown()  # (and creates no manager)
    assert preview._MANAGER == []


# --- the view and the start rules ---------------------------------------------------------


@pytest.mark.parametrize("why", ["run_active", "aeri_active", "client_connected"])
def test_every_blocker_refuses_the_start_and_starts_nothing(rig, why):
    rig.blocked = [why, "arm_in_use"]
    with pytest.raises(preview.Refused) as caught:
        rig.mgr.start()
    assert caught.value.code == why and caught.value.status == 409
    assert caught.value.extra["reasons"] == [why, "arm_in_use"]
    assert rig.procs == []
    view = rig.mgr.view()
    assert view["state"] == "refused" and view["may_start"] is False
    assert view["reason"]["code"] == why and view["blocked_by"] == [why, "arm_in_use"]


def test_the_view_says_when_it_may_start(rig):
    view = rig.mgr.view()
    assert view["state"] == "off" and view["may_start"] is True
    assert view["reason"] is None and view["remaining"] is None
    assert view["schema"] == preview.SCHEMA


def test_a_blocker_now_is_shown_before_an_old_failure(rig):
    rig.mgr.start()
    rig.procs[0].code = 10  # cameras_busy
    rig.mgr.tick()
    assert rig.mgr.view()["reason"]["code"] == "cameras_busy"
    rig.blocked = ["aeri_active"]
    view = rig.mgr.view()
    assert view["state"] == "refused" and view["may_start"] is False
    assert view["reason"]["code"] == "aeri_active"
    assert view["blocked_by"] == ["aeri_active"]
    rig.blocked = []
    view = rig.mgr.view()  # gone: the failure is what it was
    assert view["state"] == "failed" and view["reason"]["code"] == "cameras_busy"
    assert view["may_start"] is True


def test_a_failure_stays_in_the_view_until_the_next_start(rig):
    rig.mgr.start()
    rig.procs[0].code = 10
    rig.mgr.tick()
    view = rig.mgr.view()
    assert view["state"] == "failed" and view["reason"]["code"] == "cameras_busy"
    assert view["may_start"] is True
    rig.mgr.start()
    assert rig.mgr.view()["state"] == "starting"
    assert len(rig.procs) == 2


def test_a_worker_that_cannot_be_spawned_is_a_failure_not_an_error(rig):
    def refuse(argv, **kw):
        raise FileNotFoundError("uv")

    rig.host.spawn = refuse
    view = rig.mgr.start()
    assert view["state"] == "failed"
    assert view["reason"]["code"] == "worker_unavailable"


def test_no_text_names_a_path_or_a_serial(rig):
    rig.mgr.start()
    rig.procs[0].code = 11
    rig.mgr.tick()
    text = json.dumps(rig.mgr.view())
    for forbidden in (str(rig.tmp), SITE.cameras.view, SITE.cameras.wrist):
        assert forbidden not in text
    for table in (preview.REFUSALS, preview.FAILURES):
        assert "/" not in json.dumps(table)


# --- the product only --------------------------------------------------------------------


def test_only_the_product_may_open_the_real_cameras(rig):
    rig.real = False
    with pytest.raises(preview.Refused) as caught:
        rig.mgr.start()
    assert caught.value.code == "not_product" and caught.value.status == 409
    assert rig.procs == []
    view = rig.mgr.view()
    assert view["state"] == "refused" and view["may_start"] is False
    assert view["reason"]["code"] == "not_product"
    assert view["blocked_by"] == ["not_product"]
    rig.blocked = ["run_active"]  # (shown first, whatever else is in the way)
    assert rig.mgr.view()["blocked_by"][0] == "not_product"
    rig.real = True
    rig.blocked = []
    assert rig.mgr.start()["state"] == "starting" and len(rig.procs) == 1


def test_the_profile_is_asked_before_the_blockers_are_read(rig):
    rig.real = False
    rig.host.blockers = lambda: pytest.fail("the blockers were read")
    with pytest.raises(preview.Refused):
        rig.mgr.start()


# --- a client start that runs while the blockers are being read -----------------------------


def test_a_hand_back_that_starts_and_ends_while_the_blockers_are_read_refuses_the_start(
    rig,
):
    def read():
        rig.mgr.hold_off()  # the client's start: took the cameras back ...
        rig.mgr.release_hold()  # ... and launched, so its record exists now
        return []  # (this read began before the record did)

    rig.host.blockers = read
    with pytest.raises(preview.Refused) as caught:
        rig.mgr.start()
    assert caught.value.code == "client_starting"
    assert rig.procs == []
    rig.host.blockers = list
    assert rig.mgr.start()["state"] == "starting"  # asked again, it is judged afresh


def test_a_hold_taken_while_the_blockers_are_read_and_kept_refuses_the_start(rig):
    rig.host.blockers = lambda: rig.mgr.hold_off() or []
    with pytest.raises(preview.Refused) as caught:
        rig.mgr.start()
    assert caught.value.code == "client_starting" and rig.procs == []
    rig.mgr.release_hold()


def test_a_start_while_a_hold_is_on_does_not_even_read_the_blockers(rig):
    rig.mgr.hold_off()
    rig.host.blockers = lambda: pytest.fail("the blockers were read")
    with pytest.raises(preview.Refused) as caught:
        rig.mgr.start()
    assert caught.value.code == "client_starting"
    rig.mgr.release_hold()


def test_the_blockers_are_not_read_under_the_lock(rig):
    """A cold deploy status takes seconds: a hand-back must not wait for it."""
    import threading

    got: list = []

    def read():
        def other():  # (an RLock does not show against the thread that holds it)
            got.append(rig.mgr._mutex.acquire(timeout=1.0))
            if got[-1]:
                rig.mgr._mutex.release()

        thread = threading.Thread(target=other)
        thread.start()
        thread.join()
        return []

    rig.host.blockers = read
    rig.mgr.start()
    assert got == [True]


# --- the identity recorded in levi/children.py ----------------------------------------------


def test_the_recorded_identity_follows_the_leader_when_nice_execs_the_next_program(
    tmp_path,
):
    """``nice`` execs ``uv``: the identity read right after ``Popen`` can be
    ``nice``'s. A worker that never reached ``on`` must still be found by
    ``reclaim`` and ``stop_owned``."""
    state = tmp_path / "children"
    started = tmp_path / "started"
    out = tmp_path / "out"
    out.mkdir()
    fake = tmp_path / "fake.py"
    fake.write_text(
        f"import time\nopen({str(started)!r}, 'w').close()\ntime.sleep(60)\n"
    )

    def command(site, folder):
        argv = ["nice", "-n", "19", sys.executable, "-B", str(fake)]
        return argv, {"PATH": os.environ["PATH"]}, str(tmp_path)

    mgr = preview.Manager(
        preview.Host(
            command=command,
            directory=lambda: out,
            watch=lambda: (False, False),
            profile=lambda: "real",
            blockers=list,
            site_loader=lambda: SITE,
            children_state=state,
        )
    )
    mgr.start()
    proc = mgr._proc
    try:
        wait_for(lambda: started.exists(), what="the program after nice")
        mgr.tick()  # (still starting: no picture)
        assert mgr.view()["state"] == "starting"
        rows = [r for r in children.listed(state=state) if r["kind"] == preview.KIND]
        assert len(rows) == 1 and rows[0]["running"]
        assert rows[0]["identity"] == children.identity(proc.pid)
        assert Path(rows[0]["identity"]["executable"]).name != "nice"
        # so a reclaim (the owner is gone) or the service stop finds the group
        # and signals it, though no picture ever came
        assert children.terminate(rows[0], 2.0) is True
        wait_for(lambda: proc.poll() is not None, what="the worker to be signalled")
    finally:
        mgr.stop("requested", timeout=3.0)
    assert proc.poll() is not None


# --- frames ------------------------------------------------------------------------------


def test_a_frame_is_the_newest_file_with_its_age(rig):
    rig.running()
    (rig.out / "side.jpg").write_bytes(b"\xff\xd8\xff\xe0newest\xff\xd9")
    old = time.time() - 2.5
    os.utime(rig.out / "side.jpg", (old, old))
    found = rig.mgr.frame("side")
    assert found.jpeg == b"\xff\xd8\xff\xe0newest\xff\xd9"
    assert found.age_s == pytest.approx(2.5, abs=0.5)


@pytest.mark.parametrize(
    "content",
    [b"", b"not a picture", b"\xff\xd8\xff\xe0 cut off", b"x\xff\xd8\xff\xe0\xff\xd9"],
    ids=["empty", "text", "no end marker", "no start marker"],
)
def test_a_file_that_is_no_whole_picture_is_no_frame(rig, content):
    rig.running()
    (rig.out / "side.jpg").write_bytes(content)
    with pytest.raises(preview.NoFrame) as caught:
        rig.mgr.frame("side")
    assert caught.value.reason == "no_frame"


def test_a_link_or_a_folder_is_no_frame(rig):
    rig.running()
    (rig.out / "side.jpg").unlink()
    (rig.out / "elsewhere").write_bytes(b"\xff\xd8\xff\xe0x\xff\xd9")
    (rig.out / "side.jpg").symlink_to(rig.out / "elsewhere")
    (rig.out / "wrist.jpg").unlink()
    (rig.out / "wrist.jpg").mkdir()
    for camera in preview.CAMERAS:
        with pytest.raises(preview.NoFrame) as caught:
            rig.mgr.frame(camera)
        assert caught.value.reason == "no_frame"


def test_while_starting_a_missing_picture_is_no_frame_not_off(rig):
    rig.mgr.start()
    with pytest.raises(preview.NoFrame) as caught:
        rig.mgr.frame("wrist")
    assert caught.value.reason == "no_frame"


def test_with_no_preview_there_is_no_picture_to_give(rig):
    with pytest.raises(preview.NoFrame) as caught:
        rig.mgr.frame("side")
    assert caught.value.reason == "preview_off"


def test_a_leftover_picture_of_an_earlier_preview_is_not_served(rig):
    rig.pictures()  # from a worker that ran before
    rig.mgr.start()
    assert not list(rig.out.glob("*.jpg"))
    with pytest.raises(preview.NoFrame):
        rig.mgr.frame("side")


# --- a real (fake) worker process ----------------------------------------------------------


def wait_for(condition, seconds=10.0, what="the condition"):
    deadline = time.monotonic() + scaled(seconds)
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


class Real:
    """A manager over ``fake_camera_worker.py`` as a real process."""

    def __init__(self, tmp_path, mode="ok"):
        self.tmp = Path(tmp_path)
        self.out = self.tmp / "out"
        self.out.mkdir()
        self.state = self.tmp / "children"
        self.spawned = 0
        self.mode = mode
        env = {"PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1"}

        def command(site, out):
            argv = [sys.executable, str(FAKE), "--out", str(out), "--mode", self.mode]
            return argv, env, str(self.tmp)

        def spawn(argv, **kw):
            self.spawned += 1
            return subprocess.Popen(argv, **kw)

        self.mgr = preview.Manager(
            preview.Host(
                spawn=spawn,
                command=command,
                directory=lambda: self.out,
                blockers=list,
                watch=lambda: (False, False),
                profile=lambda: "real",
                site_loader=lambda: SITE,
                children_state=self.state,
            )
        )

    def rows(self):
        return [
            r for r in children.listed(state=self.state) if r["kind"] == preview.KIND
        ]

    def on(self):
        self.mgr.start()
        wait_for(
            lambda: (self.mgr.tick(), self.mgr.view()["state"] == "on")[1], what="on"
        )

    def close(self):
        self.mgr.stop("requested")


@pytest.fixture
def real(tmp_path):
    made = []

    def build(mode="ok"):
        r = Real(tmp_path, mode)
        made.append(r)
        return r

    yield build
    for r in made:
        r.close()


def test_a_real_worker_goes_through_the_whole_lifecycle(real):
    r = real()
    view = r.mgr.start()
    assert view["state"] == "starting"
    proc = r.mgr._proc
    rows = r.rows()
    assert len(rows) == 1 and rows[0]["pid"] == proc.pid and rows[0]["running"]
    assert rows[0]["owner"]["pid"] == os.getpid()
    wait_for(lambda: (r.mgr.tick(), r.mgr.view()["state"] == "on")[1], what="on")
    side, wrist = r.mgr.frame("side"), r.mgr.frame("wrist")
    assert b"side" in side.jpeg and b"wrist" in wrist.jpeg
    assert side.jpeg.startswith(b"\xff\xd8\xff") and side.jpeg.endswith(b"\xff\xd9")
    first = side.jpeg
    wait_for(lambda: r.mgr.frame("side").jpeg != first, what="a newer picture")
    began = time.monotonic()
    assert r.mgr.stop("requested") is True
    assert proc.poll() is not None  # exited, which is what releases the devices
    # a clean exit, not the abort of a thread still reading standard input, and
    # nowhere near the SIGKILL point
    assert proc.returncode == 0
    assert time.monotonic() - began < scaled(preview.GRACE_S / 2)
    assert r.mgr.view()["killed"] is False
    assert r.rows() == []  # untracked
    view = r.mgr.view()
    assert view["state"] == "off" and view["stopped_by"] == "requested"
    assert not list(r.out.glob("*.jpg"))


def test_a_worker_is_untracked_when_it_exits_by_itself(real):
    r = real("crash")
    r.mgr.start()
    wait_for(
        lambda: (r.mgr.tick(), r.mgr.view()["state"] == "failed")[1], what="failed"
    )
    assert r.rows() == []
    assert r.mgr.view()["reason"]["code"] == "worker_failed"  # exit 3


@pytest.mark.parametrize(
    "mode, code",
    [("busy", "cameras_busy"), ("notfound", "cameras_not_found")],
)
def test_the_worker_exit_code_becomes_a_reason_and_nothing_retries(real, mode, code):
    r = real(mode)
    r.mgr.start()
    wait_for(
        lambda: (r.mgr.tick(), r.mgr.view()["state"] == "failed")[1], what="failed"
    )
    view = r.mgr.view()
    assert view["reason"]["code"] == code
    assert view["reason"]["text"] == preview.FAILURES[code]
    time.sleep(0.5)  # a retry loop would show here
    r.mgr.tick()
    assert r.spawned == 1


def test_a_worker_that_ignores_sigterm_is_killed(real):
    r = real("stubborn")
    r.on()
    proc = r.mgr._proc
    began = time.monotonic()
    assert r.mgr.stop("requested", timeout=1.5) is True
    assert proc.returncode == -signal.SIGKILL
    assert time.monotonic() - began < scaled(5.0)
    assert r.rows() == []


def test_the_end_of_the_pipe_ends_the_worker_so_no_service_means_no_cameras(real):
    r = real()
    r.on()
    proc = r.mgr._proc
    proc.stdin.close()  # what the death of the service does
    wait_for(lambda: proc.poll() is not None, what="exit")
    assert proc.returncode == 0
    r.mgr.tick()


def test_the_service_stop_signals_the_recorded_group(real):
    r = real()
    r.on()
    proc = r.mgr._proc
    stopped = children.stop_owned(2.0, state=r.state)
    assert [row["pid"] for row in stopped] == [proc.pid]
    wait_for(lambda: proc.poll() is not None, what="exit")
    r.mgr.tick()
    assert r.rows() == []


def test_handback_leaves_a_real_worker_exited(real):
    r = real()
    r.on()
    proc = r.mgr._proc
    began = time.monotonic()
    r.mgr.hold_off()
    assert proc.poll() is not None
    assert proc.returncode == 0 and r.mgr.view()["killed"] is False
    assert time.monotonic() - began < scaled(preview.GRACE_S / 2)
    r.mgr.release_hold()


def test_a_real_start_is_refused_while_a_blocker_holds(real):
    r = real()
    r.mgr.host.blockers = lambda: ["arm_in_use"]
    with pytest.raises(preview.Refused):
        r.mgr.start()
    assert r.spawned == 0


# --- the command of the real worker --------------------------------------------------------


def test_the_command_runs_the_worker_niced_with_the_overlay_and_the_sites_serials(
    tmp_path,
):
    site = make_site(tmp_path)
    make_openpi_tree(tmp_path)
    environ = {
        "HOME": "/home/tester",
        "USER": "tester",
        "PATH": "/usr/bin",
        "LEVI_UI_TOKEN": "secret",
        "HF_TOKEN": "secret",
        "VIRTUAL_ENV": "/somewhere",
        "ROS_DISTRO": "jazzy",
    }
    out = tmp_path / "preview"
    argv, env, cwd = preview.production_command(site, out, environ)
    assert argv[:7] == ["nice", "-n", "19", "uv", "run", "--no-sync", "--with"]
    assert argv[7] == aeri_prep.EXECUTE_OVERLAY
    assert argv[8:11] == [
        "python",
        "-B",
        str(ROOT / "integrations/camera_preview/worker.py"),
    ]
    assert cwd == site.openpi_checkout
    flat = " ".join(argv)
    assert f"--camera side={site.cameras.view}" in flat
    assert f"--camera wrist={site.cameras.wrist}" in flat
    assert argv[argv.index("--out") + 1] == str(out)
    assert (
        argv[argv.index("--width") + 1] == "640"
        and argv[argv.index("--fps") + 1] == "15"
    )
    assert env["UV_OFFLINE"] == "1" and env["OMP_NUM_THREADS"] == "1"
    assert not any(k.startswith(("LEVI_", "HF_", "ROS_")) for k in env)
    assert "VIRTUAL_ENV" not in env
    assert "~/.cache" not in flat and "UV_CACHE_DIR" not in env


def test_the_serials_are_the_sites_so_swapping_a_camera_changes_the_command(tmp_path):
    make_openpi_tree(tmp_path)
    one = make_site(tmp_path, cameras={"view": "100000000001", "wrist": "100000000002"})
    two = make_site(tmp_path, cameras={"view": "100000000009", "wrist": "100000000008"})
    environ = {"HOME": "/home/tester", "PATH": "/usr/bin"}
    a, *_ = preview.production_command(one, tmp_path, environ)
    b, *_ = preview.production_command(two, tmp_path, environ)
    assert "side=100000000001" in " ".join(a) and "wrist=100000000002" in " ".join(a)
    assert "side=100000000009" in " ".join(b) and "wrist=100000000008" in " ".join(b)
    source = (ROOT / "levi/marvel0/preview.py").read_text()
    assert "262622274256" not in source and "262622275490" not in source


def test_without_the_openpi_checkout_or_a_home_the_command_is_unavailable(tmp_path):
    site = make_site(tmp_path)  # (no openpi tree was made)
    with pytest.raises(preview.Refused) as caught:
        preview.production_command(site, tmp_path, {"HOME": "/home/tester"})
    assert caught.value.code == "preview_unavailable" and caught.value.status == 503
    make_openpi_tree(tmp_path)
    with pytest.raises(preview.Refused):
        preview.production_command(site, tmp_path, {"HOME": "relative"})


def test_the_default_directory_is_in_the_workspace_and_private(tmp_path, monkeypatch):
    from levi import paths

    monkeypatch.setattr(paths, "workspace", lambda: tmp_path / "ws")
    target = preview.default_directory()
    assert target == Workspace.at(tmp_path / "ws").state / "marvel0" / "camera_preview"
    assert oct(target.stat().st_mode & 0o777) == "0o700"


# --- the worker file and the words -----------------------------------------------------------


def load_worker():
    spec = importlib.util.spec_from_file_location("camera_preview_worker", WORKER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_exit_codes_of_the_worker_and_of_the_manager_agree():
    worker = load_worker()
    assert preview.EXIT_FAILURES[worker.EXIT_BUSY] == "cameras_busy"
    assert preview.EXIT_FAILURES[worker.EXIT_NOT_FOUND] == "cameras_not_found"
    assert preview.EXIT_FAILURES[worker.EXIT_UNAVAILABLE] == "worker_unavailable"
    assert preview.EXIT_FAILURES[worker.EXIT_NO_FRAMES] == "no_frames"
    assert worker.EXIT_OK not in preview.EXIT_FAILURES
    assert set(preview.EXIT_FAILURES.values()) <= set(preview.FAILURES)
    assert preview.DEFAULT_FAILURE in preview.FAILURES


def test_the_worker_imports_nothing_but_the_standard_library():
    source = WORKER.read_text()
    top = [
        line for line in source.splitlines() if line.startswith(("import ", "from "))
    ]
    heavy = ("cv2", "numpy", "pyrealsense2")
    assert not [line for line in top if line.split()[1].split(".")[0] in heavy]


CATALOGUES = {
    name: json.loads((ROOT / "src" / "i18n" / f"{name}.json").read_text())
    for name in ("en", "zh")
}


@pytest.mark.parametrize("language", ["en", "zh"])
def test_the_page_has_a_sentence_for_every_code_in_both_languages(language):
    catalogue = CATALOGUES[language]
    missing = [
        key
        for key in (
            *[f"marvel0.preview.reason.{c}" for c in preview.REASON_CODES],
            *[f"marvel0.preview.stopped.{c}" for c in preview.STOPPED_BY],
            *[f"marvel0.preview.frame.{c}" for c in preview.FRAME_REASONS],
            "marvel0.run.refuse.camera_preview_busy",
        )
        if key not in catalogue
    ]
    assert missing == []


def test_the_hosted_client_refusal_is_the_one_the_hand_back_raises():
    from levi.marvel0 import run

    status, text = run.REFUSALS["camera_preview_busy"]
    assert status == 409 and "camera preview" in text
    assert preview.HandbackFailed.code == "camera_preview_busy"


def test_the_service_stop_asks_the_preview_to_let_the_cameras_go():
    source = (ROOT / "levi" / "service.py").read_text()
    assert "preview.shutdown()" in source
    # before the backstop that terminates every group the service started
    assert source.index("preview.shutdown()") < source.index("children.stop_owned()")


def test_the_routes_are_joined_to_the_marvel0_router():
    from levi.marvel0 import api, preview_api

    joined = {r.path for r in api.router.routes}
    for route in preview_api.router.routes:
        assert route.path in joined
