#!/usr/bin/env python3
"""Stop the AERI terminal D that the marvel0 page hosts, without the page.

    python3 -I scripts/aeri_page_stop.py [--workspace DIR | --record FILE] [--again] [--dry-run]
    python3 -I scripts/aeri_page_stop.py [--workspace DIR | --record FILE] --scope [--dry-run]

Use it when the page is not there to press Stop: the browser is gone, the page
at :7860 does not answer, or the core is stuck while terminal D still lives. It
is the Ctrl+C of the hosted run, from outside: **one SIGINT to terminal D**, the
same signal a Ctrl+C in a terminal gives it. D then stops sending motion
commands, holds the arm where it is and shuts its services down (VLM and policy
units, the GPU lock); it takes up to about two minutes. **It does not go home.**
It is not an emergency stop: the physical emergency stop and Franka Desk are.

What it does, and nothing else:

* reads the page's private record (``<workspace>/outputs/LEVI/workbench/marvel0/
  aeri/record.json``, written by the page when it started D) and D's heartbeat
  file (``runner.json``, which D writes about every two seconds);
* checks that the pid in the heartbeat is still **the same process** (start time
  and boot id, as the page recorded them), and sends it SIGINT; with no usable
  heartbeat it sends SIGINT to the launcher's process group, after the same
  check of the launcher (the launcher ignores SIGINT; D is in its group);
* writes ``stop_sent_epoch`` into the record (before the signal, see below, and
  under the record's lock, where it also looks once more at whether a Stop was
  sent), so that the page, running or restarted later, knows a Stop was sent and
  offers none of its own. **A second Ctrl+C during D's wrap-up kills the
  ``systemctl`` calls it stops its units with and leaves them running**, so this
  script refuses to send again unless you say ``--again`` (D did not answer within
  a few seconds).

**When the record cannot be read** (it is corrupt, or the disk fails) the script
says so and stops, because it then has no name, no heartbeat and no way to tell
whether a Stop was sent before. ``--scope`` is the way on, for that case only (it
refuses when the record can be read): it looks in the user's own cgroup tree for
the scope the page started D in (``levi-aeri-termd-marvel0-<run>.scope``) and
takes the one process in it whose command line is ``aeri.py execute`` (SIGINT to
that pid, as the heartbeat route does), or else the launcher (``hostchild.py``)
that leads its process group (SIGINT to the group), and sends that one signal. It
writes nothing: **the page will not know of this Stop**, and nothing guards
against a second Ctrl+C, so make sure none went out before. It reads only the
cgroup files of your own scope and ``/proc``; it asks no service and starts
nothing.

It opens no terminal, no socket and no port, reads no secret, imports nothing of
LEVI, and sends nothing but that one signal. It is safe for anyone to run.
Exit code 0: sent. 1: nothing to stop, or already sent. 2: refused or unreadable
(the reason is printed).

The page's own Stop button is the same action; use that when the page is up. The
run is not recoverable from the page after a hang-up: see
``docs/MARVEL0_AERI_RUN.md``."""

import argparse
import errno
import fcntl
import json
import os
import re
import signal
import stat
import sys
import time
from pathlib import Path

RECORD_REL = ("outputs", "LEVI", "workbench", "marvel0", "aeri", "record.json")
NAME_RE = re.compile(r"aeri-[0-9]{8}-(?:ext|loc)-r[0-9]{2}")
RUNNER_RE = re.compile(
    r".*/\.aeri/runs/(aeri-[0-9]{8}-(?:ext|loc)-r[0-9]{2})/runner\.json"
)
EXITED = frozenset("ZXx")
# How long the record's lock is tried for: a Ctrl+C does not wait for it longer.
LOCK_WAIT_S = 2.0
# The scope the page starts D in (``aeri_run.UNIT_PREFIX`` + the run id), looked
# for below the user manager's own cgroup only.
SCOPE_RE = re.compile(r"levi-aeri-termd-marvel0-[0-9]{8}T[0-9]{6}-[0-9a-f]{6}\.scope")
CGROUP_ROOT = "/sys/fs/cgroup"


def identity(pid):
    """``{"start_ticks", "boot"}`` of a live process of ours, else None."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if stat[0] in EXITED:
            return None
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return {"start_ticks": stat[19], "boot": boot}
    except (OSError, IndexError):
        return None


def same(recorded, found) -> bool:
    if not isinstance(recorded, dict) or found is None:
        return False
    return all(str(recorded.get(k)) == str(found[k]) for k in ("start_ticks", "boot"))


def read_json(path: Path, limit: int = 1 << 18):
    """A regular file of ours, never through a link."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise OSError("not a regular file of yours")
        raw = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(raw) > limit:
        raise OSError("too large")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("not a JSON object")  # noqa: TRY004 - callers catch ValueError
    return value


