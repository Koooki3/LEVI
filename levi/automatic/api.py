"""HTTP interface of the automatic evaluation pipeline (T-CL-11, T-API-1).

Routes under ``/api/levi/automatic`` exactly as the interface contract of
LEVI 2.0 lists them (``levi2/design/aeri-ui-api-contract.md`` §2, §3): the
product's front end proxies them with the person's token; an agent's Bearer
credential never reaches a write route (``_person``).

This module only *reads* run folders and *calls* the layers below it:

- ``launch.plan`` / ``launch.launch`` / ``launch.attach`` (the launch core);
- ``control.write_command`` / ``control.wait_result`` (the command channel of
  a running runner: stop, resume);
- ``metrics.label_operator`` (an operator's blind outcome label);
- ``adapters.human.write_answer`` (a person's answer to a scene question);
- ``jobs_wizard`` (a form becomes a new job file), ``setup_guide``.

Nothing here moves a robot, opens a port, starts a GPU program or starts a
unit other than through ``launch``. In this version only ``dry_run`` can be
launched: every other execution mode answers 501 ``no_robot_adapter``.

Safety rules kept in one place:

- every id (run, command, request, evidence, frame) is matched against a
  strict pattern before it touches a path; files below a run folder are
  opened component by component with ``O_NOFOLLOW`` (a symbolic link is never
  followed) and read with a size cap;
- a text that leaves this module is scrubbed of absolute paths;
- a GET writes nothing (the resume ``challenge`` is memory only);
- a write needs the person (``_person``), a ``request_id`` (the same request
  again returns the first answer) and, for actions that change a run, a
  typed ``confirm``; one operation at a time per resource (423 otherwise);
- the *blind* rule (T-CL-14): until the operator has given an episode a
  success/failure label nothing here shows or hints at how the automatic
  system judged it or why the episode ended (``goal_verified`` and the
  like are hidden in events and in the card, the run-wide automatic rates
  of the metrics are withheld).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import stat
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from levi.domain import aeri

from . import cli, launch, metrics, policies
from .journal import Journal, JournalError, read_plan
from .recorder import EVIDENCE, FRAMES, MANIFEST, pending_card

router = APIRouter(prefix="/api/levi/automatic", tags=["Automatic evaluation"])

POLICY_ROOT_ENV = "LEVI_AERI_POLICY_ROOT"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ID = launch.ID
MAX_JSON_BYTES = 1 << 20
MAX_FRAME_BYTES = 8 << 20
MAX_EVENTS = 500
MAX_JOBS_SHOWN = 200
MAX_RUNS_SHOWN = 50
POLICY_CACHE_S = 30.0
CHALLENGE_S = 60.0
STOP_WAIT_S = 3.0
RESUME_WAIT_S = 3.0
PRINCIPAL = "ui"
# How a launch is hosted. Tests replace it (``inprocess`` runs a short dry
# run in the calling thread); production is always a systemd user unit.
LAUNCH_BACKEND = "systemd"
LAUNCH_WAIT_S = 0.0
LAUNCH_DEADLINE_S: float | None = None

# The 13 states, as the state machine defines them.
STATES = aeri.AERI_STATES
WAITING = ("WAIT_HUMAN", "FAULT_LOCKED")
# Why an episode ended: not shown before the operator's own label.
BLIND_REASONS = ("goal_verified", "horizon_exhausted", "operator_stop")
HIDDEN = "hidden_until_labelled"
MODES = launch.MODES
LABEL_VALUES = metrics.OPERATOR_VALUES

_PATH = re.compile(r"(?<![\w/.:-])/(?:[A-Za-z0-9_.~+@%=,-]+/)*[A-Za-z0-9_.~+@%=,-]+")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- errors, the person, scrubbing -----------------------------------------------------


def _fail(status: int, code: str, message: str, **extra) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message, **extra})


def _person(request: Request) -> None:
    """Only a person at the page may do this. The service's middleware has
    already turned an agent's Bearer credential away and demanded the UI
    token; this refuses again, so the rule does not depend on how the router
    is mounted. Any Bearer is refused: the only Bearer credentials LEVI
    knows are agents'."""
    if request.headers.get("authorization", "").lower().startswith("bearer "):
        raise _fail(
            403, "person_only", "This is a person's action; agents cannot do it"
        )
    secret = os.getenv("LEVI_UI_TOKEN")
    if secret and not secrets.compare_digest(
        request.headers.get("x-levi-ui-token", ""), secret
    ):
        raise _fail(401, "ui_token", "Use the LEVI Web UI")


