"""``GET /api/levi/rig/status``: one read-only answer to "is the robot busy?"
(``levi/rig/status.py``). The local authentication is the service's own
(the web UI token), as for ``GET /api/levi/setup/status``.

Where things are looked up: the arm-controller marker is the file setup.md
names, ``lab/levi-hub/.arm-controller-active`` of the main checkout (also when
this LEVI runs from a worktree); the rollout roots are the live workspace's
configuration in force, found as ``levi.setup.probes`` finds it and remembered
nowhere. The robot-process guard is the setup probes' own ``guard`` part
(``probes.status()``, which shares its cache with ``/api/levi/setup/status``).

**Nothing can hang a request, and one part never fails another.** Each part
reads in its own daemon thread with a deadline (``LIMITS``); a part that
fails, misses its deadline or cannot get a thread answers ``unknown`` with a
detail.

**Requests share reads.** A request that arrives while a part's read is in
flight waits for that read, until the read's own limit, and uses its result.
Only once the limit has passed is the part stuck (a dead network mount): later
requests then answer ``unknown`` for it at once instead of starting another
read, until it finishes. The finished read of ``eval_sessions`` and ``units``
(``CACHE_S``) answers requests for two seconds after it began, so several
open pages cost one ``systemctl`` and one folder scan; their ages are as of
the read. The marker (one ``lstat``) is read for every request, and the guard
has the setup probes' own cache.
"""

import threading
import time
from pathlib import Path

from fastapi import APIRouter

from levi.setup import probes

from . import status

router = APIRouter(prefix="/api/levi/rig", tags=["Rig"])

MARKER_FILE = ".arm-controller-active"
HUB = ("lab", "levi-hub")
# Seconds each part may take (the guard waits for the setup probes' sample).
LIMITS = {
    "marker": 1.5,
    "eval_sessions": 1.5,
    "units": status.SYSTEMCTL_TIMEOUT_S + 0.5,
    "guard": probes.WAIT_S + probes.CONTEXT_TIMEOUT_S + 1.0,
}
# Seconds after it began that a finished read still answers requests.
CACHE_S = {"eval_sessions": 2.0, "units": 2.0}
# The oldest setup-probes sample whose guard still counts (it caches for 2 s).
GUARD_MAX_AGE_S = 10.0
STILL_BLOCKED = (
    "an earlier read has not finished (a slow or blocked file system?); "
    "not started again"
)

monotonic = time.monotonic  # tests replace it

_LOCK = threading.Lock()
_READS: dict = {}  # part name -> its latest Read
_MARKER: list = []  # the marker's path, once a main checkout has been found


def marker_path() -> Path:
    """``<main checkout>/lab/levi-hub/.arm-controller-active``. A linked
    worktree resolves to the main checkout (``locate.checkouts``). Only a path
    under a main checkout is remembered; without a ``.git`` directory anywhere
    the answer is this checkout's, worked out afresh each time (it is a guess,
    and a wrong one for a worktree)."""
    if _MARKER:
        return _MARKER[0]
    from levi.live import locate

    found = locate.checkouts()
    main = next((c for c in found if (c / ".git").is_dir()), None)
    if main is None:
        return found[0].joinpath(*HUB, MARKER_FILE)
    _MARKER[:] = [main.joinpath(*HUB, MARKER_FILE)]
    return _MARKER[0]


def rollout_roots() -> tuple:
    """``(roots, why)``: the rollout roots of the live workspace's
    configuration in force, and why there are none (a reason code, never a
    path)."""
    from levi.live import config as live_config
    from levi.live import locate
    from levi.paths import ROOT

    found = locate.find(ROOT)
    if found.workspace is None or not locate.is_live(found.workspace):
        return [], f"no live workspace is known ({found.problem or 'not_live'})"
    root = found.workspace
    written = next(
        (
            p
            for p in (root / "live" / "effective.toml", root / "live.toml")
            if p.is_file()
        ),
        None,
    )
    try:
        config = live_config.load(written)
    except ValueError:
        return [], "the live configuration could not be read"
    roots = [str(Path(r).expanduser()) for r in config.watch.roots]
    return roots, None if roots else "the live configuration lists no rollout roots"


