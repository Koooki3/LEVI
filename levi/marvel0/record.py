"""What the policy-server deploy keeps on disk, in one private folder of the
product workspace (``<workspace state>/marvel0/deploy``):

* ``record.json``: the one deploy this page knows (intent, then what happened);
* ``requests/<request id>.json``: one file per accepted request, so a request
  that is sent again is answered, never acted on twice, also after a restart;
* ``logs/<unit>-<UTC time>.log``: what a server wrote (created here, appended
  to by systemd);
* ``history.jsonl``: one audit line per event.

The folder is created ``0700`` and every file ``0600``. A symlink anywhere in
it, a path systemd could misread, or a place inside source data (a rollout
root, the checkpoints, the openpi checkout) makes it unusable: nothing is
written there. Files are written whole (temporary file, ``fsync``,
``os.replace``) and read as ``levi.setup.probes.read_regular`` reads them.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import time
from datetime import datetime
from pathlib import Path

from levi import paths
from levi.live import jsonio
from levi.setup import probes
from levi.workspace import Workspace

SCHEMA = "levi.marvel0.deploy_record.v1"
REL = ("marvel0", "deploy")
SUBFOLDERS = ("logs", "requests")
RECORD = "record.json"
HISTORY = "history.jsonl"
REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{8,80}")
# systemd expands % in properties and splits on whitespace; the same class
# fr3_policy_swap.UNSAFE_PATH holds (a test keeps the two equal).
UNSAFE = re.compile(r"[$%\s\x00-\x1f\x7f]")
UNIT_PREFIX = "levi-aeri-policy-manual-marvel0-"
UNIT_RE = re.compile(UNIT_PREFIX + r"[a-z0-9][a-z0-9-]{0,31}")
LOG_RE = re.compile(UNIT_RE.pattern + r"-\d{8}T\d{6}Z(?:-\d{1,2})?\.log")
MAX_SLUG = 32
KEEP_LOGS = 10
KEEP_REQUESTS = 200
AUDIT_MAX_BYTES = 256 * 1024
MAX_FILE_BYTES = 1 << 18
DETAIL_CHARS = 300
AUDIT_KEYS = ("action", "phase", "request_id", "model_id", "code", "detail")
_PATH = re.compile(r"(?<![\w/.:-])/(?:[A-Za-z0-9_.~+@%=,-]+/)*[A-Za-z0-9_.~+@%=,-]+")


class StateDirUnusable(Exception):
    """The folder cannot be used safely; the message says why (no path)."""


class RecordCorrupt(Exception):
    """A file of ours exists but cannot be read as what it should be."""


def iso(now) -> str:
    return datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds")


def scrub(text) -> str:
    """``text`` with every absolute path replaced by ``<path>``."""
    return _PATH.sub("<path>", str(text))


# --------------------------------------------------------------- the folder


def _check_dir(path, create):
    """``path`` is a folder of ours (not a link): created ``0700`` when asked."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if not create:
            return
        try:
            os.mkdir(path, 0o700)
        except OSError as exc:
            raise StateDirUnusable(f"cannot create {path.name}") from exc
        os.chmod(path, 0o700)
        return
    except OSError as exc:
        raise StateDirUnusable(f"cannot examine {path.name}") from exc
    if not stat.S_ISDIR(info.st_mode):  # lstat: a link to a folder is not one
        raise StateDirUnusable(f"{path.name} is not a plain folder")
    if info.st_uid != os.getuid():
        raise StateDirUnusable(f"{path.name} belongs to another user")
    if stat.S_IMODE(info.st_mode) != 0o700 and create:
        os.chmod(path, 0o700)


def state_dir(avoid, workspace=None, create=True) -> Path:
    """The deploy folder of ``workspace`` (default: the process-wide one).
    ``avoid`` lists the source-data folders it must not lie in."""
    base = (workspace or Workspace.at(paths.workspace())).state
    target = base / Path(*REL)
    if UNSAFE.search(str(target)):
        raise StateDirUnusable("the folder path has characters systemd misreads")
    for root in avoid:
        if target.is_relative_to(Path(root).expanduser().resolve()):
            raise StateDirUnusable("the folder would lie inside source data")
    if create:
        base.mkdir(parents=True, exist_ok=True)
    for folder in (target.parent, target, *(target / name for name in SUBFOLDERS)):
        _check_dir(folder, create)
    return target


# --------------------------------------------------------------- unit names


def unit_name(model_id) -> str:
    """``levi-aeri-policy-manual-marvel0-<slug>``: a name ``own_units.POLICY_UNIT``
    accepts, so every other part of LEVI sees the unit. An id that does not
    become a plain slug by lower-casing and ``_`` -> ``-``, or is longer than
    ``MAX_SLUG``, is cut and carries a hash of the whole id."""
    text = str(model_id)
    slug = text.replace("_", "-")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", text) or len(slug) > MAX_SLUG:
        low = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
        digest = hashlib.sha256(text.encode()).hexdigest()[:8]
        slug = f"{low[: MAX_SLUG - 9].strip('-') or 'model'}-{digest}"
    return UNIT_PREFIX + slug


# --------------------------------------------------------------- files


