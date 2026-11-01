"""Chunk acceleration v3, the phase clock: the three mode schedulers on top of the engine (design section 4, WP2).

Design: fr3_accel_test/decisions/2026-11-01-phase-clock-design.md (the contract; section numbers below refer to it).

``phase.py`` owns the clock (phase, lambda, leash, blend, gripper). This file owns WHEN a chunk is asked for, WHEN it
is swapped in and what the client's own loop sees; it never touches a pose except through the engine:

    PlainPhase          hook A + hook B (plain inference): the client's open-loop loop, one engine target per tick
    PhaseRtcChunker     hook C: replaces rc._rtc_chunker's RtcChunker; mode "rtc"; same interface and summary keys
    PhaseVlashChunker   hook D: the scheduler factory of collector.main; mode "vlash"; same interface and summary keys

Golden mode (``PhaseParams.golden_params()``) makes all three reproduce today's schedulers bit for bit (I2): the
actions, the request payloads (prev arrays, delay integers, future states), the prefetch ticks, the starvation and
reset counts and the common summary keys. Tests: test_schedulers.py.

Reply handling is behind a Transport (section 6): ``ThreadTransport`` is the production one (a daemon thread per
request, rtcc._Request for RTC and vrt._Request for VLASH, loaded read-only by path), ``SimTransport`` is the
deterministic one (a reply is ready a scripted number of ticks after its request; used by the offline loop
simulation and by the tests). The user's rtc_chunker.py and lab/vlash/runtime.py are only loaded, never copied or
edited.

How the schedulers drive the engine each tick (the contract WP1 implements; tests pin it):

    RTC    1. collect a finished reply without blocking -> ``engine.swap(rows, s_obs)`` (origin = the reference phase
              of the tick that sent the request);  2. no rows -> synchronous request -> ``engine.set_chunk``;
           3. ``engine.begin``; while ``past_end``: block for the reply in flight (``starved_ticks``) and ``swap``,
              or query synchronously, then ``begin`` again;  4. ``engine.commit``;  5. start the next request when
              u >= execute_horizon or the early trigger (section 3.9), ``prev = engine.prev_rows()``.
    VLASH  the same, with ``engine.append`` of the prefetched chunk (golden: at ``past_end``, like VlashChunker's
           switch; accelerated: as soon as the reply is here, so the braking profile sees it) and the prefetch started at
           the page's overlap position or earlier.
    So ``swap`` / ``set_chunk`` / ``append`` may come before ``begin`` (steps 1-2, no proposal yet) and after a
    ``begin`` (past_end), and ``begin`` may be called more than once per tick; only ``commit`` moves the clock.
    Chunk origin is always s_obs (RTC) or origin + H (VLASH append): rows are never re-timed by the scheduler.

Hook B (the clamp wrapper) and the launcher (run_collect_phase.py) call ``engine.executed`` / ``engine.rejected`` and
read ``engine.summary()`` / ``engine.drain_records()``; every scheduler exposes ``.engine``. The schedulers push what
only they know into it: ``engine.bump`` (starved_ticks, early_triggers, plain_end_holds), ``engine.note_value``
(delays_rows) and ``engine.note`` (``request_meta`` and ``reply`` trace lines: rows left, delay sent, latency, anchor);
the engine itself traces the chunks and the ``prev`` / ``future`` payloads. Copies stay on the scheduler:
``stats`` (RtcChunker's keys and more), ``request_log``, ``reply_log``. These three calls are additions to the frozen
engine interface; an engine without them still runs.
"""

from __future__ import annotations

import copy
import hashlib
import math
import os
import sys
import time
import types
from collections import Counter, deque
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from phase import Inflight, Measured, NonFiniteChunk, PhaseEngine, PhaseParams

# The sha256 of lab/vlash/runtime.py the design was reviewed against (WP0 aligned the LEVI source with the installed
# copy; section 4.3). The VLASH path moves the arm: a different file is refused, not adapted to.
REVIEWED_VRT_SHA256 = "bbedd1196a7d38722bd430f0f8441ebe46548ea607a90f8328163d5a4fc350b2"

# Used only when rtc_chunker.py cannot be found (offline tests without openpi); with the module present its own
# values win. test_schedulers.py pins them equal.
_FALLBACK_WAIT_LIMIT_S = 30.0
_FALLBACK_MAX_SEED_DELAY = 4
_EPS = 1e-9

# Ticks added to the expected reply delay d-hat wherever the schedulers use it: the early trigger (rows left <= lambda *
# (d-hat + 1) + 1, section 3.9) and the governor's view of the request in flight. The design's formula is margin 0; it
# starves 1.3-4.8 % of the ticks on a trace with the measured 10 % tail of 177-261 ms (a 3-tick reply behind a history
# of 1-tick replies; the engine's soft fall of 0.5 per tick cannot brake in time), which fails the WP2 test 4 limit of
# 1 %. One tick of margin gives 0-1 % on eight traces of that distribution at lambda_eff 2.38 instead of 2.40, and
# for VLASH 0 starvations at the same request count (test_schedulers.py, AccelRtcSpeedTests). Cost for RTC: more
# requests per tick (about 0.8 instead of 0.6 at execute horizon 6, lambda 2.5), see the WP2 report. Deviation from
# section 3.9, decided in WP2 to meet the test; the design owner may set it back to 0.
EARLY_MARGIN_TICKS = 1


# --------------------------------------------------------------------------------------------------------------
# Read-only loading of the user's modules (by path, never copied, never edited, no bytecode written next to them)
# --------------------------------------------------------------------------------------------------------------


def openpi_root() -> Path:
    """The openpi checkout: $CHUNK_ACCEL_OPENPI_ROOT, else two levels above the install dir (openpi/lab/chunk_accel)."""
    env = os.environ.get("CHUNK_ACCEL_OPENPI_ROOT")
    return Path(env) if env else Path(__file__).absolute().parents[2]


def _exec_source(name: str, path: Path, source: bytes) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__file__ = str(path)
    exec(compile(source, str(path), "exec", dont_inherit=True), module.__dict__)  # noqa: S102 - the reviewed file
    return module