def _sessions(now) -> dict:
    roots, why = rollout_roots()
    return status.sessions_part(roots, now, why)


def _guard() -> dict:
    answer = probes.status()
    guard = answer.get("guard") if isinstance(answer, dict) else None
    if isinstance(guard, dict):
        if _old_sample(answer):
            return _unknown_guard(
                "the setup probes sample is old; a robot process cannot be ruled out",
                status.GUARD_OLD_SAMPLE,
            )
        return guard
    detail = answer.get("detail") if isinstance(answer, dict) else None
    return {
        "state": "unknown",
        "detail": str(detail or "the setup probes gave no guard part")[:200],
    }


def _old_sample(answer) -> bool:
    """True when ``probes.status()`` handed back its previous sample (``stale``)
    or one older than ``GUARD_MAX_AGE_S``, or one that does not say how old it
    is (every answer of the probes has an ``age_s``)."""
    if answer.get("stale"):
        return True
    age = answer.get("age_s")
    if isinstance(age, bool) or not isinstance(age, (int, float)):
        return True
    return not age <= GUARD_MAX_AGE_S  # also true for NaN


def _unknown_guard(detail, code=None) -> dict:
    """The guard when it cannot be used. ``code`` (for ``robot_idle``'s reason)
    is only set for a sample that was dropped; the guard is not in the answer."""
    out = {"state": "unknown", "detail": detail}
    if code:
        out["code"] = code
    return out


class Read:
    """One read of one part, running in its own daemon thread."""

    def __init__(self, began, value):
        self.began = began
        self.done = threading.Event()
        # What an answer is if the thread ends without setting one (a
        # BaseException such as SystemExit): the part stays schema-valid.
        self.value = value


def _acquire(name, read, fallback):
    """``(value, None)`` when the part answers at once (a finished read that
    is still fresh, or one stuck past its limit), else ``(None, Read)``: the
    read in flight, or a new one, to wait for."""
    now = monotonic()
    with _LOCK:
        latest = _READS.get(name)
        if latest is not None:
            age = now - latest.began
            if latest.done.is_set():
                if age < CACHE_S.get(name, 0.0):
                    return latest.value, None
            elif age >= LIMITS[name]:
                return fallback(STILL_BLOCKED), None
            else:
                return None, latest
        mine = _READS[name] = Read(now, fallback(f"{name} did not finish"))

    def run():
        try:
            mine.value = read()
        except Exception as exc:  # noqa: BLE001 - one failing part never fails the others
            mine.value = fallback(f"{name} could not be read ({type(exc).__name__})")
        finally:
            mine.done.set()

    try:
        threading.Thread(target=run, name=f"levi-rig-{name}", daemon=True).start()
    except RuntimeError as exc:  # no thread to be had: answer for the part now
        mine.value = fallback(f"{name} could not be read ({type(exc).__name__})")
        mine.done.set()
        return mine.value, None
    return None, mine


def _await(name, task, fallback):
    """The result of ``task`` if it comes within the part's limit (counted
    from the read's start), else the part's ``unknown``."""
    left = task.began + LIMITS[name] - monotonic()
    if task.done.wait(max(0.0, left)):
        return task.value
    return fallback(f"timed out after {LIMITS[name]:g} s")


def collect(now=None) -> dict:
    """The ``levi.rig.status.v1`` document for this moment."""
    now = time.time() if now is None else now
    parts = {
        "marker": (
            lambda: status.marker_part(marker_path(), now),
            status.unknown_marker,
        ),
        "eval_sessions": (lambda: _sessions(now), status.unknown_sessions),
        "units": (status.units_part, status.unknown_units),
        "guard": (_guard, _unknown_guard),
    }
    started = {name: _acquire(name, *part) for name, part in parts.items()}
    out = {}
    for name, (value, task) in started.items():
        out[name] = value if task is None else _await(name, task, parts[name][1])
    return status.document(
        now, out["marker"], out["eval_sessions"], out["units"], out["guard"]
    )


@router.get("/status")
def rig_status():
    """The arm-controller marker, recent evaluation sessions, our
    ``levi-aeri-*`` units and an advisory busy/idle verdict
    (``levi.rig.status.v1``). Read-only; sampled when asked."""
    return collect()
