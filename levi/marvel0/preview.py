"""The camera preview of the marvel0 page: the page's Run card shows both
cameras between runs, and the cameras go back to the client before it starts.

The client the page starts (``run.py``, ``aeri_run.py``) opens both RealSense
pipelines when it starts and holds them until it exits, and a pipeline has one
owner at a time. While no client runs, a person can ask the page for a preview
(``POST /api/levi/marvel0/preview/start``): LEVI then starts the camera worker
(``integrations/camera_preview/worker.py``, as a process group of its own,
recorded in ``levi/children.py``), which opens the two colour streams **by the
serials of the site file** (``site.cameras.view`` is the "side" picture,
``site.cameras.wrist`` the "wrist" one) and keeps the newest JPEG of each in a
file below the workspace; ``GET /preview/frame`` reads that file. Nothing here
opens a camera in the LEVI process, connects to a port, signals anything but
the worker, or touches the arm.

**When it may start.** Only in the product LEVI (``levi-product.service`` in the
main checkout, as the hosted run and the AERI run: any other instance would hold
the real cameras where the product's hand-back cannot see it), and only when
nothing else can hold the cameras; an answer that cannot be read counts as
"something does" (``blockers``): the page's hosted evaluation run is active
(its record is read directly, so an unreadable one refuses), the page's hosted
AERI run is active, the arm lock is held, or a client is connected to the policy
port. The first two read the supervisors' ``active()`` (after ``adopt()``, as
``run_api.run_active`` does), the last two the policy deploy's passive status, as
``speech_api.gate`` does; a deploy status whose state is ``unknown`` or whose
socket table could not be read is unreadable.

**Handing back.** Every path that starts a client calls ``handback()`` first
(``run.Supervisor._start``, ``aeri_run.Supervisor._start``): it fences new
previews (also one whose start was reading the blockers meanwhile), stops the
worker and waits (``HANDBACK_S``) until its process has exited, which is when the
devices are released. A stop closes the worker's standard input (the clean path,
measured at about 2 s with both D405s), sends SIGTERM, and sends SIGKILL only
after ``GRACE_S``; a worker that had to be killed is logged, shown in the state
(``killed``) and followed by ``SETTLE_S`` of waiting. A worker that does not
exit in time refuses the start (``HandbackFailed``) instead of letting a client
fail on a busy camera. The preview also ends by itself: ``IDLE_S`` after the
last frame request (the page was closed), ``MAX_S`` after it started (so it never
holds the cameras against a client someone starts from a terminal for long), and
when the service stops.

**Watching.** While it starts or runs, the watcher looks every ``PROBE_S`` for a
client it was not told about: a hosted run, an AERI run, the arm lock (the AERI
terminal D takes it before it loads anything or connects) or a policy connection
stop it (``client_start``); a state that cannot be read twice in a row stops it
too. A client started by hand that takes no lock (the openpi client) is seen only
when it connects to the policy port, which can be after its cameras are wanted.

The module reads no secret and names no path or serial in any answer.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from levi import children, paths
from levi.setup import probes
from levi.workspace import Workspace

from . import aeri_prep, record, stack

log = logging.getLogger("levi.marvel0.preview")

SCHEMA = "levi.marvel0.preview.v1"
CAMERAS = ("side", "wrist")  # the names of the pictures
SITE_FIELD = {"side": "view", "wrist": "wrist"}  # -> site.cameras.<field>
KIND = "camera-preview"  # levi/children.py kind
WORKER = ("integrations", "camera_preview", "worker.py")
REL = ("marvel0", "camera_preview")
LOG_NAME = "worker.log"

WIDTH, HEIGHT, FPS = 640, 480, 15
IDLE_S = 20.0  # no frame asked for this long: the page is gone
MAX_S = 600.0  # the longest a preview holds the cameras
# The longest a stop waits for the worker to exit. A clean exit takes about 2 s
# (both D405s, measured 2026-11-02 after the pipe closed): SIGKILL comes only
# after GRACE_S, and the rest of HANDBACK_S is left for the group to die of it.
HANDBACK_S = 6.0
GRACE_S = 5.0  # the longest a stop waits for the worker to exit by itself
KILL_WAIT_S = HANDBACK_S - GRACE_S  # then, for the group to die of SIGKILL
SETTLE_S = 0.5  # after a SIGKILL, before the cameras are called free
START_TIMEOUT_S = 30.0  # uv, the overlay and the two pipelines, then a picture
STALE_S = 10.0  # a running preview whose pictures stopped
TICK_S = 0.5
PROBE_S = 1.0  # how often a starting or running preview looks for a client
UNREADABLE_STOPS = 2  # separate readings in a row that cannot be read: it stops
# What the watcher stops for (a start refuses on these too, and on the rest).
WATCHED = frozenset({"run_active", "aeri_active", "client_connected", "arm_in_use"})
MAX_FRAME_BYTES = 4 << 20

# worker exit code -> failure code (``integrations/camera_preview/worker.py``;
# a test reads both files). 2 and 126/127 are uv's and the shell's own: the
# environment of the worker is missing.
EXIT_FAILURES = {
    10: "cameras_busy",
    11: "cameras_not_found",
    12: "worker_unavailable",
    13: "no_frames",
    2: "worker_unavailable",
    126: "worker_unavailable",
    127: "worker_unavailable",
}
DEFAULT_FAILURE = "worker_failed"

# code -> (HTTP status, English text). The page words each code itself
# (``marvel0.preview.reason.<code>``); a test keeps the catalogues in step.
REFUSALS = {
    "not_product": (
        409,
        (
            "Only the product LEVI (levi-product.service in the main checkout) may "
            "open the real cameras; this instance only shows the preview"
        ),
    ),
    "run_active": (
        409,
        "An evaluation run of this page is active and holds the cameras.",
    ),
    "aeri_active": (
        409,
        "The AERI run of this page is active and holds the cameras.",
    ),
    "client_connected": (
        409,
        "A client is connected to the policy server and may be using the cameras.",
    ),
    "arm_in_use": (
        409,
        "The arm lock is held, so a run may be using the cameras.",
    ),
    "state_unreadable": (
        409,
        (
            "The state of the runs, the policy server or the arm lock could not be "
            "read, so a client is not ruled out."
        ),
    ),
    "client_starting": (
        409,
        "A client is being started; the cameras go to it.",
    ),
    "preview_stopping": (
        409,
        "The preview is still stopping; try again in a moment.",
    ),
    "preview_unavailable": (
        503,
        "The camera worker cannot be started on this machine.",
    ),
}
FAILURES = {
    "cameras_busy": (
        "Another program holds one of the cameras. Close it, then open the preview "
        "again."
    ),
    "cameras_not_found": (
        "A camera named in the site file is not connected. Check the cables and "
        "the serials."
    ),
    "worker_unavailable": (
        "The camera worker could not start: its Python environment or the "
        "pyrealsense2 overlay is missing."
    ),
    "no_frames": "The cameras opened but sent no pictures.",
    "worker_failed": "The camera worker stopped unexpectedly.",
}
# why a preview ended without a failure
STOPPED_BY = (
    "requested",
    "idle",
    "max_lifetime",
    "client_start",
    "state_unreadable",
    "service",
)
FRAME_REASONS = {
    "preview_off": "The camera preview is not running.",
    "no_frame": "The preview has no complete picture of this camera yet.",
}
# The codes the page words as ``marvel0.preview.reason.<code>`` (a refusal to
# start or a failure) and ``marvel0.preview.stopped.<by>``.
REASON_CODES = tuple(REFUSALS) + tuple(FAILURES)


class Refused(Exception):
    """A start that does not go on: stable ``code``, HTTP ``status``, English
    ``message`` and the facts as ``extra``."""

    def __init__(self, code: str, **extra):
        self.status, self.message = REFUSALS[code]
        self.code = code
        self.extra = extra
        super().__init__(f"{code}: {self.message}")


class HandbackFailed(Exception):
    """The worker did not exit in time: the cameras are not known to be free."""

    code = "camera_preview_busy"


class NoFrame(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason
        self.message = FRAME_REASONS[reason]


@dataclass(frozen=True)
class Frame:
    jpeg: bytes
    age_s: float  # seconds since the worker wrote it


# --- what may start it --------------------------------------------------------------


def product_profile() -> str:
    """``real`` only in the product LEVI (``stack.profile``: its own systemd
    unit, run from the main checkout); ``fake`` anywhere else or when that cannot
    be read."""
    try:
        return stack.profile(stack._read_self_cgroup(), stack._in_main_checkout())[
            "profile"
        ]
    except Exception:  # noqa: BLE001 - fail closed
        return "fake"


def _hosted_record(sup) -> str | None:
    """What the hosted run's record says when the supervisor reports no run:
    ``"active"`` (an ``intent`` record, or a ``started`` one whose launcher is
    the process it names), ``"unreadable"`` (the record cannot be read, or a
    ``started`` one whose launcher is gone or cannot be checked: ``adopt`` would
    have settled it, and it has not), or None. The supervisor reads the record
    once after a restart and takes one it cannot read for "no run" (``adopt``);
    the preview does not, so it reads it itself, every time. Only the real
    supervisor is asked (a test's stand-in has no record)."""
    from . import run

    if not isinstance(sup, run.Supervisor):
        return None
    try:
        rec = record.read_record(sup._folder(sup.site_loader(), create=False))
    except Exception:  # noqa: BLE001 - fail closed
        return "unreadable"
    phase = rec.get("phase") if rec is not None else None
    if phase == "intent":
        return "active"
    if phase != "started":
        return None
    leader = rec.get("leader") or {}
    pid = leader.get("pid")
    try:
        alive = isinstance(pid, int) and sup.host.identity(pid) == leader.get(
            "identity"
        )
    except Exception:  # noqa: BLE001 - fail closed
        return "unreadable"
    return "active" if alive else "unreadable"


def blockers(fresh: bool = False) -> list[str]:
    """Why the cameras may not be opened now, in the order they are reported
    (``REFUSALS`` codes); empty when they may. Anything that cannot be read is
    ``state_unreadable``: unknown is "a client may be running" (a deploy status
    that is ``unknown``, a socket table or an arm lock that cannot be read, a
    hosted-run record that cannot be read). ``fresh`` drops the deploy status's
    2 s cache first, so the answer is a reading taken after the call began.
    Raises only the site's own 412 (the deploy status cannot be had without the
    site file)."""
    from fastapi import HTTPException

    from . import aeri_run_api, deploy_api, run_api

    found: list[str] = []
    unreadable = False
    try:
        sup = run_api.supervisor()
        sup.adopt()
        if sup.active() is not None:
            found.append("run_active")
        else:
            seen = _hosted_record(sup)
            if seen == "active":
                found.append("run_active")
            elif seen == "unreadable":
                unreadable = True
    except Exception:  # noqa: BLE001 - fail closed
        unreadable = True
    try:
        if aeri_run_api.active() is not None:
            found.append("aeri_active")
    except Exception:  # noqa: BLE001
        unreadable = True
    status = None
    try:
        deploy = deploy_api.supervisor()
        if fresh:
            deploy.invalidate()
        status = deploy.status()
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        unreadable = True
    established, arm, net_table = None, None, None
    if isinstance(status, dict):
        state = status.get("state")
        if not isinstance(state, str) or state == "unknown":
            unreadable = True  # a source of the deploy could not be read
        listener = status.get("listener")
        established = (
            listener.get("established") if isinstance(listener, dict) else None
        )
        rows = {
            c.get("code"): c for c in status.get("checks") or () if isinstance(c, dict)
        }
        arm, net_table = rows.get("arm_lock"), rows.get("net_table")
    if isinstance(established, bool) or not isinstance(established, int):
        unreadable = True
    if not isinstance(arm, dict) or arm.get("level") not in ("ok", "refuse"):
        unreadable = True  # no row, or "unknown": who holds the arm is not known
    if not isinstance(net_table, dict) or net_table.get("level") != "ok":
        # The deploy counts 0 connections when it cannot read the table.
        unreadable = True
    if unreadable:
        found.append("state_unreadable")
    if (
        isinstance(established, int)
        and not isinstance(established, bool)
        and established > 0
    ):
        found.append("client_connected")
    if isinstance(arm, dict) and arm.get("level") == "refuse":
        found.append("arm_in_use")
    return found


def blockers_safely(fresh: bool = False) -> list[str]:
    """``blockers()`` for the watcher: whatever cannot be read is
    ``state_unreadable`` (the site file's own 412 is not: with no site there is
    nothing to compare with, and a preview cannot have been started)."""
    from fastapi import HTTPException

    try:
        return blockers(fresh)
    except HTTPException as exc:
        return [] if exc.status_code == 412 else ["state_unreadable"]
    except Exception:  # noqa: BLE001
        return ["state_unreadable"]


def watch(fresh: bool = False) -> tuple[bool, bool]:
    """What the watcher learns in one look: ``(a client is there, the state could
    not be read)``. A client is anything a start refuses for except an unreadable
    state: a hosted run, an AERI run, a policy connection, the arm lock.
    ``fresh`` makes the deploy status a new reading (see ``blockers``)."""
    found = set(blockers_safely(fresh))
    return bool(found & WATCHED), "state_unreadable" in found


def client_signal() -> bool:
    """Whether a client definitely started while the preview runs."""
    return watch()[0]


# --- the machine --------------------------------------------------------------------


def default_directory() -> Path:
    """``<workspace state>/marvel0/camera_preview`` (``0700``, no link in it)."""
    base = Workspace.at(paths.workspace()).state
    target = base / Path(*REL)
    base.mkdir(parents=True, exist_ok=True)
    for folder in (target.parent, target):
        record._check_dir(folder, True)
    return target


def production_command(site, out: Path, environ=None) -> tuple[list, dict, str]:
    """``(argv, environment, cwd)`` of the worker. The way the AERI terminal D
    gets ``pyrealsense2``: ``uv run --no-sync --with <pin>`` from the openpi
    checkout (whose environment has numpy and OpenCV), offline, niced; the
    environment is the allowlist of the hosted client (no ``LEVI_*``, no
    token). Raises ``Refused("preview_unavailable")``."""
    from . import run

    worker = Path(paths.PROJECT).joinpath(*WORKER)
    if not worker.is_file() or not Path(site.openpi_checkout).is_dir():
        raise Refused("preview_unavailable")
    try:
        env = run.child_env(os.environ if environ is None else environ)
    except run.Refuse:
        raise Refused("preview_unavailable") from None
    env["OMP_NUM_THREADS"] = "1"
    argv = [
        "nice",
        "-n",
        "19",
        "uv",
        "run",
        "--no-sync",
        "--with",
        aeri_prep.EXECUTE_OVERLAY,
        "python",
        "-B",
        str(worker),
        "--out",
        str(out),
        *[
            part
            for name in CAMERAS
            for part in (
                "--camera",
                f"{name}={getattr(site.cameras, SITE_FIELD[name])}",
            )
        ],
        "--width",
        str(WIDTH),
        "--height",
        str(HEIGHT),
        "--fps",
        str(FPS),
        "--max-fps",
        str(FPS),
    ]
    return argv, env, site.openpi_checkout


@dataclass
class Host:
    """Everything the preview reaches outside Python; tests replace what they
    need (a fake worker, a clock). ``production_host()`` is the real one."""

    spawn: Callable = subprocess.Popen
    command: Callable = production_command  # (site, out_dir) -> (argv, env, cwd)
    killpg: Callable = os.killpg
    directory: Callable = default_directory
    blockers: Callable = lambda: blockers()  # -> [code]
    # One look of the watcher: (fresh) -> (a client is there, the state is
    # unreadable); ``fresh`` asks for a reading newer than the last look's.
    watch: Callable = lambda fresh=False: watch(fresh)
    profile: Callable = lambda: product_profile()  # -> "real" | "fake"
    site_loader: Callable = None  # -> Site (default: site.require)
    monotonic: Callable = time.monotonic
    wall: Callable = time.time
    sleep: Callable = time.sleep
    children_state: Path | None = None  # tests keep their own processes.json
    # Run the watcher in a thread of its own (tests call ``tick`` by hand).
    background: bool = False


def production_host() -> Host:
    return Host(background=True)


class Manager:
    """The one preview of this process: start, frames, stop, state."""

    def __init__(self, host: Host | None = None):
        self.host = host or Host()
        self._mutex = threading.RLock()
        self._proc = None
        self._state = "off"  # off | starting | on | stopping | failed
        self._failure: str | None = None
        self._stopped_by: str | None = None
        self._stop_by: str | None = None  # why the running stop was asked
        self._abort: str | None = None  # failure the watcher is stopping it for
        self._held = 0
        self._handbacks = 0  # holds taken so far: a start that read the blockers
        # while one was taken and released has read a state that is gone
        self._killed = False  # the last stop ended in SIGKILL
        self._unreadable = 0  # probes in a row that could not be read
        self._tracked: dict | None = None  # identity recorded in children.py
        self._started = 0.0
        self._last_ask = 0.0
        self._last_frame = 0.0
        self._probed = 0.0
        self._out: Path | None = None
        self._watching = False

    # ---- reading

    def _site(self):
        loader = self.host.site_loader
        if loader is None:
            from . import site

            loader = site.require
        return loader()

    def _ages(self) -> dict:
        """Age in ms of each picture file, None where there is none."""
        out = self._out
        now = self.host.wall()
        ages: dict = {}
        for camera in CAMERAS:
            age = None
            if out is not None:
                with contextlib.suppress(OSError):
                    info = os.lstat(out / f"{camera}.jpg")
                    age = round(max(0.0, now - info.st_mtime) * 1000)
            ages[camera] = age
        return ages

    def view(self) -> dict:
        """The ``levi.marvel0.preview.v1`` document. It notices an exit; every
        other step of the watcher (a stop takes seconds, a kill more) is left to
        the watcher thread while there is one."""
        self.tick(act=not self._watching)
        with self._mutex:
            state, failure = self._state, self._failure
            stopped_by, held = self._stopped_by, self._held
            now = self.host.monotonic()
            running = state in ("starting", "on")
            remaining = (
                {
                    "idle_s": round(max(0.0, IDLE_S - (now - self._last_ask)), 1),
                    "lifetime_s": round(max(0.0, MAX_S - (now - self._started)), 1),
                }
                if running
                else None
            )
        blocked: list[str] = []
        if state in ("off", "failed"):
            if self.host.profile() != "real":
                blocked = ["not_product"]  # nothing else matters here
            else:
                blocked = (
                    [] if not held else ["client_starting"]
                ) + self.host.blockers()
        reason = None
        shown = state
        if state in ("off", "failed") and blocked:
            # What stops a start now is shown before an old failure: the person
            # cannot open it again until that is gone, whatever failed before.
            shown = "refused"
            reason = {"code": blocked[0], "text": REFUSALS[blocked[0]][1]}
        elif state == "failed" and failure:
            reason = {"code": failure, "text": FAILURES[failure]}
        return {
            "schema": SCHEMA,
            "state": shown,
            "reason": reason,
            "may_start": state in ("off", "failed") and not blocked,
            "blocked_by": blocked or None,
            "stopped_by": stopped_by if state == "off" else None,
            # the last stop needed SIGKILL (the cameras were not let go cleanly)
            "killed": self._killed and state in ("off", "failed"),
            "frames": {camera: {"age_ms": age} for camera, age in self._ages().items()},
            "limits": {"idle_s": IDLE_S, "max_s": MAX_S},
            "remaining": remaining,
        }

    def frame(self, camera: str) -> Frame:
        """The newest complete picture of ``camera`` (it counts as a sign that
        the page is still there). Raises ``NoFrame``."""
        with self._mutex:
            if self._state not in ("starting", "on") or self._out is None:
                raise NoFrame("preview_off")
            self._last_ask = self.host.monotonic()
            path = self._out / f"{camera}.jpg"
        try:
            data, truncated, info = probes.read_regular(path, MAX_FRAME_BYTES)
        except OSError:  # includes NotRegular: a link, a folder, a FIFO
            raise NoFrame("no_frame") from None
        if truncated or not (
            data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9")
        ):
            raise NoFrame("no_frame")
        return Frame(data, max(0.0, self.host.wall() - info.st_mtime))

    # ---- starting

    def start(self) -> dict:
        """Open the cameras for a preview, or raise ``Refused``. Asking again
        while it runs answers its state."""
        site = self._site()  # 412 as everywhere
        if self.host.profile() != "real":
            raise Refused("not_product")
        with self._mutex:
            if self._held:
                raise Refused("client_starting")
            if self._state == "stopping":
                raise Refused("preview_stopping")
            running = self._state in ("starting", "on")
            fence = self._handbacks
        if running:
            return self.view()
        # Outside the lock: it reads other supervisors and the deploy status,
        # which can take seconds, and a client's hand-back must not wait for it.
        why = self.host.blockers()
        with self._mutex:
            # A client whose start took the cameras back while the blockers were
            # being read (and was launched, or was refused) is not in ``why``.
            if self._held or self._handbacks != fence:
                raise Refused("client_starting")
            if self._state == "stopping":
                raise Refused("preview_stopping")
            if self._state not in ("starting", "on"):
                if why:
                    raise Refused(why[0], reasons=why)
                self._launch(site)
        return self.view()

    def _launch(self, site) -> None:
        h = self.host
        try:
            out = h.directory()
        except (OSError, record.StateDirUnusable):
            raise Refused("preview_unavailable") from None
        argv, env, cwd = h.command(site, out)
        for camera in CAMERAS:
            with contextlib.suppress(OSError):
                os.unlink(out / f"{camera}.jpg")
        self._out = out
        self._failure = self._stopped_by = self._stop_by = self._abort = None
        self._killed, self._unreadable, self._tracked = False, 0, None
        try:
            log_fd = os.open(
                out / LOG_NAME,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
        except OSError:
            raise Refused("preview_unavailable") from None
        try:
            proc = h.spawn(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=log_fd,
                close_fds=True,
                start_new_session=True,
            )
        except (OSError, ValueError):
            self._state, self._failure = "failed", "worker_unavailable"
            return
        finally:
            os.close(log_fd)
        now = h.monotonic()
        self._proc, self._state = proc, "starting"
        self._started = self._last_ask = self._last_frame = self._probed = now
        self._track(proc)
        if h.background and not self._watching:
            self._watching = True
            threading.Thread(
                target=self._watch, name="levi-marvel0-preview", daemon=True
            ).start()

    def _track(self, proc) -> None:
        """Record the worker's group in ``levi/children.py`` with the identity
        the leader has now. Right after ``Popen`` the leader is still ``nice``,
        which execs ``uv``; ``uv`` stays the leader and runs python as its child.
        ``tick`` records again once the leader is another program, so ``reclaim``
        and ``stop_owned`` never meet an identity that no longer matches."""
        with contextlib.suppress(Exception):
            row = children.track(proc, KIND, "worker", state=self.host.children_state)
            self._tracked = row["identity"] if row else None

    def _retrack(self, proc) -> None:
        with contextlib.suppress(
            Exception
        ):  # (the watcher must outlive a /proc hiccup)
            now = children.identity(proc.pid)
            if now is not None and now != self._tracked:
                self._track(proc)

    # ---- watching

    def _watch(self) -> None:
        while True:
            try:
                alive = self.tick()
            except (
                Exception
            ):  # the thread must not die: ``view`` leaves the stops to it
                log.exception("camera preview watcher")
                self.host.sleep(TICK_S)
                continue
            if alive:
                self.host.sleep(TICK_S)
                continue
            with self._mutex:  # a start since the tick keeps the watcher going
                if self._proc is None:
                    self._watching = False
                    return

    def _pictures(self) -> bool:
        """Both pictures are there."""
        return all(age is not None for age in self._ages().values())

    def tick(self, act: bool = True) -> bool:
        """One step of the watcher: notice an exit, a first picture, the idle
        time, the lifetime, stale pictures, a client. True while a worker is
        alive. Without ``act`` only an exit is noticed (``view`` while the
        watcher thread runs)."""
        with self._mutex:
            proc, state = self._proc, self._state
        if proc is None:
            return False
        code = proc.poll()
        if code is not None:
            self._finish(proc, code)
            return False
        if not act:
            return True
        h = self.host
        now = h.monotonic()
        if state == "stopping":
            return True
        if state == "starting":
            self._retrack(proc)
            # The worker opens the cameras seconds after it starts: a client
            # that appears meanwhile is looked for from the first probe on.
            if self._look(now):
                return True
            if self._pictures():
                with self._mutex:
                    if self._proc is proc and self._state == "starting":
                        self._state, self._last_frame = "on", now
                self._retrack(proc)
            elif now - self._started > START_TIMEOUT_S:
                self._fail(proc, "no_frames")
            return True
        if state != "on":
            return True
        ages = [a for a in self._ages().values() if a is not None]
        if ages and min(ages) < STALE_S * 1000:
            self._last_frame = now
        if now - self._last_ask > IDLE_S:
            self.stop("idle")
        elif now - self._started > MAX_S:
            self.stop("max_lifetime")
        elif now - self._last_frame > STALE_S:
            self._fail(proc, "no_frames")
        else:
            self._look(now)
        return True

    def _look(self, now: float) -> bool:
        """Every ``PROBE_S``: stop when a client is there (``client_start``), or
        when the state could not be read in two separate readings in a row
        (``state_unreadable``; the second look drops the deploy status's cache,
        which would otherwise hand back the first look's answer). True when it
        stopped."""
        if now - self._probed < PROBE_S:
            return False
        self._probed = now
        client, unreadable = self.host.watch(self._unreadable > 0)
        self._unreadable = self._unreadable + 1 if unreadable else 0
        if client:
            self.stop("client_start")
            return True
        if self._unreadable >= UNREADABLE_STOPS:
            self.stop("state_unreadable")
            return True
        return False

    # ---- stopping

    def _signal(self, proc, number: int) -> None:
        # Only a process we have not reaped: its number is still ours.
        if proc.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                self.host.killpg(proc.pid, number)

    def _wait(self, proc, until: float):
        h = self.host
        while True:
            code = proc.poll()
            if code is not None:
                return code
            if h.monotonic() >= until:
                return None
            h.sleep(0.02)

    def _terminate(self, proc, timeout: float):
        """End the worker's group and wait: close its standard input (the
        lifeline, the way the worker was measured to leave the cameras cleanly),
        SIGTERM it, and SIGKILL what is left once the grace is over (``GRACE_S``
        of the default ``timeout``, never more than ``timeout`` less one second).
        The exit code, or None when it is still there after ``timeout``. A kill
        is logged, remembered (``killed`` in the view) and followed by
        ``SETTLE_S`` of waiting."""
        import signal

        h = self.host
        begin = h.monotonic()
        stdin = getattr(proc, "stdin", None)
        if stdin is not None:
            with contextlib.suppress(OSError, ValueError):
                stdin.close()
        self._signal(proc, signal.SIGTERM)
        grace = min(GRACE_S, max(timeout - KILL_WAIT_S, timeout / 2))
        code = self._wait(proc, begin + grace)
        if code is None:
            log.warning(
                "camera preview worker still there %.1f s after the stop: SIGKILL",
                h.monotonic() - begin,
            )
            self._killed = True
            self._signal(proc, signal.SIGKILL)
            code = self._wait(proc, begin + timeout)
            if code is not None:
                h.sleep(SETTLE_S)  # the devices are the kernel's to release
        return code

    def stop(self, by: str = "requested", timeout: float = HANDBACK_S) -> bool:
        """Stop the worker and wait until its process has exited (the devices
        are free then). True when nothing runs afterwards; False when the worker
        is still there after ``timeout`` (the state stays ``stopping``)."""
        with self._mutex:
            proc = self._proc
            if proc is None:
                return True
            if proc.poll() is None:
                self._state = "stopping"
                self._stop_by = self._stop_by or by
        code = self._terminate(proc, timeout)
        if code is None:
            return False
        self._finish(proc, code)
        return True

    def _fail(self, proc, failure: str) -> None:
        """Stop the worker because of ``failure`` (it ends as ``failed``)."""
        with self._mutex:
            if self._proc is not proc:
                return
            self._abort = failure
            if proc.poll() is None:
                self._state = "stopping"
        code = self._terminate(proc, HANDBACK_S)
        if code is not None:
            self._finish(proc, code)

    def _finish(self, proc, code: int) -> None:
        """The worker has exited: forget it, remove its pictures, set the state
        it ends in."""
        with self._mutex:
            if self._proc is not proc:
                return
            self._proc = None
            if self._abort is not None:
                self._state, self._failure = "failed", self._abort
            elif self._stop_by is not None:
                self._state, self._stopped_by = "off", self._stop_by
            else:  # it ended by itself
                self._state = "failed"
                self._failure = EXIT_FAILURES.get(code, DEFAULT_FAILURE)
            self._abort = self._stop_by = None
            out = self._out
        stdin = getattr(proc, "stdin", None)
        if stdin is not None:
            with contextlib.suppress(OSError, ValueError):
                stdin.close()
        for camera in CAMERAS:
            if out is not None:
                with contextlib.suppress(OSError):
                    os.unlink(out / f"{camera}.jpg")
        with contextlib.suppress(Exception):
            children.untrack(proc.pid, 0.5, state=self.host.children_state)

    # ---- handing the cameras to a client

    def hold_off(self, timeout: float = HANDBACK_S) -> None:
        """Stop the preview and keep new ones from starting until
        ``release_hold``. Raises ``HandbackFailed`` (the hold is not kept)
        when the worker is still there after ``timeout``."""
        with self._mutex:
            self._held += 1
            self._handbacks += 1  # (never decremented: see ``start``)
        try:
            if not self.stop("client_start", timeout):
                raise HandbackFailed
        except BaseException:
            self.release_hold()
            raise

    def release_hold(self) -> None:
        with self._mutex:
            self._held = max(0, self._held - 1)

    def shutdown(self) -> None:
        """The service stops: let the cameras go (best effort)."""
        with contextlib.suppress(Exception):
            self.stop("service")


# --- the process's preview ------------------------------------------------------------

_LOCK = threading.Lock()
_MANAGER: list = []


def manager() -> Manager:
    """The process's preview (tests replace this function)."""
    with _LOCK:
        if not _MANAGER:
            _MANAGER.append(Manager(production_host()))
        return _MANAGER[0]


@contextlib.contextmanager
def handback(timeout: float = HANDBACK_S):
    """Around the start of a client: the preview is stopped first and no new one
    starts until the block ends. Raises ``HandbackFailed`` when the cameras
    could not be taken back."""
    mgr = manager()
    mgr.hold_off(timeout)
    try:
        yield
    finally:
        mgr.release_hold()


def shutdown() -> None:
    """For the service's stop."""
    with _LOCK:
        mgr = _MANAGER[0] if _MANAGER else None
    if mgr is not None:
        mgr.shutdown()
