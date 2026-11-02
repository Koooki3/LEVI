"""The phase-clock schedulers (schedulers.py) against today's schedulers: WP2 tests 1-9 of the design (section 7).

No robot, no GPU, no server, no port. The user's rtc_chunker.py, lab/vlash/runtime.py (installed copy), the client's
_clamp_action / _rtc_chunker and lab/vlash/run_collect_vlash.py are loaded read-only BY PATH (compiled from their bytes,
no bytecode written next to them, a stub for the two libraries the client imports at module level); without an openpi
checkout (CHUNK_ACCEL_OPENPI_ROOT, or an `openpi` folder above this one) those tests skip. Nothing of theirs is copied.

HARNESS. A golden comparison runs one scripted episode (chunks, reply delays, resets, failed steps) through today's
scheduler and through the phase scheduler and compares everything the client sees. Time is deterministic:
  * today's RtcChunker / VlashChunker run in a private copy of their module whose request thread (_Request) is replaced
    by a stand-in that is "done" a scripted number of ticks after its tick, and whose clock is a fake one; a wait / take
    before that models a blocked loop (the reply is forced);
  * the phase schedulers run on SimTransport with the same script and the same fake clock;
  * FakeServer answers both sides; every request (kind, observation id, prev bytes, delay / future bytes) is recorded.
The threaded tests use the real _Request threads with Event gates the test opens.
The engine under test is either the real PhaseEngine (WP1) or ``RefEngine``, a test-local reference of the frozen
engine interface in golden mode (integer phase, hard switches) plus a constant-lambda mode: it proves the harness and
the scheduler logic independently of the core. Cases on the real engine fail with NotImplementedError until WP1 lands.
"""

import collections
import contextlib
import dataclasses
import functools
import hashlib
import itertools
import math
import os
import re
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

import numpy as np

HERE = Path(__file__).absolute().parent
sys.path.insert(0, str(HERE))
import phase
import schedulers
from phase import (
    NonFiniteChunk,
    PhaseParams,
    Proposal,
    TickOut,
)
from schedulers import (
    REVIEWED_VRT_SHA256,
    PhaseRtcChunker,
    PhaseVlashChunker,
    PlainPhase,
    SimTransport,
    ThreadTransport,
    load_vlash_runtime,
    phase_vlash_factory,
    wrap_rtc_chunker,
)

GOLDEN = PhaseParams.golden_params()
ACCEL = PhaseParams.for_page(2.5, 0.005)
SHIPPED = PhaseParams.for_page(
    3.0, 0.005
)  # the page default (levi/marvel0/inference_mode.py)


# ==============================================================================================================
# Read-only loading of the user's code
# ==============================================================================================================


def _find_openpi() -> Path | None:
    env = os.environ.get("CHUNK_ACCEL_OPENPI_ROOT")
    if env:
        root = Path(env)
        return root if (root / "examples/fr3_local/rtc_chunker.py").is_file() else None
    for parent in HERE.parents:
        if (parent / "openpi/examples/fr3_local/rtc_chunker.py").is_file():
            return parent / "openpi"
    return None


OPENPI = _find_openpi()
needs_openpi = unittest.skipUnless(
    OPENPI is not None, "no openpi checkout (CHUNK_ACCEL_OPENPI_ROOT)"
)
RTCC_PATH = OPENPI / "examples/fr3_local/rtc_chunker.py" if OPENPI else None
RC_PATH = OPENPI / "examples/fr3_local/run_robotiq_client.py" if OPENPI else None
VRT_INSTALLED = OPENPI / "lab/vlash/runtime.py" if OPENPI else None
VRT_LEVI = HERE.parent / "vlash/runtime.py"
RCV_PATH = OPENPI / "lab/vlash/run_collect_vlash.py" if OPENPI else None

_MISSING = object()


@contextlib.contextmanager
def patched_modules(**mods):
    """sys.modules entries for the duration of a load (None removes), restored afterwards; sys.path restored too."""
    saved = {k: sys.modules.get(k, _MISSING) for k in mods}
    path = list(sys.path)
    for k, v in mods.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v
    try:
        yield
    finally:
        sys.path[:] = path
        for k, v in saved.items():
            if v is _MISSING:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def load_ro(name: str, path: Path, register: bool = False) -> types.ModuleType:
    """A private copy of a module compiled from the file's bytes (no .pyc is written next to the user's file)."""
    module = types.ModuleType(name)
    module.__file__ = str(path)
    if register:
        sys.modules[name] = module
    exec(  # noqa: S102 - the user's reviewed file, compiled from its bytes
        compile(path.read_bytes(), str(path), "exec", dont_inherit=True),
        module.__dict__,
    )
    return module


@functools.cache
def client_module():
    """run_robotiq_client with `tyro` and `openpi_client` stubbed (they are only needed to run the client) and its
    sibling rtc_chunker as a private copy. Only its pure functions are used."""
    rtcc = load_ro("rtc_chunker_for_rc", RTCC_PATH)
    oc = types.ModuleType("openpi_client")
    wcp = types.ModuleType("openpi_client.websocket_client_policy")
    oc.websocket_client_policy = wcp
    with patched_modules(
        tyro=types.ModuleType("tyro"),
        openpi_client=oc,
        **{
            "openpi_client.websocket_client_policy": wcp,
            "rtc_chunker": rtcc,
            "rc_under_test": None,
        },
    ):
        rc = load_ro("rc_under_test", RC_PATH, register=True)
    sys.modules.pop("rc_under_test", None)
    return rc


# ==============================================================================================================
# Time, scripts, server
# ==============================================================================================================


class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    __call__ = monotonic


@dataclasses.dataclass
class Script:
    """One scripted episode; both sides of a comparison get the same one."""

    seed: int
    n_ticks: int = 150
    exec_h: int = 5
    H: int = 10
    overlap: int = 2
    replies: tuple = (
        (1, 0.083),
    )  # per asynchronous request, cycled: (ticks until visible, latency seconds)
    sync_lat: tuple = (0.083,)  # per synchronous request, cycled
    resets: frozenset = (
        frozenset()
    )  # ticks after which the client rejects / fails the step and resets
    adjusts: frozenset = frozenset()  # VLASH: ticks whose clamp changed the target
    binarize: bool = True


def random_script(
    ep: int, *, vlash: bool = False, n_ticks: int = 150, overlap: int | None = None
) -> Script:
    rng = np.random.default_rng(7000 + ep + (500000 if vlash else 0))
    ticks = rng.integers(0, 7, size=40)  # 0-6 ticks; 0 is seen at the next tick like 1
    replies = tuple(
        (int(t), (max(int(t), 1) - 0.5) * 0.1 + float(rng.uniform(-0.02, 0.02)))
        for t in ticks
    )
    sync = tuple(float(x) for x in rng.choice([0.0, 0.04, 0.083, 0.25, 0.45], size=6))
    p_reset = (
        rng.random(n_ticks) < 0.03 + 0.015
    )  # rejected jumps (3 %) and failed steps (1.5 %)
    p_adj = rng.random(n_ticks) < 0.02
    return Script(
        seed=ep,
        n_ticks=n_ticks,
        exec_h=(3, 5, 6, 8)[ep % 4],
        overlap=overlap if overlap is not None else (1, 2, 4)[ep % 3],
        replies=replies,
        sync_lat=sync,
        resets=frozenset(int(i) for i in np.flatnonzero(p_reset)),
        adjusts=frozenset(int(i) for i in np.flatnonzero(p_adj))
        if vlash
        else frozenset(),
        binarize=bool(ep % 2 == 0),
    )


def make_rows(key, H: int = 10) -> np.ndarray:
    """A deterministic chunk: a drifting pose (rx near +-pi so the wrap is exercised), a gripper track with raw values
    near the 0.5 threshold, and a per-chunk offset so consecutive chunks do not agree (switch jumps)."""
    rng = np.random.default_rng(list(key))
    base = np.array([0.45, 0.05, 0.30, math.pi - 0.03, 0.02, -0.5]) + rng.normal(
        0, 0.003, 6
    )
    inc = rng.normal(0, 0.004, 6)
    rows = base + np.arange(H)[:, None] * inc
    rows[:, 3] = (rows[:, 3] + math.pi) % (2 * math.pi) - math.pi
    g0 = rng.choice([1.0, 0.9, 0.51, 0.5, 0.0, 0.1, 0.49])
    g = np.full(H, g0)
    if rng.random() < 0.5:
        j = int(rng.integers(1, H))
        g[j:] = rng.choice([0.0, 0.2, 0.49] if g0 >= 0.5 else [1.0, 0.8, 0.5, 0.51])
    return np.column_stack([rows, g]).astype(np.float32)


class FakeServer:
    """The harness's policy server: ``rtc_request`` / ``vlash_request`` are the payload builders the schedulers take."""

    def __init__(self, script: Script, clock: Clock):
        self.script, self.clock = script, clock
        self.calls: list[tuple] = []

    def _rows(self, obs) -> np.ndarray:
        return make_rows(
            (self.script.seed, len(self.calls), int(obs["id"])), self.script.H
        )

    def _sync_wait(self) -> None:
        sync = [c for c in self.calls if c[0] == "sync"]
        self.clock.now += self.script.sync_lat[
            (len(sync) - 1) % len(self.script.sync_lat)
        ]

    def rtc_request(self, obs, prev, delay):
        rows = self._rows(obs)
        if prev is None:
            self.calls.append(("sync", int(obs["id"]), None, 0))
        else:
            prev = np.asarray(prev)
            self.calls.append(
                (
                    "rtc",
                    int(obs["id"]),
                    (prev.dtype.str, prev.shape, prev.tobytes()),
                    int(delay),
                )
            )
        sync = prev is None
        diag = (
            {"rtc": {"frozen_pos_err_m": 0.001 * (len(self.calls) % 7)}}
            if len(self.calls) % 3
            else {}
        )

        def run():
            if sync:
                self._sync_wait()
            return rows.copy(), dict(diag)

        return run

    def vlash_request(self, obs, future):
        rows = self._rows(obs)
        if future is None:
            self.calls.append(("sync", int(obs["id"]), None))
        else:
            future = np.asarray(future)
            self.calls.append(
                (
                    "prefetch",
                    int(obs["id"]),
                    (future.dtype.str, future.shape, future.tobytes()),
                )
            )
        sync = future is None

        def run():
            if sync:
                self._sync_wait()
            return rows.copy(), {}

        return run


def make_obs(k: int, issued_g: float) -> dict:
    pose = np.array([0.4, 0.0, 0.3, 3.0, 0.0, 0.0, issued_g], dtype=np.float32)
    return {
        "id": k,
        "robot_state": {"cartesian_position": pose, "gripper_position": 0.5},
    }


def sent_of(action: np.ndarray) -> np.ndarray:
    out = np.array(action, dtype=np.float32)
    out[6] = 1.0 if out[6] >= 0.5 else 0.0  # the client's binarization
    return out


class _FakeDone:
    def __init__(self, req):
        self._req = req

    def is_set(self) -> bool:
        return self._req.forced or self._req.ctx["tick"] >= self._req.ready_tick


def fake_request_class(ctx: dict, script: Script, *, vlash: bool):
    """Stand-in for rtcc._Request (fn, obs_tick, sent_delay) / vrt._Request (function): the reply is visible
    ``ticks`` ticks after the request; waiting before that forces it. The function runs at once, like SimTransport."""

    class Fake:
        ordinal = 0

        def __init__(self, fn, obs_tick=None, sent_delay=0):
            ticks, lat = script.replies[Fake.ordinal % len(script.replies)]
            Fake.ordinal += 1
            self.ctx = ctx
            self.obs_tick = ctx["tick"] if obs_tick is None else int(obs_tick)
            self.sent_delay = int(sent_delay)
            self.ready_tick = self.obs_tick + ticks
            self.forced = False
            self.latency_s = lat
            self.result, self.error = None, None
            try:
                self.result = fn()
            except BaseException as exc:  # noqa: BLE001
                self.error = exc
            self.done = _FakeDone(self)

        def wait(self, limit_s=30.0):
            self.forced = True

        def take(self, timeout=None):
            self.wait()
            if self.error is not None:
                raise self.error
            return self.result

    return Fake


@dataclasses.dataclass
class Result:
    actions: list
    calls: list
    say: list
    summary: dict
    error: tuple | None
    sched: object = None


def drive(
    sched,
    script: Script,
    ctx: dict,
    say: list,
    server: FakeServer,
    *,
    hook_b: bool,
    vlash: bool = False,
) -> Result:
    """The client loop of the episode: observation, next_action, the client's own checks (a scripted reset, or for
    VLASH a scripted changed target), then close. ``hook_b`` mirrors the clamp wrapper's calls into the engine."""
    actions, error = [], None
    issued = 1.0
    for k in range(script.n_ticks):
        ctx["tick"] = k
        try:
            a = sched.next_action(make_obs(k, issued))
        except Exception as exc:  # noqa: BLE001 - both sides must fail alike
            error = (k, type(exc).__name__, str(exc))
            break
        actions.append(np.array(a))
        sent = sent_of(a)
        if k in script.resets:
            if hook_b:
                sched.engine.rejected()
            sched.reset()
            continue
        if vlash:
            changed = sent.copy()
            if k in script.adjusts:
                changed[0] += 1e-3
            sched.action_adjusted(sent, changed)
            if k in script.adjusts:
                if hook_b:
                    sched.engine.rejected()
                continue
        if hook_b:
            sched.engine.executed(sent)
        issued = float(sent[6])
    try:
        sched.close()
    except Exception as exc:  # noqa: BLE001
        error = error or (script.n_ticks, type(exc).__name__, str(exc))
    return Result(actions, list(server.calls), list(say), sched.summary(), error, sched)


def run_rtc_original(script: Script) -> Result:
    rtcc = load_ro("rtc_chunker_golden", RTCC_PATH)
    clock, ctx, say = Clock(), {"tick": 0}, []
    rtcc.time = types.SimpleNamespace(monotonic=clock.monotonic)
    rtcc._Request = fake_request_class(ctx, script, vlash=False)
    server = FakeServer(script, clock)
    sched = rtcc.RtcChunker(
        server.rtc_request, execute_horizon=script.exec_h, period_s=0.1, say=say.append
    )
    return drive(sched, script, ctx, say, server, hook_b=False)


def run_rtc_phase(
    script: Script, params: PhaseParams, make_engine, *, cls=PhaseRtcChunker
) -> Result:
    clock, ctx, say = Clock(), {"tick": 0}, []
    server = FakeServer(script, clock)
    engine = make_engine(params, 10.0) if make_engine else None
    sched = cls(
        server.rtc_request,
        execute_horizon=script.exec_h,
        period_s=0.1,
        params=params,
        transport=SimTransport(script.replies),
        say=say.append,
        clock=clock.monotonic,
        engine=engine,
    )
    return drive(sched, script, ctx, say, server, hook_b=True)


def run_vlash_original(script: Script, vrt_path: Path) -> Result:
    vrt = load_ro("vlash_runtime_golden", vrt_path)
    clock, ctx, say = Clock(), {"tick": 0}, []
    vrt.time = types.SimpleNamespace(monotonic=clock.monotonic)
    vrt._Request = fake_request_class(ctx, script, vlash=True)
    server = FakeServer(script, clock)
    sched = vrt.VlashChunker(
        server.vlash_request,
        overlap_steps=script.overlap,
        horizon=script.H,
        binarize_gripper=script.binarize,
    )
    return drive(sched, script, ctx, say, server, hook_b=False, vlash=True)


def run_vlash_phase(
    script: Script, params: PhaseParams, make_engine, *, cls=PhaseVlashChunker
) -> Result:
    clock, ctx, say = Clock(), {"tick": 0}, []
    server = FakeServer(script, clock)
    engine = make_engine(params, 10.0) if make_engine else None
    sched = cls(
        server.vlash_request,
        overlap_steps=script.overlap,
        horizon=script.H,
        params=params,
        binarize_gripper=script.binarize,
        transport=SimTransport(script.replies),
        clock=clock.monotonic,
        engine=engine,
    )
    return drive(sched, script, ctx, say, server, hook_b=True, vlash=True)


_EXTRA_RE = re.compile(r", rows [-\d.]+, lam [-\d.]+, anchor [-+\d.]+\)$")