def load_rtc_module(path: str | os.PathLike | None = None) -> types.ModuleType:
    """rtc_chunker.py (RtcChunker, _Request, WAIT_LIMIT_S, MAX_SEED_DELAY): the client process's own module when it is
    already imported, else loaded by path from ``path`` / the openpi checkout. FileNotFoundError when absent."""
    if path is None:
        loaded = sys.modules.get("rtc_chunker")
        if loaded is not None and hasattr(loaded, "_Request"):
            return loaded
        path = openpi_root() / "examples/fr3_local/rtc_chunker.py"
    path = Path(path)
    return _exec_source("rtc_chunker_phase", path, path.read_bytes())


def load_vlash_runtime(
    path: str | os.PathLike | None = None,
    *,
    expected_sha256: str | None = REVIEWED_VRT_SHA256,
) -> types.ModuleType:
    """lab/vlash/runtime.py by path under the module name ``vlash_runtime_phase`` (as lab/vlash/test_runtime.py does).

    The file is read once; its sha256 must be ``expected_sha256`` (None skips the check, tests only) and the module
    is built from exactly those bytes, so a file swapped after the check cannot run. ValueError otherwise."""
    path = Path(
        path
        or os.environ.get("CHUNK_ACCEL_VLASH_RUNTIME")
        or openpi_root() / "lab/vlash/runtime.py"
    )
    source = path.read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise ValueError(
            f"{path} has sha256 {digest}, not the reviewed {expected_sha256}: the VLASH runtime moves the arm and is "
            "not adapted silently. Re-review lab/vlash/runtime.py and update REVIEWED_VRT_SHA256 in schedulers.py."
        )
    module = _exec_source("vlash_runtime_phase", path, source)
    module.sha256 = digest
    return module


def _rtc_view(source: Any) -> types.SimpleNamespace | None:
    """The three names the RTC scheduler takes from rtc_chunker.py, read from a module or from the globals of an
    ``RtcChunker`` instance's class (which works even when the module is not in sys.modules)."""
    if source is None:
        return None
    if isinstance(source, types.ModuleType) or hasattr(source, "_Request"):
        g = vars(source)
    else:
        g = getattr(type(source).__init__, "__globals__", {})
    if "_Request" not in g:
        return None
    return types.SimpleNamespace(
        _Request=g["_Request"],
        WAIT_LIMIT_S=g.get("WAIT_LIMIT_S", _FALLBACK_WAIT_LIMIT_S),
        MAX_SEED_DELAY=g.get("MAX_SEED_DELAY", _FALLBACK_MAX_SEED_DELAY),
    )


def _vector(value) -> np.ndarray:
    """vrt.vector: a finite float32 (7,) copy (pinned equal to the module's by test_schedulers.py)."""
    result = np.asarray(value, dtype=np.float32)
    if result.shape != (7,) or not np.isfinite(result).all():
        raise ValueError("expected a finite absolute FR3 state/action of shape (7,)")
    return result.copy()


# --------------------------------------------------------------------------------------------------------------
# Transports
# --------------------------------------------------------------------------------------------------------------


class Handle(Protocol):
    """A request in flight. ``done.is_set()`` says the reply has arrived; ``take()`` waits (bounded) and returns
    ``(chunk, info)`` or raises the request's error; ``wait(limit)`` waits without taking."""

    obs_tick: int
    sent_delay: int
    latency_s: float | None
    done: Any

    def take(self) -> tuple[np.ndarray, Any]: ...

    def wait(self, limit: float | None = None) -> None: ...


class Transport(Protocol):
    def start(
        self, fn: Callable[[], tuple], obs_tick: int, sent_delay: int
    ) -> Handle: ...


class _VlashHandle:
    """vrt._Request (``take(timeout)``, no obs_tick) behind the Handle interface."""

    def __init__(self, request, obs_tick: int, sent_delay: int, timeout_s: float):
        self._request = request
        self.obs_tick = int(obs_tick)
        self.sent_delay = int(sent_delay)
        self._timeout = float(timeout_s)

    @property
    def done(self):
        return self._request.done

    @property
    def latency_s(self):
        return self._request.latency_s

    def wait(self, limit: float | None = None) -> None:
        limit = self._timeout if limit is None else limit
        if not self._request.done.wait(limit):
            raise TimeoutError(
                "VLASH inference did not finish; socket cannot be reused"
            )

    def take(self):
        return self._request.take(self._timeout)


class ThreadTransport:
    """The production transport: the request runs on a daemon thread (the websocket client is not thread safe, so the
    schedulers keep one request in flight). ``request_cls`` is rtcc._Request (``kind="rtc"``) or vrt._Request
    (``kind="vlash"``), both taken from the user's loaded module."""

    def __init__(self, request_cls, *, kind: str = "rtc", timeout_s: float = 30.0):
        if kind not in ("rtc", "vlash"):
            raise ValueError("ThreadTransport kind must be 'rtc' or 'vlash'")
        self.request_cls, self.kind, self.timeout_s = (
            request_cls,
            kind,
            float(timeout_s),
        )

    @classmethod
    def rtc(cls, module=None) -> ThreadTransport:
        view = _rtc_view(module) or _rtc_view(load_rtc_module())
        return cls(view._Request, kind="rtc")

    @classmethod
    def vlash(cls, module=None, timeout_s: float = 30.0) -> ThreadTransport:
        module = module or load_vlash_runtime()
        return cls(module._Request, kind="vlash", timeout_s=timeout_s)

    def start(self, fn, obs_tick: int, sent_delay: int):
        if self.kind == "rtc":
            return self.request_cls(fn, obs_tick, sent_delay)
        return _VlashHandle(self.request_cls(fn), obs_tick, sent_delay, self.timeout_s)


class _SimDone:
    def __init__(self, handle: _SimHandle):
        self._handle = handle

    def is_set(self) -> bool:
        return self._handle.ready()