def record_path(args) -> Path:
    if args.record:
        return Path(args.record)
    base = args.workspace or os.environ.get("LEVI_WORKSPACE")
    if base is None:
        base = str(Path(__file__).resolve().parents[1] / ".state")
    return Path(base).expanduser().joinpath(*RECORD_REL)


class AlreadySent(Exception):
    """The record already says a Stop was sent (``args[0]``: when)."""


def mark_sent(path: Path, when: float, refuse_if_sent: bool = False) -> None:
    """``stop_sent_epoch`` into the record, whole or not at all, under the lock the
    page takes for its own writes. With ``refuse_if_sent`` the record is looked at
    again **inside** the lock and ``AlreadySent`` is raised if a Stop is in it: the
    page's Stop writes under the same lock, so one of the two finds the other."""
    lock = os.open(
        str(path) + ".lock",
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise OSError(errno.EAGAIN, "the record's lock is held") from exc
                time.sleep(0.02)
        data = read_json(path)
        if refuse_if_sent and data.get("stop_sent_epoch"):
            raise AlreadySent(float(data["stop_sent_epoch"]))
        data["stop_sent_epoch"] = when
        tmp = path.with_name(f".{path.name}.stop.{os.getpid()}.tmp")
        fd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, indent=1))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        os.close(lock)


# --- the record cannot be read: terminal D by the scope the page started it in ----------------


def scope_dirs(root) -> list:
    """The cgroup folders of D's scopes below this user's own manager (nothing
    outside it, no link followed)."""
    uid = os.getuid()
    base = Path(root) / "user.slice" / f"user-{uid}.slice" / f"user@{uid}.service"
    found = []
    for folder, names, _files in os.walk(base):
        for name in list(names):
            if SCOPE_RE.fullmatch(name):
                found.append(Path(folder) / name)
                names.remove(name)
    return found


def scope_pids(scope: Path) -> list:
    try:
        words = (scope / "cgroup.procs").read_text().split()
    except OSError:
        return []
    return [int(w) for w in words if w.isdigit()]


def ours(pid: int) -> bool:
    try:
        return os.stat(f"/proc/{pid}").st_uid == os.getuid()
    except OSError:
        return False


def words_of(pid: int):
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return [w.decode("utf-8", "replace") for w in raw.split(b"\0") if w]


def is_terminal_d(words) -> bool:
    """``python ... scripts/aeri.py execute ...`` (not ``uv``, whose command line
    holds the same words)."""
    if not words or not os.path.basename(words[0]).startswith("python"):
        return False
    for n, word in enumerate(words):
        if (word == "aeri.py" or word.endswith("/aeri.py")) and "execute" in words[n:]:
            return True
    return False


def is_launcher(words) -> bool:
    return bool(words) and any(w.endswith("hostchild.py") for w in words)