_SWITCH_RE = re.compile(r", switch [-\d.]+ mm")
JUMP_KEYS = ("switch_jump_m_max",)


def assert_same(
    tc: unittest.TestCase,
    a: Result,
    b: Result,
    label: str,
    *,
    strip_extra: bool = False,
    jump: bool = True,
) -> None:
    """Everything the client sees is equal: actions (value and dtype), requests, log lines, common summary keys.

    ``jump=False`` leaves out the switch-jump statistic (the ``switch`` part of a log line and ``switch_jump_m_max``):
    it is the one number the engine, not the scheduler, produces (``assert_same_jump`` checks it on its own)."""
    tc.assertEqual(a.error, b.error, f"{label}: failure")
    tc.assertEqual(len(a.actions), len(b.actions), f"{label}: number of actions")
    for k, (x, y) in enumerate(zip(a.actions, b.actions)):
        if not (x.dtype == y.dtype == np.float32 and np.array_equal(x, y)):
            tc.fail(
                f"{label}: action at tick {k} differs: {x} ({x.dtype}) vs {y} ({y.dtype})"
            )
    tc.assertEqual(
        a.calls, b.calls, f"{label}: requests (kind, observation, prev / future, delay)"
    )
    la = list(a.say)
    lb = [_EXTRA_RE.sub(")", s) for s in b.say] if strip_extra else list(b.say)
    if not jump:
        la, lb = (
            [_SWITCH_RE.sub("", s) for s in la],
            [_SWITCH_RE.sub("", s) for s in lb],
        )
    tc.assertEqual(la, lb, f"{label}: log lines")
    for key, value in a.summary.items():
        if not jump and key in JUMP_KEYS:
            continue
        tc.assertIn(key, b.summary, f"{label}: summary lacks {key}")
        tc.assertEqual(value, b.summary[key], f"{label}: summary[{key}]")


def assert_same_jump(tc: unittest.TestCase, a: Result, b: Result, label: str) -> None:
    """The switch jump of rtcc:233-237 (None when the old chunk is already over) in the log lines and the summary."""
    tc.assertEqual(
        [m.group(0) for s in a.say if (m := _SWITCH_RE.search(s))],
        [m.group(0) for s in b.say if (m := _SWITCH_RE.search(s))],
        f"{label}: switch jumps in the log lines",
    )
    for key in JUMP_KEYS:
        tc.assertEqual(a.summary[key], b.summary[key], f"{label}: summary[{key}]")


# ==============================================================================================================
# The test-local reference engine
# ==============================================================================================================


def _interp(rows: np.ndarray, local: float) -> np.ndarray:
    """X(phi) of design 3.1 on one chunk (float64 inside, float32 out; a row is returned as an exact copy)."""
    h = len(rows)
    if local >= h - 1:
        return rows[h - 1].copy()
    if local <= 0:
        return rows[0].copy()
    j = min(math.floor(local), h - 2)
    f = local - j
    if f == 0:
        return rows[j].copy()
    a, b = rows[j].astype(np.float64), rows[j + 1].astype(np.float64)
    out = a.copy()
    out[:3] = a[:3] + f * (b[:3] - a[:3])
    out[3:6] = a[3:6] + f * phase.wrap_to_pi(b[3:6] - a[3:6])
    out[6] = a[6]  # the gripper value is the floor row's, never interpolated
    return out.astype(np.float32)


class RefEngine:
    """Test-local reference of the frozen PhaseEngine interface (design section 6): golden mode (lambda 1, integer
    phase, hard switches, exact row copies) and, for the structural scheduler tests, a constant lambda_max with the
    section 3.9 governor and no leash, blend or gripper window. It is NOT the core; it exists so the harness and the
    scheduler logic can be checked while the core is written. Conventions: ``s`` is the global phase of the last
    emitted target; a synchronous chunk is placed with its row 0 at this tick's candidate; a swap puts the origin at
    s_obs; an append puts the origin behind the last chunk."""

    def __init__(self, params: PhaseParams, hz: float = 10.0):
        self.params, self.hz = params, float(hz)
        self.dt = 1.0 / self.hz
        self._lam_cfg = 1.0 if params.golden else float(params.lambda_max)
        self._chunks: list[list] = []  # [origin, rows]
        self._s = -1.0
        self._last_lam = 1.0
        self._prop: tuple | None = None
        self.n_swaps = self.n_appends = self.n_sets = 0
        self.counters: collections.Counter = (
            collections.Counter()
        )  # what the scheduler pushed in
        self.values: dict[str, list] = collections.defaultdict(list)
        self.notes: list[dict] = []

    # ---- helpers
    def _cand(self) -> float:
        return self._prop[1] if self._prop is not None else self._s + self._lam_cfg

    def _end(self) -> float:
        o, rows = self._chunks[-1]
        return o + len(rows) - 1

    def _at(self, phi: float) -> tuple[np.ndarray, float]:
        idx = max(
            (i for i, (o, _) in enumerate(self._chunks) if o <= phi + 1e-9), default=0
        )
        o, rows = self._chunks[idx]
        local = phi - o
        if (
            idx + 1 < len(self._chunks) and local > len(rows) - 1
        ):  # between two chunks: join last row to next row 0
            f = local - (len(rows) - 1)
            nxt = self._chunks[idx + 1][1][0]
            two = np.stack([rows[-1], nxt])
            return _interp(two, f), local
        return _interp(rows, local), local

    # ---- interface
    def reset(self) -> None:
        self._chunks, self._prop, self._last_lam = [], None, 1.0

    def has_rows(self) -> bool:
        return bool(self._chunks)

    def begin(self, m, inflight) -> Proposal:
        lam = self._lam_cfg
        if inflight is not None and not inflight.done and not self.params.golden:
            gov = max(0.0, self._end() - self._s - 0.5) / max(
                1, inflight.d_hat_ticks - inflight.age_ticks
            )
            lam = min(lam, gov)
        s_cand = self._s + lam
        self._prop = (lam, s_cand)
        return Proposal(
            lam=lam, s_cand=s_cand, past_end=bool(s_cand > self._end() + 1e-9)
        )

    def set_chunk(self, rows, kind) -> None:
        rows = np.array(rows, dtype=np.float32)
        if not np.isfinite(rows).all():
            raise NonFiniteChunk("non-finite chunk")
        self._chunks = [[self._cand(), rows]]
        self.n_sets += 1

    def swap(self, rows, s_obs) -> dict:
        rows = np.array(rows, dtype=np.float32)
        if not np.isfinite(rows).all():
            raise NonFiniteChunk("non-finite chunk")
        cand = self._cand()
        d_rows = cand - s_obs
        jump = None
        if (
            self._chunks
        ):  # rtcc:233-237: the old chunk's row against the new chunk's row at this tick
            old_row, old_local = self._at(cand)
            if 0 <= old_local < len(self._chunks[-1][1]) and d_rows < len(rows):
                new_row = _interp(rows, d_rows)
                jump = float(
                    np.linalg.norm(np.asarray(new_row[:3], np.float64) - old_row[:3])
                )
        self._chunks = [[float(s_obs), rows]]
        self.n_swaps += 1
        return {
            "d_rows": float(d_rows),
            "jump_m": jump,
            "anchor_rows": 0.0,
            "n_blend": 0,
        }

    def append(self, rows) -> None:
        rows = np.array(rows, dtype=np.float32)
        if not np.isfinite(rows).all():
            raise NonFiniteChunk("non-finite chunk")
        o, last = self._chunks[-1]
        self._chunks.append([o + len(last), rows])
        self.n_appends += 1

    def splice_plain(self, rows, m, h) -> int:
        rows = np.array(rows, dtype=np.float32)[:h]
        if not np.isfinite(rows).all():
            raise NonFiniteChunk("non-finite chunk")
        self._chunks = [[self._s + self._lam_cfg, rows]]
        self._prop = None
        return (
            len(rows)
            if self.params.golden
            else math.ceil((len(rows) - 1) / self._lam_cfg) + 1
        )

    def preview(self, n) -> np.ndarray:
        return self._chunks[-1][1][:n].copy()

    def commit(self) -> TickOut:
        lam, s_cand = (
            self._prop
            if self._prop is not None
            else (self._lam_cfg, self._s + self._lam_cfg)
        )
        s_new = min(s_cand, self._end())
        target, _ = self._at(s_new)
        moved = s_new - self._s
        self._s, self._prop, self._last_lam = s_new, None, moved
        while len(self._chunks) > 1 and self._chunks[1][0] <= s_new + 1e-9:
            self._chunks.pop(0)
        u = (
            s_new - self._chunks[-1][0]
            if self._chunks[-1][0] <= s_new + 1e-9
            else s_new - self._chunks[0][0]
        )
        return TickOut(
            target=target.astype(np.float32), s=s_new, u=u, e=u, lam=moved, cap=lam
        )

    def tick(self, m, inflight=None) -> TickOut:
        self.begin(m, inflight)
        return self.commit()

    def executed(self, sent) -> None:
        pass

    def rejected(self) -> None:
        pass

    def prev_rows(self) -> np.ndarray:
        o, rows = self._chunks[0] if len(self._chunks) == 1 else self._chunks[-1]
        u = self._s - o
        if u == int(u):
            return rows[int(u) :].copy()
        n = math.floor(len(rows) - 1 - u + 1e-9) + 1
        return np.stack([_interp(rows, u + j) for j in range(n)]).astype(np.float32)

    def future_row(self) -> np.ndarray:
        return self._chunks[-1][1][-1].copy()

    @property
    def u(self) -> float:
        return self._s - self._chunks[-1][0]

    @property
    def s(self) -> float:
        return self._s

    @property
    def lam(self) -> float:
        return self._last_lam

    @property
    def rows_left(self) -> float:
        return self._end() - self._s

    def bump(self, name: str, n: int = 1) -> None:
        self.counters[name] += int(n)

    def note(self, record: dict) -> None:
        self.notes.append(dict(record))

    def note_value(self, name: str, value: float) -> None:
        self.values[name].append(float(value))

    def summary(self) -> dict:
        return {}

    def drain_records(self) -> list:
        out, self.notes = self.notes, []
        return out


class RefMixin:
    make_engine = staticmethod(RefEngine)
    models_core = False  # a golden / constant-lambda reference: the accelerated assertions of the core are skipped


class RealMixin:
    make_engine = None  # schedulers build phase.PhaseEngine
    models_core = True


# ==============================================================================================================
# 0. The harness itself
# ==============================================================================================================


@needs_openpi
class HarnessTests(unittest.TestCase):
    def test_a_script_is_deterministic(self):
        s = random_script(3)
        a, b = run_rtc_original(s), run_rtc_original(s)
        assert_same(self, a, b, "twice the same")
        va, vb = (
            run_vlash_original(s, VRT_INSTALLED),
            run_vlash_original(s, VRT_INSTALLED),
        )
        assert_same(self, va, vb, "twice the same (vlash)")

    def test_the_scripts_cover_what_the_golden_test_claims(self):
        """200 scripts: delays 0-6, resets, all four execute horizons, starvation, dropped replies, tight-delay warning."""
        seen = {
            "starved": 0,
            "dropped": 0,
            "tight": 0,
            "resets": 0,
            "late": 0,
            "long_delay": 0,
        }
        horizons = set()
        for ep in range(60):
            s = random_script(ep)
            r = run_rtc_original(s)
            horizons.add(s.exec_h)
            seen["starved"] += r.summary["starved_ticks"] > 0
            seen["dropped"] += r.summary["dropped_replies"] > 0
            seen["tight"] += any("measured delay" in line for line in r.say)
            seen["resets"] += len(s.resets) > 0
            seen["long_delay"] += any(int(k) >= 5 for k in r.summary["delays"])
        self.assertEqual(horizons, {3, 5, 6, 8})
        for key, count in seen.items():
            if key != "late":
                self.assertGreater(count, 0, f"no script exercises {key}")

    def test_the_comparison_has_teeth(self):
        """A seeded bug on the phase side (the delay sent is one too long) must be caught by assert_same."""

        class Off(PhaseRtcChunker):
            def _start(self, obs, k, prev, out):
                orig = self._request

                def request(o, p, d):
                    return orig(o, p, d + 1 if p is not None else d)

                self._request = request
                try:
                    super()._start(obs, k, prev, out)
                finally:
                    self._request = orig

        s = random_script(1, n_ticks=80)
        a = run_rtc_original(s)
        b = run_rtc_phase(s, GOLDEN, RefEngine, cls=Off)
        with self.assertRaises(AssertionError):
            assert_same(self, a, b, "seeded bug", strip_extra=True)

    def test_the_vlash_comparison_has_teeth_too(self):
        """A prefetch one row too late on the phase side must be caught (the calls and the starvations differ)."""

        class Late(PhaseVlashChunker):
            def _maybe_prefetch(self, observation, k, out):
                if float(self.engine.rows_left) > (self.overlap - 2) + 1e-9:
                    return
                super()._maybe_prefetch(observation, k, out)

        s = Script(seed=3, n_ticks=80, overlap=2, replies=((1, 0.08),))
        a = run_vlash_original(s, VRT_INSTALLED)
        b = run_vlash_phase(s, GOLDEN, RefEngine, cls=Late)
        with self.assertRaises(AssertionError):
            assert_same(self, a, b, "seeded bug")

    def test_a_blocked_wait_forces_the_reply(self):
        ctx, script = {"tick": 0}, Script(seed=0, replies=((3, 0.25),))
        cls = fake_request_class(ctx, script, vlash=False)
        req = cls(lambda: ("chunk", {}), 0, 2)
        self.assertFalse(req.done.is_set())
        ctx["tick"] = 3
        self.assertTrue(req.done.is_set())
        req2 = cls(lambda: ("chunk", {}), 3, 2)
        self.assertFalse(req2.done.is_set())
        self.assertEqual(req2.take(), ("chunk", {}))
        self.assertTrue(req2.done.is_set())

    def test_sim_transport(self):
        tp = SimTransport([(2, 0.15), (0, 0.0), 3])
        h = tp.start(lambda: (np.zeros((2, 7)), {}), 5, 1)
        tp.on_tick(6)
        self.assertFalse(h.done.is_set())
        tp.on_tick(7)
        self.assertTrue(h.done.is_set())
        self.assertEqual(h.latency_s, 0.15)
        h2 = tp.start(lambda: (np.zeros((2, 7)), {}), 7, 1)
        tp.on_tick(7)
        self.assertTrue(h2.done.is_set())  # 0 ticks: ready at once
        h3 = tp.start(lambda: (np.zeros((2, 7)), {}), 7, 1)
        self.assertFalse(h3.done.is_set())
        h3.take()
        self.assertTrue(h3.blocked and h3.done.is_set() and h3.taken)
        h4 = tp.start(lambda: (np.zeros((2, 7)), {}), 7, 1)  # the list cycles
        self.assertEqual(h4.ticks, 2)
        failing = tp.start(lambda: 1 / 0, 8, 1)
        with self.assertRaises(ZeroDivisionError):
            failing.take()
        tp2 = SimTransport.from_seconds([0.083, 0.1, 0.177, 0.261], period_s=0.1)
        self.assertEqual(
            [h.ticks for h in (tp2.start(lambda: None, 0, 0) for _ in range(4))],
            [1, 1, 2, 3],
        )