def _fsync_dir(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _temporary(final: Path) -> tuple[int, Path]:
    for n in range(100):
        tmp = final.with_name(f".{final.name}.{os.getpid()}.{n}.tmp")
        try:
            fd = os.open(
                tmp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
        except FileExistsError:
            continue
        return fd, tmp
    raise OSError(f"no temporary name for {final.name}")


def _store(final: Path, value, replace: bool) -> None:
    """``value`` as JSON in ``final``, whole or not at all. ``replace=False``
    fails with ``FileExistsError`` when the file is there."""
    if replace:
        try:
            info = os.lstat(final)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(info.st_mode):
                raise OSError(f"{final.name} is not a regular file")
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=1).encode()
    fd, tmp = _temporary(final)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(tmp, final)
        else:
            os.link(tmp, final)  # atomic, and fails if the file is there
            os.unlink(tmp)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _fsync_dir(final.parent)


def _load(path: Path):
    try:
        data, truncated, _ = probes.read_regular(path, MAX_FILE_BYTES)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RecordCorrupt(f"{path.name} cannot be read") from exc
    try:
        value = json.loads(data)
    except ValueError as exc:
        raise RecordCorrupt(f"{path.name} is not JSON") from exc
    if truncated or not isinstance(value, dict):
        raise RecordCorrupt(f"{path.name} is not a JSON object")
    return value


def read_record(directory):
    """The record, or None when there is none. Raises ``RecordCorrupt``."""
    return _load(Path(directory) / RECORD)


def write_record(directory, value) -> None:
    _store(Path(directory) / RECORD, value, replace=True)


@contextlib.contextmanager
def locked(directory):
    """The record's cross-thread, cross-process write lock (``flock``)."""
    fd = os.open(
        Path(directory) / (RECORD + ".lock"),
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing releases the lock


def update_record(directory, **fields) -> dict:
    """``fields`` merged into the record, read and written under the lock."""
    with locked(directory):
        value = read_record(directory) or {}
        value.update(fields)
        write_record(directory, value)
    return value


# --------------------------------------------------------------- requests


def _request_path(directory, request_id) -> Path:
    if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
        raise ValueError("request_id is 8-80 characters of A-Z a-z 0-9 . _ : -")
    return Path(directory) / "requests" / f"{request_id}.json"


def get_request(directory, request_id):
    return _load(_request_path(directory, request_id))


def put_request(directory, request_id, entry, replace=False) -> None:
    """Store the request; ``FileExistsError`` if it is already stored (unless
    ``replace``)."""
    _store(_request_path(directory, request_id), entry, replace)


def drop_request(directory, request_id) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(_request_path(directory, request_id))


def fingerprint(action, body) -> str:
    """What makes two requests the same: the action and the body, without the
    id and the confirmation phrase."""
    shown = {k: v for k, v in body.items() if k not in ("confirm", "request_id")}
    text = json.dumps(
        {"action": action, "body": shown}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(text.encode()).hexdigest()


# --------------------------------------------------------------- logs


def log_path(directory, unit, now) -> Path:
    """Where the log of a deploy of ``unit`` started at ``now`` goes: the first
    free name (a second deploy within the same second gets ``-2``)."""
    if not UNIT_RE.fullmatch(unit):
        raise ValueError("not one of our unit names")
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    for n in range(1, 100):
        suffix = "" if n == 1 else f"-{n}"
        path = Path(directory) / "logs" / f"{unit}-{stamp}{suffix}.log"
        if UNSAFE.search(str(path)):
            raise ValueError("the log path has characters systemd misreads")
        if not os.path.lexists(path):
            return path
    raise FileExistsError("no free log name")


def create_log(directory, unit, now) -> Path:
    """A new empty log for ``unit``, exclusively, ``0600``."""
    path = log_path(directory, unit, now)
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    os.close(fd)
    return path


def _newest(folder: Path, pattern, keep_n, protect):
    """Delete the regular files of ``folder`` whose name matches ``pattern``,
    keeping the newest ``keep_n`` and the names in ``protect``."""
    found = []
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if not pattern.fullmatch(entry.name):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    found.append((info.st_mtime, entry.name))
    except OSError:
        return
    found.sort(reverse=True)
    for _, name in found[keep_n:]:
        if name not in protect:
            with contextlib.suppress(OSError):
                os.unlink(folder / name)


def prune(directory, keep=()) -> None:
    """Keep the newest ``KEEP_LOGS`` logs and ``KEEP_REQUESTS`` request files
    (and the log names in ``keep``). Only regular files of our own names are
    ever deleted."""
    directory = Path(directory)
    _newest(directory / "logs", LOG_RE, KEEP_LOGS, set(keep))
    request = re.compile(REQUEST_ID.pattern + r"\.json")
    _newest(directory / "requests", request, KEEP_REQUESTS, set())


# --------------------------------------------------------------- audit


def audit(directory, event, now=None) -> None:
    """One audit line: the person, through the product, and the event's
    ``AUDIT_KEYS`` with every path scrubbed. Never raises."""
    now = time.time() if now is None else now
    line = {"time": iso(now), "actor": "person", "via": "product"}
    for key in AUDIT_KEYS:
        value = event.get(key)
        if value is not None:
            line[key] = scrub(value)[:DETAIL_CHARS]
    with contextlib.suppress(Exception):
        jsonio.append_line(
            Path(directory) / HISTORY, line, max_bytes=AUDIT_MAX_BYTES, keep=1
        )
