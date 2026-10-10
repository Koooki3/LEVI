"""The launch core of an automatic run (T-CL-07, design X2 §3): the one
place that turns a job file into a plan and a plan into a running run. The
command line (``levi automatic plan|run|attach``) and the later HTTP API are
thin shells over ``plan()`` and ``launch()``, so both give the same
``plan_sha256`` for the same job file.

``plan(request)`` reads the job file and its Initial State Contract and
returns a ``LaunchPlan``. It changes nothing about the job, the run or the
robot: its only possible write is the core key under ``LEVI_AERI_HOME``,
created once (0600) the first time a token is needed. The plan says what
would run (execution mode, reset mode, scene check, roles, adapters, the run
folder), every check with its result, the refusals, whether it is
``launchable``, and a ``launch_token``.

``plan_sha256`` is the sha256 of the normalised plan: the job loader's plan
(``cli.load_job``: keys sorted, defaults filled in, the rollout root an
absolute real path, the contract file's byte hash, the keys the reset mode
ignores left out) with the execution mode and the allowed overrides applied.
Who asks (``entry``, ``principal``) and how the run is hosted (``backend``)
are not part of it, nor are facts of the environment (the LEVI commit), so
the command line and the API compute the same digest.

``launch_token`` is ``v1.<expires_wall_ns>.<hmac>``: an HMAC-SHA256, under
the core key, of the plan digest, the job file's identity (device, inode,
size, modification and change times, sha256) and the expiry (ten minutes).
``launch()`` plans again and checks the token, so a job file edited after
its plan was read is refused (``E_PLAN_CHANGED``) instead of launched.

Execution modes: only ``dry_run`` (in-process fakes, no network) can be
launched in this version. ``shadow``, ``assisted`` and ``autonomous`` plans
are never launchable, and ``launch()`` refuses them again
(``E_NO_ROBOT_ADAPTER``) whatever the plan says.

Backends: ``inprocess`` (dry runs only: the runner in the calling thread),
``foreground`` (the runner in this process, which owns the terminal) and
``systemd`` (``systemd-run --user --unit=levi-aeri-<run_id> --collect -p
KillMode=control-group -p TimeoutStopSec=120 <python> -m
levi.automatic.runner ...``, never with ``Restart``: a runner that crashed
is brought back by a person with ``attach``, which locks the run in
FAULT_LOCKED). The runner is never registered in the product's
``processes.json`` (``levi/children.py``): stopping or restarting the
product must not stop a run.

Files: ``$LEVI_AERI_HOME`` (default ``~/.levi-aeri``, 0700) holds
``core.key``, the robot locks ``robot-<adapter_id>.lock``, the run index
``runs/<run_id>.json``, the command audit ``control.jsonl`` and the folders
of launched dry runs ``dry-runs/<run_id>/``. ``LEVI_AERI_JOB_ROOTS`` (paths
separated by ``:``) lists where job files may be picked from by the API.
"""

import contextlib
import copy
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import cli
from .journal import fsync_dir, plan_digest, write_durable

HOME_ENV = "LEVI_AERI_HOME"
JOB_ROOTS_ENV = "LEVI_AERI_JOB_ROOTS"
DEFAULT_HOME = "~/.levi-aeri"
KEY_FILE = "core.key"
RUNS = "runs"
DRY_RUNS = "dry-runs"
CONTROL_LOG = "control.jsonl"
LAUNCH_RECORD = "launch.json"
RUNNER_RECORD = "runner.json"
INDEX_SCHEMA = "levi.aeri.run_index.v1"
LAUNCH_SCHEMA = "levi.aeri.launch.v1"
TOKEN_TTL_NS = 10 * 60 * 1_000_000_000
TOKEN_VERSION = "v1"
SYSTEMD_TIMEOUT_S = 30
MAX_RECORD_BYTES = 64 * 1024
# Runner records of a runner that is gone (a foreground runner's process
# lives on after it).
ENDED_RUNNER = ("exited", "refused", "failed_to_start")

DRY_RUN = "dry_run"
MODES = (DRY_RUN, *cli.MODES)  # dry_run, shadow, assisted, autonomous
BACKENDS = ("inprocess", "foreground", "systemd")
ENTRIES = ("cli", "api")
SCENES = ("ready", "reset_required", "unknown")
# Overrides a request may carry (they take effect and enter the plan).
OVERRIDES = ("episodes", "scenes")
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
UNIT_PREFIX = "levi-aeri-"
PROJECT = Path(__file__).resolve().parents[2]

# Refusal codes and the HTTP status the API gives them (T-CL-11).
STATUS = {
    "E_JOB_INVALID": 422,
    "E_JOB_OUTSIDE_ROOTS": 403,
    "E_OVERRIDE": 422,
    "E_NO_ROBOT_ADAPTER": 501,
    "E_NO_CONTRACT": 422,
    "E_SCENE_PROVIDER_MISSING": 422,
    "E_RUN_EXISTS": 409,
    "E_ROLLOUT_ROOT": 422,
    "E_KEEP_DIR": 422,
    "E_ROBOT_BUSY": 409,
    "E_ROBOT_BUSY_CLIENT": 409,
    "E_PLAN_CHANGED": 412,
    "E_TOKEN_EXPIRED": 412,
    "E_TOKEN_INVALID": 412,
    "E_BACKEND": 422,
    "E_KEY": 503,
    "E_SYSTEMD": 503,
    "E_REQUEST": 422,
    "E_RUNNER_ALIVE": 409,
    "E_RUN_COMPLETED": 409,
}