def from_scope(args, why: str) -> int:
    """The signal to terminal D found by its scope. 0 sent, 1 not found, 2 refused."""
    scopes = scope_dirs(args.cgroup_root)
    if not scopes:
        print("no scope of a hosted terminal D was found: it is not running here")
        return 1
    if len(scopes) > 1:
        print(
            "more than one scope of a hosted terminal D was found; nothing was sent",
            file=sys.stderr,
        )
        return 2
    scope = scopes[0]
    pids = [p for p in scope_pids(scope) if ours(p)]
    runs = [p for p in pids if is_terminal_d(words_of(p))]
    if len(runs) > 1:
        print(
            "more than one process of the scope looks like terminal D; nothing was sent",
            file=sys.stderr,
        )
        return 2
    if runs:
        target, how = runs[0], "pid"
    else:
        leaders = []
        for pid in pids:
            try:
                if is_launcher(words_of(pid)) and os.getpgid(pid) == pid:
                    leaders.append(pid)
            except OSError:
                continue
        if len(leaders) != 1:
            print("terminal D is not running (nothing of it in its scope)")
            return 1
        target, how = leaders[0], "group"
    where = (
        f"terminal D (pid {target})"
        if how == "pid"
        else f"the launcher's process group {target} (terminal D is in it)"
    )
    if args.dry_run:
        print(f"would send SIGINT to {where}, found in {scope.name}")
        return 0
    try:
        if how == "pid":
            os.kill(target, signal.SIGINT)
        else:
            os.killpg(target, signal.SIGINT)
    except OSError as exc:
        print(
            f"the signal could not be sent ({exc.__class__.__name__})", file=sys.stderr
        )
        return 2
    print(
        f"SIGINT sent to {where}, found by its systemd scope {scope.name} "
        f"(the record could not be read: {why}). The page will not know of this Stop, "
        "and nothing guards against a second one. It stops the motion and holds the "
        "arm; it does not go home."
    )
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--workspace",
        help="the LEVI workspace of the product (default $LEVI_WORKSPACE, else this checkout's .state)",
    )
    parser.add_argument("--record", help="the page's record.json itself")
    parser.add_argument(
        "--again",
        action="store_true",
        help="send a second SIGINT (D did not answer the first)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="say what would be signalled, send nothing",
    )
    parser.add_argument(
        "--scope",
        action="store_true",
        help="the record cannot be read: find terminal D by its systemd scope instead",
    )
    parser.add_argument(
        "--cgroup-root",
        default=CGROUP_ROOT,
        help=argparse.SUPPRESS,  # (tests)
    )
    args = parser.parse_args(argv)
    path = record_path(args)
    try:
        record = read_json(path)
    except FileNotFoundError:
        print(
            f"no record at {path.name}: the page has not hosted a run here",
            file=sys.stderr,
        )
        return 1
    except (OSError, ValueError) as exc:
        if args.scope:
            return from_scope(args, exc.__class__.__name__)
        print(
            f"the record cannot be read ({exc.__class__.__name__}). If terminal D "
            "still lives, --scope finds it by its systemd scope instead; that route "
            "cannot tell whether a Stop was sent before, so make sure none was.",
            file=sys.stderr,
        )
        return 2
    if args.scope:
        print(
            "--scope is for a record that cannot be read; this record can be read: "
            "run without it",
            file=sys.stderr,
        )
        return 2
    name = record.get("name")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        print("the record names no AERI run", file=sys.stderr)
        return 2
    if record.get("phase") not in ("intent", "started"):
        print(f"run {name} is not running (phase {record.get('phase')})")
        return 1
    if record.get("stop_sent_epoch") and not args.again:
        age = time.time() - float(record["stop_sent_epoch"])
        print(
            f"a SIGINT was already sent to run {name} {age:.0f} s ago; a second one can leave its "
            "units running (use --again only if terminal D did not react)",
            file=sys.stderr,
        )
        return 1
    leader = record.get("leader") or {}
    runner_file = record.get("runner_json")
    target = None
    how = None
    if isinstance(runner_file, str):
        match = RUNNER_RE.fullmatch(runner_file)
        if match and match.group(1) == name:
            try:
                runner = read_json(Path(runner_file))
                pid = runner.get("pid")
                if (
                    isinstance(pid, int)
                    and not isinstance(pid, bool)
                    and same(runner.get("identity"), identity(pid))
                ):
                    target, how = pid, "pid"
            except (OSError, ValueError):
                pass
    if target is None:
        pid = leader.get("pid")
        if (
            isinstance(pid, int)
            and not isinstance(pid, bool)
            and same(leader.get("identity"), identity(pid))
        ):
            target, how = pid, "group"
    if target is None:
        print(
            f"terminal D of run {name} is not running (no process of the recorded identity)"
        )
        return 1
    where = (
        f"terminal D (pid {target})"
        if how == "pid"
        else f"the launcher's process group {target} (terminal D is in it)"
    )
    if args.dry_run:
        print(f"would send SIGINT to {where}")
        return 0
    # The record first, the signal after: a page that looks at the record between
    # the two must find "a Stop was sent" and not offer its own, and a signal that
    # then fails puts the old value back. (A script killed between the two leaves
    # a record that says "sent": the page shows it unconfirmed after a few seconds
    # and offers its one retry.) A record that cannot be written never keeps the
    # signal from going out.
    previous = record.get("stop_sent_epoch")
    noted = True
    try:
        mark_sent(path, time.time(), refuse_if_sent=not args.again)
    except AlreadySent as exc:
        print(
            f"a SIGINT was already sent to run {name} {time.time() - exc.args[0]:.0f} s "
            "ago (the page's own Stop, just now); a second one can leave its units "
            "running (use --again only if terminal D did not react)",
            file=sys.stderr,
        )
        return 1
    except (OSError, ValueError) as exc:
        noted = False
        print(
            f"the record could not be updated ({exc.__class__.__name__}): "
            "the page will not know of this Stop",
            file=sys.stderr,
        )
    try:
        if how == "pid":
            os.kill(target, signal.SIGINT)
        else:
            os.killpg(target, signal.SIGINT)
    except OSError as exc:
        if noted:
            try:
                mark_sent(path, previous)
            except (OSError, ValueError):
                pass
        print(
            f"the signal could not be sent ({exc.__class__.__name__})", file=sys.stderr
        )
        return 2
    print(
        f"SIGINT sent to {where}. It stops the motion and holds the arm; it does not go home."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