def scrub(value: Any) -> Any:
    """``value`` with every absolute path in its texts replaced by
    ``<path>`` (recursively; numbers and keys are left alone)."""
    if isinstance(value, str):
        return _PATH.sub("<path>", value)
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    return value


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _ms(ns) -> int | None:
    return ns // 1_000_000 if isinstance(ns, int) else None


_REFUSAL = {
    "E_NO_ROBOT_ADAPTER": "no_robot_adapter",
    "E_RUN_EXISTS": "run_exists",
    "E_ROBOT_BUSY": "robot_busy",
    "E_ROBOT_BUSY_CLIENT": "robot_busy",
    "E_PLAN_CHANGED": "plan_changed",
    "E_TOKEN_EXPIRED": "token_expired",
    "E_TOKEN_INVALID": "token_invalid",
    "E_SYSTEMD": "systemd_unavailable",
    "E_KEY": "unavailable",
    "E_RUNNER_ALIVE": "runner_alive",
    "E_RUN_COMPLETED": "run_completed",
    "E_JOB_INVALID": "job_invalid",
    "E_JOB_OUTSIDE_ROOTS": "job_outside_roots",
}


def _refused(exc: launch.LaunchRefused) -> HTTPException:
    code = _REFUSAL.get(exc.code, exc.code.removeprefix("E_").lower())
    return _fail(exc.status, code, scrub(exc.detail)[:300], launch_code=exc.code)


# --- memory shared by the routes -----------------------------------------------------


class _Memo:
    """Bounded memory of answered write requests: the same ``request_id``
    with the same body returns the first answer; another body under a used
    id is a conflict."""

    def __init__(self, size: int = 1024):
        self.size = size
        self.items: OrderedDict = OrderedDict()
        self.lock = threading.Lock()

    def get(self, scope: str, request_id: str, body: Any):
        with self.lock:
            found = self.items.get((scope, request_id))
        if found is None:
            return None
        if found[0] != _fingerprint(body):
            raise _fail(
                409,
                "request_id_used",
                "This request_id belongs to another request; use a new one",
            )
        return found[1]

    def put(self, scope: str, request_id: str, body: Any, answer: Any) -> None:
        with self.lock:
            self.items[(scope, request_id)] = (_fingerprint(body), answer)
            while len(self.items) > self.size:
                self.items.popitem(last=False)


def _fingerprint(body: Any) -> str:
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, default=str).encode()
    ).hexdigest()


_MEMO = _Memo()
_OPERATIONS: dict[str, threading.Lock] = {}


@contextlib.contextmanager
def _operation(key: str):
    """One operation at a time on a resource; a second one is 423."""
    lock = _OPERATIONS.setdefault(key, threading.Lock())
    if not lock.acquire(blocking=False):
        raise _fail(423, "busy", "Another operation on this is in progress; wait")
    try:
        yield
    finally:
        lock.release()