class LoaderTests(unittest.TestCase):
    def test_the_vlash_runtime_is_refused_unless_it_is_the_reviewed_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "runtime.py"
            p.write_text("x = 1\n")
            with self.assertRaises(ValueError) as cm:
                load_vlash_runtime(p)
            self.assertIn("sha256", str(cm.exception))
            self.assertEqual(
                load_vlash_runtime(
                    p, expected_sha256=hashlib.sha256(p.read_bytes()).hexdigest()
                ).x,
                1,
            )
            self.assertEqual(load_vlash_runtime(p, expected_sha256=None).x, 1)

    def test_the_reviewed_sha_is_the_aligned_runtime(self):
        self.assertEqual(
            hashlib.sha256(VRT_LEVI.read_bytes()).hexdigest(), REVIEWED_VRT_SHA256
        )

    @needs_openpi
    def test_the_installed_runtime_loads_and_is_the_reviewed_file(self):
        vrt = load_vlash_runtime(VRT_INSTALLED)
        self.assertEqual(vrt.sha256, REVIEWED_VRT_SHA256)
        self.assertTrue(
            hasattr(vrt, "VlashChunker")
            and hasattr(vrt, "_Request")
            and hasattr(vrt, "check_server")
        )

    @needs_openpi
    def test_default_paths_follow_the_openpi_root_variable(self):
        with (
            patched_modules(rtc_chunker=None),
            mock.patch.dict(os.environ, {"CHUNK_ACCEL_OPENPI_ROOT": str(OPENPI)}),
        ):
            self.assertEqual(schedulers.openpi_root(), OPENPI)
            self.assertTrue(hasattr(schedulers.load_rtc_module(), "_Request"))
            self.assertEqual(load_vlash_runtime().sha256, REVIEWED_VRT_SHA256)
        # the client's own module wins when it is already imported
        fake = types.SimpleNamespace(_Request=object, MAX_SEED_DELAY=4)
        fake_mod = types.ModuleType("rtc_chunker")
        fake_mod.__dict__.update(vars(fake))
        with patched_modules(rtc_chunker=fake_mod):
            self.assertIs(schedulers.load_rtc_module(), fake_mod)

    @needs_openpi
    def test_vector_and_constants_are_the_users(self):
        vrt = load_ro("vrt_for_vector", VRT_INSTALLED)
        rtcc = load_ro("rtcc_for_constants", RTCC_PATH)
        for good in (np.arange(7), [0.0] * 7, np.ones(7, np.float64)):
            np.testing.assert_array_equal(schedulers._vector(good), vrt.vector(good))
            self.assertEqual(schedulers._vector(good).dtype, vrt.vector(good).dtype)
        for bad in (np.zeros(6), np.full(7, np.nan), np.full(7, np.inf)):
            with self.assertRaises(ValueError):
                vrt.vector(bad)
            with self.assertRaises(ValueError):
                schedulers._vector(bad)
        self.assertEqual(rtcc.WAIT_LIMIT_S, schedulers._FALLBACK_WAIT_LIMIT_S)
        self.assertEqual(rtcc.MAX_SEED_DELAY, schedulers._FALLBACK_MAX_SEED_DELAY)
        self.assertEqual(
            schedulers.load_rtc_module(RTCC_PATH).MAX_SEED_DELAY, rtcc.MAX_SEED_DELAY
        )

    @needs_openpi
    def test_loading_by_path_leaves_the_users_tree_untouched(self):
        before = {p: p.stat().st_mtime_ns for p in (RTCC_PATH, VRT_INSTALLED, RC_PATH)}
        pycache = {p.parent / "__pycache__" for p in before}
        listing = {
            d: sorted(x.name for x in d.glob("*")) if d.exists() else None
            for d in pycache
        }
        schedulers.load_rtc_module(RTCC_PATH)
        load_vlash_runtime(VRT_INSTALLED)
        client_module()
        self.assertEqual(before, {p: p.stat().st_mtime_ns for p in before})
        self.assertEqual(
            listing,
            {
                d: sorted(x.name for x in d.glob("*")) if d.exists() else None
                for d in pycache
            },
        )


# ==============================================================================================================
# 1. Golden RTC against RtcChunker
# ==============================================================================================================


class GoldenRtcTests:
    @needs_openpi
    def test_200_scripted_episodes_match_rtcchunker_exactly(self):
        """Actions, request payloads (prev, delay), log lines and every common summary key, except the switch jump."""
        for ep in range(200):
            s = random_script(ep)
            a = run_rtc_original(s)
            b = run_rtc_phase(s, GOLDEN, self.make_engine)
            assert_same(
                self,
                a,
                b,
                f"episode {ep} (execute {s.exec_h})",
                strip_extra=True,
                jump=False,
            )
            self.assertIsNone(a.error, f"episode {ep}")

    @needs_openpi
    def test_200_scripted_episodes_report_rtcchunkers_switch_jump(self):
        """switch_jump_m_max and the `switch N mm` part of the log line: None when the old chunk is already over
        (rtcc:235), which is the case in about a third of the swaps of these scripts."""
        scripts = [random_script(ep) for ep in range(200)] + [
            Script(
                seed=11, n_ticks=120, exec_h=8, replies=((6, 0.55),), sync_lat=(0.2,)
            ),
            Script(
                seed=12, n_ticks=100, exec_h=3, replies=((11, 1.0),), sync_lat=(0.05,)
            ),
        ]
        for s in scripts:
            a = run_rtc_original(s)
            b = run_rtc_phase(s, GOLDEN, self.make_engine)
            assert_same_jump(self, a, b, f"script {s.seed} (execute {s.exec_h})")

    @needs_openpi
    def test_late_replies_starve_the_same_ticks(self):
        """Every reply 6 ticks late with execute 8 (the chunk ends 2 ticks after the request): starved every chunk."""
        s = Script(
            seed=11, n_ticks=120, exec_h=8, replies=((6, 0.55),), sync_lat=(0.2,)
        )
        a, b = run_rtc_original(s), run_rtc_phase(s, GOLDEN, self.make_engine)
        assert_same(self, a, b, "late replies", strip_extra=True, jump=False)
        self.assertGreater(a.summary["starved_ticks"], 5)
        self.assertEqual(a.summary["starved_ticks"], b.summary["starved_ticks"])

    @needs_openpi
    def test_a_reply_older_than_its_chunk_is_followed_by_a_synchronous_query(self):
        """delay >= H: the swapped-in chunk is already over; today queries synchronously without counting a starvation."""
        s = Script(
            seed=12, n_ticks=100, exec_h=3, replies=((11, 1.0),), sync_lat=(0.05,)
        )
        a, b = run_rtc_original(s), run_rtc_phase(s, GOLDEN, self.make_engine)
        assert_same(
            self, a, b, "reply older than its chunk", strip_extra=True, jump=False
        )

    @needs_openpi
    def test_request_errors_surface_at_the_same_tick(self):
        s = random_script(5, n_ticks=60)

        def failing(server_cls):
            class Failing(server_cls):
                def rtc_request(self, obs, prev, delay):
                    run = super().rtc_request(obs, prev, delay)
                    if (
                        prev is not None
                        and len([c for c in self.calls if c[0] == "rtc"]) == 2
                    ):

                        def boom():
                            raise RuntimeError("server went away")

                        return boom
                    return run

            return Failing

        original_server = globals()["FakeServer"]
        try:
            globals()["FakeServer"] = failing(original_server)
            a, b = run_rtc_original(s), run_rtc_phase(s, GOLDEN, self.make_engine)
        finally:
            globals()["FakeServer"] = original_server
        assert_same(self, a, b, "request error", strip_extra=True)
        self.assertEqual(a.error[1], "RuntimeError")
        self.assertIn("server went away", a.error[2])


class GoldenRtcRef(RefMixin, GoldenRtcTests, unittest.TestCase):
    pass


class GoldenRtcReal(RealMixin, GoldenRtcTests, unittest.TestCase):
    pass


# ==============================================================================================================
# 2. Golden VLASH against the installed runtime and the LEVI source
# ==============================================================================================================


class GoldenVlashTests:
    def _episodes(self, vrt_path, n, ticks=120):
        for ep in range(n):
            s = random_script(ep, vlash=True, n_ticks=ticks)
            a = run_vlash_original(s, vrt_path)
            b = run_vlash_phase(s, GOLDEN, self.make_engine)
            assert_same(self, a, b, f"episode {ep} (overlap {s.overlap})")
        return s

    @needs_openpi
    def test_installed_runtime(self):
        self._episodes(VRT_INSTALLED, 90)

    def test_levi_source_runtime(self):
        self._episodes(VRT_LEVI, 90)

    def test_every_overlap_and_the_counters_that_matter(self):
        stats = {
            "starvations": 0,
            "discarded_replies": 0,
            "adjusted_targets": 0,
            "prefetches": 0,
        }
        for overlap, ep in itertools.product((1, 2, 4), range(20)):
            s = random_script(ep, vlash=True, overlap=overlap, n_ticks=120)
            a = run_vlash_original(s, VRT_LEVI)
            b = run_vlash_phase(s, GOLDEN, self.make_engine)
            assert_same(self, a, b, f"overlap {overlap} episode {ep}")
            for key in stats:
                stats[key] += a.summary[key]
        for key, total in stats.items():
            self.assertGreater(total, 0, f"no script exercises {key}")

    def test_a_closed_scheduler_refuses_like_the_original(self):
        s = Script(seed=0, n_ticks=3)
        r = run_vlash_phase(s, GOLDEN, self.make_engine)
        with self.assertRaisesRegex(RuntimeError, "already closed"):
            r.sched.next_action(make_obs(9, 1.0))

    def test_invalid_horizon_or_overlap(self):
        for overlap, horizon in ((0, 10), (10, 10), (True, 10), (2, 10.0)):
            with self.assertRaises(ValueError):
                PhaseVlashChunker(
                    lambda o, f: None,
                    overlap_steps=overlap,
                    horizon=horizon,
                    params=GOLDEN,
                    transport=SimTransport(),
                )


class GoldenVlashRef(RefMixin, GoldenVlashTests, unittest.TestCase):
    pass


class GoldenVlashReal(RealMixin, GoldenVlashTests, unittest.TestCase):
    pass


# ==============================================================================================================
# 3. Golden plain
# ==============================================================================================================


class GoldenPlainTests:
    def _plain(self, h):
        return PlainPhase(
            GOLDEN,
            h,
            engine=self.make_engine(GOLDEN, 10.0) if self.make_engine else None,
        )

    def test_on_query_and_on_action_return_the_chunk_rows_bit_for_bit(self):
        for ep in range(60):
            rng = np.random.default_rng(300 + ep)
            h = int(rng.choice([1, 3, 6, 8, 10]))
            pp = self._plain(h)
            state = np.array([0.4, 0, 0.3, 3.0, 0, 0, 1.0], dtype=np.float32)
            for q in range(4):
                chunk = make_rows((ep, q, 0), H=int(rng.choice([6, 10, 12])))
                plan = pp.on_query(chunk, state, 0.4)
                want = min(h, len(chunk))
                self.assertEqual(
                    plan.shape, (want, 7), f"placeholder length is h (episode {ep})"
                )
                self.assertEqual(plan.dtype, np.float32)
                np.testing.assert_array_equal(plan, chunk[:h])
                for i in range(
                    len(plan)
                ):  # the client's loop executes min(h, len(plan)) ticks
                    a = pp.on_action(state, 0.4)
                    self.assertEqual(a.dtype, np.float32)
                    self.assertTrue(
                        np.array_equal(a, chunk[i]), f"episode {ep} query {q} row {i}"
                    )
                    pp.executed(sent_of(a))
                self.assertEqual(pp.plain_end_holds, 0)

    def test_a_rejected_plan_is_dropped_and_the_next_query_starts_clean(self):
        pp = self._plain(6)
        state = np.array([0.4, 0, 0.3, 3.0, 0, 0, 1.0], dtype=np.float32)
        c1, c2 = make_rows((1, 1, 1)), make_rows((1, 2, 2))
        pp.on_query(c1, state)
        a = pp.on_action(state)
        self.assertTrue(np.array_equal(a, c1[0]))
        pp.rejected()
        pp.on_query(c2, state)
        self.assertTrue(np.array_equal(pp.on_action(state), c2[0]))

    def test_non_finite_chunk_raises(self):
        pp = self._plain(6)
        bad = make_rows((2, 2, 2))
        bad[3, 1] = np.nan
        with self.assertRaises(NonFiniteChunk):
            pp.on_query(bad, np.zeros(7, np.float32))

    def test_action_before_query(self):
        pp = self._plain(6)
        with self.assertRaises(RuntimeError):
            pp.on_action(np.zeros(7, np.float32))


class GoldenPlainRef(RefMixin, GoldenPlainTests, unittest.TestCase):
    pass


class GoldenPlainReal(RealMixin, GoldenPlainTests, unittest.TestCase):
    pass


# ==============================================================================================================
# 4./5. The accelerated schedulers (structure on the reference engine, speed on the core)
# ==============================================================================================================

# The linear world of the accelerated tests: a policy that follows one straight line x(phi) = x0 + d * phi, with a
# gripper track that closes at CLOSE_AT and opens at OPEN_AT (policy rows). Chunks are exact samples of the line (and
# an async request is guided by prev, so it continues exactly where prev says), so any interpolation of any chunk is
# on the line and the phase of a pose can be read back: phi = (x - x0) / dx.
X0 = np.array([0.05, 0.10, 0.30, 3.0, 0.0, -0.5])
DX = np.array([0.001, 0.0004, 0.0, 0.0, 0.0, 0.0005])
CLOSE_AT, OPEN_AT = 120, 400


def line(phi: float) -> np.ndarray:
    return X0 + DX * phi


def grip_at(phi: float) -> float:
    return 0.0 if CLOSE_AT <= math.floor(phi) < OPEN_AT else 1.0


def line_rows(phi0: float, H: int = 10) -> np.ndarray:
    return np.array(
        [[*line(phi0 + j), grip_at(phi0 + j)] for j in range(H)], dtype=np.float32
    )


def phase_of(pose6: np.ndarray) -> float:
    return float((np.asarray(pose6, np.float64)[0] - X0[0]) / DX[0])


class LineServer:
    """A guided policy on the line world: an RTC request returns the line continued from prev[0] (perfect guidance),
    a VLASH prefetch the line continued from the row after `future`, a synchronous request the line from the phase the
    loop reports. Every call is recorded with what it was asked."""

    def __init__(self, H: int = 10, lat: float = 0.083, clock: Clock | None = None):
        self.H, self.lat, self.clock = H, lat, clock
        self.calls: list[dict] = []
        self.chunks: list[np.ndarray] = []

    def _answer(self, phi0: float, call: dict):
        rows = line_rows(phi0, self.H)
        call["chunk_index"] = len(self.chunks)
        self.chunks.append(rows)
        self.calls.append(call)
        return rows

    def rtc_request(self, obs, prev, delay):
        if prev is None:
            rows = self._answer(
                math.floor(obs["phase"]) + 1.0, {"kind": "sync", "obs": obs["id"]}
            )
        else:
            rows = self._answer(
                phase_of(np.asarray(prev)[0, :6]),
                {
                    "kind": "rtc",
                    "obs": obs["id"],
                    "prev": np.array(prev),
                    "delay": int(delay),
                },
            )

        def run():
            if prev is None and self.clock is not None:
                self.clock.now += self.lat
            return rows.copy(), {}

        return run

    def vlash_request(self, obs, future):
        if future is None:
            rows = self._answer(
                math.floor(obs["phase"]) + 1.0, {"kind": "sync", "obs": obs["id"]}
            )
        else:
            rows = self._answer(
                phase_of(np.asarray(future)[:6]) + 1.0,
                {"kind": "prefetch", "obs": obs["id"], "future": np.array(future)},
            )

        def run():
            if future is None and self.clock is not None:
                self.clock.now += self.lat
            return rows.copy(), {}

        return run


class Arm:
    """A first-order follower of the target (0.4 of the gap per tick, about the controller's 0.2 s) with the issued
    gripper command and a finger aperture that starts moving 0.2 s after a command."""

    def __init__(self):
        self.pose = np.array([*line(0.0), 1.0], dtype=np.float64)
        self.g_cmd, self.aperture, self._closing_at = 1.0, 0.0, None
        self.t = 0

    def obs(self, k: int, phase_s: float) -> dict:
        state = self.pose.astype(np.float32)
        state[6] = self.g_cmd
        return {
            "id": k,
            "phase": phase_s,
            "robot_state": {
                "cartesian_position": state,
                "gripper_position": self.aperture,
            },
        }

    def step(self, sent: np.ndarray) -> None:
        gap = np.asarray(sent[:6], np.float64) - self.pose[:6]
        gap[3:6] = phase.wrap_to_pi(gap[3:6])
        self.pose[:6] += 0.4 * gap
        if float(sent[6]) != self.g_cmd:
            self.g_cmd, self._closing_at = float(sent[6]), self.t + 2
        if self._closing_at is not None and self.t >= self._closing_at:
            target = 0.08 if self.g_cmd == 0.0 else 0.0
            self.aperture += float(np.clip(target - self.aperture, -0.02, 0.02))
        self.t += 1


