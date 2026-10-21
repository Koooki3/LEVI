"""The sessions the marvel0 page hosts, as the rest of the product sees them:
the AERI terminal D and the evaluation client (terminal E). ``levi stop``, the R6
restart, the service's start and its shutdown.

* **``levi stop`` and every other process** (the CLI, the setup probes, the core's
  own stop route) ask ``levi.activity.running_jobs()`` which jobs would be killed.
  A hosted D (or E) must be among them, or an agent that restarts the product by
  R6 starts the hang-up chain under a session that is moving the arm: for D
  (SIGINT, 3 s, SIGKILL) the 7/7 shutdown is cut short and the policy and VLM
  units stay behind; for E the client is killed outright.
  ``job_rows`` answers that **from the run record on disk and the identity of the
  launcher's pid, read only**: it must not use the supervisors of ``aeri_run_api``
  or ``run_api``, whose ``adopt`` signals a session it finds without a keyboard
  (D gets a SIGINT, E is frozen), which is right in a restarted service and wrong
  in a ``levi stop`` process that merely asks. A record that cannot be read is not
  "no session": it is listed (as ``record-unreadable``), so a wrong answer is
  loud, never quiet.
* **The service's start** (``adopt_on_start``) takes over a D that outlived the
  service (``aeri_run_api.adopt_on_start``: one SIGINT, then watched).
* **The service's shutdown** (``shutdown_hosted``) stops a hosted D the way a
  Stop press does (``Supervisor.stop``: Ctrl+C by the pty, once), then waits for
  its 7/7 wrap-up up to ``SHUTDOWN_WAIT_S``. Never a bare kill: what is left when
  the wait ends is the hang-up chain's job (the pty closes with the process). The
  evaluation client has no wrap-up to wait for and is left to its chain. **The wait
  only fits if the service unit gives it the time:** ``levi-product.service`` needs
  ``TimeoutStopSec`` of at least 150 s (the 130 s wait and the chain's 3 s); with a
  shorter one systemd kills the core in the middle of the wait (see the doc).
* **The start gate** (``hosted_visible_to_activity``) is the mechanical check
  that the first of these can see a D at all: a start whose D ``levi stop`` could
  not list is refused (before, as a self-test of the wiring and the record
  folder; after the launch bookkeeping, against the real ``running_jobs``).

Nothing here starts, signals or writes anything except through ``Supervisor.stop``
and ``Supervisor.adopt``, which are the page's own Stop and take-over.
"""

import contextlib
import logging
import os
import stat
import tempfile
import time
from pathlib import Path

from levi import children, paths
from levi.workspace import Workspace

from . import record

KIND = "marvel0-aeri"
CLIENT_KIND = "marvel0-client"
# <workspace state>/marvel0/aeri/record.json and .../marvel0/run/record.json
# (aeri_run.REL, run.REL and record.RECORD; a test compares them so a move cannot
# blind this reader).
REL = ("marvel0", "aeri")
CLIENT_REL = ("marvel0", "run")
HOSTED = ((KIND, REL), (CLIENT_KIND, CLIENT_REL))
# A record in phase "intent" is written just before the launcher is spawned: it is
# a start in flight for a moment, and a leftover of a service that died in between
# after that.
INTENT_GRACE_S = 120.0
# A hosted D wraps up with a 7/7 shutdown of at most about 2 x 60 s
# (subtask_units); the wait is a little longer. ``levi-product.service`` must
# allow it: ``TimeoutStopSec`` >= ``UNIT_STOP_TIMEOUT_S`` (see the doc).
SHUTDOWN_WAIT_S = 130.0
# What the unit's ``TimeoutStopSec`` must be at least: the wait, the chain's grace
# (3 s) and the rest of the service's own shutdown.
UNIT_STOP_TIMEOUT_S = 150.0
SHUTDOWN_POLL_S = 0.5
UNREADABLE = "record-unreadable"
CANARY = "wiring-check"

log = logging.getLogger("levi")


def _identity(pid) -> dict | None:
    """``{"start_ticks", "boot"}`` of ``pid`` (what ``run._identity`` records),
    None once it is gone."""
    who = children.identity(pid)
    if who is None:
        return None
    return {"start_ticks": who["start_ticks"], "boot": who["boot"]}