class _Challenges:
    """The one-time values a resume carries (bound to ``expected_seq``, 60 s,
    memory only). A snapshot asks for one; while the sequence has not moved
    and the value has half its time left, the same value is given again so a
    dialog opened earlier stays valid."""

    def __init__(self):
        self.lock = threading.Lock()
        self.by_run: dict[str, dict[str, tuple[int, float]]] = {}

    def issue(self, run_id: str, seq: int) -> str:
        now = time.monotonic()
        with self.lock:
            live = {
                token: (s, exp)
                for token, (s, exp) in self.by_run.get(run_id, {}).items()
                if exp > now
            }
            for token, (s, exp) in live.items():
                if s == seq and exp - now > CHALLENGE_S / 2:
                    self.by_run[run_id] = live
                    return token
            token = secrets.token_urlsafe(16)
            live[token] = (seq, now + CHALLENGE_S)
            if len(live) > 8:
                for stale in sorted(live, key=lambda t: live[t][1])[:-8]:
                    live.pop(stale)
            self.by_run[run_id] = live
            if len(self.by_run) > 256:
                self.by_run.pop(next(iter(self.by_run)))
            return token

    def take(self, run_id: str, token: str, seq: int) -> bool:
        """True (and the value is used up) when ``token`` was issued for
        ``seq`` and has not expired."""
        now = time.monotonic()
        with self.lock:
            held = self.by_run.get(run_id, {})
            found = held.get(token)
            if found is None or found[1] <= now or found[0] != seq:
                return False
            held.pop(token)
            return True


_CHALLENGES = _Challenges()

# plan_sha256 -> what produced it (mode, overrides): a launch carries only
# the digest, so the server remembers which request it belongs to.
_PLANS: OrderedDict = OrderedDict()
_PLANS_LOCK = threading.Lock()
PLAN_MEMORY_S = 3600.0


def _remember_plan(job_id: str, digest: str, mode: str | None, overrides: dict):
    with _PLANS_LOCK:
        _PLANS[(job_id, digest)] = (mode, dict(overrides), time.monotonic())
        while len(_PLANS) > 128:
            _PLANS.popitem(last=False)


def _recall_plan(job_id: str, digest: str):
    with _PLANS_LOCK:
        found = _PLANS.get((job_id, digest))
    if found and time.monotonic() - found[2] < PLAN_MEMORY_S:
        return found[0], found[1]
    return None


# --- reading files below a run folder ----------------------------------------------


def _open_below(base: Path, parts: tuple[str, ...]) -> int:
    """A read-only descriptor of the regular file ``base/parts...``: every
    component opened with ``O_NOFOLLOW`` below the one before it, so no
    symbolic link and no ``..`` leads out of ``base``. ``OSError`` when it is
    not there or not a regular file."""
    if any(not part or part in (".", "..") or "/" in part for part in parts):
        raise OSError("bad path component")
    fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        leaf = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
        )
    finally:
        os.close(fd)
    try:
        if not stat.S_ISREG(os.fstat(leaf).st_mode):
            raise OSError("not a regular file")
    except BaseException:
        os.close(leaf)
        raise
    return leaf


def _read_below(base: Path, parts: tuple[str, ...], limit: int) -> bytes:
    fd = _open_below(base, parts)
    try:
        data = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise OSError("too large")
    return data


def _json_below(base: Path, parts: tuple[str, ...]):
    try:
        return json.loads(_read_below(base, parts, MAX_JSON_BYTES))
    except (OSError, ValueError):
        return None


# --- finding a run ---------------------------------------------------------------------