def latency_trace(n: int, seed: int = 4) -> list[float]:
    """The measured distribution: median 83 ms, 10 % in 177-261 ms."""
    rng = np.random.default_rng(seed)
    return [
        float(rng.uniform(0.177, 0.261))
        if rng.random() < 0.10
        else float(rng.normal(0.083, 0.006))
        for _ in range(n)
    ]


def run_line(
    kind: str,
    params: PhaseParams,
    make_engine,
    *,
    ticks: int,
    latencies=None,
    exec_h=6,
    overlap=2,
    H=10,
    resets=(),
    rc_clamp=None,
    margin=None,
    sync_lat=0.083,
):
    """One closed-loop episode on the line world with the arm model. Returns a dict with everything the assertions need.
    ``sync_lat`` is how long a synchronous request blocks (the first call of an episode, for example)."""
    clock = Clock()
    server = LineServer(H=H, lat=sync_lat, clock=clock)
    lat = list(latencies) if latencies is not None else [0.083]
    transport = StrictSimTransport.from_seconds(lat, period_s=0.1)
    engine = make_engine(params, 10.0) if make_engine else None
    if kind == "rtc":
        sched = PhaseRtcChunker(
            server.rtc_request,
            execute_horizon=exec_h,
            period_s=0.1,
            params=params,
            transport=transport,
            say=lambda _s: None,
            clock=clock.monotonic,
            engine=engine,
            early_margin_ticks=margin,
        )
    else:
        sched = PhaseVlashChunker(
            server.vlash_request,
            overlap_steps=overlap,
            horizon=H,
            params=params,
            binarize_gripper=True,
            transport=transport,
            clock=clock.monotonic,
            engine=engine,
            early_margin_ticks=margin,
        )
    arm, trace = Arm(), []
    notes_seen = []
    args = None
    if rc_clamp is not None:
        rc = client_module()
        args = rc.Args()
    for k in range(ticks):
        clock.now += 0.1  # the loop's period: the engine reads the measurement time (gripper settle, speed)
        state = arm.obs(k, sched.engine.s)
        raw = sched.next_action(state)
        pose = state["robot_state"]["cartesian_position"]
        if rc_clamp is not None:
            sent, notes = rc._clamp_action(raw, pose, args)
            notes_seen.extend(notes)
            refused = any(n.startswith(("STEP", "ROT")) for n in notes)
        else:
            sent, refused = sent_of(raw), False
        # hook B: the clamp wrapper tells the engine what the client did with the target, then the client's own loop acts
        if (
            refused or k in resets
        ):  # a refused jump / a failed step: nothing is executed, the scheduler resets
            sched.engine.rejected()
            sched.reset()
            continue
        sched.engine.executed(sent)
        if kind == "vlash":
            sched.action_adjusted(raw, sent)
        trace.append(
            {
                "k": k,
                "raw": np.array(raw),
                "sent": sent,
                "s": sched.engine.s,
                "u": sched.engine.u,
                "lam": sched.engine.lam,
                "state": arm.pose.copy(),
            }
        )
        arm.step(sent)
    sched.close()
    return {
        "sched": sched,
        "server": server,
        "transport": transport,
        "trace": trace,
        "notes": notes_seen,
        "summary": sched.summary(),
        "requests": list(sched.request_log),
        "replies": list(getattr(sched, "reply_log", [])),
    }


def run_plain_line(params, make_engine, *, h, ticks, rc_clamp=True):
    """The client's plain open-loop loop (rc:1582-1605) on the line world: query when the plan is used up, one hook-B
    target per tick, the client's own clamp (the real one when openpi is there), the arm model."""
    clock = Clock()
    pp = PlainPhase(
        params,
        h,
        engine=make_engine(params, 10.0) if make_engine else None,
        clock=clock.monotonic,
    )
    arm, trace, notes_seen = Arm(), [], []
    rc = client_module() if rc_clamp else None
    args = rc.Args() if rc_clamp else None
    plan, idx, queries, refused = None, 0, 0, 0
    for k in range(ticks):
        clock.now += 0.1
        state = arm.obs(k, 0.0)["robot_state"]["cartesian_position"]
        aperture = arm.aperture
        if plan is None or idx >= min(h, len(plan)):
            chunk = line_rows(
                phase_of(state[:6]) + 1.0
            )  # the policy continues from where it sees the arm
            plan, idx, queries = pp.on_query(chunk, state, aperture), 0, queries + 1
        raw = pp.on_action(state, aperture)
        idx += 1
        if rc_clamp:
            sent, notes = rc._clamp_action(raw, state, args)
            notes_seen.extend(notes)
        else:
            sent, notes = sent_of(raw), []
        if any(n.startswith(("STEP", "ROT")) for n in notes):
            pp.rejected()
            plan, refused = None, refused + 1
            continue
        pp.executed(sent)
        trace.append(
            {
                "k": k,
                "raw": np.array(raw),
                "sent": sent,
                "plan_len": len(plan),
                "arm": arm.pose.copy(),
            }
        )
        arm.step(sent)
    return {
        "pp": pp,
        "trace": trace,
        "queries": queries,
        "refused": refused,
        "notes": notes_seen,
        "arm_phase": phase_of(arm.pose[:6]),
    }


class StrictSimTransport(SimTransport):
    """A SimTransport that fails the test the moment a request starts while an earlier one has not replied."""

    def start(self, fn, obs_tick, sent_delay):
        for h in self.handles:
            if not h.ready():
                raise AssertionError(
                    f"a request was started at tick {obs_tick} while the one from tick {h.obs_tick} was still in flight"
                )
        return super().start(fn, obs_tick, sent_delay)


class AccelRtcTests:
    params_golden = False
    H = 10

    def _params(self):
        return GOLDEN if self.params_golden else ACCEL

    def test_the_prev_rows_are_the_policys_own_grid_and_one_request_at_a_time(self):
        # StrictSimTransport fails the run if a request starts before the previous reply
        r = run_line(
            "rtc",
            self._params(),
            self.make_engine,
            ticks=200,
            latencies=latency_trace(60, seed=9),
        )
        calls = [c for c in r["server"].calls if c["kind"] == "rtc"]
        self.assertEqual(len(calls), len(r["requests"]))
        self.assertGreater(len(calls), 5)
        offsets = []
        for call, rec in zip(calls, r["requests"]):
            prev = call["prev"]
            want = math.floor(self.H - 1 - rec["u"] + 1e-9) + 1
            self.assertEqual(
                len(prev),
                want,
                f"prev covers X_raw(u + j) for j = 0..floor(H-1-u) (u = {rec['u']})",
            )
            self.assertEqual(rec["prev_rows"], len(prev))
            # rows of one chunk at consecutive integer offsets: exactly one policy row apart on the line
            np.testing.assert_allclose(
                np.diff(prev[:, :6], axis=0), np.tile(DX, (len(prev) - 1, 1)), atol=1e-6
            )
            self.assertEqual(prev.dtype, np.float32)
            offsets.append(phase_of(prev[0, :6]) - rec["s_obs"])
        # a guided policy continues prev, so the policy row behind a reference phase s never drifts from s
        self.assertLess(
            np.ptp(offsets),
            1e-3,
            "prev[0] is X_raw(u): the policy's own row at the reference phase",
        )

    def test_the_delay_is_recorded_in_rows_and_ticks(self):
        r = run_line(
            "rtc",
            self._params(),
            self.make_engine,
            ticks=200,
            latencies=latency_trace(60, seed=2),
        )
        s, replies = r["summary"], r["replies"]
        self.assertGreater(len(replies), 5)
        self.assertEqual(len(s["delays_rows"]), len(replies))
        self.assertEqual(sum(s["delays"].values()), len(replies))
        for rec in replies:
            self.assertGreater(rec["d_rows"], 0)
            if self.params_golden:
                self.assertEqual(
                    rec["d_rows"], rec["d_ticks"]
                )  # one row per tick today
            elif not self.models_core:
                self.assertGreater(
                    rec["d_rows"], rec["d_ticks"]
                )  # lambda 2.5 walks more rows than ticks
        self.assertIn("blend_ticks", s)
        engine = r["sched"].engine
        if hasattr(
            engine, "values"
        ):  # the scheduler pushes the delays into the engine's summary
            self.assertEqual(
                engine.values["delays_rows"], [x["d_rows"] for x in replies]
            )

    def test_early_trigger_starts_requests_before_the_execute_horizon(self):
        r = run_line("rtc", self._params(), self.make_engine, ticks=200, exec_h=8)
        us = [x["u"] for x in r["requests"]]
        self.assertTrue(us)
        if self.params_golden:
            self.assertTrue(
                all(u >= 8 for u in us), "golden: only at the page's execute horizon"
            )
            self.assertEqual(r["sched"].stats["early_triggers"], 0)
        else:
            self.assertTrue(
                any(u < 8 for u in us),
                "accelerated: earlier than the page's horizon when the rows left fit the delay",
            )
            self.assertGreater(r["sched"].stats["early_triggers"], 0)
            self.assertTrue(all(x["early"] == (x["u"] < 8) for x in r["requests"]))

    def test_reset_keeps_the_phase_and_the_pipeline_restarts_with_a_synchronous_query(
        self,
    ):
        r = run_line(
            "rtc",
            self._params(),
            self.make_engine,
            ticks=120,
            resets={30, 70},
            latencies=latency_trace(40, seed=3),
        )
        syncs = [c for c in r["server"].calls if c["kind"] == "sync"]
        self.assertGreaterEqual(len(syncs), 3)  # the start, and one after each reset
        ss = [t["s"] for t in r["trace"]]
        self.assertTrue(
            all(b >= a - 1e-9 for a, b in itertools.pairwise(ss)),
            "the phase never rewinds",
        )


class AccelRtcGoldenRef(RefMixin, AccelRtcTests, unittest.TestCase):
    params_golden = True


class AccelRtcRef(RefMixin, AccelRtcTests, unittest.TestCase):
    pass


class AccelRtcSpeedTests:
    """Speed assertions of the accelerated RTC scheduler: they need the core (governor, leash, rates). Run at lambda 2.5
    (``ACCEL``) and at the page default 3.0 (``SHIPPED``)."""

    H = 10
    P = ACCEL

    def test_83_ms_replies_never_starve_the_pipeline(self):
        r = run_line("rtc", self.P, self.make_engine, ticks=300)
        self.assertEqual(r["summary"]["starved_ticks"], 0)
        self.assertGreater(
            r["trace"][-1]["s"],
            300 * 1.5,
            "the phase clock really runs faster than the nominal rows",
        )

    def test_real_latency_trace_starves_at_most_one_percent_of_ticks(self):
        r = run_line(
            "rtc", self.P, self.make_engine, ticks=400, latencies=latency_trace(80)
        )
        self.assertLessEqual(r["summary"]["starved_ticks"], 4)
        self.assertGreater(len(r["requests"]), 5)

    def test_a_tick_of_margin_on_the_expected_delay_removes_the_tail_starvation(self):
        """The design's early trigger (margin 0) starves up to ~5 % of the ticks behind a 3-tick reply at lambda 2.5
        (see EARLY_MARGIN_TICKS); the default, one tick of margin, keeps the starved ticks at or under 1 % (4 of 400) on
        five traces of the measured distribution, and an explicit margin of 1 is the same run."""
        self.assertEqual(schedulers.EARLY_MARGIN_TICKS, 1)
        starved, design, explicit = [], [], []
        for seed in (4, 5, 6, 7, 8):
            lat = latency_trace(80, seed=seed)
            r = run_line("rtc", self.P, self.make_engine, ticks=400, latencies=lat)
            starved.append(r["summary"]["starved_ticks"])
            design.append(
                run_line(
                    "rtc",
                    self.P,
                    self.make_engine,
                    ticks=400,
                    latencies=lat,
                    margin=0,
                )["summary"]["starved_ticks"]
            )
            explicit.append(
                run_line(
                    "rtc",
                    self.P,
                    self.make_engine,
                    ticks=400,
                    latencies=lat,
                    margin=1,
                )["summary"]["starved_ticks"]
            )
        self.assertEqual(starved, explicit)
        # Measured 2026-11-02 (strong profile, after the review fixes), starved ticks of 400 per trace, default margin:
        # lambda 2.5: 0, 4, 4, 0, 0; lambda 3.0: 3, 4, 4, 0, 0 (at most 1.0 %). Margin 0: lambda 2.5: 5, 13, 19, 9, 5
        # (up to 4.75 %); lambda 3.0: 3, 4, 4, 1, 0 (lambda 3.0 is held by its caps more often, so the trigger matters less).
        self.assertLessEqual(
            max(starved), 4, f"starved ticks of 400 per trace: {starved}"
        )
        self.assertGreater(
            sum(design), sum(starved), f"design formula {design} vs default {starved}"
        )


class AccelRtcSpeed(RealMixin, AccelRtcSpeedTests, unittest.TestCase):
    pass


class AccelRtcSpeedShipped(RealMixin, AccelRtcSpeedTests, unittest.TestCase):
    P = SHIPPED


class AccelRtcGoldenReal(RealMixin, AccelRtcTests, unittest.TestCase):
    params_golden = True


class AccelRtcReal(RealMixin, AccelRtcTests, unittest.TestCase):
    pass


class AccelVlashTests:
    H = 10
    params_golden = False

    def _params(self):
        return GOLDEN if self.params_golden else ACCEL

    def test_the_future_state_is_the_last_row_of_the_chunk_it_continues(self):
        r = run_line(
            "vlash",
            self._params(),
            self.make_engine,
            ticks=200,
            latencies=latency_trace(60, seed=5),
        )
        calls, chunks = r["server"].calls, r["server"].chunks
        prefetches = [c for c in calls if c["kind"] == "prefetch"]
        self.assertGreater(len(prefetches), 5)
        for c in prefetches:
            before = chunks[c["chunk_index"] - 1]
            np.testing.assert_array_equal(
                c["future"], before[-1], "future == chunk[-1], exactly (I1)"
            )
            self.assertEqual(c["future"].dtype, np.float32)

    def test_the_prefetch_starts_at_the_pages_overlap_or_earlier_when_accelerated(self):
        r = run_line("vlash", self._params(), self.make_engine, ticks=200, overlap=2)
        reqs = r["requests"]
        self.assertGreater(len(reqs), 5)
        page = 2 - 1  # the page's position u == H - overlap leaves overlap - 1 rows
        if self.params_golden:
            self.assertTrue(
                all(abs(x["rows_left"] - page) < 1e-9 for x in reqs),
                "golden: u == H - overlap",
            )
            self.assertEqual(r["sched"].extra["early_prefetches"], 0)
        else:
            self.assertTrue(
                all(x["rows_left"] >= page - 1e-9 for x in reqs),
                "never later than the page's position",
            )
            self.assertTrue(
                any(x["rows_left"] > page + 1e-9 for x in reqs),
                "and earlier when lambda > 1",
            )
            self.assertGreater(r["sched"].extra["early_prefetches"], 0)

    def test_the_prefetch_joins_when_it_arrives_accelerated_and_at_the_join_in_golden(
        self,
    ):
        appended_at = []

        def make_engine(params, hz):
            engine = (self.make_engine or phase.PhaseEngine)(params, hz)
            real_append = engine.append

            def append(rows):
                appended_at.append(engine.rows_left)
                return real_append(rows)

            engine.append = append
            return engine

        run_line("vlash", self._params(), make_engine, ticks=200, latencies=[0.083])
        self.assertGreater(len(appended_at), 5)
        if self.params_golden:
            self.assertTrue(
                all(abs(x) < 1e-9 for x in appended_at),
                f"golden: at the join, like VlashChunker {appended_at}",
            )
        else:
            self.assertTrue(
                all(x > 0.5 for x in appended_at),
                f"accelerated: as soon as the reply is here {appended_at}",
            )

    @needs_openpi
    def test_driven_by_the_clients_own_clamp_no_target_is_ever_adjusted(self):
        r = run_line(
            "vlash",
            self._params(),
            self.make_engine,
            ticks=250,
            rc_clamp=True,
            latencies=latency_trace(60, seed=6),
        )
        self.assertEqual(
            r["summary"]["adjusted_targets"],
            0,
            "no clamp (min_z / xy / rpy / gripper) changed an engine target",
        )
        self.assertEqual(
            [n for n in r["notes"] if n.startswith(("STEP", "ROT"))],
            [],
            "no STEP / ROT refusal either",
        )
        self.assertGreater(len(r["trace"]), 200)
        for t in r["trace"]:
            self.assertEqual(t["raw"].dtype, np.float32)

    def test_the_prefetched_chunk_is_appended_behind_the_current_one(self):
        r = run_line("vlash", self._params(), self.make_engine, ticks=150)
        self.assertGreater(
            len([c for c in r["server"].calls if c["kind"] == "prefetch"]), 5
        )
        self.assertGreater(
            r["trace"][-1]["s"],
            2 * self.H,
            "the phase ran through several appended chunks",
        )
        self.assertEqual(r["summary"]["adjusted_targets"], 0)
        engine = r["sched"].engine
        if hasattr(engine, "n_appends"):
            self.assertGreater(engine.n_appends, 5)
            self.assertEqual(
                engine.n_sets,
                1,
                "one synchronous chunk at the start, then only appends",
            )