class _SimHandle:
    """A request whose reply becomes visible ``ticks`` ticks after the tick that sent it (the function runs at once, on
    the caller's thread; nothing is concurrent). ``wait`` / ``take`` before that time model a blocked loop: the reply
    is forced ready (the wall time spent blocked is not modelled), ``blocked`` records that it happened."""

    def __init__(
        self,
        transport: SimTransport,
        fn,
        obs_tick: int,
        sent_delay: int,
        ticks: int,
        latency_s: float,
    ):
        self.obs_tick = int(obs_tick)
        self.sent_delay = int(sent_delay)
        self.ticks = int(ticks)
        self.latency_s = float(latency_s)
        self.blocked = False
        self.taken = False
        self._forced = False
        self._transport = transport
        self.done = _SimDone(self)
        self._result: tuple | None = None
        self._error: BaseException | None = None
        try:
            self._result = fn()
        except BaseException as exc:  # noqa: BLE001 - handed to the loop's thread by take(), like a thread's
            self._error = exc

    def ready(self) -> bool:
        return self._forced or (self._transport.now - self.obs_tick) >= self.ticks

    def wait(self, limit: float | None = None) -> None:
        if not self.ready():
            self.blocked = True
        self._forced = True

    def take(self):
        self.wait()
        self.taken = True
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


class SimTransport:
    """Deterministic transport: request i is ready ``delays[i]`` ticks after the tick that sent it.

    ``delays`` is a sequence (cycled) of ints (ticks) or ``(ticks, latency_s)`` pairs; a bare int gets the latency
    ``(ticks - 0.5) * period_s``. ``from_seconds`` draws the ticks from a latency trace: a reply of L seconds is
    visible ``ceil(L / period)`` ticks later (the observation is read at the start of a tick). The scheduler tells
    the transport the current tick (``on_tick``) before it looks at a handle. ``handles`` keeps every request."""

    def __init__(self, delays=(1,), *, period_s: float = 0.1):
        seq = list(delays)
        if not seq:
            raise ValueError("SimTransport needs at least one delay")
        self._delays = seq
        self.period_s = float(period_s)
        self.now = 0
        self.handles: list[_SimHandle] = []

    @classmethod
    def from_seconds(cls, latencies_s, *, period_s: float = 0.1) -> SimTransport:
        return cls(
            [
                (max(0, math.ceil(float(x) / period_s - _EPS)), float(x))
                for x in latencies_s
            ],
            period_s=period_s,
        )

    def on_tick(self, k: int) -> None:
        self.now = int(k)

    def _next(self) -> tuple[int, float]:
        item = self._delays[len(self.handles) % len(self._delays)]
        if isinstance(item, (tuple, list)):
            return int(item[0]), float(item[1])
        return int(item), (int(item) - 0.5) * self.period_s

    def start(self, fn, obs_tick: int, sent_delay: int) -> _SimHandle:
        ticks, latency_s = self._next()
        handle = _SimHandle(self, fn, obs_tick, sent_delay, ticks, latency_s)
        self.handles.append(handle)
        return handle


# --------------------------------------------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------------------------------------------


def _measure(obs: dict, clock: Callable[[], float]) -> Measured:
    """The engine's view of this tick's observation: the state the client's clamp sees (cartesian_position, whose
    [6] is the issued gripper command), the measured aperture if the robot reported one, and the tick's time."""
    robot = obs["robot_state"]
    pose = np.asarray(robot["cartesian_position"], dtype=np.float32)
    aperture = robot.get("gripper_position") if hasattr(robot, "get") else None
    try:
        aperture = None if aperture is None else float(aperture)
    except (TypeError, ValueError):
        aperture = None
    if aperture is not None and not math.isfinite(aperture):
        aperture = None
    return Measured(pose=pose, aperture=aperture, t=float(clock()))


def _check_rows(chunk, what: str = "action chunk") -> np.ndarray:
    """A private float32 (H, 7) copy; ValueError for a wrong shape, NonFiniteChunk (I10) for NaN / inf."""
    rows = np.array(chunk, dtype=np.float32)
    if rows.ndim != 2 or len(rows) < 1:
        raise ValueError(f"expected an (H, 7) {what}, got {rows.shape}")
    if rows.shape[1] != 7:
        raise ValueError(f"expected an (H, 7) {what}, got {rows.shape}")
    if not np.isfinite(rows).all():
        raise NonFiniteChunk(
            f"the {what} has non-finite values; the episode is aborted, the arm holds its target"
        )
    return rows


def _ceil(x: float) -> int:
    return math.ceil(float(x) - _EPS)


def _max_or_none(values) -> float | None:
    return round(float(max(values)), 5) if values else None


def _notify(transport, k: int) -> None:
    hook = getattr(transport, "on_tick", None)
    if hook is not None:
        hook(k)


# The engine collects the scheduler's counters and trace lines into its own summary() / drain_records() when it offers
# bump / note / note_value (PhaseEngine does; the three calls are additions to the frozen interface, so a bare engine
# works without them and the schedulers keep their own copies in .stats / .request_log / .reply_log).
def _bump(engine, name: str, n: int = 1) -> None:
    hook = getattr(engine, "bump", None)
    if hook is not None:
        hook(name, n)


def _note(engine, record: dict) -> None:
    hook = getattr(engine, "note", None)
    if hook is not None:
        hook(record)


def _note_value(engine, name: str, value: float) -> None:
    hook = getattr(engine, "note_value", None)
    if hook is not None:
        hook(name, value)


# --------------------------------------------------------------------------------------------------------------
# Plain: hook A (query) + hook B (every tick)
# --------------------------------------------------------------------------------------------------------------