class _Run:
    """One run as the routes see it: the index entry, the run folder, the
    journal as it stands and the plan (read once)."""

    def __init__(self, run_id: str, entry: dict, run_dir: Path):
        self.run_id = run_id
        self.entry = entry
        self.run_dir = run_dir
        self.scan = Journal.read(run_dir)
        self.events = self.scan.events
        self.state = self.scan.effective_state if self.events else "INIT"
        self.seq = len(self.events)
        try:
            kept = read_plan(run_dir)
        except JournalError:
            kept = None
        self.plan = (kept or {}).get("plan") or {}
        self.manifest = _json_below(run_dir, (MANIFEST,)) or {}

    @property
    def reset_mode(self):
        return (
            self.entry.get("reset_mode")
            or self.plan.get("reset_mode")
            or (self.plan.get("run") or {}).get("reset_strategy")
        )

    @property
    def scene_check(self):
        return (
            self.plan.get("scene_check")
            or metrics.mode_of(
                self.events, manifest=self.manifest, reset_mode=self.reset_mode
            )["scene_check"]
        )

    @property
    def episodes_total(self):
        value = (self.plan.get("run") or {}).get("episodes")
        return value if isinstance(value, int) else None

    def runner(self) -> dict:
        record = _json_below(self.run_dir, (launch.RUNNER_RECORD,))
        if not isinstance(record, dict):
            record = self.entry.get("runner")
        record = record if isinstance(record, dict) else {}
        alive = launch.runner_alive(record)
        out = {"alive": alive}
        if alive and isinstance(record.get("pid"), int):
            out["pid"] = record["pid"]
        return out

    def revealed(self) -> set:
        """Episodes the operator has labelled success or failure."""
        try:
            lines = metrics.LabelStore(self.run_dir).lines("operator_label")
        except (metrics.LabelRefused, OSError):
            return set()
        return {
            line["episode_id"]
            for line in lines
            if line.get("subject") == "task_outcome"
            and line.get("value") in metrics.DECIDED
        }

    def unlabelled_ended(self) -> list:
        revealed = self.revealed()
        return [e for e in metrics.ended_forward(self.events) if e not in revealed]


def _locate(run_id: str) -> _Run:
    if not isinstance(run_id, str) or not ID.fullmatch(run_id):
        raise _fail(404, "not_found", "No such run")
    try:
        entry = launch.read_record(launch.index_path(run_id))
        run_dir = Path(entry["run_dir"])
        launched = (run_dir / launch.LAUNCH_RECORD).is_file()
    except (OSError, ValueError, KeyError, TypeError, launch.LaunchRefused):
        raise _fail(404, "not_found", "No such run") from None
    if run_dir.name != run_id or not launched or run_dir.is_symlink():
        raise _fail(404, "not_found", "No such run")
    return _Run(run_id, entry, run_dir)


# --- capabilities, policies --------------------------------------------------------------

NO_ADAPTER = "no real robot adapter in this version (only a dry run can launch)"


@router.get("/capabilities")
def capabilities():
    """What this LEVI can launch. Reads nothing."""
    unavailable = {"available": False, "reason": NO_ADAPTER}
    return {
        "adapters": [
            {"id": "fake", "kind": "fake", "available": True},
            {"id": "fr3", "kind": "robot", "available": False, "reason": NO_ADAPTER},
        ],
        "modes": {
            "dry_run": {"available": True},
            "shadow": dict(unavailable),
            "assisted": dict(unavailable),
            "autonomous": dict(unavailable),
        },
        "reset_modes": list(aeri.RESET_MODES),
        "scene_checks": list(aeri.SCENE_CHECKS),
        "defaults": {
            "max_steps": 120,
            "episodes": 1,
            "reset_wait_s": aeri.DEFAULT_HUMAN_SCENE_TIMEOUT_S,
        },
    }


_POLICY_CACHE: dict = {"key": None, "at": 0.0, "report": None}
_POLICY_LOCK = threading.Lock()


def policy_root() -> str:
    return os.environ.get(POLICY_ROOT_ENV, "").strip()


def discover_policies():
    """The cached ``DiscoveryReport`` of the configured policy root (or
    ``None`` when none is configured). Read only, never loads weights."""
    root = policy_root()
    if not root:
        return None
    now = time.monotonic()
    with _POLICY_LOCK:
        if _POLICY_CACHE["key"] == root and now - _POLICY_CACHE["at"] < POLICY_CACHE_S:
            return _POLICY_CACHE["report"]
    report = policies.discover_report(root)
    with _POLICY_LOCK:
        _POLICY_CACHE.update(key=root, at=now, report=report)
    return report


def _sha_status(entry) -> str:
    """``verified``: a hash list that names the weight files exists on disk
    (LEVI never recomputes it); ``recorded``: a list for this directory's
    other files; ``none``: nothing, or only the hashes of other files."""
    if entry.sha256_state == "recorded":
        return "verified" if entry.params_hashed else "recorded"
    return "none"