def record_dir(workspace: Workspace | None = None, rel=REL) -> Path | None:
    """The run record's folder of ``workspace`` (default: the process-wide one),
    or None when there is none to read (no folder, a link, another user's)."""
    base = (workspace or Workspace.at(paths.workspace())).state
    target = base / Path(*rel)
    try:
        info = os.lstat(target)
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        return None
    return target


def _row(rec: dict, pid, now: float, kind=KIND) -> dict:
    began = rec.get("created_epoch")
    return {
        "kind": kind,
        "id": rec.get("run_id"),
        "pid": pid if isinstance(pid, int) and pid > 0 else None,
        "running_seconds": round(now - began) if isinstance(began, (int, float)) else 0,
        "progress": None,
        # Not a pool or conversion worker that a restart merely interrupts: a
        # robot session. A consumer that wants only the heavy jobs filters it.
        "hosted": True,
    }


def row_of(
    rec, *, identity=_identity, now: float | None = None, kind=KIND
) -> dict | None:
    """The job row for a run record, or None when no D runs for it. A pure
    decision on the record and on what ``identity`` says of the launcher's pid.

    ``started`` and the launcher is the process the record names: a D. ``started``
    and that pid is another process or none: over (an earlier service died, the
    reboot...). ``started`` with no identity recorded: not verifiable, so a pid
    that still exists counts. ``intent``: only while young."""
    now = time.time() if now is None else now
    phase = rec.get("phase")
    if phase == "intent":
        began = rec.get("created_epoch")
        young = isinstance(began, (int, float)) and 0 <= now - began < INTENT_GRACE_S
        return _row(rec, None, now, kind) if young else None
    if phase != "started":
        return None
    leader = rec.get("leader") or {}
    pid = leader.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return None
    want = leader.get("identity")
    got = identity(pid)
    if got is None:
        return None
    if not isinstance(want, dict):
        return _row(rec, pid, now, kind)
    same = all(str(want.get(k)) == str(got.get(k)) for k in ("start_ticks", "boot"))
    return _row(rec, pid, now, kind) if same else None


def hosted_job(
    workspace: Workspace | None = None,
    *,
    identity=_identity,
    now=None,
    kind=KIND,
    rel=REL,
) -> dict | None:
    """The hosted session of ``kind`` (default: D) as a job row, or None. Raises
    ``record.RecordCorrupt`` for a record that exists and cannot be read."""
    directory = record_dir(workspace, rel)
    if directory is None:
        return None
    rec = record.read_record(directory)
    if rec is None:
        return None
    return row_of(rec, identity=identity, now=now, kind=kind)


def job_rows(workspace: Workspace | None = None, *, identity=_identity) -> list:
    """What ``levi.activity.running_jobs`` adds: the hosted D and the hosted
    client, or a loud row for one whose answer cannot be had. Never raises."""
    out = []
    for kind, rel in HOSTED:
        try:
            row = hosted_job(workspace, identity=identity, kind=kind, rel=rel)
        except Exception as exc:  # noqa: BLE001 - an unreadable answer is not "idle"
            why = UNREADABLE if isinstance(exc, record.RecordCorrupt) else "error"
            out.append(
                {
                    "kind": kind,
                    "id": why,
                    "pid": None,
                    "running_seconds": 0,
                    "progress": None,
                    "hosted": True,
                }
            )
        else:
            if row is not None:
                out.append(row)
    return out


def hosted_visible_to_activity(
    workspace: Workspace | None = None,
    *,
    run_id: str | None = None,
    identity=_identity,
) -> bool:
    """Whether ``levi.activity.running_jobs()`` lists (or, before a launch, can
    list) the hosted D. False means: do not start, or stop what was just started.

    ``run_id`` given (after the launch bookkeeping: the record says ``started``
    with the launcher's identity): the very ``running_jobs`` is asked, and a row
    of this kind with this id must be in it. Without it (the start gate, before
    anything exists): the wiring is checked, not a run: ``running_jobs`` has this
    module among its sources, the record folder is not something it would refuse
    to read, and a record naming the current process is turned into a row by
    ``row_of``."""
    from levi import activity

    try:
        if run_id is not None:
            return any(
                row.get("kind") == KIND and row.get("id") == run_id
                for row in activity.running_jobs(workspace)
            )
        base = (workspace or Workspace.at(paths.workspace())).state / Path(*REL)
        if os.path.lexists(base) and record_dir(workspace) is None:
            return False  # there, but a link, a file or another user's: never read
        return _canary_listed(identity)
    except Exception:  # noqa: BLE001 - a check that cannot run does not pass
        return False