class PlainPhase:
    """Plain inference on the phase clock. Hook A calls ``on_query`` where the client queries the policy; hook B calls
    ``on_action`` once per tick for the target and then the client's own clamp on it.

    ``on_query`` returns a placeholder plan (m, 7): the client's open-loop loop executes ``min(h, m)`` ticks of it
    and queries again, so ``m`` is the number of ticks the engine predicts it needs to walk the first ``h`` rows;
    the targets themselves come from ``on_action`` every tick, closed on the measured pose. Golden mode: m = h and
    both return ``chunk[:h]`` row by row, bit for bit (a longer plan than h would not be executed anyway).

    The engine can drop its plan in the middle of a placeholder (the third leash hold in a row, section 3.8); the
    client's loop does not know and keeps calling ``on_action`` until the placeholder is used up and it queries
    again (rc:1582). Those ticks re-send the last target (or walk it back toward a pushed arm, ``_recovery_target``):
    the arm holds, exactly the "hold until the natural re-query" of section 3.8. ``on_action`` raises only when it is
    called before any query of this episode.

    Both hooks take the observation's own time ``t`` (``time.monotonic()`` where the client read the observation):
    the query tick's two looks then share one time stamp whatever the inference takes, and the engine's speed from
    successive measured poses is taken over the real tick spacing. Without ``t`` the clock at hook time is used."""

    mode = "plain"

    def __init__(
        self,
        params: PhaseParams,
        h: int,
        *,
        hz: float = 10.0,
        engine: PhaseEngine | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        if type(h) is not int or h < 1:
            raise ValueError(f"PlainPhase h must be a positive integer, got {h!r}")
        self.params, self.h = params, h
        self.engine = engine if engine is not None else PhaseEngine(params, hz=hz)
        self._clock = clock
        self.queries = 0
        self.plain_end_holds = 0
        self.dropped_plan_holds = 0
        self._ticks = 0
        self._lam_sum = 0.0

    def _measured(self, pose, aperture, t) -> Measured:
        return Measured(
            pose=pose,
            aperture=aperture,
            t=float(self._clock() if t is None else t),
        )

    def on_query(
        self, chunk, state, aperture: float | None = None, t: float | None = None
    ) -> np.ndarray:
        """Hook A: join the freshly queried chunk (section 3.7) and return the placeholder plan."""
        rows = _check_rows(chunk)
        h = min(self.h, len(rows))
        measured = self._measured(state, aperture, t)
        m = max(1, int(self.engine.splice_plain(rows, measured, h)))
        self.queries += 1
        return self.engine.preview(m)

    def on_action(
        self, current, aperture: float | None = None, t: float | None = None
    ) -> np.ndarray:
        """Hook B: this tick's target (float32 (7,)), before the client's clamp. ``current`` is the client's ``state``."""
        measured = self._measured(current, aperture, t)
        if not self.engine.has_rows():
            if self.queries == 0:
                raise RuntimeError(
                    "PlainPhase.on_action before on_query: no chunk to walk"
                )
            # the engine dropped the plan (three leash holds in a row): hold until the client's own re-query
            self.dropped_plan_holds += 1
            _bump(self.engine, "plain_dropped_plan_holds")
            self.engine.begin(measured, None)
            out = self.engine.commit()
            self._ticks += 1
            self._lam_sum += float(out.lam)
            return out.target
        at_end = (
            float(self.engine.rows_left) <= _EPS
        )  # already on the plan's last row: this tick has nowhere to go
        self.engine.begin(measured, None)
        if at_end:
            # the phase reached the last planned row before the client queried again: hold it (expected 0, section 4.1)
            self.plain_end_holds += 1
            _bump(self.engine, "plain_end_holds")
        out = self.engine.commit()
        self._ticks += 1
        self._lam_sum += float(out.lam)
        return out.target

    # the clamp wrapper reports what the client did with the target
    def executed(self, sent) -> None:
        self.engine.executed(sent)

    def rejected(self) -> None:
        self.engine.rejected()

    def reset(self) -> None:
        self.engine.reset()

    def close(self) -> None:
        self.reset()

    def summary(self) -> dict:
        """The scheduler's own counters; the per-episode record (section 8) is ``engine.summary()``."""
        return {
            "mode": self.mode,
            "h": self.h,
            "queries": self.queries,
            "plain_end_holds": self.plain_end_holds,
            "plain_dropped_plan_holds": self.dropped_plan_holds,
            "phase_lambda_mean": round(self._lam_sum / self._ticks, 4)
            if self._ticks
            else None,
        }


# --------------------------------------------------------------------------------------------------------------
# RTC: hook C
# --------------------------------------------------------------------------------------------------------------


class PhaseRtcChunker:
    """RtcChunker's interface (``next_action`` / ``reset`` / ``close`` / ``summary`` / ``stats``, ``mode = "rtc"``) on
    the phase clock; section 4.2. ``request(obs, prev, delay)`` is the client's own payload builder (rc._rtc_chunker's
    closure), called on the loop's thread; it returns the function that does the round trip.

    Per tick (rtcc:135-152, with the engine in between): collect a finished reply and swap it in, load a chunk
    synchronously when there is none, ``begin``, collect / query while the proposal runs past the chunk end,
    ``commit``, then start the next request when u >= execute_horizon or the early trigger fires. The new chunk's
    origin is the reference phase of the tick that sent the request (rtcc:241's ``chunk_tick = obs_tick``)."""

    mode = "rtc"

    def __init__(
        self,
        request,
        *,
        execute_horizon: int,
        period_s: float,
        params: PhaseParams,
        transport: Transport | None = None,
        say: Callable[[str], Any] = print,
        delay_window: int | None = None,
        engine: PhaseEngine | None = None,
        clock: Callable[[], float] = time.monotonic,
        rtc_module: Any = None,
        early_margin_ticks: int | None = None,
    ):
        if execute_horizon < 1:
            raise ValueError(f"execute_horizon must be >= 1, got {execute_horizon}")
        self._rtc = _rtc_view(rtc_module)
        self._max_seed = int(
            getattr(self._rtc, "MAX_SEED_DELAY", _FALLBACK_MAX_SEED_DELAY)
        )
        self._transport = transport  # None: a ThreadTransport over rtcc._Request, made at the first request
        self._request = request
        self.execute_horizon = int(execute_horizon)
        self.period_s = float(period_s)
        self.params = params
        self._say = say
        self._clock = clock
        self._margin = int(
            EARLY_MARGIN_TICKS if early_margin_ticks is None else early_margin_ticks
        )
        self.engine = (
            engine
            if engine is not None
            else PhaseEngine(params, hz=1.0 / self.period_s)
        )
        window = max(
            1, int(delay_window if delay_window is not None else params.delay_history)
        )
        self._delays: deque[int] = deque(
            maxlen=window
        )  # ticks, as rtcc: the governor's d-hat and the stats
        self._delays_rows: deque[float] = deque(
            maxlen=window
        )  # rows: what is sent as the inference delay
        self._tick = 0
        self._inflight: Handle | None = None
        self._inflight_info: dict = {}
        self._abandoned: list[Handle] = []
        self._seed_delay: int | None = None
        self._warned_tight = False
        self._lam_sum = 0.0
        self.request_log: list[
            dict
        ] = []  # one line per background request (tick, phase, rows left, delay sent, ...)
        self.reply_log: list[
            dict
        ] = []  # one line per reply swapped in (latency, delay in ticks and rows, anchor, ...)
        # same keys as RtcChunker.stats (rc reads stats["requests"]), plus the phase-clock ones
        self.stats: dict[str, Any] = {
            "requests": 0,
            "rtc_requests": 0,
            "starved_ticks": 0,
            "dropped_replies": 0,
            "delays": Counter(),
            "latency_ms": [],
            "frozen_pos_err_m": [],
            "switch_jump_m": [],
            "delays_rows": [],
            "swap_anchor_rows": [],
            "blend_ticks": 0,
            "early_triggers": 0,
            "sync_requests": 0,
        }

    # ---- the eval loop's interface ---------------------------------------------------------------------------

    def next_action(self, obs: dict) -> np.ndarray:
        """The raw (unclamped) action for this tick. Call once per tick, after the observation."""
        k = self._tick
        _notify(self._transport, k)
        measured = _measure(obs, self._clock)
        self._collect(k, block=False)
        if not self.engine.has_rows():
            self._sync(obs, k)
        proposal = self.engine.begin(measured, self._inflight_view(k))
        starved = False
        attempts = 0
        while proposal.past_end:
            attempts += 1
            if attempts > 3:  # a reply, then a query, always end it; never spin
                raise RuntimeError(
                    "the phase engine is still past the end of the chunk after a reply and a query"
                )
            # the chunk runs out before the next one came back: wait for it (the arm holds), or query synchronously
            if self._inflight is not None:
                if not starved:
                    self.stats["starved_ticks"] += 1
                    _bump(self.engine, "starved_ticks")
                    starved = True
                self._collect(k, block=True)
            else:
                self._sync(obs, k)
            proposal = self.engine.begin(measured, self._inflight_view(k))
        out = self.engine.commit()
        self._lam_sum += float(out.lam)
        self._tick = k + 1
        if getattr(out, "reset", False):
            # the third leash hold in a row: the engine dropped its plan; drop the reply in flight as well, so the next
            # tick asks synchronously from where the arm is (section 3.8)
            self.reset()
        elif self._inflight is None:
            self._maybe_start(obs, k, out)
        return out.target

    def reset(self) -> None:
        """Forget the chunk and any reply in flight (rejected jump, failed step, new episode)."""
        if self._inflight is not None:
            self._abandoned.append(self._inflight)
            self._inflight = None
            self._inflight_info = {}
        self.engine.reset()

    def close(self) -> None:
        """End of episode: let a background request finish so nothing touches the socket after."""
        self.reset()
        self._drain()

    def summary(self) -> dict:
        """RtcChunker.summary()'s keys and values, plus delays_rows, swap_anchor_rows, blend_ticks, phase_lambda_mean."""
        s = self.stats
        lat = s["latency_ms"]
        return {
            "execute_horizon": self.execute_horizon,
            "requests": int(s["requests"]),
            "rtc_requests": int(s["rtc_requests"]),
            "starved_ticks": int(s["starved_ticks"]),
            "dropped_replies": int(s["dropped_replies"]),
            "delays": {str(key): int(v) for key, v in sorted(s["delays"].items())},
            "latency_ms_median": round(float(np.median(lat)), 1) if lat else None,
            "latency_ms_max": round(float(np.max(lat)), 1) if lat else None,
            "frozen_pos_err_m_max": _max_or_none(s["frozen_pos_err_m"]),
            "switch_jump_m_max": _max_or_none(s["switch_jump_m"]),
            "delays_rows": [round(float(x), 3) for x in s["delays_rows"]],
            "swap_anchor_rows": [round(float(x), 3) for x in s["swap_anchor_rows"]],
            "blend_ticks": int(s["blend_ticks"]),
            "phase_lambda_mean": round(self._lam_sum / self._tick, 4)
            if self._tick
            else None,
        }

    # ---- internals -------------------------------------------------------------------------------------------

    def _transport_for_start(self) -> Transport:
        if self._transport is None:
            view = self._rtc or _rtc_view(load_rtc_module())
            if view is None:
                raise RuntimeError("rtc_chunker.py does not define _Request")
            self._rtc = view
            self._max_seed = int(view.MAX_SEED_DELAY)
            self._transport = ThreadTransport(view._Request, kind="rtc")
        return self._transport

    def _inflight_view(self, k: int) -> Inflight | None:
        req = self._inflight
        if req is None:
            return None
        return Inflight(
            age_ticks=max(0, k - req.obs_tick),
            d_hat_ticks=self._d_hat(),
            done=bool(req.done.is_set()),
        )

    def _d_hat(self) -> int:
        """Expected delay of the next reply in ticks: the slowest of the last replies (the seed before the first), plus
        the margin."""
        return (
            int(max(self._delays)) if self._delays else int(self._seed_delay or 1)
        ) + self._margin

    def _drain(self, block: bool = True) -> None:
        """Retire abandoned requests; with ``block`` wait for the ones still running (rtcc:182-192)."""
        running = []
        for req in self._abandoned:
            if block:
                req.wait()
            if req.done.is_set():
                self.stats["dropped_replies"] += 1
            else:
                running.append(req)
        self._abandoned = running

    def _sync(self, obs: dict, k: int) -> None:
        # Only after a reset, at the start, or when the chunk ran out; either way nothing is in flight.
        assert self._inflight is None
        self._drain()
        self.stats["requests"] += 1
        self.stats["sync_requests"] += 1
        t0 = self._clock()
        chunk, _ = self._request(obs, None, 0)()
        latency_s = self._clock() - t0
        self.stats["latency_ms"].append(latency_s * 1000)
        if self._seed_delay is None:
            # The RTC request costs more than this plain one (a backward pass per denoising step), hence the extra
            # tick until the first RTC reply has been measured (rtcc:203-206).
            self._seed_delay = min(
                self._max_seed, math.ceil(latency_s / self.period_s) + 1
            )
        self.engine.set_chunk(_check_rows(chunk), "sync")

    def _maybe_start(self, obs: dict, k: int, out) -> None:
        """Step 5: the next request when the executed rows reach the page's horizon or the early trigger fires."""
        u = float(out.u)
        rows_left = float(self.engine.rows_left)
        early = False
        if self.params.early_trigger:
            early = rows_left <= float(out.lam) * (self._d_hat() + 1) + 1.0 + _EPS
        if not (u >= self.execute_horizon or early):
            return
        prev = np.array(self.engine.prev_rows(), dtype=np.float32)
        if len(prev) < 1:
            return
        early_only = early and not u >= self.execute_horizon
        if early_only:
            self.stats["early_triggers"] += 1
            _bump(self.engine, "early_triggers")
        self._start(obs, k, prev, out, rows_left=rows_left, early=early_only)

    def _start(
        self,
        obs: dict,
        k: int,
        prev: np.ndarray,
        out,
        *,
        rows_left: float = 0.0,
        early: bool = False,
    ) -> None:
        measured = bool(self._delays_rows)
        if measured:
            delay = _ceil(max(self._delays_rows))
        else:
            delay = _ceil((self._seed_delay or 1) * max(1.0, float(out.lam)))
        delay = max(1, delay)
        if delay >= len(prev):
            if measured and not self._warned_tight:
                self._warned_tight = True
                self._say(
                    f"      (rtc: measured delay {delay} ticks >= the {len(prev)} actions left in the "
                    f"chunk; the arm waits between chunks. Lower --rtc-execute-horizon.)"
                )
            delay = len(prev)
        self.stats["requests"] += 1
        self.stats["rtc_requests"] += 1
        function = self._request(obs, prev, int(delay))
        self._inflight = self._transport_for_start().start(function, k, int(delay))
        self._inflight_info = {
            "s_obs": float(out.s),
            "k_obs": k,
            "lam_sum": self._lam_sum,
        }
        line = {
            "kind": "request_meta",
            "mode": "rtc",
            "k": k,
            "s_obs": float(out.s),
            "u": float(out.u),
            "rows_left": float(rows_left),
            "lam": float(out.lam),
            "delay_rows": int(delay),
            "prev_rows": len(prev),
            "early": bool(early),
        }
        self.request_log.append(line)
        _note(self.engine, line)

    def _collect(self, k: int, block: bool) -> None:
        self._drain(block=False)
        req = self._inflight
        if req is None or not (block or req.done.is_set()):
            return
        chunk, info = req.take()
        info_ = self._inflight_info
        self._inflight = None
        self._inflight_info = {}
        rows = _check_rows(chunk)
        actual = k - req.obs_tick
        self._delays.append(actual)
        self.stats["delays"][actual] += 1
        self.stats["latency_ms"].append(req.latency_s * 1000)
        swap = self.engine.swap(rows, float(info_["s_obs"]))
        d_rows = float(swap.get("d_rows", actual))
        jump = swap.get("jump_m")
        anchor = swap.get(
            "anchor_rows"
        )  # None when there was no old chunk to compare with
        n_blend = int(swap.get("n_blend") or 0)
        self._delays_rows.append(d_rows)
        self.stats["delays_rows"].append(d_rows)
        _note_value(self.engine, "delays_rows", d_rows)
        if anchor is not None:
            anchor = float(anchor)
            self.stats["swap_anchor_rows"].append(anchor)
        self.stats["blend_ticks"] += n_blend
        if jump is not None:
            jump = float(jump)
            self.stats["switch_jump_m"].append(jump)
        diag = (info or {}).get("rtc") or {}
        if "frozen_pos_err_m" in diag:
            self.stats["frozen_pos_err_m"].append(float(diag["frozen_pos_err_m"]))
        flight = max(1, k - int(info_.get("k_obs", req.obs_tick)))
        lam_bar = (self._lam_sum - float(info_.get("lam_sum", self._lam_sum))) / flight
        line = {
            "kind": "reply",
            "mode": "rtc",
            "k": k,
            "k_obs": req.obs_tick,
            "s_obs": float(info_["s_obs"]),
            "lat_ms": req.latency_s * 1000,
            "d_ticks": actual,
            "d_rows": d_rows,
            "anchor_rows": anchor,
            "jump_m": jump,
            "n_blend": n_blend,
            "lam_mean": lam_bar,
        }
        self.reply_log.append(line)
        _note(self.engine, line)
        self._say(
            f"      (rtc chunk from tick {req.obs_tick}: {req.latency_s * 1000:.0f} ms, "
            f"delay {actual} (sent {req.sent_delay})"
            + (
                f", frozen err {diag['frozen_pos_err_m'] * 1000:.1f} mm"
                if "frozen_pos_err_m" in diag
                else ""
            )
            + (f", switch {jump * 1000:.1f} mm" if jump is not None else "")
            + f", rows {d_rows:.2f}, lam {lam_bar:.2f}, anchor {0.0 if anchor is None else anchor:+.1f})"
        )


def wrap_rtc_chunker(
    original, params: PhaseParams, *, say: Callable[[str], Any] | None = None
):
    """Hook C: replacement for ``rc._rtc_chunker``. Builds the user's RtcChunker as the original does (so the payload
    closure, the execute horizon and the period are exactly the client's; the object itself is never started) and
    returns a PhaseRtcChunker over the same ``_request``. ``args.rtc`` stays True: the server check and the log
    headers of the client need no patch."""

    def phase_rtc_chunker(client, args, prompt):
        base = original(client, args, prompt)
        return PhaseRtcChunker(
            base._request,
            execute_horizon=base.execute_horizon,
            period_s=base.period_s,
            params=params,
            say=say if say is not None else getattr(base, "_say", print),
            rtc_module=base,
        )

    phase_rtc_chunker.__wrapped__ = original
    return phase_rtc_chunker


# --------------------------------------------------------------------------------------------------------------
# VLASH: hook D
# --------------------------------------------------------------------------------------------------------------


class PhaseVlashChunker:
    """VlashChunker's interface (``next_action`` / ``reset`` / ``action_adjusted`` / ``close`` / ``summary``,
    ``mode = "vlash"``) on the phase clock; section 4.3. ``request(obs, future)`` snapshots a payload on the caller's
    thread and returns the network call; the future state of a prefetch is ``chunk[-1]`` exactly.

    The prefetched chunk joins behind the current one at ``past_end`` (``engine.append``: origin + H, no clamping of
    the join). The prefetch starts at the page's overlap position (u == H - overlap, vrt:226) or, accelerated, as soon
    as the rows left fit the expected delay: rows_left <= lambda * (d_hat + 1) + 1 (section 3.9)."""

    mode = "vlash"

    def __init__(
        self,
        request,
        *,
        overlap_steps: int,
        horizon: int,
        params: PhaseParams,
        binarize_gripper: bool = False,
        timeout_s: float = 30.0,
        transport: Transport | None = None,
        hz: float = 10.0,
        engine: PhaseEngine | None = None,
        clock: Callable[[], float] = time.monotonic,
        vlash_module: Any = None,
        early_margin_ticks: int | None = None,
    ):
        if (
            type(overlap_steps) is not int
            or type(horizon) is not int
            or not 1 <= overlap_steps < horizon
        ):
            raise ValueError("invalid VLASH chunk horizon/overlap")
        self._request = request
        self.overlap = overlap_steps
        self.horizon = horizon
        self.timeout = timeout_s
        self.binarize_gripper = binarize_gripper
        self.params = params
        self._clock = clock
        self._vlash_module = vlash_module
        self._margin = int(
            EARLY_MARGIN_TICKS if early_margin_ticks is None else early_margin_ticks
        )
        self._transport = transport  # None: a ThreadTransport over vrt._Request (sha checked), made at the first prefetch
        self.engine = engine if engine is not None else PhaseEngine(params, hz=hz)
        self.pending: Handle | None = None
        self._pending_k = 0
        self.abandoned: list[Handle] = []
        self.closed = False
        self._tick = 0
        self._lam_sum = 0.0
        self._lat_hist: deque[float] = deque(maxlen=max(1, int(params.delay_history)))
        self._seed_ticks: int | None = None
        self.request_log: list[
            dict
        ] = []  # one line per prefetch (tick, phase, rows left, early or at the page's position)
        self.stats = {
            "requests": 0,
            "prefetches": 0,
            "starvations": 0,
            "discarded_replies": 0,
            "adjusted_targets": 0,
        }
        self.extra = {"sync_fallbacks": 0, "early_prefetches": 0}
        # Reply latency of every request, in order (the first is the blocking one at the start of an episode).
        self.latency_ms: list[float] = []

    # ---- the eval loop's interface ---------------------------------------------------------------------------

    def next_action(self, observation: dict) -> np.ndarray:
        if self.closed:
            raise RuntimeError("VLASH scheduler already closed")
        k = self._tick
        _notify(self._transport, k)
        measured = _measure(observation, self._clock)
        if not self.engine.has_rows():
            self._sync(observation)
        elif (
            self.pending is not None
            and not self.params.golden
            and self.pending.done.is_set()
        ):
            # accelerated: join the chunk as soon as it is here, so the braking profile sees its first rows (a gripper
            # window right behind the join) before the phase gets there. Golden waits for the join like today (the
            # counters of an abandoned reply are then the same as VlashChunker's).
            self._append_pending()
        proposal = self.engine.begin(measured, self._inflight_view(k))
        starved = False
        attempts = 0
        while proposal.past_end:
            attempts += 1
            if attempts > 3:  # a prefetch, then a query, always end it; never spin
                raise RuntimeError(
                    "the phase engine is still past the end of the chunk after a prefetch and a query"
                )
            if self.pending is None:
                # not reachable in golden mode (the prefetch starts before the end); accelerated, never run on a stale
                # plan: ask again from here (a chunk with no future state) and count it
                self.extra["sync_fallbacks"] += 1
                self._sync(observation)
            else:
                if not starved and not self.pending.done.is_set():
                    self.stats["starvations"] += 1
                    _bump(self.engine, "starved_ticks")
                starved = True
                self._append_pending()
            proposal = self.engine.begin(measured, self._inflight_view(k))
        out = self.engine.commit()
        self._lam_sum += float(out.lam)
        self._tick = k + 1
        if getattr(out, "reset", False):
            self.reset()  # the third leash hold in a row: the engine dropped its plan, so drop the prefetch too (3.8)
        elif self.pending is None:
            self._maybe_prefetch(observation, k, out)
        return out.target

    def reset(self) -> None:
        if self.pending is not None:
            self.abandoned.append(self.pending)
            self.pending = None
        self.engine.reset()

    def action_adjusted(self, raw, actual) -> None:
        if not np.array_equal(np.asarray(raw), np.asarray(actual)):
            self.stats["adjusted_targets"] += 1
            self.reset()

    def close(self) -> None:
        self.closed = True
        self.reset()
        self._drain()

    def summary(self) -> dict:
        first = self.latency_ms[0] if self.latency_ms else None
        prefetch = self.latency_ms[1:]
        return {
            "mode": self.mode,
            "overlap_steps": self.overlap,
            "action_horizon": self.horizon,
            "experimental": True,
            "delay_training_verified": False,
            **self.stats,
            "latency_ms_first": round(first, 1) if first is not None else None,
            "latency_ms_median": round(float(np.median(prefetch)), 1)
            if prefetch
            else None,
            "latency_ms_max": round(max(prefetch), 1) if prefetch else None,
            "sync_fallbacks": self.extra["sync_fallbacks"],
            "early_prefetches": self.extra["early_prefetches"],
            "phase_lambda_mean": round(self._lam_sum / self._tick, 4)
            if self._tick
            else None,
        }

    # ---- internals -------------------------------------------------------------------------------------------

    def _append_pending(self) -> None:
        pending, self.pending = self.pending, None
        answer = pending.take()
        self._record_latency(pending.latency_s)
        self.engine.append(self._chunk(answer))

    def _record_latency(self, seconds) -> None:
        if seconds is not None:
            self.latency_ms.append(seconds * 1000.0)
            self._lat_hist.append(float(seconds))

    def _chunk(self, answer) -> np.ndarray:
        chunk, _ = answer
        chunk = np.asarray(chunk, dtype=np.float32)
        if chunk.shape != (self.horizon, 7):
            raise ValueError("invalid VLASH chunk shape/values")
        if not np.isfinite(chunk).all():
            raise NonFiniteChunk("invalid VLASH chunk shape/values")
        chunk = chunk.copy()
        if self.binarize_gripper:
            # Same command encoding as the existing FR3 safety loop.
            chunk[:, 6] = (chunk[:, 6] >= 0.5).astype(np.float32)
        return chunk

    def _drain(self) -> None:
        for req in self.abandoned:
            req.take()
            self.stats["discarded_replies"] += 1
        self.abandoned.clear()

    def _d_hat(self) -> int:
        """Expected delay of a prefetch in ticks: the slowest of the last replies (section 3.9)."""
        dt = self.engine.dt
        if self._lat_hist:
            return max(1, _ceil(max(self._lat_hist) / dt)) + self._margin
        return int(self._seed_ticks or 1) + self._margin

    def _inflight_view(self, k: int) -> Inflight | None:
        if self.pending is None:
            return None
        return Inflight(
            age_ticks=max(0, k - self._pending_k),
            d_hat_ticks=self._d_hat(),
            done=bool(self.pending.done.is_set()),
        )

    def _transport_for_start(self) -> Transport:
        if self._transport is None:
            self._transport = ThreadTransport.vlash(
                self._vlash_module, timeout_s=self.timeout
            )
        return self._transport

    def _sync(self, observation: dict) -> None:
        self._drain()
        self.stats["requests"] += 1
        started = self._clock()
        answer = self._request(copy.deepcopy(observation), None)()
        elapsed = self._clock() - started
        self._record_latency(elapsed)
        if self._seed_ticks is None:
            self._seed_ticks = min(4, max(1, _ceil(elapsed / self.engine.dt)))
        self.engine.set_chunk(self._chunk(answer), "sync")

    def _maybe_prefetch(self, observation: dict, k: int, out) -> None:
        rows_left = float(self.engine.rows_left)
        golden_pos = (
            rows_left <= (self.overlap - 1) + _EPS
        )  # u == H - overlap (vrt:226) on the current chunk
        early = False
        if self.params.early_trigger:
            early = rows_left <= float(out.lam) * (self._d_hat() + 1) + 1.0 + _EPS
        if not (golden_pos or early):
            return
        if early and not golden_pos:
            self.extra["early_prefetches"] += 1
            _bump(self.engine, "early_triggers")
        future = _vector(self.engine.future_row())
        function = self._request(copy.deepcopy(observation), future)
        self.pending = self._transport_for_start().start(function, k, 0)
        self._pending_k = k
        self.stats["requests"] += 1
        self.stats["prefetches"] += 1
        line = {
            "kind": "request_meta",
            "mode": "vlash",
            "k": k,
            "s_obs": float(out.s),
            "u": float(out.u),
            "rows_left": rows_left,
            "lam": float(out.lam),
            "early": bool(early and not golden_pos),
        }
        self.request_log.append(line)
        _note(self.engine, line)


def phase_vlash_factory(
    params: PhaseParams, vrt_module, *, rc_module: Any = None, clock=time.monotonic
):
    """Hook D: the ``scheduler_factory`` for ``collector.main(args, scheduler_factory=..., server_validator=
    vrt.check_server)``. Written after lab/vlash/run_collect_vlash.py:22-46 (the payload is the client's own
    ``_policy_payload`` plus the VLASH protocol keys; test_schedulers.py asserts it equal key by key)."""

    def factory(client, args, prompt):
        rc = (
            rc_module
            if rc_module is not None
            else sys.modules.get("run_robotiq_client")
        )
        if rc is None:
            raise RuntimeError(
                "phase_vlash_factory needs the client module run_robotiq_client (not imported)"
            )
        meta = client.get_server_metadata()
        vrt_module.check_server(meta, args)

        def request(observation, future):
            state = np.asarray(
                observation["robot_state"]["cartesian_position"], dtype=np.float32
            )
            payload = rc._policy_payload(observation, state.copy(), prompt)
            payload["vlash_protocol_version"] = 1
            if future is not None:
                payload["vlash_future_state"] = future.copy()

            def infer():
                response = client.infer(payload)
                return rc._chunk_of(response), response

            return infer

        return PhaseVlashChunker(
            request,
            overlap_steps=args.vlash_overlap_steps,
            horizon=meta["vlash"]["action_horizon"],
            params=params,
            binarize_gripper=args.binarize_gripper,
            transport=ThreadTransport.vlash(vrt_module),
            clock=clock,
        )

    return factory


__all__ = [
    "EARLY_MARGIN_TICKS",
    "REVIEWED_VRT_SHA256",
    "Handle",
    "PhaseRtcChunker",
    "PhaseVlashChunker",
    "PlainPhase",
    "SimTransport",
    "ThreadTransport",
    "Transport",
    "load_rtc_module",
    "load_vlash_runtime",
    "openpi_root",
    "phase_vlash_factory",
    "wrap_rtc_chunker",
]