def _checkpoint(entry) -> dict:
    notes = [scrub(n) for n in (*entry.warnings, *entry.problems)]
    if entry.sha256_state == "indirect":
        notes.append("hashes of the source weights only: not a hash of these weights")
    if entry.role == "unknown":
        notes.append(f"role not stated ({entry.role_source})")
    return {
        "id": entry.name,
        "name": entry.name,
        "role": entry.role,
        "config": entry.config,
        "sha256_status": _sha_status(entry),
        "family": entry.model or entry.variant,
        "notes": notes,
    }


@router.get("/policies")
def list_policies():
    """Checkpoints a run may deploy (JAX directories with their parameters
    and statistics), from the directory listing only. ``reset_available`` is
    false when no checkpoint is stated to be a reset policy: the page then
    offers a person's reset only."""
    report = discover_policies()
    if report is None:
        return {
            "checkpoints": [],
            "reset_available": False,
            "root_configured": False,
            "problems": ["policy_root_not_configured"],
        }
    found = [e for e in report.entries if e.deployable]
    return {
        "checkpoints": [_checkpoint(e) for e in found],
        "reset_available": any(e.role == "reset" for e in found),
        "root_configured": True,
        "hidden": len(report.entries) - len(found),
        "truncated": report.truncated,
        "problems": list(report.problems),
    }


def checkpoint_ids(role: str) -> dict:
    """``{id: entry}`` of the deployable checkpoints that may serve as the
    ``role`` policy (forward: stated forward or unstated; reset: stated reset
    only)."""
    report = discover_policies()
    if report is None:
        return {}
    keep = {"reset"} if role == "reset" else {"forward", "unknown"}
    return {e.name: e for e in report.entries if e.deployable and e.role in keep}


# --- job files -----------------------------------------------------------------------------


def job_id_of(root, name: str) -> str:
    digest = hashlib.sha256(f"{root}\0{name}".encode()).hexdigest()
    return f"j-{digest[:20]}"


def _job_items(limit: int = 500) -> dict:
    """``{job id: launch.jobs() item}`` of the job files under the roots."""
    return {
        job_id_of(item["root"], item["name"]): item for item in launch.jobs(limit=limit)
    }


def _job_path(job_id: str) -> str:
    if not isinstance(job_id, str) or not ID.fullmatch(job_id):
        raise _fail(404, "not_found", "No such job")
    item = _job_items().get(job_id)
    if item is None:
        raise _fail(404, "not_found", "No such job")
    return item["path"]


@router.get("/jobs")
def list_jobs():
    roots = [Path(r) for r in launch.job_roots()]
    items = _job_items()
    out = []
    for job_id, item in list(items.items())[:MAX_JOBS_SHOWN]:
        errors, reset_mode, valid = [], None, True
        try:
            job = cli.load_job(item["path"])
            reset_mode = job["config"].reset_strategy
        except cli.JobError as exc:
            valid, errors = False, [scrub(str(exc))[:300]]
        except OSError as exc:
            valid, errors = False, [scrub(exc.strerror or str(exc))]
        out.append(
            {
                "id": job_id,
                "name": item["name"],
                "modified": _ms(item["mtime_ns"]),
                "valid": valid,
                "reset_mode": reset_mode,
                "errors": errors,
            }
        )
    return {
        "roots": [r.name or "root" for r in roots],
        "jobs": out,
        "truncated": len(items) > MAX_JOBS_SHOWN,
    }


# --- the plan ----------------------------------------------------------------------------------


class PlanBody(_Strict):
    job_id: str = Field(min_length=1, max_length=128)
    execution_mode: Literal["dry_run", "shadow", "assisted", "autonomous"] | None = None
    overrides: dict[str, Any] | None = None


def _severity(value: str) -> str:
    return "warn" if value == "warning" else value


