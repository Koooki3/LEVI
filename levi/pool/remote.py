"""Remote targets and pushes of finished pool exports: rsync over SSH.

- A target is a name for ``[user@]host:/path`` — ``host`` may be an alias
  from ``~/.ssh/config`` — kept in ``<workspace>/pool/remotes.json``. No
  password is ever stored or accepted: SSH runs with ``BatchMode=yes`` (key
  or agent authentication only) and ``StrictHostKeyChecking=yes`` (the host
  key must already be in ``known_hosts``).
- Every piece of a target is checked against a strict pattern, nothing may
  start with ``-``, and the command is an argument list (no shell); rsync's
  ``--protect-args`` keeps the remote shell from splitting the path.
- Only a finished pool export (a folder holding ``pool_export.json``, not a
  ``.partial`` one) inside ``LEVI_EXPORT_ROOTS`` or the workspace's export
  folder can be pushed. It lands in ``<target path>/<export name>/``; the
  target path must exist on the remote.
- ``rsync -a --partial``: an interrupted push keeps what arrived, and pushing
  again resumes. Progress comes from ``--info=progress2``.
"""

import json
import os
import re
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import settings

NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
# A host name, an IPv4 address or an ~/.ssh/config alias.
HOST = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$"
USER = r"^[A-Za-z_][A-Za-z0-9._-]{0,31}$"
# Absolute, or relative to the remote home (``~/``); no spaces, quotes,
# shell metacharacters or ``..``.
REMOTE_PATH = r"^(?:/|~/)[A-Za-z0-9._/+@=,-]{0,1000}$"
SPEC = re.compile(r"^(?:(?P<user>[^@:\s]+)@)?(?P<host>[^@:\s]+):(?P<path>\S+)$")



def ssh_program() -> str:
    """``LEVI_POOL_SSH`` (default ``ssh``): the SSH client. Tests point it at
    a stand-in that runs the remote side locally."""
    return os.getenv("LEVI_POOL_SSH") or "ssh"


SSH_OPTIONS = (
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=yes",
    "-o",
    "PasswordAuthentication=no",
    "-o",
    "KbdInteractiveAuthentication=no",
    "-o",
    "ConnectTimeout=20",
)
STAGES = ["Connect", "Transfer"]


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=NAME)
    host: str = Field(pattern=HOST)
    user: str | None = Field(None, pattern=USER)
    path: str = Field(pattern=REMOTE_PATH)
    port: int | None = Field(None, ge=1, le=65535)
    description: str = Field("", max_length=500)

    @field_validator("path")
    @classmethod
    def _path(cls, value):
        if any(part == ".." for part in value.split("/")):
            raise ValueError("The remote path may not contain '..'")
        return value.rstrip("/") or "/"

    def address(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    def display(self) -> str:
        port = f" (port {self.port})" if self.port else ""
        return f"{self.address()}:{self.path}{port}"


def parse_spec(spec: str) -> dict:
    """``[user@]host:/path`` -> ``{user, host, path}``; the parts are then
    checked by :class:`Target`."""
    spec = (spec or "").strip()
    match = SPEC.fullmatch(spec)
    if not match:
        raise ValueError(
            "A target is [user@]host:/path (host may be an ~/.ssh/config alias)"
        )
    return {k: v for k, v in match.groupdict().items() if v is not None}


def target_from(name: str, value: dict) -> Target:
    """A target from an API or CLI body: either ``spec`` or the fields."""
    value = dict(value)
    spec = value.pop("spec", None)
    if spec:
        value = {**parse_spec(spec), **{k: v for k, v in value.items() if v}}
    value["name"] = name
    return Target.model_validate(value)


# ------------------------------------------------------------------ store


def _store() -> Path:
    return settings.pool_dir() / "remotes.json"


def _read() -> dict:
    try:
        return json.loads(_store().read_text()).get("targets", {})
    except (OSError, ValueError):
        return {}


def _write(targets: dict) -> None:
    path = _store()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".remotes.{os.getpid()}.tmp")
    temp.write_text(json.dumps({"targets": targets}, indent=1, ensure_ascii=False))
    os.replace(temp, path)


def _target(value: dict) -> Target:
    return Target.model_validate({k: v for k, v in value.items() if k != "saved_at"})


def listing() -> list[dict]:
    out = []
    for name, value in sorted(_read().items()):
        try:
            target = _target(value)
        except ValueError:
            continue
        out.append({**target.model_dump(), "display": target.display()})
    return out


def get(name: str) -> Target:
    if not re.fullmatch(NAME, name or ""):
        raise ValueError("Invalid target name")
    value = _read().get(name)
    if value is None:
        raise KeyError(name)
    return _target(value)


def save(target: Target) -> dict:
    targets = _read()
    targets[target.name] = {**target.model_dump(), "saved_at": time.time()}
    _write(targets)
    return {**target.model_dump(), "display": target.display()}


def delete(name: str) -> bool:
    if not re.fullmatch(NAME, name or ""):
        raise ValueError("Invalid target name")
    targets = _read()
    existed = targets.pop(name, None) is not None
    _write(targets)
    return existed


# ------------------------------------------------------------------ source