class _ReadyReply:
    """A prefetch handle whose reply is already here."""

    def __init__(self, rows: np.ndarray):
        self.rows = np.asarray(rows, np.float32)
        self.latency_s = 0.05
        self.done = threading.Event()
        self.done.set()

    def take(self):
        return self.rows.copy(), {}

    def wait(self, limit=None):
        return None


class VlashColdStartAndGraspReplanTests(unittest.TestCase):
    """Real-run findings of 2026-11-02 (VLASH, phase clock): a cold server's first call stretched the expected reply
    delay and the queue (plans up to 5 s old), and grasps were planned from a state 79-95 mm from where the camera saw
    the arm. Real engine."""

    def _loaded(self, params):
        clock = Clock()
        server = LineServer(H=10, clock=clock)
        sched = PhaseVlashChunker(
            server.vlash_request,
            overlap_steps=2,
            horizon=10,
            params=params,
            binarize_gripper=True,
            transport=StrictSimTransport.from_seconds([0.083], period_s=0.1),
            clock=clock.monotonic,
        )
        arm = Arm()
        clock.now += 0.1
        sched.next_action(arm.obs(0, 0.0))  # the synchronous first chunk
        self.assertEqual([c["kind"] for c in server.calls], ["sync"])
        return sched, server, arm

    @staticmethod
    def _closing_rows(phi0: float = 12.0) -> np.ndarray:
        rows = line_rows(phi0)
        rows[5:, 6] = (
            0.0  # the reply closes the gripper at its row 5 (the queue ends open)
        )
        return rows

    def test_a_slow_first_call_neither_stretches_the_delay_nor_the_queue(self):
        r = run_line("vlash", SHIPPED, None, ticks=200, sync_lat=1.5)
        self.assertAlmostEqual(r["summary"]["latency_ms_first"], 1500.0, delta=1.0)
        reqs = r["requests"]
        self.assertGreater(len(reqs), 5)
        worst = max(x["rows_left"] for x in reqs)
        self.assertLessEqual(
            worst,
            10 + 1e-9,
            f"a prefetch started with {worst:.1f} rows queued: more than one chunk ahead (11-02: 9 -> 31 rows)",
        )
        self.assertEqual(
            r["sched"]._d_hat(),
            1 + schedulers.EARLY_MARGIN_TICKS,
            "83 ms prefetch replies: one tick; the 1.5 s synchronous call is not a reply delay",
        )
        self.assertEqual(r["summary"]["starvations"], 0)
        self.assertEqual(
            r["summary"]["grasp_replans"], 0, "1 mm rows never open a 35 mm gap"
        )

    def test_the_prefetch_records_how_far_its_state_is_from_the_arm(self):
        sched, server, arm = self._loaded(SHIPPED)
        obs = arm.obs(1, sched.engine.s)
        obs["robot_state"]["cartesian_position"][0] -= (
            0.06  # the camera sees the arm 6 cm behind
        )
        out = types.SimpleNamespace(lam=3.0, s=sched.engine.s, u=sched.engine.u)
        sched._maybe_prefetch(obs, 1, out)
        self.assertIsNotNone(sched.pending)
        future = server.calls[-1]["future"]
        here = np.asarray(obs["robot_state"]["cartesian_position"], np.float64)[:3]
        self.assertAlmostEqual(
            sched._pending_gap, float(np.linalg.norm(future[:3] - here)), places=6
        )
        self.assertGreater(sched._pending_gap, 0.05)

    def test_a_close_planned_from_a_far_state_is_planned_again_from_the_arm(self):
        sched, server, arm = self._loaded(SHIPPED)
        sched.pending, sched._pending_gap = _ReadyReply(self._closing_rows()), 0.05
        sched._append_pending(arm.obs(1, sched.engine.s), 1)
        self.assertEqual(
            [c["kind"] for c in server.calls],
            ["sync", "sync"],
            "the close reply is dropped and the chunk asked again with no future state",
        )
        self.assertEqual(sched.stats["grasp_replans"], 1)
        self.assertIsNone(sched.pending)
        np.testing.assert_array_equal(
            sched.engine.last_gripper, server.chunks[-1][-1][6]
        )
        self.assertLessEqual(sched.engine.rows_left, 10.0)

    def test_a_close_planned_near_the_arm_is_joined(self):
        sched, server, arm = self._loaded(SHIPPED)
        sched.pending, sched._pending_gap = _ReadyReply(self._closing_rows()), 0.02
        sched._append_pending(arm.obs(1, sched.engine.s), 1)
        self.assertEqual([c["kind"] for c in server.calls], ["sync"])
        self.assertEqual(sched.stats["grasp_replans"], 0)
        self.assertEqual(sched.engine.last_gripper, 0.0)

    def test_a_far_reply_without_a_close_is_joined(self):
        sched, server, arm = self._loaded(SHIPPED)
        sched.pending, sched._pending_gap = _ReadyReply(line_rows(12.0)), 0.09
        sched._append_pending(arm.obs(1, sched.engine.s), 1)
        self.assertEqual([c["kind"] for c in server.calls], ["sync"])
        self.assertEqual(sched.stats["grasp_replans"], 0)

    def test_golden_and_the_switch_off_never_replan(self):
        for params in (GOLDEN, dataclasses.replace(SHIPPED, grasp_replan_m=0.0)):
            with self.subTest(golden=params.golden):
                sched, server, arm = self._loaded(params)
                sched.pending, sched._pending_gap = (
                    _ReadyReply(self._closing_rows()),
                    0.09,
                )
                sched._append_pending(arm.obs(1, sched.engine.s), 1)
                self.assertEqual([c["kind"] for c in server.calls], ["sync"])
                self.assertEqual(sched.stats["grasp_replans"], 0)

    def test_replies_slower_than_a_tick_keep_the_queue_within_one_chunk(self):
        r = run_line("vlash", SHIPPED, None, ticks=200, latencies=[0.3])
        worst = max(x["rows_left"] for x in r["requests"])
        self.assertLessEqual(
            worst, 10 + 1e-9, f"{worst:.1f} rows queued at a prefetch (one chunk is 10)"
        )

    def test_a_slow_synchronous_call_stays_out_of_the_delay_history(self):
        sched, server, arm = self._loaded(SHIPPED)
        server.lat = 1.5  # a mid-episode synchronous call that waits for a cold server
        sched._sync(arm.obs(1, sched.engine.s))
        self.assertEqual(len(sched._lat_hist), 0)
        self.assertEqual(sched._d_hat(), 1 + schedulers.EARLY_MARGIN_TICKS)
        self.assertGreater(sched.latency_ms[-1], 1400.0, "still reported")

    def test_a_reply_that_is_closed_from_its_first_row_counts_as_a_close(self):
        sched, _server, arm = self._loaded(SHIPPED)
        rows = line_rows(12.0)
        rows[:, 6] = (
            0.0  # the close is at the join: the queue ends open, the reply starts closed
        )
        sched.pending, sched._pending_gap = _ReadyReply(rows), 0.05
        sched._append_pending(arm.obs(1, sched.engine.s), 1)
        self.assertEqual(sched.stats["grasp_replans"], 1)

    def test_the_ninth_far_close_in_a_row_is_joined(self):
        sched, _server, arm = self._loaded(SHIPPED)
        cap = schedulers.GRASP_REPLAN_MAX_IN_ROW
        for i in range(cap + 1):
            sched.pending, sched._pending_gap = (
                _ReadyReply(self._closing_rows()),
                0.09,
            )
            sched._append_pending(arm.obs(1 + i, sched.engine.s), 1 + i)
        self.assertEqual(sched.stats["grasp_replans"], cap)
        self.assertEqual(sched.engine.last_gripper, 0.0, "the last one was joined")

    def test_a_replan_while_the_arm_lags_never_steps_the_target_back(self):
        """Closed loop, 8 mm rows at lambda 3 (the arm lags 50-60 mm), the policy planning a synchronous chunk from
        where the camera sees the arm: the close is re-planned, sent, and the target never moves backward."""
        here = sys.modules[__name__]
        orig = LineServer.vlash_request

        def from_arm(self, obs, future):
            if future is None:
                obs = dict(obs)
                obs["phase"] = phase_of(
                    np.asarray(obs["robot_state"]["cartesian_position"])[:6]
                )
            return orig(self, obs, future)

        dx = np.array([0.008, 0.0, 0.0, 0.0, 0.0, 0.0])
        with (
            mock.patch.object(here, "DX", dx),
            mock.patch.object(LineServer, "vlash_request", from_arm),
        ):
            r = run_line("vlash", SHIPPED, None, ticks=120, latencies=[0.083])
        s = r["summary"]
        self.assertGreaterEqual(s["grasp_replans"], 1)
        self.assertEqual(s["starvations"], 0)
        self.assertEqual(s["sync_fallbacks"], 0)
        self.assertEqual(
            r["sched"].engine.summary()["grasp_replans"], s["grasp_replans"]
        )
        sent = [t["sent"] for t in r["trace"]]
        self.assertTrue(any(x[6] == 0.0 for x in sent), "the close was sent")
        back = [
            round(1000 * float(b[0] - a[0]), 1)
            for a, b in itertools.pairwise(sent)
            if b[0] < a[0] - 1e-6
        ]
        self.assertEqual(back, [], "backward target steps (mm)")

    def test_replan_is_refused_in_golden(self):
        engine = phase.PhaseEngine(GOLDEN, 10.0)
        with self.assertRaises(RuntimeError):
            engine.replan(line_rows(1.0))

    def test_replans_in_a_row_are_bounded(self):
        sched, server, arm = self._loaded(SHIPPED)
        sched._replans_in_row = schedulers.GRASP_REPLAN_MAX_IN_ROW
        sched.pending, sched._pending_gap = _ReadyReply(self._closing_rows()), 0.09
        sched._append_pending(arm.obs(1, sched.engine.s), 1)
        self.assertEqual([c["kind"] for c in server.calls], ["sync"])
        self.assertEqual(
            sched._replans_in_row, 0, "a joined reply starts the count again"
        )


class AccelVlashGoldenRef(RefMixin, AccelVlashTests, unittest.TestCase):
    params_golden = True


class AccelVlashRef(RefMixin, AccelVlashTests, unittest.TestCase):
    pass


class AccelVlashSpeedTests:
    """Speed assertions of the accelerated VLASH scheduler: they need the core. Lambda 2.5 and the page default 3.0."""

    H = 10
    P = ACCEL

    def test_83_ms_replies_never_starve_the_prefetch(self):
        r = run_line("vlash", self.P, self.make_engine, ticks=300)
        self.assertEqual(r["summary"]["starvations"], 0)
        self.assertEqual(r["summary"]["sync_fallbacks"], 0)
        self.assertGreater(r["trace"][-1]["s"], 300 * 1.5)

    @needs_openpi
    def test_real_latency_trace_starves_at_most_one_percent_of_ticks(self):
        starved = []
        for seed in (4, 5, 6, 7, 8):
            r = run_line(
                "vlash",
                self.P,
                self.make_engine,
                ticks=400,
                latencies=latency_trace(80, seed=seed),
            )
            starved.append(r["summary"]["starvations"])
        self.assertLessEqual(
            max(starved), 4, f"starved ticks of 400 per trace: {starved}"
        )


class AccelVlashSpeed(RealMixin, AccelVlashSpeedTests, unittest.TestCase):
    pass


class AccelVlashSpeedShipped(RealMixin, AccelVlashSpeedTests, unittest.TestCase):
    P = SHIPPED


class AccelVlashGoldenReal(RealMixin, AccelVlashTests, unittest.TestCase):
    params_golden = True


class AccelVlashReal(RealMixin, AccelVlashTests, unittest.TestCase):
    pass