def _plan_view(job_id: str, found: launch.LaunchPlan) -> dict:
    return {
        "job_id": job_id,
        "plan_sha256": found.plan_sha256,
        "run_id": found.run_id,
        "execution_mode": found.execution_mode,
        "reset_mode": found.reset_mode,
        "scene_check": found.scene_check,
        "roles": found.roles,
        "episodes": found.episodes,
        "launchable": found.launchable,
        "refusals": found.refusals,
        "checks": [
            {
                "code": c["code"],
                "ok": c["ok"],
                "severity": _severity(c["severity"]),
                "detail": scrub(str(c["detail"]))[:400],
            }
            for c in found.checks
        ],
        "isc": (
            {k: found.isc[k] for k in ("id", "version", "status")}
            if found.isc
            else None
        ),
        "uses_live": found.uses_live,
        "motion": found.motion,
        "needs_arming": found.needs_arming,
        "warnings": [scrub(w)[:400] for w in found.warnings],
        "launch_token": found.launch_token,
        "token_expires_at": _ms(found.token_expires_wall_ns),
    }


def _request_for(path: str, mode: str | None, overrides: dict):
    return launch.LaunchRequest(
        job_path=path,
        execution_mode=mode,
        overrides=overrides,
        entry="api",
        principal={"kind": "operator", "id": PRINCIPAL},
        backend=LAUNCH_BACKEND,
    )


@router.post("/plan")
def make_plan(body: PlanBody):
    """The launch plan of a job. Writes nothing but the core key on first
    use. A mode left out means ``dry_run`` (the only mode that can launch)."""
    path = _job_path(body.job_id)
    mode = body.execution_mode or launch.DRY_RUN
    overrides = dict(body.overrides or {})
    try:
        found = launch.plan(_request_for(path, mode, overrides))
    except launch.LaunchRefused as exc:
        raise _refused(exc) from None
    if found.plan_sha256 is None:
        errors = [scrub(str(c["detail"]))[:300] for c in found.checks if not c["ok"]]
        raise _fail(422, "job_invalid", "The job cannot be planned", errors=errors)
    _remember_plan(body.job_id, found.plan_sha256, mode, overrides)
    return _plan_view(body.job_id, found)


# --- runs: list and snapshot --------------------------------------------------------------------


def _last_activity_ms(run: _Run) -> int | None:
    if run.events:
        return _ms(run.events[-1].emitted_wall_ns)
    return _ms(run.entry.get("created_wall_ns"))


def _summary(run: _Run) -> dict:
    return {
        "run_id": run.run_id,
        "state": run.state,
        "reset_mode": run.reset_mode,
        "execution_mode": run.entry.get("execution_mode"),
        "episodes_done": len(metrics.ended_forward(run.events)),
        "episodes_total": run.episodes_total,
        "started_at": _ms(run.entry.get("created_wall_ns")),
        "updated_at": _last_activity_ms(run),
    }


@router.get("/runs")
def list_runs():
    rows = []
    for entry in launch.runs()[:MAX_RUNS_SHOWN]:
        run_id = entry.get("run_id")
        if not isinstance(run_id, str) or not ID.fullmatch(run_id):
            continue
        with contextlib.suppress(HTTPException):
            rows.append(_summary(_locate(run_id)))
    return {"runs": rows}


def _names(items) -> list:
    return [str(i) for i in items] if isinstance(items, (list, tuple)) else []