def check_source(path: str | Path) -> Path:
    """A folder that may be pushed: a finished pool export inside the export
    roots (or the workspace's export folder)."""
    given = Path(path).expanduser()
    if given.is_symlink():
        raise PermissionError(f"Refusing to push a symbolic link: {given}")
    source = given.resolve()
    bases = settings.export_roots() + [settings.default_export_parent().resolve()]
    if not settings.inside_any(source, bases):
        raise PermissionError(
            f"{source} is outside LEVI_EXPORT_ROOTS "
            f"({', '.join(map(str, settings.export_roots()))})"
        )
    if not source.is_dir():
        raise ValueError(f"Not a folder: {source}")
    if source.name.startswith(".") or source.name.endswith(".partial"):
        raise ValueError(f"Not a finished export: {source}")
    record = source / "pool_export.json"
    if not record.is_file() or record.is_symlink():
        raise PermissionError(
            f"{source} is not a training-pool export (no pool_export.json); "
            "only pool exports can be pushed"
        )
    return source


# ------------------------------------------------------------------ command


def ssh_command(target: Target) -> list[str]:
    port = ["-p", str(target.port)] if target.port else []
    return [ssh_program(), *SSH_OPTIONS, *port]


def destination(target: Target, source: Path) -> str:
    base = target.path.rstrip("/")
    return f"{target.address()}:{base}/{source.name}/"


def rsync_command(source: Path, target: Target, dry_run: bool = False) -> list[str]:
    """The argument list (never a shell string). ``-e`` is split by rsync on
    whitespace; every word in it is fixed or validated above."""
    shell = ssh_command(target)
    if any(not word or any(c.isspace() for c in word) for word in shell):
        raise ValueError("The SSH program path may not contain spaces")
    return [
        "rsync",
        "-a",
        "--partial",
        "--info=progress2",
        "--no-inc-recursive",
        "--protect-args",
        "-e",
        " ".join(shell),
        *(["--dry-run"] if dry_run else []),
        "--",
        f"{source}/",
        destination(target, source),
    ]


PROGRESS = re.compile(
    r"^\s*(?P<bytes>[\d,]+)\s+(?P<percent>\d{1,3})%\s+(?P<rate>\S+)\s+"
    r"(?P<eta>\d+:\d{2}:\d{2})"
    r"(?:\s+\((?:xfr#(?P<files>\d+),\s*)?(?:to|ir)-chk=(?P<left>\d+)/(?P<total>\d+)\))?"
)


def parse_progress(line: str) -> dict | None:
    """One ``--info=progress2`` update, or None for any other line."""
    match = PROGRESS.match(line)
    if not match:
        return None
    hours, minutes, seconds = (int(x) for x in match["eta"].split(":"))
    out = {
        "bytes": int(match["bytes"].replace(",", "")),
        "percent": min(100, int(match["percent"])),
        "rate": match["rate"],
        "eta_seconds": hours * 3600 + minutes * 60 + seconds,
    }
    if match["total"]:
        out["files_total"] = int(match["total"])
        out["files_left"] = int(match["left"])
    if match["files"]:
        out["files_sent"] = int(match["files"])
    return out


def _updates(stream):
    """Lines of a stream split on both ``\\r`` and ``\\n`` (progress2 rewrites
    its line with ``\\r``)."""
    buffer = b""
    while True:
        chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096)
        if not chunk:
            break
        buffer += chunk
        parts = re.split(rb"[\r\n]", buffer)
        buffer = parts.pop()
        for part in parts:
            if part.strip():
                yield part.decode("utf-8", "replace")
    if buffer.strip():
        yield buffer.decode("utf-8", "replace")


EXIT_HINTS = {
    255: "SSH failed: check that key login works without a prompt "
    "(`ssh -o BatchMode=yes <host> true`) and that the host key is in known_hosts",
    12: "rsync protocol error (is rsync installed on the remote?)",
    3: "The target path does not exist on the remote or is not writable",
    11: "File I/O error on the remote (disk full or not writable?)",
    20: "Cancelled",
    23: "Some files could not be transferred",
    30: "Timed out",
}


def push(
    source: Path,
    target: Target,
    dry_run: bool = False,
    progress_path: Path | None = None,
    echo=None,
) -> dict:
    """Run rsync in the foreground of this process (a pool worker or the
    CLI); rsync stays in this process group, so cancelling the worker's group
    stops it too."""
    from ..conversion.progress import Progress

    source = check_source(source)
    command = rsync_command(source, target, dry_run)
    progress = Progress(progress_path, STAGES)
    progress.stage("Connect", 0)
    started = time.time()
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
    )
    errors: deque[str] = deque(maxlen=40)

    def drain():
        for line in _updates(proc.stderr):
            errors.append(line)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    last: dict = {}
    transferring = False
    try:
        for line in _updates(proc.stdout):
            update = parse_progress(line)
            if update is None:
                continue
            if not transferring:
                progress.stage("Transfer", 100)
                transferring = True
            last = update
            progress.state.update(
                done=update["percent"],
                current=f"{update['bytes']:,} B · {update['rate']}",
                bytes=update["bytes"],
                rate=update["rate"],
                rsync_eta_seconds=update["eta_seconds"],
                files_total=update.get("files_total"),
                files_left=update.get("files_left"),
            )
            progress._write()
            if echo:
                echo(update)
        code = proc.wait()
    except BaseException:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
        raise
    reader.join(5)
    result = {
        "ok": code == 0,
        "exit_code": code,
        "source": str(source),
        "target": target.name,
        "destination": destination(target, source),
        "dry_run": dry_run,
        "bytes": last.get("bytes", 0),
        "seconds": round(time.time() - started, 1),
        "command": command,
    }
    if code != 0:
        tail = "\n".join(errors)[-2000:]
        hint = EXIT_HINTS.get(code, f"rsync exited with {code}")
        result["error"] = f"{hint}\n{tail}".strip()
        progress.finish("failed")
    else:
        progress.finish()
    return result