class LaunchRefused(Exception):
    """A launch (or attach) that may not happen; ``code`` is one of
    ``STATUS``."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail

    @property
    def status(self) -> int:
        return STATUS.get(self.code, 409)


# --- paths --------------------------------------------------------------------------------------


def home(path=None) -> Path:
    """``LEVI_AERI_HOME`` (default ``~/.levi-aeri``), absolute; not created."""
    value = path if path is not None else os.environ.get(HOME_ENV) or DEFAULT_HOME
    return Path(os.path.abspath(os.path.expanduser(str(value))))


def ensure_home(path=None) -> Path:
    """The home folder, created 0700 when missing."""
    found = home(path)
    with contextlib.suppress(FileExistsError):
        found.mkdir(mode=0o700, parents=True)
    if not found.is_dir():
        raise LaunchRefused("E_KEY", f"{found} is not a folder")
    return found


def job_roots() -> list[Path]:
    """``LEVI_AERI_JOB_ROOTS``: where the API may pick job files (real
    paths); empty when unset."""
    value = os.environ.get(JOB_ROOTS_ENV) or ""
    return [Path(os.path.realpath(p)) for p in value.split(os.pathsep) if p.strip()]


def jobs(roots=None, limit: int = 500) -> list[dict]:
    """The job files (``*.yaml``, ``*.yml``) under the job roots, read-only:
    ``{path, root, name, size, mtime_ns}``; bounded by ``limit``."""
    found = []
    for root in job_roots() if roots is None else roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if len(found) >= limit:
                return found
            if path.suffix not in (".yaml", ".yml") or path.is_symlink():
                continue
            with contextlib.suppress(OSError):
                info = path.stat()
                if stat.S_ISREG(info.st_mode):
                    found.append(
                        {
                            "path": str(path),
                            "root": str(root),
                            "name": str(path.relative_to(root)),
                            "size": info.st_size,
                            "mtime_ns": info.st_mtime_ns,
                        }
                    )
    return found


def index_path(run_id: str, path=None) -> Path:
    return home(path) / RUNS / f"{_id(run_id, 'run id')}.json"


def dry_folder(run_id: str, keep_dir=None, path=None) -> Path:
    """Where a launched dry run writes: ``keep_dir``, or
    ``$LEVI_AERI_HOME/dry-runs/<run_id>``."""
    if keep_dir:
        return Path(os.path.abspath(keep_dir))
    return home(path) / DRY_RUNS / _id(run_id, "run id")


def run_dir_of(folder: Path, run_id: str) -> Path:
    return Path(folder) / ".aeri" / "runs" / run_id


def _id(value, what: str) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise LaunchRefused("E_REQUEST", f"not a {what}: {str(value)[:60]!r}")
    return value


# --- the request, the plan, the handle ---------------------------------------------------------


@dataclass(frozen=True)
class LaunchRequest:
    job_path: str
    execution_mode: str | None = None
    overrides: dict = field(default_factory=dict)
    entry: str = "cli"
    principal: dict = field(
        default_factory=lambda: {"kind": "operator", "id": "operator"}
    )
    backend: str = "systemd"
    keep_dir: str | None = None

    def problems(self) -> list[str]:
        out = []
        if not isinstance(self.job_path, str) or not os.path.isabs(self.job_path):
            out.append("job_path is an absolute path")
        if self.execution_mode is not None and self.execution_mode not in MODES:
            out.append(f"execution_mode is one of {', '.join(MODES)}")
        if self.entry not in ENTRIES:
            out.append(f"entry is one of {', '.join(ENTRIES)}")
        if self.backend not in BACKENDS:
            out.append(f"backend is one of {', '.join(BACKENDS)}")
        principal = self.principal
        if (
            not isinstance(principal, dict)
            or set(principal) != {"kind", "id"}
            or principal["kind"] != "operator"
            or not isinstance(principal["id"], str)
            or not ID.fullmatch(principal["id"])
        ):
            out.append("principal is {kind: operator, id: <opaque id>}")
        if not isinstance(self.overrides, dict):
            out.append("overrides is a mapping")
        if self.keep_dir is not None and (
            not isinstance(self.keep_dir, str) or not os.path.isabs(self.keep_dir)
        ):
            out.append("keep_dir is an absolute path")
        return out

    def record(self) -> dict:
        return asdict(self)

    @classmethod
    def from_record(cls, value) -> "LaunchRequest":
        if not isinstance(value, dict) or set(value) != {
            "job_path",
            "execution_mode",
            "overrides",
            "entry",
            "principal",
            "backend",
            "keep_dir",
        }:
            raise LaunchRefused("E_REQUEST", "not a launch request")
        request = cls(**value)
        problems = request.problems()
        if problems:
            raise LaunchRefused("E_REQUEST", "; ".join(problems))
        return request


@dataclass
class LaunchPlan:
    plan_sha256: str | None
    run_id: str | None
    execution_mode: str | None
    reset_mode: str | None
    scene_check: str | None
    roles: list
    adapters: dict
    rollout_root: str | None
    run_dir: str | None
    folders: dict
    episodes: int | None
    isc: dict | None
    motion: bool
    needs_arming: bool
    uses_live: dict
    checks: list
    refusals: list
    launchable: bool
    launch_token: str | None
    job_path: str
    job_rollout_root: str | None = None
    token_expires_wall_ns: int | None = None
    warnings: list = field(default_factory=list)
    ignored: dict = field(default_factory=dict)
    plan: dict | None = None

    def public(self) -> dict:
        """The plan as JSON (the normalised plan included)."""
        return asdict(self)


@dataclass
class RunHandle:
    run_id: str
    run_dir: str
    plan_sha256: str
    backend: str
    unit: str | None
    pid: int | None
    identity: dict | None
    started_wall_ns: int
    state: str = "launching"
    exit_code: int | None = None
    final_state: str | None = None

    def public(self) -> dict:
        return asdict(self)


# --- normalising ---------------------------------------------------------------------------------


def _override_problems(overrides: dict, mode: str) -> list[str]:
    out = []
    unknown = sorted(set(overrides) - set(OVERRIDES))
    if unknown:
        out.append(f"overrides: only {', '.join(OVERRIDES)} ({', '.join(unknown)})")
    if "episodes" in overrides:
        value = overrides["episodes"]
        if type(value) is not int or not 0 <= value <= 100_000:
            out.append("overrides.episodes is a whole number from 0 to 100000")
    if "scenes" in overrides:
        value = overrides["scenes"]
        if mode != DRY_RUN:
            out.append("overrides.scenes scripts the fake scene of a dry run only")
        elif (
            not isinstance(value, list)
            or len(value) > 64
            or any(s not in SCENES for s in value)
        ):
            out.append(f"overrides.scenes is a list of {', '.join(SCENES)}")
    return out


def normalise(request: LaunchRequest) -> dict:
    """The job with the request's mode and overrides applied: ``cli.load_job``'s
    dictionary whose ``plan``, ``plan_sha256`` and ``config`` are the run's,
    plus ``mode`` and ``overrides``. Pure: reads the job file and its
    contract, writes nothing. ``cli.JobError`` / ``LaunchRefused`` say why
    not."""
    problems = request.problems()
    if problems:
        raise LaunchRefused("E_REQUEST", "; ".join(problems))
    job = cli.load_job(request.job_path)
    mode = request.execution_mode or job["mode"]
    overrides = dict(request.overrides)
    problems = _override_problems(overrides, mode)
    if problems:
        raise LaunchRefused("E_OVERRIDE", "; ".join(problems))
    plan = copy.deepcopy(job["plan"])
    plan["mode"] = mode
    if "episodes" in overrides:
        plan["run"]["episodes"] = overrides["episodes"]
    if overrides.get("scenes"):
        plan["dry_run"] = {"scenes": list(overrides["scenes"])}
    digest = plan_digest(plan)
    fields = cli._fields(job["config"])
    fields["plan_sha256"] = digest
    if "episodes" in overrides:
        fields["episodes"] = overrides["episodes"]
    config = cli.RunConfig(**fields)
    return {
        **job,
        "config": config,
        "plan": plan,
        "plan_sha256": digest,
        "mode": mode,
        "overrides": overrides,
    }


def job_fact(path) -> dict:
    """What identifies the job file's bytes now (the token binds them)."""
    path = os.path.realpath(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise LaunchRefused("E_JOB_INVALID", f"{path} is not a regular file")
        digest = hashlib.sha256()
        while True:
            block = os.read(descriptor, 1 << 20)
            if not block:
                break
            digest.update(block)
    finally:
        os.close(descriptor)
    return {
        "path": path,
        "dev": info.st_dev,
        "ino": info.st_ino,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
        "sha256": digest.hexdigest(),
    }


# --- the core key and the token ------------------------------------------------------------------


def core_key(path=None) -> bytes:
    """The core key (32 random bytes, ``core.key`` 0600 in the home folder),
    created once. Never printed, never part of any output."""
    folder = ensure_home(path)
    where = folder / KEY_FILE
    try:
        descriptor = os.open(
            where, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
    except FileExistsError:
        pass
    else:
        try:
            os.write(descriptor, secrets.token_bytes(32))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        fsync_dir(folder)
    try:
        descriptor = os.open(where, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise LaunchRefused("E_KEY", f"the core key cannot be read: {exc.strerror}")
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_uid != os.getuid()
        ):
            raise LaunchRefused(
                "E_KEY", f"{where} must be a regular file of this user, mode 0600"
            )
        key = os.read(descriptor, 64)
    finally:
        os.close(descriptor)
    if len(key) != 32:
        raise LaunchRefused("E_KEY", f"{where} does not hold a 32-byte key")
    return key


def _mac(key: bytes, digest: str, fact: dict, expires_ns: int) -> str:
    body = json.dumps(
        {"plan_sha256": digest, "job": fact, "expires_wall_ns": expires_ns},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def make_token(key: bytes, digest: str, fact: dict, now_ns: int) -> tuple[str, int]:
    expires = now_ns + TOKEN_TTL_NS
    return f"{TOKEN_VERSION}.{expires}.{_mac(key, digest, fact, expires)}", expires


def check_token(key: bytes, token, digest: str, fact: dict, now_ns: int) -> None:
    """``LaunchRefused`` unless ``token`` was made for this plan digest and
    these job file bytes, and has not expired."""
    parts = token.split(".") if isinstance(token, str) else []
    if len(parts) != 3 or parts[0] != TOKEN_VERSION or not parts[1].isdigit():
        raise LaunchRefused("E_TOKEN_INVALID", "not a launch token: plan again")
    expires = int(parts[1])
    if not hmac.compare_digest(parts[2], _mac(key, digest, fact, expires)):
        raise LaunchRefused(
            "E_PLAN_CHANGED",
            "the job file or its plan changed since the plan was made (or the "
            "token belongs to another plan): read the new plan first",
        )
    if now_ns >= expires:
        raise LaunchRefused("E_TOKEN_EXPIRED", "the plan is older than ten minutes")


# --- robot mutual exclusion -------------------------------------------------------------------------


def robot_lock_path(adapter_id: str, path=None) -> Path:
    return home(path) / f"robot-{_id(adapter_id, 'adapter id')}.lock"


def robot_lock_holder(adapter_id: str, path=None) -> dict | None:
    """Who holds the robot's lock (``{"held": True, ...}`` with what the
    holder wrote), or None when it is free. Takes the lock for an instant
    when it is free, creates nothing."""
    where = robot_lock_path(adapter_id, path)
    try:
        descriptor = os.open(where, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return {"held": True, "detail": f"cannot open the lock: {exc.strerror}"}
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                said = json.loads(os.read(descriptor, MAX_RECORD_BYTES) or b"null")
            except ValueError:
                said = None
            return {"held": True, "holder": said if isinstance(said, dict) else None}
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return None
    finally:
        os.close(descriptor)


class RobotLock:
    """The exclusive lock on one robot (``robot-<adapter_id>.lock``), held by
    the runner for its lifetime; the kernel drops it when the runner dies."""

    def __init__(self, adapter_id: str, holder: dict, path=None):
        ensure_home(path)
        self.path = robot_lock_path(adapter_id, path)
        self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._fd)
            self._fd = None
            raise LaunchRefused(
                "E_ROBOT_BUSY", f"another run holds the robot {adapter_id}"
            ) from None
        os.ftruncate(self._fd, 0)
        os.pwrite(self._fd, json.dumps(holder, sort_keys=True).encode(), 0)

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def busy_clients(roots) -> list[dict]:
    """Live evaluation sessions (C2) that are not AERI's own under these
    rollout roots: not ended, not crashed (fresh ``updated_at`` or a live
    process). Read-only: session files only, no port."""
    from levi.live import sessions as c2

    found = []
    for root in roots:
        try:
            read = c2.read_sessions([root])
        except Exception:  # noqa: BLE001, S112 - an unreadable folder is no session
            continue
        for session in read.values():
            if session.raw_state in ("stopped", "finished") or session.crashed:
                continue
            try:
                raw = json.loads(Path(session.path).read_bytes()[:MAX_RECORD_BYTES])
            except (OSError, ValueError):
                raw = {}
            if isinstance(raw, dict) and isinstance(raw.get("aeri"), dict):
                continue  # an AERI run's own file: the robot lock covers those
            found.append(
                {
                    "group": session.group,
                    "task_folder": session.task_folder,
                    "state": session.raw_state,
                    "pid": session.pid,
                    "age_s": None if session.age_s is None else round(session.age_s, 1),
                }
            )
    return found


# --- planning ------------------------------------------------------------------------------------


def _check(checks, code, ok, detail, severity="error"):
    checks.append(
        {
            "code": code,
            "ok": bool(ok),
            "severity": severity,
            "detail": str(detail)[:500],
        }
    )


def _writable_folder(path: Path) -> bool:
    return path.is_dir() and os.access(path, os.W_OK | os.X_OK)


def _creatable(path: Path) -> bool:
    """``path`` is an empty folder, or can be made inside a writable one."""
    if path.exists():
        return path.is_dir() and not any(path.iterdir()) and _writable_folder(path)
    parent = path.parent
    while not parent.exists():
        parent = parent.parent
    return _writable_folder(parent)


def _adapters(mode: str, human: bool, attested: bool) -> dict:
    if mode == DRY_RUN:
        return {
            "robot": "fake",
            "policy_forward": "fake",
            "policy_reset": None if human else "fake",
            "events": "fake",
            "verifier": "fake",
            "scene": "human-scripted" if attested else "fake",
        }
    # No real adapter exists in this version: named so the plan says what a
    # real run would need, never available.
    return {
        "robot": "fr3",
        "policy_forward": None,
        "policy_reset": None,
        "events": None,
        "verifier": None,
        "scene": "human" if attested else None,
    }


def _empty_plan(request, checks) -> LaunchPlan:
    return LaunchPlan(
        plan_sha256=None,
        run_id=None,
        execution_mode=request.execution_mode,
        reset_mode=None,
        scene_check=None,
        roles=[],
        adapters={},
        rollout_root=None,
        run_dir=None,
        folders={},
        episodes=None,
        isc=None,
        motion=False,
        needs_arming=False,
        uses_live={"c5": False, "gate": False},
        checks=checks,
        refusals=[
            c["code"] for c in checks if not c["ok"] and c["severity"] == "error"
        ],
        launchable=False,
        launch_token=None,
        job_path=str(request.job_path),
    )


def plan(request: LaunchRequest, *, now_ns=None, path=None, key=None) -> LaunchPlan:
    """The plan of ``request`` with every check (see the module text)."""
    now_ns = time.time_ns() if now_ns is None else now_ns
    checks: list = []
    problems = request.problems()
    if problems:
        _check(checks, "E_REQUEST", False, "; ".join(problems))
        return _empty_plan(request, checks)
    job_path = os.path.realpath(request.job_path)
    request = LaunchRequest(**{**request.record(), "job_path": job_path})
    if request.entry == "api":
        roots = job_roots()
        inside = any(Path(job_path).is_relative_to(root) for root in roots)
        _check(
            checks,
            "E_JOB_OUTSIDE_ROOTS",
            inside,
            "inside LEVI_AERI_JOB_ROOTS"
            if inside
            else "the API picks job files under LEVI_AERI_JOB_ROOTS only",
        )
    try:
        before = job_fact(job_path)
        job = normalise(request)
        fact = job_fact(job_path)
    except cli.JobError as exc:
        _check(checks, "E_JOB_INVALID", False, exc)
        return _empty_plan(request, checks)
    except LaunchRefused as exc:
        _check(checks, exc.code, False, exc.detail)
        return _empty_plan(request, checks)
    except OSError as exc:
        _check(checks, "E_JOB_INVALID", False, f"{job_path}: {exc.strerror or exc}")
        return _empty_plan(request, checks)
    if fact != before:
        _check(
            checks, "E_PLAN_CHANGED", False, "the job file changed while it was read"
        )
    _check(checks, "E_JOB_INVALID", True, f"job file {job_path}")
    config = job["config"]
    mode = job["mode"]
    dry = mode == DRY_RUN
    human = config.reset_strategy == "human_assisted"
    attested = config.scene_check == "operator_attested"
    _check(
        checks,
        "E_NO_ROBOT_ADAPTER",
        dry,
        "dry run: in-process fakes, no network, no motion authority over anything real"
        if dry
        else f"{mode}: no real robot adapter in this version (only dry_run can launch)",
    )
    # Where the run writes.
    job_root = job["rollout_root"]
    folder = Path(
        dry_folder(config.run_id, request.keep_dir, path) if dry else job_root or ""
    )
    run_dir = run_dir_of(folder, config.run_id) if (dry or job_root) else None
    exists = (run_dir is not None and run_dir.exists()) or index_path(
        config.run_id, path
    ).exists()
    _check(
        checks,
        "E_RUN_EXISTS",
        not exists,
        f"run {config.run_id} already exists (its folder or its index entry): "
        "choose another experiment.name, or attach to it"
        if exists
        else f"run {config.run_id} is new",
    )
    if dry:
        if request.keep_dir:
            _check(
                checks,
                "E_KEEP_DIR",
                _creatable(folder),
                f"{folder}: a new or empty folder in a writable place",
            )
        if job_root is not None and not _writable_folder(Path(job_root)):
            _check(
                checks,
                "E_ROLLOUT_ROOT",
                False,
                f"{job_root}: missing or read-only (a dry run does not write there)",
                severity="warning",
            )
    else:
        writable = job_root is not None and _writable_folder(Path(job_root))
        _check(
            checks,
            "E_ROLLOUT_ROOT",
            writable,
            f"{job_root}: writable"
            if writable
            else f"recording.rollout_root {job_root or 'not set'}: missing or read-only",
        )
    contract = job["contract"]
    isc = None
    if contract is not None:
        described = job["plan"]["contract"]
        isc = {
            "id": described["id"],
            "version": described["version"],
            "status": described["status"],
            "sha256": described["sha256"],
        }
        if described["status"] != "confirmed":
            _check(
                checks,
                "W_CONTRACT_DRAFT",
                False,
                f"Initial State Contract {described['id']}@{described['version']} "
                f"is {described['status']}: not confirmed by the user (HA-23)",
                severity="warning",
            )
    for problem in cli.launch_problems(
        job,
        dry_run=dry,
        scene_provider=cli.dry_run_provider(config) if dry else None,
    ):
        code = (
            "E_SCENE_PROVIDER_MISSING"
            if problem.startswith(cli.SCENE_PROVIDER_MISSING)
            else "E_NO_CONTRACT"
        )
        _check(checks, code, False, problem)
    adapters = _adapters(mode, human, attested)
    if adapters["robot"] != "fake":
        held = robot_lock_holder(adapters["robot"], path)
        _check(
            checks,
            "E_ROBOT_BUSY",
            held is None,
            f"robot {adapters['robot']} is free"
            if held is None
            else f"another run holds robot {adapters['robot']}: {held.get('holder')}",
        )
        clients = busy_clients([job_root] if job_root else [])
        _check(
            checks,
            "E_ROBOT_BUSY_CLIENT",
            not clients,
            "no other evaluation client runs"
            if not clients
            else f"an evaluation client that is not AERI runs: {clients[:3]}",
        )
    else:
        _check(checks, "E_ROBOT_BUSY", True, "fake robot of this run only", "info")
    refusals = [c["code"] for c in checks if not c["ok"] and c["severity"] == "error"]
    token, expires = None, None
    try:
        key = core_key(path) if key is None else key
        token, expires = make_token(key, job["plan_sha256"], fact, now_ns)
    except LaunchRefused as exc:
        _check(checks, exc.code, False, exc.detail)
        refusals.append(exc.code)
    folders = {"forward": config.forward_folder}
    if not human:
        folders["reset"] = config.reset_folder
    return LaunchPlan(
        plan_sha256=job["plan_sha256"],
        run_id=config.run_id,
        execution_mode=mode,
        reset_mode=config.reset_strategy,
        scene_check=config.scene_check,
        roles=["forward"] if human else ["forward", "reset"],
        adapters=adapters,
        rollout_root=str(folder) if (dry or job_root) else None,
        run_dir=str(run_dir) if run_dir is not None else None,
        folders=folders,
        episodes=config.episodes,
        isc=isc,
        motion=not dry,
        needs_arming=mode in ("assisted", "autonomous"),
        uses_live={"c5": not dry, "gate": not dry},
        checks=checks,
        refusals=refusals,
        launchable=not refusals,
        launch_token=token,
        job_path=job_path,
        job_rollout_root=job_root,
        token_expires_wall_ns=expires,
        warnings=list(job["warnings"]),
        ignored=dict(job["ignored"]),
        plan=job["plan"],
    )


# --- records -------------------------------------------------------------------------------------


def read_record(path: Path) -> dict:
    """A small JSON record written by this module (no symlink, bounded)."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"{path} is not a regular file")
        data = os.read(descriptor, MAX_RECORD_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(data) > MAX_RECORD_BYTES:
        raise ValueError(f"{path} is too large")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")  # noqa: TRY004
    return value


def write_record(path: Path, value: dict) -> None:
    write_durable(
        Path(path), (json.dumps(value, sort_keys=True, indent=1) + "\n").encode()
    )
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def _create_exclusive(path: Path, value: dict) -> None:
    """Write ``path`` only if it does not exist (``FileExistsError``)."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    data = (json.dumps(value, sort_keys=True, indent=1) + "\n").encode()
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.link(temporary, path)  # never replaces an existing file
    finally:
        os.unlink(temporary)
    fsync_dir(path.parent)


def update_index(run_id: str, path=None, **changes) -> None:
    """Merge ``changes`` into the run's index entry (best effort: the run
    folder holds the truth)."""
    where = index_path(run_id, path)
    try:
        value = read_record(where)
    except (OSError, ValueError):
        return
    value.update(changes)
    with contextlib.suppress(OSError):
        write_record(where, value)


def runs(path=None) -> list[dict]:
    """Every run in the index, newest first, with whether its runner lives."""
    folder = home(path) / RUNS
    out = []
    if not folder.is_dir():
        return out
    for entry in folder.glob("*.json"):
        if entry.name.startswith(".") or not ID.fullmatch(entry.stem):
            continue
        try:
            value = read_record(entry)
        except (OSError, ValueError):
            continue
        runner = value.get("runner") if isinstance(value.get("runner"), dict) else {}
        alive = runner_alive(runner)
        value["runner_alive"] = alive
        state = None
        run_dir = value.get("run_dir")
        if isinstance(run_dir, str):
            with contextlib.suppress(OSError, ValueError):
                state = json.loads((Path(run_dir) / "state.json").read_text()).get(
                    "state"
                )
        value["state"] = state
        out.append(value)
    out.sort(key=lambda v: v.get("created_wall_ns") or 0, reverse=True)
    return out


def runner_alive(runner) -> bool:
    """The process a runner record names is that runner and still runs."""
    from levi.children import identity

    if not isinstance(runner, dict) or runner.get("state") in ENDED_RUNNER:
        return False
    pid, said = runner.get("pid"), runner.get("identity")
    if not isinstance(pid, int) or pid <= 0 or not isinstance(said, dict):
        return False
    now = identity(pid)
    return bool(
        now
        and now.get("start_ticks") == said.get("start_ticks")
        and now.get("boot") == said.get("boot")
    )


# --- launching ------------------------------------------------------------------------------------


def systemd_argv(
    run_id: str, run_dir: Path, digest: str, *, attach=False, path=None
) -> list:
    """The ``systemd-run`` command of a runner (no Restart: see the module
    text). The unit gets this interpreter, this code and this home."""
    program = shutil.which("systemd-run")
    if program is None:
        raise LaunchRefused("E_SYSTEMD", "systemd-run is not on PATH")
    pythonpath = os.pathsep.join(
        p for p in (str(PROJECT), os.environ.get("PYTHONPATH", "")) if p
    )
    argv = [
        program,
        "--user",
        f"--unit={UNIT_PREFIX}{run_id}",
        "--collect",
        "-p",
        "KillMode=control-group",
        "-p",
        "TimeoutStopSec=120",
        f"--description=LEVI automatic evaluation run {run_id}",
        f"--setenv={HOME_ENV}={home(path)}",
        f"--setenv=PYTHONPATH={pythonpath}",
        sys.executable,
        "-m",
        "levi.automatic.runner",
        "--run-dir",
        str(run_dir),
        "--plan-sha256",
        digest,
    ]
    if attach:
        argv.append("--attach")
    return argv


def start_systemd(argv: list) -> None:
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=SYSTEMD_TIMEOUT_S,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LaunchRefused("E_SYSTEMD", f"systemd-run: {exc}") from None
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip()[:300]
        raise LaunchRefused(
            "E_SYSTEMD", f"systemd-run exited {done.returncode}: {detail}"
        )


def wait_runner(run_dir: Path, wait_s: float) -> dict | None:
    """The runner's record once it wrote one (within ``wait_s``)."""
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        with contextlib.suppress(OSError, ValueError):
            return read_record(Path(run_dir) / RUNNER_RECORD)
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.05)


def launch(
    request: LaunchRequest,
    *,
    plan_sha256: str,
    launch_token: str,
    path=None,
    now_ns=None,
    wait_s: float = 0.0,
    deadline_s: float | None = None,
) -> RunHandle:
    """Launch the plan the caller read (``plan_sha256``, ``launch_token``).
    Plans again and refuses (``LaunchRefused``) unless it is the same plan,
    from the same job file bytes, within the token's ten minutes, and
    launchable. ``inprocess``/``foreground`` run the runner here and return
    when it exits; ``systemd`` returns once ``systemd-run`` started the unit
    (``wait_s``: how long to wait for the runner to name itself)."""
    now_ns = time.time_ns() if now_ns is None else now_ns
    found = plan(request, now_ns=now_ns, path=path)
    mode = found.execution_mode
    if mode != DRY_RUN and mode is not None:
        raise LaunchRefused(
            "E_NO_ROBOT_ADAPTER",
            f"{mode}: no real robot adapter in this version; only dry_run runs",
        )
    if found.plan_sha256 is None:
        first = found.refusals[0] if found.refusals else "E_JOB_INVALID"
        detail = next((c["detail"] for c in found.checks if c["code"] == first), "")
        raise LaunchRefused(first, detail)
    if plan_sha256 != found.plan_sha256:
        raise LaunchRefused(
            "E_PLAN_CHANGED",
            f"the plan is now {found.plan_sha256[:12]}, not {str(plan_sha256)[:12]}: "
            "read the new plan first",
        )
    check_token(
        core_key(path),
        launch_token,
        found.plan_sha256,
        job_fact(found.job_path),
        now_ns,
    )
    if found.refusals:
        code = found.refusals[0]
        detail = next(c["detail"] for c in found.checks if c["code"] == code)
        raise LaunchRefused(code, detail)
    if request.backend == "inprocess" and mode != DRY_RUN:
        raise LaunchRefused("E_BACKEND", "inprocess runs dry runs only")
    run_id, run_dir = found.run_id, Path(found.run_dir)
    request = LaunchRequest(**{**request.record(), "job_path": found.job_path})
    ensure_home(path)
    (home(path) / RUNS).mkdir(mode=0o700, exist_ok=True)
    index = index_path(run_id, path)
    started = time.time_ns()
    unit = f"{UNIT_PREFIX}{run_id}" if request.backend == "systemd" else None
    entry = {
        "schema": INDEX_SCHEMA,
        "run_id": run_id,
        "run_dir": str(run_dir),
        "plan_sha256": found.plan_sha256,
        "execution_mode": mode,
        "reset_mode": found.reset_mode,
        "backend": request.backend,
        "unit": unit,
        "entry": request.entry,
        "principal_id": request.principal["id"],
        "created_wall_ns": started,
        "runner": None,
    }
    try:
        _create_exclusive(index, entry)
    except FileExistsError:
        raise LaunchRefused("E_RUN_EXISTS", f"run {run_id} is in the index") from None
    try:
        try:
            run_dir.parent.mkdir(parents=True, exist_ok=True)
            run_dir.mkdir(mode=0o700)
        except FileExistsError:
            raise LaunchRefused("E_RUN_EXISTS", f"{run_dir} exists") from None
        except OSError as exc:
            raise LaunchRefused(
                "E_KEEP_DIR" if mode == DRY_RUN else "E_ROLLOUT_ROOT",
                f"{run_dir}: {exc.strerror or exc}",
            ) from None
        fsync_dir(run_dir.parent)
        write_record(
            run_dir / LAUNCH_RECORD,
            {
                "schema": LAUNCH_SCHEMA,
                "request": request.record(),
                "plan_sha256": found.plan_sha256,
                "run_id": run_id,
                "unit": unit,
                "launched_wall_ns": started,
            },
        )
    except BaseException:
        # Nothing ran: the run id is free again.
        with contextlib.suppress(OSError):
            index.unlink()
        if run_dir.exists() and not any(
            p.name != LAUNCH_RECORD for p in run_dir.iterdir()
        ):
            shutil.rmtree(run_dir, ignore_errors=True)
        raise
    handle = RunHandle(
        run_id=run_id,
        run_dir=str(run_dir),
        plan_sha256=found.plan_sha256,
        backend=request.backend,
        unit=unit,
        pid=None,
        identity=None,
        started_wall_ns=started,
    )
    return start(handle, attach=False, path=path, wait_s=wait_s, deadline_s=deadline_s)


def start(
    handle: RunHandle, *, attach: bool, path=None, wait_s=0.0, deadline_s=None
) -> RunHandle:
    """Start the runner of a prepared run folder on the handle's backend."""
    run_dir = Path(handle.run_dir)
    if handle.backend == "systemd":
        try:
            start_systemd(
                systemd_argv(
                    handle.run_id, run_dir, handle.plan_sha256, attach=attach, path=path
                )
            )
        except LaunchRefused:
            update_index(handle.run_id, path, runner={"state": "failed_to_start"})
            raise
        handle.state = "started"
        record = wait_runner(run_dir, wait_s) if wait_s else None
        if record:
            handle.pid = record.get("pid")
            handle.identity = record.get("identity")
            handle.state = record.get("state", handle.state)
        return handle
    from . import runner

    result = runner.serve(
        run_dir,
        handle.plan_sha256,
        attach=attach,
        signals=handle.backend == "foreground",
        deadline_s=deadline_s,
        path=path,
    )
    handle.pid = os.getpid()
    handle.state = "exited"
    handle.exit_code = result.exit_code
    handle.final_state = result.state
    handle.identity = result.identity
    return handle


def attach(
    run_dir, *, backend: str = "systemd", path=None, wait_s=0.0, deadline_s=None
) -> RunHandle:
    """Bring a run back whose runner is gone: a new runner restores it from
    its journal (``Orchestrator.restore``: FAULT_LOCKED, recovery_ambiguous,
    nothing replayed). Refused while a runner of the run lives
    (``E_RUNNER_ALIVE``), for a completed run, or when the run folder has no
    launch record."""
    if backend not in BACKENDS:
        raise LaunchRefused("E_BACKEND", f"backend is one of {', '.join(BACKENDS)}")
    run_dir = Path(os.path.abspath(run_dir))
    try:
        record = read_record(run_dir / LAUNCH_RECORD)
        request = LaunchRequest.from_record(record.get("request"))
        digest = record["plan_sha256"]
        run_id = _id(record["run_id"], "run id")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise LaunchRefused(
            "E_REQUEST", f"{run_dir}: no launch record ({exc})"
        ) from None
    if request.execution_mode not in (None, DRY_RUN):
        raise LaunchRefused("E_NO_ROBOT_ADAPTER", "only dry runs run in this version")
    with contextlib.suppress(OSError, ValueError):
        if runner_alive(read_record(run_dir / RUNNER_RECORD)):
            raise LaunchRefused("E_RUNNER_ALIVE", f"a runner of {run_id} still runs")
    from .journal import Journal

    scan = Journal.read(run_dir)
    if not scan.events:
        raise LaunchRefused("E_REQUEST", f"{run_dir} has no journal to attach to")
    if scan.corrupt is None and scan.replay.completed:
        raise LaunchRefused("E_RUN_COMPLETED", f"run {run_id} is completed")
    handle = RunHandle(
        run_id=run_id,
        run_dir=str(run_dir),
        plan_sha256=digest,
        backend=backend,
        unit=f"{UNIT_PREFIX}{run_id}" if backend == "systemd" else None,
        pid=None,
        identity=None,
        started_wall_ns=time.time_ns(),
    )
    update_index(run_id, path, backend=backend, unit=handle.unit)
    return start(handle, attach=True, path=path, wait_s=wait_s, deadline_s=deadline_s)


def audit(event: dict, path=None) -> None:
    """One line of the command audit ``$LEVI_AERI_HOME/control.jsonl``
    (appended under an flock; best effort, never raises)."""
    try:
        folder = ensure_home(path)
        line = (
            json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        descriptor = os.open(
            folder / CONTROL_LOG,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            os.write(descriptor, line)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except (OSError, LaunchRefused, TypeError, ValueError):
        return  # the run's journal holds the authoritative audit