def card_of(run: _Run, *, force: bool = False) -> dict | None:
    """The contract's ``PendingCard``; ``None`` when no person is waited for
    (and not ``force``). The automatic verdict is ``null`` until the
    operator labelled the episode success or failure."""
    raw = pending_card(run.run_dir)
    if not raw.get("waiting") and not force:
        return None
    operator = raw.get("operator_label") or {}
    last = raw.get("last_episode")
    revealed = bool(operator.get("revealed"))
    verdict = operator.get("automatic_verdict") if revealed else None
    verdict = verdict.get("verdict") if isinstance(verdict, dict) else None
    contract = raw.get("contract") or {}
    evidence = raw.get("evidence")
    assessment = None
    if evidence:
        assessment = {
            "decision": evidence.get("decision"),
            "failed": _names(evidence.get("failed_predicates")),
            "unknown": _names(evidence.get("unknown_predicates")),
            "frame_refs": [
                f["sha256"]
                for f in (evidence.get("frames") or [])
                if isinstance(f, dict) and f.get("file") and f.get("sha256")
            ],
        }
    episode = None
    if last:
        rollout = last.get("rollout") or {}
        label = (
            f"{rollout.get('task_folder')}/{rollout.get('demo')}"
            if rollout.get("demo")
            else None
        )
        episode = {
            "episode_id": last.get("episode_id"),
            "rollout_label": label,
            "ended_by": operator.get("ended_by"),
            "stop_reason": last.get("stop_reason"),
        }
    reason = raw.get("reason")
    if reason in BLIND_REASONS and operator and not revealed:
        reason = HIDDEN
    return {
        "reason": reason,
        "resume_seq": raw.get("expected_seq"),
        "waited_ms": raw.get("waited_ms") or 0,
        "nth_wait": raw.get("human_wait_number") or 0,
        "last_episode": episode,
        "contract": {
            "id_version": contract.get("key"),
            "status": contract.get("status"),
            "predicates": [p["name"] for p in contract.get("predicates", [])],
        },
        "assessment": assessment,
        "operator_label": {
            "episode_id": operator.get("episode_id"),
            "current": operator.get("current"),
            "automatic_verdict": verdict,
            "hidden_until_labelled": not revealed,
        },
    }


def scene_question_of(run: _Run) -> dict | None:
    """The open scene question of a person-attested run (the file protocol
    of ``adapters.human.FileTransport`` under ``<run>/scene/``)."""
    base = run.run_dir
    try:
        names = sorted(
            n
            for n in os.listdir(base / "scene" / "questions")
            if n.endswith(".json") and not n.startswith(".")
        )
    except OSError:
        return None
    best = None
    for name in names[-8:]:
        value = _json_below(base, ("scene", "questions", name))
        if isinstance(value, dict) and ID.fullmatch(str(value.get("request_id"))):
            best = (name, value)
    if best is None:
        return None
    name, value = best
    try:
        asked = _ms(os.stat(base / "scene" / "questions" / name).st_mtime_ns)
    except OSError:
        asked = None
    timeout = (run.plan.get("run") or {}).get("human_scene_timeout_ns")
    return {
        "request_id": value["request_id"],
        "nonce": value.get("nonce"),
        "frames_sha256": value.get("frames_sha256"),
        "frames": [
            f["sha256"]
            for f in value.get("frames", [])
            if isinstance(f, dict) and f.get("file") and f.get("sha256")
        ],
        "predicates": [
            {
                "name": p.get("name"),
                "text": scrub(str(p.get("text", ""))),
                "required": bool(p.get("required")),
            }
            for p in value.get("predicates", [])
            if isinstance(p, dict)
        ],
        "asked_at": asked,
        "timeout_s": (
            timeout // 1_000_000_000
            if isinstance(timeout, int)
            else aeri.DEFAULT_HUMAN_SCENE_TIMEOUT_S
        ),
    }


def snapshot_of(run: _Run) -> dict:
    committed = [e for e in run.events if e.record == "committed"]
    ended = metrics.ended_forward(run.events)
    automation = metrics.automation(run.events, run.reset_mode)
    current = next((e.episode_id for e in reversed(committed) if e.episode_id), None)
    return {
        "run_id": run.run_id,
        "state": run.state,
        "seq": run.seq,
        "reset_mode": run.reset_mode,
        "scene_check": run.scene_check,
        "execution_mode": run.entry.get("execution_mode"),
        "episodes": {
            "done": len(ended),
            "total": run.episodes_total,
            "current": None if run.state == "COMPLETED" else current,
        },
        "counters": {
            "planned_interventions": automation["planned"],
            "unplanned_interventions": automation["unplanned"],
            "faults": sum(1 for e in committed if e.to_state == "FAULT_LOCKED"),
        },
        "pending_card": card_of(run),
        "scene_question": scene_question_of(run),
        "challenge": _CHALLENGES.issue(run.run_id, run.seq),
        "runner": run.runner(),
        "updated_at": _last_activity_ms(run),
    }