def _canary_listed(identity) -> bool:
    """A record of a made-up run that names this very process, in a throw-away
    workspace, through the real ``running_jobs``: the source is wired in, the
    record is in the form the launch writes, and the identity check agrees."""
    from levi import activity

    me = os.getpid()
    with tempfile.TemporaryDirectory(prefix="levi-wiring-") as tmp:
        fake = Workspace.at(tmp)
        folder = fake.state / Path(*REL)
        folder.mkdir(parents=True, mode=0o700)
        record.write_record(
            folder,
            {
                "phase": "started",
                "run_id": CANARY,
                "created_epoch": time.time(),
                "leader": {"pid": me, "identity": identity(me)},
            },
        )
        rows = activity.running_jobs(fake)
    return any(r.get("kind") == KIND and r.get("id") == CANARY for r in rows)


def adopt_on_start() -> None:
    """Take over a D that outlived the service (``aeri_run_api.adopt_on_start``:
    one SIGINT to a D that has lost its keyboard, then watched). Never raises."""
    try:
        from . import aeri_run_api

        aeri_run_api.adopt_on_start()
    except Exception:
        log.exception("taking over a hosted AERI terminal D failed")


def shutdown_hosted(
    *,
    supervisor=None,
    wait_s: float = SHUTDOWN_WAIT_S,
    sleep=time.sleep,
    monotonic=time.monotonic,
) -> dict:
    """The orderly end of a hosted D when the service shuts down: the same Stop
    a person presses (Ctrl+C by the pty, sent once; a D that was told already, or
    is past stopping, is just waited for), then the wait for its wrap-up. Returns
    ``{"state": none|ended|elsewhere|timeout|error, "stop": <what Stop answered>}``
    and never raises. After a timeout nothing more is done: the pty closes with this
    process and the launcher's hang-up chain (SIGINT, 3 s, SIGKILL) takes over.

    A run that **another live core owns** (Stop answers ``driven_elsewhere``) is
    that core's to stop and to wait for: this one holds no pty of it, so closing
    nothing here hangs the launcher up. It returns ``elsewhere`` at once, without
    the wait and without saying the chain takes over (mechanics review 2, N3)."""
    try:
        if supervisor is None:
            from . import aeri_run_api

            supervisor = aeri_run_api.supervisor()
        job = supervisor.active()
    except Exception:
        log.exception("asking for a hosted AERI terminal D failed at shutdown")
        return {"state": "error", "stop": None}
    if job is None:
        return {"state": "none", "stop": None}
    sent = _stop(supervisor, job)
    if sent == "driven_elsewhere":
        log.info(
            "the hosted AERI terminal D belongs to another core: it is that "
            "core's to stop; nothing to wait for here"
        )
        return {"state": "elsewhere", "stop": sent}
    deadline = monotonic() + wait_s
    while monotonic() < deadline:
        try:
            with contextlib.suppress(Exception):
                supervisor.tick()
            if supervisor.active() is None:
                log.info("the hosted AERI terminal D wrapped up before shutdown")
                return {"state": "ended", "stop": sent}
        except Exception:  # noqa: BLE001
            return {"state": "error", "stop": sent}
        sleep(SHUTDOWN_POLL_S)
    log.warning(
        "the hosted AERI terminal D was still wrapping up after %.0f s: "
        "the hang-up chain takes over",
        wait_s,
    )
    return {"state": "timeout", "stop": sent}


def _stop(supervisor, job) -> str:
    """One Stop, the page's own (``Supervisor.stop``); what it answered."""
    from .aeri_state import Refuse

    try:
        supervisor.stop({"request_id": f"shutdown-{job.get('id')}"[:80]})
        return "sent"
    except Refuse as exc:
        # d_gone, stop_spent, state_not_allowed (fault_locked...): nothing to
        # send, the wrap-up goes on by itself.
        return exc.code
    except Exception:
        log.exception("stopping the hosted AERI terminal D at shutdown failed")
        return "error"
