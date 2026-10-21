#!/usr/bin/env python3
"""Stop the AERI terminal D that the marvel0 page hosts, without the page.

    python3 -I scripts/aeri_page_stop.py [--workspace DIR | --record FILE] [--again] [--dry-run]

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
* writes ``stop_sent_epoch`` into the record, so that a page that is restarted
  later does not send a second one. **A second Ctrl+C during D's wrap-up kills the
  ``systemctl`` calls it stops its units with and leaves them running**, so this
  script refuses to send again unless you say ``--again`` (D did not answer within
  a few seconds).

It opens no terminal, no socket and no port, reads no secret, imports nothing of
LEVI, and sends nothing but that one signal. It is safe for anyone to run.
Exit code 0: sent. 1: nothing to stop, or already sent. 2: refused or unreadable
(the reason is printed).

The page's own Stop button is the same action; use that when the page is up. The
run is not recoverable from the page after a hang-up: see
``docs/MARVEL0_AERI_RUN.md``."""

import argparse
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
    return json.loads(raw)


def record_path(args) -> Path:
    if args.record:
        return Path(args.record)
    base = args.workspace or os.environ.get("LEVI_WORKSPACE")
    if base is None:
        base = str(Path(__file__).resolve().parents[1] / ".state")
    return Path(base).expanduser().joinpath(*RECORD_REL)


def mark_sent(path: Path, when: float) -> None:
    """``stop_sent_epoch`` into the record, whole or not at all, under the lock
    the page takes for its own writes."""
    lock = os.open(
        str(path) + ".lock",
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = read_json(path)
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
        print(f"the record cannot be read ({exc.__class__.__name__})", file=sys.stderr)
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
    try:
        mark_sent(path, time.time())
    except (OSError, ValueError) as exc:
        print(
            f"sent, but the record could not be updated ({exc.__class__.__name__})",
            file=sys.stderr,
        )
    print(
        f"SIGINT sent to {where}. It stops the motion and holds the arm; it does not go home."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
