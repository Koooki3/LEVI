"""Fixed synthetic worker, isolated from the web server and all network access.

No plugin, model URL, robot adapter or action code is accepted as input. The
socket guard is deliberately process-local: installing it in FastAPI would
break unrelated requests. Outputs are software evidence, never motion permits.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import signal
import socket
import stat
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def fail_document(current: dict, reason: str) -> dict:
    current.update(status="failed", stage=reason, updated_at=utc_now())
    current["handoff"].update(eligible=False, physical_allowed=False, reason=reason)
    current["stage_seq"] += 1
    current["events"].append(
        {
            "seq": current["stage_seq"],
            "stage": reason,
            "status": "failed",
            "reason": reason,
            "elapsed_ms": current["elapsed_ms"],
            "evidence": {},
        }
    )
    return current


def read_json(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("not a regular file")
        value = json.load(stream)
    if not isinstance(value, dict):
        raise TypeError("expected an object")
    return value


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, allow_nan=False, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def disable_network() -> None:
    """Deny socket creation, name lookup and existing-socket connections."""

    def denied(*_args, **_kwargs):
        raise PermissionError("offline reset harness forbids network access")

    def audit(event, _args):
        if event.startswith("socket."):
            denied()

    sys.addaudithook(audit)
    socket.socket = denied
    socket.create_connection = denied
    socket.getaddrinfo = denied


def execute(run_dir: Path, *, timeout_s: float = 20.0) -> int:
    from .offline_harness import run_scenario

    if run_dir.is_symlink() or not run_dir.is_dir():
        raise ValueError("invalid run directory")
    spec = read_json(run_dir / "request.json")
    if set(spec) != {"request_id", "scenario_id", "seed", "run_id"}:
        raise ValueError("invalid request keys")
    if spec["run_id"] != run_dir.name:
        raise ValueError("run identity mismatch")
    start = time.monotonic()
    last_snapshot = None
    deadline_reached = False

    def stopped():
        nonlocal deadline_reached
        if (run_dir / "stop.json").exists():
            return True
        deadline_reached = time.monotonic() - start > timeout_s
        return deadline_reached

    def persist(snapshot):
        nonlocal last_snapshot
        content = {key: value for key, value in snapshot.items() if key != "updated_at"}
        if deadline_reached and content["status"] == "stopped":
            content = copy.deepcopy(content)
            content.update(status="failed", stage="worker_deadline")
            content["handoff"].update(eligible=False, reason="worker_deadline")
            content["events"][-1].update(
                stage="worker_deadline", status="failed", reason="worker_deadline"
            )
        # Core emits its terminal snapshot and also returns it. Persisting both
        # would change updated_at after clients had already observed completion.
        if content == last_snapshot:
            return
        atomic_json(run_dir / "status.json", {**content, "updated_at": utc_now()})
        last_snapshot = content

    try:
        result = run_scenario(
            spec["scenario_id"],
            spec["seed"],
            spec["run_id"],
            on_event=persist,
            should_stop=stopped,
        )
        persist(result)
        return 1 if deadline_reached else 0
    except Exception:  # noqa: BLE001 - process boundary records any execution failure
        # Do not leak paths, environment or arbitrary exception messages to UI.
        current = read_json(run_dir / "status.json")
        atomic_json(run_dir / "status.json", fail_document(current, "worker_failed"))
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--lease-fd", type=int, required=True)
    args = parser.parse_args()
    # Manager passes an already-held flock, so even a second HTTP process sees
    # this run as active during interpreter startup. Never trust a saved PID.
    if not stat.S_ISREG(os.fstat(args.lease_fd).st_mode):
        raise ValueError("invalid lease")
    os.nice(19)
    available = sorted(os.sched_getaffinity(0))
    os.sched_setaffinity(0, set(available[-4:]))
    # This bound survives the HTTP parent's exit; a stuck worker cannot hold
    # the single-run lease forever. Its missing terminal evidence fails closed.
    signal.alarm(25)
    disable_network()
    try:
        return execute(args.run_dir)
    finally:
        os.close(args.lease_fd)


if __name__ == "__main__":
    raise SystemExit(main())