class PlainLoopTests:
    """Plain on the phase clock inside the client's open-loop loop (hook A + hook B)."""

    def test_golden_the_loop_is_todays_loop(self):
        r = run_plain_line(GOLDEN, self.make_engine, h=6, ticks=120, rc_clamp=False)
        self.assertEqual(r["queries"], 120 // 6)
        self.assertEqual({t["plan_len"] for t in r["trace"]}, {6})
        self.assertEqual(r["pp"].plain_end_holds, 0)
        self.assertEqual(r["refused"], 0)

    @needs_openpi
    def test_accelerated_the_loop_asks_sooner_and_the_clients_checks_never_fire(self):
        if not self.models_core:
            self.skipTest("a plan shorter than h comes from the core's lambda law")
        golden = run_plain_line(GOLDEN, self.make_engine, h=6, ticks=240)
        r = run_plain_line(ACCEL, self.make_engine, h=6, ticks=240)
        self.assertEqual(
            r["notes"],
            [],
            "no clamp note, no STEP / ROT: the engine's targets are always inside the client's limits",
        )
        self.assertEqual(r["refused"], 0)
        self.assertEqual(
            r["pp"].plain_end_holds,
            0,
            "the loop never reaches the end of its plan before it asks again",
        )
        self.assertGreater(
            r["queries"],
            golden["queries"],
            "plans of fewer than h ticks: the loop queries sooner",
        )
        self.assertLess(min(t["plan_len"] for t in r["trace"]), 6)
        self.assertGreater(
            r["arm_phase"],
            golden["arm_phase"] * 1.15,
            "the arm covers more of the path in the same ticks",
        )


class PlainLoopRef(RefMixin, PlainLoopTests, unittest.TestCase):
    pass


class PlainLoopReal(RealMixin, PlainLoopTests, unittest.TestCase):
    pass


class PlainRecoveryTests(unittest.TestCase):
    """Plain when the engine drops its plan inside a placeholder (three leash holds), and when the arm is pushed away
    from the last target. Both used to end the episode (RuntimeError from on_action) or hold the arm off for ever."""

    POSE = np.array([0.45, 0.0, 0.30, 3.0, 0.0, -0.5, 1.0], np.float32)

    def _fast_rows(self, pose=None, step=0.04, h=10):
        p = self.POSE if pose is None else pose
        return np.array(
            [[p[0] + step * (j + 1), p[1], p[2], *p[3:6], 1.0] for j in range(h)],
            dtype=np.float32,
        )

    def test_three_forced_holds_then_on_action_holds_until_the_next_query(self):
        clock = Clock()
        pp = PlainPhase(ACCEL, 10, clock=clock.monotonic)
        plan = pp.on_query(self._fast_rows(), self.POSE, 0.0)
        self.assertGreaterEqual(len(plan), 8)
        outs = []
        for _ in range(
            len(plan)
        ):  # a stuck arm: the leash holds, the third hold drops the plan
            clock.now += 0.1
            out = pp.on_action(self.POSE, 0.0)  # must not raise
            pp.executed(out)
            outs.append(out)
        engine = pp.engine
        self.assertFalse(engine.has_rows(), "the third hold drops the plan")
        self.assertEqual(engine.summary()["hold_resets"], 1)
        dropped = pp.dropped_plan_holds
        self.assertGreaterEqual(dropped, 3)
        self.assertEqual(engine.summary()["plain_dropped_plan_holds"], dropped)
        self.assertEqual(pp.summary()["plain_dropped_plan_holds"], dropped)
        self.assertEqual(
            pp.plain_end_holds, 0, "a dropped plan is not a plan that ran out"
        )
        for out in outs[-dropped:]:
            np.testing.assert_array_equal(out, outs[-dropped - 1])
        # the client's own re-query at the end of the placeholder splices a new chunk and the run goes on
        clock.now += 0.1
        pp.on_query(self._fast_rows(self.POSE, step=0.002), self.POSE, 0.0)
        out = pp.on_action(self.POSE, 0.0)
        self.assertTrue(engine.has_rows())
        self.assertTrue(np.isfinite(out).all())

    def test_on_action_before_any_query_still_raises(self):
        pp = PlainPhase(ACCEL, 10)
        with self.assertRaisesRegex(RuntimeError, "before on_query"):
            pp.on_action(self.POSE, 0.0)

    def test_after_a_reset_and_a_dropped_plan_the_summary_counts_it(self):
        pp = PlainPhase(ACCEL, 6)
        pp.on_query(self._fast_rows(), self.POSE, 0.0)
        pp.on_action(self.POSE, 0.0)
        pp.reset()  # the client's own reject path: reset, then it queries again before the next action
        self.assertEqual(pp.dropped_plan_holds, 0)
        pp.on_action(
            self.POSE, 0.0
        )  # but if it did not, the target is held instead of raising
        self.assertEqual(pp.dropped_plan_holds, 1)

    def test_an_arm_pushed_9_cm_is_yielded_to_and_the_run_goes_on(self):
        """The loop of the client (rc:1582): query when the placeholder is used up. The arm is pushed 9 cm off the last
        target and held there; within 3 + ceil(0.09 / 0.05) ticks the target is inside the 0.08 m leash and a later query
        walks on from the arm."""
        clock = Clock()
        h = 6
        pp = PlainPhase(ACCEL, h, clock=clock.monotonic)
        pose = self.POSE.copy()
        plan, idx, last, dps, queries = None, 0, None, [], 0
        for k in range(60):
            clock.now += 0.1
            if k == 6:  # pushed away from where the last target is, and held
                pose = np.asarray(last, np.float32).copy()
                pose[1] += 0.09
            if plan is None or idx >= min(h, len(plan)):
                chunk = self._fast_rows(pose, step=0.004, h=10)
                plan, idx, queries = pp.on_query(chunk, pose, 0.0), 0, queries + 1
            out = pp.on_action(pose, 0.0)  # never raises
            idx += 1
            dp = float(np.linalg.norm(out[:3].astype(np.float64) - pose[:3]))
            dps.append(dp)
            self.assertLessEqual(dp, 0.10, f"tick {k}: the client would refuse it")
            if last is not None:
                self.assertLessEqual(
                    float(np.linalg.norm(out[:3].astype(np.float64) - last[:3])),
                    0.05 + 1e-6,
                    f"tick {k}",
                )
            pp.executed(out)
            last = out
            if k < 6:
                pose = out.copy()  # before the push the arm is at its target
        limit = 3 + math.ceil(0.09 / 0.05)
        self.assertLessEqual(dps[6 + limit - 1], 0.08 + 1e-9, dps[6 : 6 + limit])
        self.assertGreater(queries, 2)
        self.assertGreater(pp.engine.summary()["leash_recoveries"], 0)
        self.assertGreater(pp.engine.s, 20, "the phase advanced again")


class PlainTimestampTests(unittest.TestCase):
    """The engine's speed of the arm comes from successive measured poses and their times. Plain must stamp the time of
    the observation, not the clock at hook time: after a synchronous inference (85 ms) the next tick's look would be
    15 ms later, below the engine's same-tick guard, and the speed (the close gate's speed test, grip_events
    arm_speed_m_s, D_meas) would be wrong on two ticks of three."""

    V = 0.03  # true speed of the arm, m/s
    INFER_S = 0.085

    def _run(self, stamp: bool):
        clock = Clock()
        h = 4
        pp = PlainPhase(ACCEL, h, clock=clock.monotonic)
        x0 = np.array([0.40, 0.0, 0.30, 3.0, 0.0, -0.5, 1.0], np.float64)
        speeds, plan, idx = [], None, 0
        t_obs = 1000.0
        for k in range(48):
            t_obs = 1000.0 + 0.1 * k
            clock.now = t_obs
            pose = x0.copy()
            pose[0] += self.V * (t_obs - 1000.0)
            pose = pose.astype(np.float32)
            t = t_obs if stamp else None
            if plan is None or idx >= min(h, len(plan)):
                clock.now += self.INFER_S  # the synchronous inference of the query tick
                rows = np.array(
                    [[pose[0] + 0.0004 * (j + 1), 0.0, 0.30, 3.0, 0.0, -0.5, 1.0] for j in range(10)],
                    np.float32,
                )  # fmt: skip
                plan, idx = pp.on_query(rows, pose, 0.0, t), 0
            out = pp.on_action(pose, 0.0, t)
            idx += 1
            pp.executed(out)
            if k >= 3 and pp.engine._speed is not None:
                speeds.append(pp.engine._speed)
        return speeds

    def test_with_the_observation_time_the_speed_is_right_on_every_tick(self):
        speeds = self._run(stamp=True)
        self.assertGreater(len(speeds), 30)
        for v in speeds:
            self.assertAlmostEqual(v, self.V, delta=0.1 * self.V)

    def test_stamping_at_hook_time_is_what_broke_it(self):
        speeds = self._run(stamp=False)
        self.assertTrue(
            any(abs(v - self.V) > 0.1 * self.V for v in speeds),
            "the legacy stamp should be visibly wrong (this guards the test above against being vacuous)",
        )


# ==============================================================================================================
# 6. Hook C
# ==============================================================================================================


class FakeClient:
    def __init__(self):
        self.payloads: list[dict] = []

    def infer(self, payload):
        self.payloads.append(
            {
                k: (np.array(v) if isinstance(v, np.ndarray) else v)
                for k, v in payload.items()
            }
        )
        return {"actions": np.zeros((10, 7), np.float32)}


def image_obs() -> dict:
    img = (np.arange(8 * 8 * 3) % 251).astype(np.uint8).reshape(8, 8, 3)
    return {
        "image": {"view1": img, "hand": img[::-1].copy()},
        "robot_state": {
            "cartesian_position": np.array([0.4, 0, 0.3, 3.0, 0, 0, 1.0], np.float32),
            "gripper_position": 0.3,
        },
    }


class HookCTests(unittest.TestCase):
    def test_wrap_rtc_chunker_builds_from_the_originals_request_horizon_and_period(
        self,
    ):
        base = types.SimpleNamespace(
            _request=lambda obs, prev, delay: (
                lambda: (np.zeros((10, 7), np.float32), {})
            ),
            execute_horizon=6,
            period_s=0.1,
            _say=print,
        )
        seen = []

        def original(client, args, prompt):
            seen.append((client, args, prompt))
            return base

        wrapped = wrap_rtc_chunker(original, GOLDEN)
        c, a = object(), object()
        chunker = wrapped(c, a, "pick the plate")
        self.assertEqual(seen, [(c, a, "pick the plate")])
        self.assertIsInstance(chunker, PhaseRtcChunker)
        self.assertIs(chunker._request, base._request)
        self.assertEqual(
            (chunker.execute_horizon, chunker.period_s, chunker.mode), (6, 0.1, "rtc")
        )
        self.assertIs(chunker.params, GOLDEN)
        self.assertIsNotNone(chunker.engine)
        self.assertIs(wrapped.__wrapped__, original)

    @needs_openpi
    def test_the_payload_is_the_clients_own(self):
        rc = client_module()
        args = types.SimpleNamespace(rtc_execute_horizon=5, control_hz=10.0)
        c1, c2 = FakeClient(), FakeClient()
        orig = rc._rtc_chunker(c1, args, "pick the plate")
        new = wrap_rtc_chunker(rc._rtc_chunker, GOLDEN)(c2, args, "pick the plate")
        self.assertEqual(
            (new.execute_horizon, new.period_s), (orig.execute_horizon, orig.period_s)
        )
        obs = image_obs()
        prev = make_rows((1, 2, 3))[3:]
        for p, d in ((None, 0), (prev, 4)):
            orig._request(obs, p, d)()
            new._request(obs, p, d)()
        self.assertEqual(len(c1.payloads), len(c2.payloads))
        for x, y in zip(c1.payloads, c2.payloads):
            self.assertEqual(set(x), set(y))
            for key in x:
                if isinstance(x[key], np.ndarray):
                    self.assertTrue(
                        x[key].dtype == y[key].dtype and np.array_equal(x[key], y[key]),
                        key,
                    )
                else:
                    self.assertEqual(x[key], y[key], key)
        self.assertIn("rtc_prev_actions", c2.payloads[1])
        self.assertEqual(c2.payloads[1]["rtc_inference_delay"], 4)
        self.assertNotIn("rtc_prev_actions", c2.payloads[0])

    @needs_openpi
    def test_it_takes_the_requests_class_from_the_users_module_not_a_copy(self):
        rc = client_module()
        new = wrap_rtc_chunker(rc._rtc_chunker, GOLDEN)(
            FakeClient(),
            types.SimpleNamespace(rtc_execute_horizon=5, control_hz=10.0),
            "x",
        )
        new._transport_for_start()
        self.assertIs(new._transport.request_cls, rc._rtc._Request)
        self.assertEqual(new._max_seed, rc._rtc.MAX_SEED_DELAY)


# ==============================================================================================================
# 7. VLASH payload
# ==============================================================================================================


class VlashPayloadTests(unittest.TestCase):
    META: ClassVar[dict] = {
        "config": "pi05_fr3_all_state",
        "action_dim": 7,
        "discrete_state_input": True,
        "ee_frame": "franka_hand_tcp",
        "gripper_convention": "1.0=open, 0.0=closed, commanded not measured",
        "action_horizon": 10,
        "vlash": {
            "protocol_version": 1,
            "state_action_space": "fr3_absolute_pose_gripper_v1",
            "action_horizon": 10,
            "overlap_steps": 2,
        },
    }

    class Client:
        def __init__(self, meta):
            self.meta, self.payloads = meta, []

        def get_server_metadata(self):
            return self.meta

        def infer(self, payload):
            self.payloads.append(
                {
                    k: (np.array(v) if isinstance(v, np.ndarray) else v)
                    for k, v in payload.items()
                }
            )
            return {"actions": np.zeros((10, 7), np.float32)}

    @needs_openpi
    def test_payloads_equal_run_collect_vlash_factory_key_by_key(self):
        rc = client_module()
        vrt = load_ro("vrt_for_payload", VRT_INSTALLED)

        @dataclasses.dataclass
        class CollectorArgs:
            rtc: bool = False
            step_by_step: bool = False
            binarize_gripper: bool = True

        collector = types.SimpleNamespace(
            rc=rc, Args=CollectorArgs, main=lambda *a, **k: None
        )
        with patched_modules(
            run_robotiq_client_collect_data=collector,
            tyro=types.ModuleType("tyro"),
            runtime=vrt,
            rcv_under_test=None,
        ):
            rcv = load_ro("rcv_under_test", RCV_PATH, register=True)
        sys.modules.pop("rcv_under_test", None)
        args = types.SimpleNamespace(
            rtc=False, vlash_overlap_steps=2, binarize_gripper=True
        )
        obs, future = image_obs(), np.arange(7, dtype=np.float32) / 10
        c_orig, c_new = self.Client(self.META), self.Client(self.META)
        orig = rcv.factory(c_orig, args, "pick the plate")
        new = phase_vlash_factory(GOLDEN, vrt, rc_module=rc)(
            c_new, args, "pick the plate"
        )
        self.assertEqual(
            (new.overlap, new.horizon, new.binarize_gripper),
            (orig.overlap, orig.horizon, orig.binarize_gripper),
        )
        for f in (None, future):
            orig.request(obs, f)()
            new._request(obs, f)()
        self.assertEqual(len(c_orig.payloads), len(c_new.payloads))
        for x, y in zip(c_orig.payloads, c_new.payloads):
            self.assertEqual(set(x), set(y))
            for key in x:
                if isinstance(x[key], np.ndarray):
                    self.assertTrue(
                        x[key].dtype == y[key].dtype and np.array_equal(x[key], y[key]),
                        key,
                    )
                else:
                    self.assertEqual(x[key], y[key], key)
        self.assertEqual(c_new.payloads[0]["vlash_protocol_version"], 1)
        self.assertNotIn("vlash_future_state", c_new.payloads[0])
        np.testing.assert_array_equal(c_new.payloads[1]["vlash_future_state"], future)

    @needs_openpi
    @needs_openpi
    def test_the_factory_runs_the_prefetch_on_the_given_runtimes_request_class(self):
        rc = client_module()
        vrt = load_ro("vrt_for_transport", VRT_INSTALLED)
        args = types.SimpleNamespace(
            rtc=False, vlash_overlap_steps=2, binarize_gripper=True
        )
        sched = phase_vlash_factory(GOLDEN, vrt, rc_module=rc)(
            self.Client(self.META), args, "x"
        )
        self.assertEqual(sched.mode, "vlash")
        self.assertIs(sched._transport_for_start().request_cls, vrt._Request)
        self.assertEqual(sched.timeout, 30.0)

    def test_the_factory_validates_the_server_like_the_original(self):
        rc = client_module()
        vrt = load_ro("vrt_for_validation", VRT_INSTALLED)
        args = types.SimpleNamespace(
            rtc=False, vlash_overlap_steps=2, binarize_gripper=True
        )
        bad = dict(self.META, vlash=dict(self.META["vlash"], overlap_steps=3))
        with self.assertRaises(ValueError):
            phase_vlash_factory(GOLDEN, vrt, rc_module=rc)(self.Client(bad), args, "x")
        with self.assertRaises(ValueError):
            phase_vlash_factory(GOLDEN, vrt, rc_module=rc)(
                self.Client(self.META),
                types.SimpleNamespace(
                    rtc=True, vlash_overlap_steps=2, binarize_gripper=True
                ),
                "x",
            )

    def test_without_the_client_module_the_factory_says_so(self):
        with patched_modules(run_robotiq_client=None):
            f = phase_vlash_factory(
                GOLDEN, types.SimpleNamespace(check_server=lambda m, a: None)
            )
            with self.assertRaises(RuntimeError):
                f(self.Client(self.META), types.SimpleNamespace(), "x")


# ==============================================================================================================
# 8. Gripper exactness across swap / append
# ==============================================================================================================


def gripper_flips(trace):
    flips, last = [], 1.0
    for t in trace:
        g = float(t["sent"][6])
        if g != last:
            flips.append((t["k"], g, t["raw"][:6].astype(np.float64)))
            last = g
    return flips


def change_rows(chunks):
    """Every row at which a chunk's gripper (binarized) differs from the row one policy row before it, row 0 included
    (the line world knows the gripper of the row before row 0)."""
    out = []
    for rows in chunks:
        for j in range(len(rows)):
            before = (
                rows[j - 1, 6] >= 0.5
                if j
                else grip_at(phase_of(rows[0, :6]) - 1) >= 0.5
            )
            if (rows[j, 6] >= 0.5) != before:
                out.append(rows[j, :6].astype(np.float64))
    return out


class GripperTests:
    params_golden = True
    accel = ACCEL
    H = 10

    def _params(self):
        return GOLDEN if self.params_golden else self.accel

    def _check(self, r):
        flips = gripper_flips(r["trace"])
        final_s = r["trace"][-1]["s"]
        passed = [t for t in (CLOSE_AT, OPEN_AT) if t + 1 <= final_s]
        reached = [t for t in (CLOSE_AT, OPEN_AT) if t <= final_s]
        self.assertGreaterEqual(
            len(flips),
            len(passed),
            f"a gripper change was skipped (phase reached {final_s:.1f})",
        )
        self.assertLessEqual(
            len(flips), len(reached), "a gripper change was sent twice"
        )
        self.assertEqual(
            [g for _, g, _ in flips], [0.0, 1.0][: len(flips)], "close, then open"
        )
        rows = change_rows(r["server"].chunks)
        self.assertTrue(rows)
        tol = 1e-5  # bit for bit in golden; accelerated, no blend is active in the line world (the chunks agree)
        # A reply whose chunk puts the change in the rows its own delay has already used up (row 0..d-1, which the
        # grid shifted by the fractional reference phase can do) cannot be hit any more: the engine sends it on the
        # first tick of the new plan and reports exact=False. That change goes out at most d_rows rows after its row.
        events = (
            r["sched"].engine.summary().get("grip_events", [])
            if hasattr(r["sched"].engine, "summary")
            else []
        )
        late_rows = max([*r["summary"].get("delays_rows", []), 0.0]) + 1e-6
        self.assertIn(len(events), (0, len(flips)))
        for i, (k, g, pose) in enumerate(flips):
            err = min(np.abs(pose[:3] - row[:3]).max() for row in rows)
            exact = events[i]["exact"] if events else True
            if exact:
                self.assertLessEqual(
                    err,
                    tol,
                    f"the change at tick {k} did not land on a chunk's change row",
                )
                continue
            behind = [
                phase_of(pose) - phase_of(row)
                for row in rows
                if 0.0 <= phase_of(pose) - phase_of(row) <= late_rows
            ]
            self.assertTrue(
                behind,
                f"the late change at tick {k} is not within {late_rows:.2f} rows after a change row",
            )

    def test_rtc_every_change_row_is_sent_once_at_its_row(self):
        r = run_line(
            "rtc",
            self._params(),
            self.make_engine,
            ticks=900 if self.params_golden else 420,
            exec_h=6,
        )
        self._check(r)

    def test_vlash_every_change_row_is_sent_once_at_its_row(self):
        r = run_line(
            "vlash",
            self._params(),
            self.make_engine,
            ticks=900 if self.params_golden else 420,
        )
        self._check(r)

    def test_a_reset_never_sends_a_close_twice(self):
        resets = {CLOSE_AT - 3, CLOSE_AT + 2} if self.params_golden else {50, 90}
        r = run_line(
            "rtc",
            self._params(),
            self.make_engine,
            ticks=300 if self.params_golden else 160,
            resets=resets,
        )
        flips = gripper_flips(r["trace"])
        self.assertEqual(
            [g for _, g, _ in flips],
            [0.0, 1.0][: len(flips)],
            "close, then open; never close twice",
        )


class GripperGoldenRef(RefMixin, GripperTests, unittest.TestCase):
    pass


class GripperGoldenReal(RealMixin, GripperTests, unittest.TestCase):
    pass


class GripperAccelReal(RealMixin, GripperTests, unittest.TestCase):
    """On the core, lambda 2.5: the same guarantees with the leash, the gate and the settle in the loop."""

    params_golden = False

    def test_the_close_gate_does_not_starve_the_pipeline(self):
        r = run_line(
            "rtc",
            ACCEL,
            self.make_engine,
            ticks=300,
            latencies=latency_trace(80, seed=8),
        )
        self.assertLessEqual(r["summary"]["starved_ticks"], 3)
        self.assertGreater(
            len(gripper_flips(r["trace"])), 0, "the run reached the close"
        )


class GripperShippedReal(RealMixin, GripperTests, unittest.TestCase):
    """The same guarantees at the page default lambda 3.0."""

    params_golden = False
    accel = SHIPPED


# ==============================================================================================================
# Review 11-02 of the strong profile, at the shipped lambda 3.0: the leash recovery in contact (M1) and the close
# gate across plan reloads (M2). Self-contained: a lag arm with a wall / floor and a z-floor clamp, no openpi.
# ==============================================================================================================


class WallArm:
    """First-order lag toward the target (tau 0.27 s at 10 Hz); the arm cannot pass x > wall or z < floor; seeded
    Gaussian noise on the measured position (an arm pressing on a contact reads 50-300 um of noise)."""

    def __init__(self, x0, *, wall=None, floor=None, noise=0.0, seed=1, tau=0.27):
        self.x = np.asarray(x0, np.float64).copy()
        self.a = 1.0 - math.exp(-0.1 / tau)
        self.wall, self.floor, self.noise = wall, floor, noise
        self.rng = np.random.default_rng(seed)

    def step(self, target):
        self.x[:6] += self.a * (np.asarray(target[:6], np.float64) - self.x[:6])
        self.x[6] = float(target[6])
        if self.wall is not None:
            self.x[0] = min(self.x[0], self.wall)
        if self.floor is not None:
            self.x[2] = max(self.x[2], self.floor)

    def measure(self):
        m = self.x.copy()
        if self.noise:
            m[:3] += self.rng.normal(0.0, self.noise, 3)
        return m.astype(np.float32)


def wall_obs(pose, aperture=0.08):
    return {
        "robot_state": {
            "cartesian_position": np.asarray(pose, np.float32),
            "gripper_position": aperture,
        }
    }


def floor_clamp(target, measured, min_z=None):
    """The two parts of the client's clamp these tests need: the z floor (min_z) and the jump refusal (0.10 m /
    0.35 rad from the measured pose). Returns (sent, refused)."""
    sent = np.asarray(target, np.float32).copy()
    if min_z is not None and sent[2] < min_z:
        sent[2] = min_z
    sent[6] = 1.0 if sent[6] >= 0.5 else 0.0
    d = np.asarray(target, np.float64) - np.asarray(measured, np.float64)
    refused = (
        float(np.linalg.norm(d[:3])) > 0.10
        or float(np.abs(phase.wrap_to_pi(d[3:6])).max()) > 0.35
    )
    return sent, refused


class ContactRecoveryTests(unittest.TestCase):
    """M1: the policy keeps pushing +x (10 mm per row) into a wall the arm cannot pass, the measured pose carries 50 um
    or 0.3 mm of noise. The target sits at the hard leash; noise puts it 0.1 mm outside now and then, and the recovery
    used to walk it a full 50 mm step back (a 50 mm sawtooth at ~1.5 Hz). Now it moves back just inside the leash.

    Measured on this setup (seeds 1-8, RTC execute horizon 6 and VLASH overlap 2): the largest backward step on a tick
    without a running blend was 50.0 mm before the fix and 4.1-5.4 mm after. Ticks inside a min-jerk blend onto a newly
    loaded chunk (which the policy computed from the blocked arm) move back by up to 11.1 mm: that is the blend,
    bounded by blend_step_m (25 mm) less the plan's own progress, and it was there before the fix (9.0 mm with no
    recovery at all), so it is bounded separately at 12 mm."""

    @staticmethod
    def _chunk(state):
        rows = np.repeat(np.asarray(state, np.float64)[None, :], 10, axis=0)
        rows[:, 0] += 0.010 * np.arange(1, 11)
        rows[:, 6] = 1.0
        return rows.astype(np.float32)

    def _contact(self, mode, noise, seed, ticks=200):
        clock = Clock()
        arm = WallArm(
            [0.45, 0.0, 0.20, 3.1, 0.0, -0.8, 1.0], wall=0.50, noise=noise, seed=seed
        )
        if mode == "rtc":
            sched = PhaseRtcChunker(
                lambda obs, prev, delay: (
                    lambda: (self._chunk(obs["robot_state"]["cartesian_position"]), {})
                ),
                execute_horizon=6, period_s=0.1, params=SHIPPED, transport=SimTransport((1, 2)),
                say=lambda _s: None, clock=clock.monotonic,
            )  # fmt: skip
        else:
            sched = PhaseVlashChunker(
                lambda obs, fut: (
                    lambda: (self._chunk(obs["robot_state"]["cartesian_position"] if fut is None else fut), {})
                ),
                overlap_steps=2, horizon=10, params=SHIPPED, binarize_gripper=True,
                transport=SimTransport((1, 2)), clock=clock.monotonic,
            )  # fmt: skip
        eng = sched.engine
        prev, back_flat, back_all = None, 0.0, 0.0
        for k in range(ticks):
            clock.now = 0.1 * k
            meas = arm.measure()
            target = sched.next_action(wall_obs(meas))
            sent, refused = floor_clamp(target, meas)
            self.assertFalse(
                refused, f"{mode} tick {k}: the client would refuse the target"
            )
            if mode == "vlash":
                sched.action_adjusted(target, sent)
            eng.executed(sent)
            arm.step(sent)
            tick = [r for r in eng.drain_records() if r["kind"] == "tick"][-1]
            if prev is not None:
                dx = float(sent[0]) - float(prev[0])
                back_all = min(back_all, dx)
                if tick["blend_w"] >= 1.0:
                    back_flat = min(back_flat, dx)
            prev = sent.copy()
        return back_flat, back_all, eng.summary()

    def test_contact_with_pose_noise_never_saws_the_target_back(self):
        for mode in ("rtc", "vlash"):
            for noise in (5e-5, 3e-4):
                for seed in (1, 2, 3):
                    flat, every, s = self._contact(mode, noise, seed)
                    what = f"{mode} noise {noise * 1e3:.2f} mm seed {seed}"
                    self.assertGreater(
                        s["leash_recoveries"], 0, f"{what}: the leash never tripped"
                    )
                    self.assertGreaterEqual(
                        flat, -0.010, f"{what}: backward step {flat * 1e3:.1f} mm"
                    )
                    self.assertGreaterEqual(
                        every,
                        -0.012,
                        f"{what}: backward step in a blend {every * 1e3:.1f} mm",
                    )
                    self.assertLessEqual(s["dp_max_m"], 0.08 + 1e-6, what)
                    self.assertLessEqual(s["step_max_m"], 0.05 + 1e-9, what)


class GateAcrossReloadsTests(unittest.TestCase):
    """M2: the close gate's 5-tick timeout must hold however often the plan is reloaded. The policy approaches a grasp
    pose over 3 rows and closes there (row 0 once it is within 2 cm); the arm cannot reach the closing row: stopped
    12 mm above it (fingers on the object) or held 10 mm above it by the client's min_z clamp. Before the fix plain at
    horizon 6 (a re-query every 4-6 ticks) and VLASH under the clamp (action_adjusted -> reset -> sync every tick)
    restarted the wait at each reload and never closed in 150 ticks; RTC closed at tick 8."""

    Z_GRASP = 0.050
    GRASP_XY = (0.50, 0.02)
    # re-aim: after the first gate wait the policy closes at B instead, 60 mm on in +x and above the stop, approached at
    # B_ROW per row (the close row stays inside every chunk) (verifier probe of the 11-02 fixes)
    B = (0.56, 0.02, 0.080)
    B_ROW = 0.010
    _aim_b = False

    def _policy(self, state):
        st = np.asarray(state, np.float64)
        goal = np.array([self.GRASP_XY[0], self.GRASP_XY[1], self.Z_GRASP])
        rows = np.repeat(st[None, :], 10, axis=0)
        n_app = 0 if np.linalg.norm(st[:3] - goal) < 0.02 else 3
        if self._aim_b:
            goal = np.array(self.B)
            dist = float(np.linalg.norm(st[:3] - goal))
            n_app = 0 if dist < 0.004 else min(9, int(np.ceil(dist / self.B_ROW)))
        for j in range(10):
            f = 1.0 if n_app == 0 else min(1.0, (j + 1) / n_app)
            rows[j, :3] = st[:3] + f * (goal - st[:3])
            rows[j, 6] = 0.0 if j >= n_app else 1.0
        return rows.astype(np.float32)

    def _run(
        self,
        mode,
        *,
        params=SHIPPED,
        stop=None,
        min_z=None,
        h=6,
        ticks=60,
        re_aim=False,
    ):
        """-> (tick of the first gate wait or None, tick the close was sent or None). The engine and the measured pose
        at the close are kept in self.eng / self.at_close."""
        self._aim_b = False
        clock = Clock()
        arm = WallArm(
            [0.47, 0.0, 0.12, 3.1, 0.0, -0.8, 1.0],
            floor=None if stop is None else self.Z_GRASP + stop,
        )
        z_floor = None if min_z is None else self.Z_GRASP + min_z
        if mode == "plain":
            sched = PlainPhase(params, h, clock=clock.monotonic)
        elif mode == "rtc":
            sched = PhaseRtcChunker(
                lambda obs, prev, d: (lambda: (self._policy(obs["robot_state"]["cartesian_position"]), {})),
                execute_horizon=6, period_s=0.1, params=params, transport=SimTransport((1,)),
                say=lambda _s: None, clock=clock.monotonic,
            )  # fmt: skip
        else:
            sched = PhaseVlashChunker(
                lambda obs, fut: (lambda: (self._policy(obs["robot_state"]["cartesian_position"]), {})),
                overlap_steps=2, horizon=10, params=params, binarize_gripper=True,
                transport=SimTransport((1,)), clock=clock.monotonic,
            )  # fmt: skip
        eng = self.eng = sched.engine
        plan, idx, first_gate = None, 0, None
        for k in range(ticks):
            clock.now = 0.1 * k
            state = arm.measure()
            if mode == "plain":
                if plan is None or idx >= min(h, len(plan)):
                    plan, idx = sched.on_query(self._policy(state), state, 0.08), 0
                idx += 1
                target = sched.on_action(state, 0.08)
            else:
                target = sched.next_action(wall_obs(state))
            sent, refused = floor_clamp(target, state, z_floor)
            self.assertFalse(refused, f"{mode} tick {k}")
            if mode == "vlash":
                sched.action_adjusted(target, sent)
            eng.executed(sent)
            arm.step(sent)
            if first_gate is None and eng.summary()["gate_waits"] > 0:
                first_gate = k
                self._aim_b = re_aim
            if sent[6] < 0.5:
                self.at_close = state
                return first_gate, k
        return first_gate, None

    def _assert_closes_in_time(self, mode, **kw):
        first, close = self._run(mode, **kw)
        what = f"{mode} lambda {kw.get('params', SHIPPED).lambda_max} stop {kw.get('stop')} min_z {kw.get('min_z')}"
        self.assertIsNotNone(first, f"{what}: the gate never waited")
        self.assertIsNotNone(close, f"{what}: the close never went out")
        self.assertLessEqual(
            close - first,
            PhaseParams().gate_max_ticks + 2,
            f"{what}: {first} -> {close}",
        )
        return first, close

    def test_plain_horizon_6_with_the_arm_stopped_12_mm_short_closes_at_the_timeout(
        self,
    ):
        for lam in (3.0, 2.5, 2.0):
            self._assert_closes_in_time(
                "plain", params=PhaseParams.for_page(lam, 0.005), stop=0.012
            )

    def test_plain_with_a_min_z_clamp_10_mm_above_the_close_row_closes(self):
        self._assert_closes_in_time("plain", min_z=0.010)

    def test_vlash_with_a_min_z_clamp_10_mm_above_the_close_row_closes(self):
        for lam in (3.0, 2.0):
            self._assert_closes_in_time(
                "vlash", params=PhaseParams.for_page(lam, 0.005), min_z=0.010
            )

    def test_plain_re_aimed_at_another_grasp_waits_for_the_arm_there(self):
        """The wait started at A (arm stopped 12 mm short) must not release the close at B, 60 mm on: before the
        location check the time since A's first wait (0.8-1.3 s) sent it on B's first gate tick with the arm 13.1 mm
        off at 0.059 m/s (gate_timeout); the engine before the 11-02 fixes waited at B and closed 6.2 mm off."""
        for lam in (3.0, 2.0):
            first, close = self._run(
                "plain",
                params=PhaseParams.for_page(lam, 0.005),
                stop=0.012,
                re_aim=True,
            )
            what = f"plain lambda {lam}: first gate {first}, close {close}"
            self.assertIsNotNone(close, what)
            ev = [e for e in self.eng.summary()["grip_events"] if e["kind"] == "close"][
                -1
            ]
            off_b = float(np.linalg.norm(self.at_close[:3] - np.array(self.B)))
            self.assertLess(off_b, 0.02, f"{what}: the close went out at A, not B")
            self.assertFalse(ev["gate_timeout"], f"{what}: {ev}")
            self.assertLessEqual(
                ev["arm_err_m"], PhaseParams().gate_dist_m, f"{what}: {ev}"
            )
            self.assertLessEqual(off_b, PhaseParams().gate_dist_m + 1e-3, what)

    def test_rtc_is_unchanged(self):
        """RTC reloads by swapping a reply in, which never restarted a wait that ran on: it closed at tick 8 before the
        fix (the probe of the review) and still does."""
        for kw in ({"stop": 0.012}, {"min_z": 0.010}):
            _first, close = self._assert_closes_in_time("rtc", **kw)
            self.assertEqual(close, 8, kw)

    def test_golden_has_no_gate(self):
        for mode in ("plain", "rtc", "vlash"):
            first, close = self._run(mode, params=GOLDEN, stop=0.012)
            self.assertIsNone(first, mode)
            self.assertIsNotNone(close, mode)


# ==============================================================================================================
# 9. reset / abandon / drain
# ==============================================================================================================


class ResetSemanticsTests:
    @needs_openpi
    def test_rtc_reset_with_a_reply_in_flight_is_counted_when_it_is_retired_like_rtcchunker(
        self,
    ):
        dropped = 0
        for resets in ({7}, {7, 8}, {5, 12, 13, 30}, {9}):
            s = Script(
                seed=21,
                n_ticks=40,
                exec_h=3,
                replies=((4, 0.35), (1, 0.08)),
                resets=frozenset(resets),
            )
            a, b = run_rtc_original(s), run_rtc_phase(s, GOLDEN, self.make_engine)
            assert_same(self, a, b, f"resets {sorted(resets)}", strip_extra=True)
            dropped += a.summary["dropped_replies"]
        self.assertGreater(dropped, 0, "the scenarios retire an abandoned reply")

    @needs_openpi
    def test_rtc_close_drains_what_is_still_running(self):
        s = Script(seed=22, n_ticks=9, exec_h=3, replies=((6, 0.55),))
        a, b = run_rtc_original(s), run_rtc_phase(s, GOLDEN, self.make_engine)
        assert_same(self, a, b, "close with a request in flight", strip_extra=True)
        self.assertEqual(b.summary["dropped_replies"], 1)

    def test_vlash_reset_and_action_adjusted_count_like_vlashchunker(self):
        adjusted = discarded = 0
        for resets, adjusts in (
            ({6}, set()),
            (set(), {5}),
            ({3, 4}, {9, 10}),
            ({13}, {14}),
            ({8}, set()),
        ):
            s = Script(
                seed=23,
                n_ticks=45,
                overlap=2,
                replies=((3, 0.25), (1, 0.08)),
                resets=frozenset(resets),
                adjusts=frozenset(adjusts),
            )
            a, b = (
                run_vlash_original(s, VRT_LEVI),
                run_vlash_phase(s, GOLDEN, self.make_engine),
            )
            assert_same(
                self, a, b, f"resets {sorted(resets)} adjusts {sorted(adjusts)}"
            )
            adjusted += a.summary["adjusted_targets"]
            discarded += a.summary["discarded_replies"]
        self.assertGreater(adjusted, 0)
        self.assertGreater(discarded, 0, "an abandoned prefetch is drained and counted")

    def test_vlash_an_unchanged_target_is_not_an_adjustment(self):
        r = run_vlash_phase(Script(seed=24, n_ticks=30), GOLDEN, self.make_engine)
        self.assertEqual(r.summary["adjusted_targets"], 0)
        sched = r.sched
        sched2 = PhaseVlashChunker(
            FakeServer(Script(seed=24), Clock()).vlash_request,
            overlap_steps=2,
            horizon=10,
            params=GOLDEN,
            binarize_gripper=True,
            transport=SimTransport(),
            engine=self.make_engine(GOLDEN, 10.0) if self.make_engine else None,
        )
        sched2.next_action(make_obs(0, 1.0))
        a = np.arange(7, dtype=np.float32)
        sched2.action_adjusted(a, a.copy())
        self.assertEqual(sched2.stats["adjusted_targets"], 0)
        self.assertTrue(sched2.engine.has_rows())
        sched2.action_adjusted(a, a + 1e-6)
        self.assertEqual(sched2.stats["adjusted_targets"], 1)
        self.assertFalse(
            sched2.engine.has_rows(), "an adjusted target resets the scheduler"
        )
        self.assertIsNotNone(sched)

    def test_accelerated_reset_keeps_the_phase_and_forgets_the_chunk(self):
        if not self.models_core:
            self.skipTest("core behaviour")
        r = run_line("rtc", ACCEL, self.make_engine, ticks=60, resets={20})
        s = [t["s"] for t in r["trace"]]
        self.assertTrue(all(b >= a - 1e-9 for a, b in itertools.pairwise(s)))


class ResetRef(RefMixin, ResetSemanticsTests, unittest.TestCase):
    pass


class ResetReal(RealMixin, ResetSemanticsTests, unittest.TestCase):
    pass


class GatedServer:
    """Real threads: an asynchronous request blocks on a gate the test opens. Records when each call started / finished."""

    def __init__(self):
        self.gates, self.log, self.t0 = [], [], time.monotonic()
        self.lock = threading.Lock()

    def _stamp(self, what, n):
        with self.lock:
            self.log.append((what, n, time.monotonic() - self.t0))

    def rtc_request(self, obs, prev, delay):
        n = len(self.gates)
        gate = threading.Event()
        self.gates.append(gate)
        rows = make_rows((9, n, int(obs["id"])))

        def run():
            self._stamp("start", n)
            if prev is not None:
                assert gate.wait(10), "gate never opened"
            self._stamp("end", n)
            return rows.copy(), {}

        return run

    vlash_request = None


@needs_openpi
class ThreadedRtcTests(unittest.TestCase):
    """The production transport: rtcc._Request threads, Event gates."""

    def _make(self, **kw):
        rtcc = load_ro("rtc_chunker_threads", RTCC_PATH)
        server = GatedServer()
        say = []
        sched = PhaseRtcChunker(
            server.rtc_request,
            execute_horizon=3,
            period_s=0.1,
            params=GOLDEN,
            transport=ThreadTransport.rtc(rtcc),
            say=say.append,
            engine=RefEngine(GOLDEN, 10.0),
            **kw,
        )
        return sched, server, say, rtcc

    def _ticks(self, sched, n, start=0):
        for k in range(start, start + n):
            sched.next_action(make_obs(k, 1.0))

    def test_a_reply_is_swapped_in_when_its_gate_opens_and_a_blocked_chunk_end_counts_as_starved(
        self,
    ):
        sched, server, say, _ = self._make()
        self._ticks(sched, 4)  # sync at tick 0, request at tick 3 (u == 3)
        self.assertEqual(len(server.gates), 2)
        self.assertEqual(sched.stats["rtc_requests"], 1)
        server.gates[1].set()
        sched._inflight.done.wait(5)
        self._ticks(sched, 1, start=4)
        self.assertEqual(sched.stats["delays"][1], 1)
        self.assertEqual(sched.stats["starved_ticks"], 0)
        self.assertEqual(len(say), 1)
        # a request that is late until the chunk ends: the loop blocks, the test opens the gate from another thread
        self._ticks(sched, 3, start=5)  # new chunk: request at u == 3 again
        gate = server.gates[2]
        threading.Timer(0.15, gate.set).start()
        t0 = time.monotonic()
        self._ticks(sched, 8, start=8)
        self.assertGreaterEqual(time.monotonic() - t0, 0.1)
        self.assertGreaterEqual(sched.stats["starved_ticks"], 1)
        sched.close()

    def test_reset_abandons_the_reply_and_the_next_synchronous_query_waits_for_it(self):
        sched, server, _, _ = self._make()
        self._ticks(sched, 4)
        self.assertEqual(sched.stats["rtc_requests"], 1)
        sched.reset()
        self.assertIsNone(sched._inflight)
        self.assertFalse(sched.engine.has_rows())
        threading.Timer(0.2, server.gates[1].set).start()
        sched.next_action(
            make_obs(4, 1.0)
        )  # synchronous query: drains the abandoned thread first
        ends = {n: t for what, n, t in server.log if what == "end"}
        starts = {n: t for what, n, t in server.log if what == "start"}
        self.assertLessEqual(
            ends[1],
            starts[2],
            "the abandoned request finished before the next one started",
        )
        self.assertEqual(sched.stats["dropped_replies"], 1)
        self.assertEqual(sched.stats["requests"], 3)

    def test_close_waits_for_a_running_request_and_leaves_no_thread(self):
        sched, server, _, _ = self._make()
        self._ticks(sched, 4)
        threading.Timer(0.15, server.gates[1].set).start()
        t0 = time.monotonic()
        sched.close()
        self.assertGreaterEqual(time.monotonic() - t0, 0.1)
        self.assertEqual(sched.stats["dropped_replies"], 1)
        self.assertFalse(
            [t for t in threading.enumerate() if t.name == "rtc-infer" and t.is_alive()]
        )

    def test_an_error_in_the_request_thread_reaches_the_loop(self):
        sched, server, _, _ = self._make()
        self._ticks(sched, 3)
        orig = server.rtc_request

        def failing(obs, prev, delay):
            def run():
                raise RuntimeError("socket closed")

            return run

        sched._request = failing
        sched.next_action(make_obs(3, 1.0))
        sched._inflight.done.wait(5)
        with self.assertRaisesRegex(RuntimeError, "socket closed"):
            sched.next_action(make_obs(4, 1.0))
        sched._request = orig
        sched.close()  # the failed request is retired, not raised again
        self.assertEqual(sched.stats["dropped_replies"], 1)


@needs_openpi
class ThreadedVlashTests(unittest.TestCase):
    def _make(self, replies_gated=True):
        vrt = load_vlash_runtime(VRT_INSTALLED)
        gates, log = [], []

        def request(obs, future):
            n = len(gates)
            gate = threading.Event()
            gates.append(gate)
            rows = make_rows((8, n, int(obs["id"])))

            def run():
                log.append(("start", n, time.monotonic()))
                if future is not None:
                    assert gate.wait(10)
                log.append(("end", n, time.monotonic()))
                return rows.copy(), {}

            return run

        sched = PhaseVlashChunker(
            request,
            overlap_steps=2,
            horizon=10,
            params=GOLDEN,
            binarize_gripper=True,
            transport=ThreadTransport.vlash(vrt),
            engine=RefEngine(GOLDEN, 10.0),
        )
        return sched, gates, log

    def test_starvation_blocks_until_the_prefetch_arrives(self):
        sched, gates, _ = self._make()
        for k in range(9):
            sched.next_action(make_obs(k, 1.0))
        self.assertEqual(sched.stats["prefetches"], 1)
        threading.Timer(0.15, gates[1].set).start()
        t0 = time.monotonic()
        for k in range(9, 12):
            sched.next_action(make_obs(k, 1.0))
        self.assertGreaterEqual(time.monotonic() - t0, 0.1)
        self.assertEqual(sched.stats["starvations"], 1)
        sched.close()

    def test_abandoned_prefetch_is_drained_before_the_next_request(self):
        sched, gates, log = self._make()
        for k in range(9):
            sched.next_action(make_obs(k, 1.0))
        sched.reset()
        threading.Timer(0.15, gates[1].set).start()
        sched.next_action(make_obs(9, 1.0))
        self.assertEqual(sched.stats["discarded_replies"], 1)
        end1 = next(t for what, n, t in log if what == "end" and n == 1)
        start2 = next(t for what, n, t in log if what == "start" and n == 2)
        self.assertLessEqual(end1, start2)
        sched.close()
        with self.assertRaises(RuntimeError):
            sched.next_action(make_obs(10, 1.0))


class SoakServer(LineServer):
    """The line world behind real threads: every asynchronous reply sleeps a wall-clock latency; the number of requests
    that run at the same time is counted."""

    def __init__(self, lat_s: float):
        super().__init__()
        self.lat_s, self.lock = lat_s, threading.Lock()
        self.running = self.max_running = 0

    def _wrap(self, run, sync: bool):
        def threaded():
            with self.lock:
                self.running += 1
                self.max_running = max(self.max_running, self.running)
            try:
                if not sync:
                    time.sleep(self.lat_s)
                return run()
            finally:
                with self.lock:
                    self.running -= 1

        return threaded

    def rtc_request(self, obs, prev, delay):
        return self._wrap(super().rtc_request(obs, prev, delay), prev is None)

    def vlash_request(self, obs, future):
        return self._wrap(super().vlash_request(obs, future), future is None)


@needs_openpi
class ThreadedSoakTests(unittest.TestCase):
    """The real engine, accelerated, behind real threads at the real 10 Hz: nothing deterministic, so only the
    invariants (one request at a time, a phase that never rewinds, no thread left) and a loose speed floor."""

    def _soak(self, kind: str, lat_s: float, ticks: int = 30):
        server = SoakServer(lat_s)
        if kind == "rtc":
            sched = PhaseRtcChunker(
                server.rtc_request,
                execute_horizon=6,
                period_s=0.1,
                params=ACCEL,
                transport=ThreadTransport.rtc(load_ro("rtcc_soak", RTCC_PATH)),
                say=lambda _s: None,
            )
        else:
            sched = PhaseVlashChunker(
                server.vlash_request,
                overlap_steps=2,
                horizon=10,
                params=ACCEL,
                binarize_gripper=True,
                transport=ThreadTransport.vlash(load_vlash_runtime(VRT_INSTALLED)),
            )
        arm, phases = Arm(), []
        for k in range(ticks):
            t0 = time.monotonic()
            state = arm.obs(k, sched.engine.s)
            raw = sched.next_action(state)
            sent = sent_of(raw)
            sched.engine.executed(sent)
            if kind == "vlash":
                sched.action_adjusted(raw, sent)
            arm.step(sent)
            phases.append(sched.engine.s)
            time.sleep(max(0.0, 0.1 - (time.monotonic() - t0)))
        sched.close()
        return sched, server, phases

    def _check(self, sched, server, phases):
        self.assertLessEqual(
            server.max_running, 1, "never two requests at the same time"
        )
        self.assertTrue(all(b >= a - 1e-9 for a, b in itertools.pairwise(phases)))
        self.assertGreater(
            phases[-1], 1.3 * len(phases), "faster than the nominal rows per tick"
        )
        self.assertGreaterEqual(len(server.calls), 3)
        self.assertEqual(server.running, 0)
        self.assertFalse(
            [
                t
                for t in threading.enumerate()
                if t.name in ("rtc-infer", "vlash-inference") and t.is_alive()
            ]
        )

    def test_rtc(self):
        sched, server, phases = self._soak("rtc", 0.06)
        self._check(sched, server, phases)
        self.assertGreater(sched.stats["rtc_requests"], 2)
        # every request that was started is either swapped in or retired as a dropped reply (the one in flight when
        # close() ends the episode is the usual drop); none is lost, none is counted twice
        summary = sched.summary()
        self.assertEqual(
            summary["rtc_requests"],
            sum(summary["delays"].values()) + summary["dropped_replies"],
        )
        self.assertLessEqual(summary["dropped_replies"], 1)

    def test_vlash(self):
        sched, server, phases = self._soak("vlash", 0.06)
        self._check(sched, server, phases)
        self.assertGreater(sched.stats["prefetches"], 2)
        self.assertEqual(sched.summary()["adjusted_targets"], 0)


# ==============================================================================================================
# The default transport and engine
# ==============================================================================================================


class DefaultsTests(unittest.TestCase):
    def test_defaults_build_without_openpi_until_a_request_starts(self):
        sched = PhaseRtcChunker(
            lambda *a: lambda: None, execute_horizon=5, period_s=0.1, params=GOLDEN
        )
        self.assertEqual((sched.mode, sched.execute_horizon), ("rtc", 5))
        self.assertIsInstance(sched.engine, phase.PhaseEngine)
        self.assertEqual(sched.summary()["execute_horizon"], 5)
        v = PhaseVlashChunker(
            lambda *a: lambda: None, overlap_steps=2, horizon=10, params=GOLDEN
        )
        self.assertEqual(v.mode, "vlash")
        self.assertIsInstance(v.engine, phase.PhaseEngine)
        self.assertEqual(v.summary()["latency_ms_first"], None)

    def test_every_scheduler_exposes_its_engine_and_the_summary_is_json_safe(self):
        import json

        for s in (
            PhaseRtcChunker(
                lambda *a: None, execute_horizon=3, period_s=0.1, params=ACCEL
            ),
            PhaseVlashChunker(
                lambda *a: None, overlap_steps=2, horizon=10, params=ACCEL
            ),
            PlainPhase(ACCEL, 6),
        ):
            self.assertIsInstance(s.engine, phase.PhaseEngine)
            json.dumps(s.summary())

    def test_execute_horizon_must_be_positive(self):
        with self.assertRaises(ValueError):
            PhaseRtcChunker(
                lambda *a: None, execute_horizon=0, period_s=0.1, params=GOLDEN
            )


if __name__ == "__main__":
    unittest.main()