@router.get("/runs/{run_id}")
def run_snapshot(run_id: str):
    return snapshot_of(_locate(run_id))


@router.get("/runs/{run_id}/events")
def run_events(run_id: str, after: int = -1, limit: int = 200):
    """Journal lines after ``after`` (their ``seq``): transitions and notes.
    The reason an episode ended is hidden until its operator label."""
    run = _locate(run_id)
    limit = max(1, min(int(limit), MAX_EVENTS))
    revealed = run.revealed()
    rows = []
    for event in run.events:
        if event.sequence_no <= after or event.record not in ("committed", "note"):
            continue
        reason = (
            event.reason
            if event.record == "committed"
            else (event.note.code if event.note else None)
        )
        if (
            event.record == "committed"
            and reason in BLIND_REASONS
            and event.episode_id not in revealed
        ):
            reason = HIDDEN
        rows.append(
            {
                "seq": event.sequence_no,
                "at": _ms(event.emitted_wall_ns),
                "kind": event.record,
                "from": event.from_state,
                "to": event.to_state,
                "reason": reason,
            }
        )
        if len(rows) >= limit:
            break
    return {"events": rows, "next": rows[-1]["seq"] if rows else max(after, -1)}


WITHHELD = ("autonomous", "early_termination")


@router.get("/runs/{run_id}/metrics")
def run_metrics(run_id: str):
    """``metrics.report`` of the run. While an ended episode has no
    operator label, the run's own verdict rates are withheld (``null``,
    listed in ``withheld``): the operator judges first."""
    run = _locate(run_id)
    steps = (run.plan.get("run") or {}).get("forward_max_steps")
    try:
        found = metrics.report(
            run.events,
            labels=metrics.LabelStore(run.run_dir),
            manifest=run.manifest or None,
            max_steps=steps if isinstance(steps, int) else None,
            reset_mode=run.reset_mode
            if run.reset_mode in metrics.RESET_MODES
            else None,
            scene_check=run.scene_check
            if run.scene_check in metrics.SCENE_CHECKS
            else None,
        )
    except (metrics.LabelRefused, ValueError) as exc:
        raise _fail(409, "labels_unreadable", scrub(str(exc))[:300]) from None
    found["state"] = run.state
    pending = run.unlabelled_ended()
    found["withheld"] = []
    if pending:
        for name in WITHHELD:
            found[name] = None
        found["withheld"] = list(WITHHELD)
    return found


# --- evidence and frames --------------------------------------------------------------------


@router.get("/runs/{run_id}/evidence/{eid}")
def run_evidence(run_id: str, eid: str):
    run = _locate(run_id)
    if not ID.fullmatch(eid) or eid == "initial_state":
        raise _fail(404, "not_found", "No such evidence")
    value = _json_below(run.run_dir, (EVIDENCE, f"{eid}.json"))
    if not isinstance(value, dict):
        raise _fail(404, "not_found", "No such evidence")
    return scrub(value)


_FRAME_TYPES = {".jpg": "image/jpeg", ".png": "image/png"}


@router.get("/runs/{run_id}/frames/{sha256}")
def run_frame(run_id: str, sha256: str):
    run = _locate(run_id)
    if not SHA256.fullmatch(sha256):
        raise _fail(404, "not_found", "No such frame")
    for suffix, media in _FRAME_TYPES.items():
        try:
            data = _read_below(
                run.run_dir, (EVIDENCE, FRAMES, f"{sha256}{suffix}"), MAX_FRAME_BYTES
            )
        except OSError:
            continue
        return Response(
            data,
            media_type=media,
            headers={
                "Cache-Control": "private, max-age=3600",
                "X-Content-Type-Options": "nosniff",
            },
        )
    raise _fail(404, "not_found", "No such frame")
