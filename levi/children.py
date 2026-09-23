"""Every long-running process LEVI starts, and how none of them outlives it.

SAM3 workers, conversion jobs and Pilot runtimes run in their own process
group (``start_new_session``), so a signal to the service never reaches them
by itself. Each one is recorded here, with the identity of the process that
owns it, in ``workbench/processes.json``:

- when the service stops normally it terminates the groups it owns
  (:func:`stop_owned`);
- when it was killed instead (SIGKILL, OOM, a crash), the next start -- and
  ``levi stop`` -- terminates the groups whose owner is gone (:func:`reclaim`).

A process is only ever signalled after its identity (start time, boot id and
executable) has been checked against the record, so a reused PID never
receives LEVI's signal.
"""

import contextlib
import fcntl
import json
import os
import signal
import time
from pathlib import Path

GRACE_SECONDS = 5.0


def _path() -> Path:
    from .paths import STATE

    return STATE / "processes.json"


def identity(pid):
    """What makes a PID this process and not a later one: None once it has
    exited (including a zombie awaiting its parent)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if stat[0] == "Z":
            return None
        return {
            "start_ticks": stat[19],
            "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "executable": str(Path(f"/proc/{pid}/exe").resolve(strict=True)),
        }
    except (OSError, IndexError):
        return None


@contextlib.contextmanager
def _locked():
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            rows = json.loads(path.read_text())
        except (OSError, ValueError):
            rows = []
        yield rows
        temporary = path.with_suffix(".pending")
        temporary.write_text(json.dumps(rows, indent=1))
        os.replace(temporary, path)


def track(process, kind: str, label: str = "") -> dict | None:
    """Record a process LEVI just started in its own session."""
    who = identity(process.pid)
    if who is None:
        return None
    row = {
        "pid": process.pid,
        "kind": kind,
        "label": label,
        "identity": who,
        "owner": {"pid": os.getpid(), "identity": identity(os.getpid())},
        "started_at": time.time(),
    }
    with _locked() as rows:
        rows[:] = [r for r in rows if r["pid"] != process.pid] + [row]
    return row


def untrack(pid: int, grace: float = 2.0) -> None:
    """Forget a finished process -- after stopping whatever it started that is
    still in its group (an adapter's CLI, a bridge), which would otherwise
    outlive it unrecorded."""
    with _locked() as rows:
        row = next((r for r in rows if r["pid"] == pid), None)
    if row:
        terminate(row, grace)
    with _locked() as rows:
        rows[:] = [r for r in rows if r["pid"] != pid]


def listed() -> list[dict]:
    """The recorded processes, each marked with whether it still runs and
    whether its owner does."""
    try:
        rows = json.loads(_path().read_text())
    except (OSError, ValueError):
        return []
    return [
        {
            **row,
            "running": identity(row["pid"]) == row["identity"],
            "owner_running": identity(row["owner"]["pid"]) == row["owner"]["identity"],
        }
        for row in rows
    ]


def _members(row) -> list[int]:
    """The live processes of the recorded group: the leader, if it is still
    the recorded process, and whatever it started that is still in the group
    (started no earlier than the leader, on this boot)."""
    leader = row["pid"]
    boot = row["identity"]["boot"]
    try:
        current_boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return []
    if current_boot != boot:
        return []
    # A live process with the leader's PID that is not the recorded leader
    # means the PID was reused; its group is someone else's.
    now = identity(leader)
    if now is not None and now != row["identity"]:
        return []
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if stat[0] == "Z" or int(stat[2]) != leader:
            continue
        if int(stat[19]) < int(row["identity"]["start_ticks"]):
            continue
        pid = int(entry.name)
        if pid == leader and identity(pid) != row["identity"]:
            continue
        found.append(pid)
    return found


def terminate(row, grace: float = GRACE_SECONDS) -> bool:
    """SIGTERM the recorded group, SIGKILL whatever is left after ``grace``
    seconds. True when something was signalled."""
    members = _members(row)
    if not members:
        return False
    for pid in members:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        # Our own exited child stays a zombie until its Popen reaps it; a
        # zombie no longer counts as a member.
        if not _members(row):
            return True
        time.sleep(0.1)
    for pid in _members(row):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)
    return True


def stop_owned(grace: float = GRACE_SECONDS) -> list[dict]:
    """Terminate every recorded group this process started (service stop)."""
    me = os.getpid()
    with _locked() as rows:
        mine = [r for r in rows if r["owner"]["pid"] == me]
    stopped = [r for r in mine if terminate(r, grace)]
    with _locked() as rows:
        rows[:] = [r for r in rows if r["owner"]["pid"] != me]
    return stopped


def reclaim(grace: float = GRACE_SECONDS) -> list[dict]:
    """Terminate every recorded group whose owner is gone -- left behind by a
    service that was killed rather than stopped -- and forget finished ones."""
    with _locked() as rows:
        snapshot = list(rows)
    orphaned = [
        r for r in snapshot if identity(r["owner"]["pid"]) != r["owner"]["identity"]
    ]
    stopped = [r for r in orphaned if terminate(r, grace)]
    gone = {r["pid"] for r in orphaned}
    with _locked() as rows:
        rows[:] = [
            r
            for r in rows
            if r["pid"] not in gone and identity(r["pid"]) == r["identity"]
        ]
    return stopped
