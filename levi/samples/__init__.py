"""Test samples a workspace downloads for itself.

A new workspace (``paths.configure`` creating it) is marked so that the
first time the service runs it draws a DROID raw sample of 500 episodes
(:mod:`levi.samples.droid`; the first draw was 11.6 GiB). ``levi sample draw``
(or ``POST /api/levi/samples/droid_raw``) draws another at any time: a new
``droid_raw_<size>_drawNN`` capture from the workspace's next unused
positions of the order, sharing no episode with its earlier draws.

An unfinished draw resumes where it stopped the next time it is started.
The service, when it starts, resumes by itself a draw left interrupted (its
process stopped, or Ctrl-C) or failed on the network -- at most
:data:`AUTO_ATTEMPTS` automatic attempts per draw; a draw cancelled or failed
for another reason waits for ``levi sample draw``.

``LEVI_DROID_SAMPLE`` = ``off``/``0``/``false``/``no``: LEVI never starts or
resumes a download by itself (tests and CI set it); drawing by hand still
works. ``auto``/``on``/``1``/``true``/``yes`` or unset: it does. Any other
value is ``off``, with a warning.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

from . import droid

log = logging.getLogger("levi")


ENABLED = ("auto", "on", "1", "true", "yes")
DISABLED = ("off", "0", "false", "no")
AUTO_ATTEMPTS = 3
_warned: set[str] = set()


def setting() -> str:
    """``auto`` or ``off``. A setting meant to stop downloads never starts
    one by a typo: an unrecognised value is ``off``, with a warning."""
    raw = os.environ.get("LEVI_DROID_SAMPLE")
    value = (raw or "").strip().lower()
    if not value or value in ENABLED:
        return "auto"
    if value not in DISABLED and value not in _warned:
        _warned.add(value)
        log.warning(
            "LEVI_DROID_SAMPLE=%r is none of %s (on) or %s (off); "
            "the automatic DROID sample stays off",
            raw,
            "/".join(ENABLED),
            "/".join(DISABLED),
        )
    return "off"


def note_new_workspace() -> None:
    """Called once when LEVI creates a workspace: offer it a test sample."""
    droid.update_ledger(
        lambda v: v.update(auto="pending", workspace_created_at=time.time())
    )


def running() -> bool:
    """A process holds the draw lock, or a worker LEVI started is still
    alive (it may not have taken the lock yet)."""
    with droid.run_lock() as free:
        if not free:
            return True
    from .. import children

    return any(r["kind"] == "sample" and r["running"] for r in children.listed())


CLOSED = ("ready", "discarded")


def open_draw(ledger: dict) -> dict | None:
    """The draw not yet finished (at most one), if any."""
    return next((d for d in ledger.get("draws", []) if d["status"] not in CLOSED), None)


def status() -> dict:
    ledger = droid.read_json(droid.ledger_path(), {})
    active = running()
    draws = []
    for d in ledger.get("draws", []):
        row = dict(d)
        if row["status"] in droid.ACTIVE and not active:
            row["status"] = "interrupted"
        progress = droid.read_json(droid.progress_path(d["name"]), None)
        if progress and row["status"] != "ready":
            row["progress"] = progress
        draws.append(row)
    return {
        "droid_raw": {
            "source": droid.SOURCE,
            "setting": setting(),
            "auto": ledger.get("auto"),
            "running": active,
            "next_draw": _next_number(ledger),
            "draws": draws,
            "disk": droid.disk(),
        }
    }


def _next_number(ledger: dict) -> int:
    numbers = [d["draw"] for d in ledger.get("draws", [])]
    return max(numbers, default=0) + 1


def resumable(draw: dict) -> bool:
    """A draw the service resumes by itself when it starts: one a stopped
    process left (an active status, or ``interrupted``) or one that failed on
    the network -- at most AUTO_ATTEMPTS automatic attempts per draw."""
    if draw.get("auto_attempts", 0) >= AUTO_ATTEMPTS:
        return False
    return (
        draw["status"] in droid.ACTIVE
        or draw["status"] == "interrupted"
        or (draw["status"] == "failed" and draw.get("transient") is True)
    )


def plan(
    size: int = droid.SIZE, reason: str = "manual", automatic: bool = False
) -> dict:
    """The draw to run now: the unfinished one, or a new one after the last."""
    if not 1 <= size <= 5000:
        raise ValueError("A draw holds 1-5000 episodes")
    chosen: dict = {}

    def change(v):
        # Sample folders in place move the cursor past their positions (and a
        # published draw the ledger missed is recorded ready) first.
        droid.reconcile(v)
        # Any draw, by hand or automatic, answers a new workspace's offer.
        if v.get("auto") == "pending":
            v["auto"] = "started"
        current = open_draw(v)
        if current:
            current["attempts"] = current.get("attempts", 0) + 1
            if automatic:
                current["auto_attempts"] = current.get("auto_attempts", 0) + 1
            chosen.update(current)
            return
        number = max([_next_number(v)] + [f["draw"] + 1 for f in droid.on_disk()])
        name = droid.dataset_name(size, number)
        while droid.final_root(name).exists() or droid.partial_root(name).exists():
            number += 1
            name = droid.dataset_name(size, number)
        entry = {
            "draw": number,
            "name": name,
            "size": size,
            "first": v.get("cursor", 0),
            "status": "planned",
            "reason": reason,
            "planned_at": time.time(),
            "attempts": 1,
            "auto_attempts": 1 if automatic else 0,
        }
        v["draws"].append(entry)
        chosen.update(entry)

    droid.update_ledger(change)
    return chosen


def start(
    size: int = droid.SIZE,
    workers: int | None = None,
    reason: str = "manual",
    automatic: bool = False,
):
    """Start (or resume) a draw in a worker process LEVI tracks; returns the
    draw. Refused while another process draws."""
    if running():
        raise RuntimeError("A DROID sample is being drawn already")
    draw = plan(size, reason, automatic)
    workers = workers or _workers()
    log_file = droid.log_path(draw["name"])
    log_file.parent.mkdir(parents=True, exist_ok=True)
    from .. import children
    from ..paths import PROJECT

    with log_file.open("a") as handle:
        handle.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} {reason}\n")
        handle.flush()
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "levi.samples",
                "run",
                "--draw",
                str(draw["draw"]),
                "--workers",
                str(workers),
            ],
            stdout=handle,
            stderr=subprocess.STDOUT,
            cwd=PROJECT,
            start_new_session=True,
        )
    children.track(process, "sample", draw["name"])

    def reap():
        code = process.wait()
        children.untrack(process.pid)
        if code == 0:
            # Register the new capture now rather than at the next scan.
            from ..sync import SYNC

            try:
                SYNC.scan()
            except Exception:
                log.exception("scanning after the DROID sample failed")

    threading.Thread(target=reap, daemon=True, name=f"sample-{draw['name']}").start()
    return draw


def cancel(discard: bool = False, grace: float = 10.0) -> dict:
    """Stop the running draw and close it: ``cancelled`` (its partial folder
    stays and a later start resumes it) or, with ``discard``, ``discarded``
    (the folder is deleted; the next draw then takes the same positions).

    A worker LEVI started is terminated. A draw running in a process LEVI did
    not start (a foreground ``levi sample draw``) is interrupted through the
    pid its draw records, once that process's identity (start time, boot,
    executable) matches the record. A draw that cannot be stopped is neither
    marked nor deleted (RuntimeError); nothing changes until the run lock is
    held, i.e. until no process draws.
    """
    from .. import children

    stopped = [
        row for row in children.listed() if row["kind"] == "sample" and row["running"]
    ]
    for row in stopped:
        children.terminate(row)
    labels = [r["label"] for r in stopped]
    other = _stop_untracked(grace)
    if other:
        labels.append(other)
    with droid.run_lock(wait=2.0) as free:
        if not free:
            raise RuntimeError(
                "A DROID sample draw is still running; nothing was cancelled "
                "(levi sample status)"
            )
        ledger = droid.update_ledger(droid.reconcile)
        current = open_draw(ledger)
        discarded = None
        if current:
            if discard:
                partial = droid.partial_root(current["name"])
                shutil.rmtree(partial, ignore_errors=True)
                discarded = str(partial)
            droid.set_draw(
                current["draw"],
                status="discarded" if discard else "cancelled",
                pid=None,
                identity=None,
            )
    return {"stopped": labels, "discarded": discarded}


def _lock_frees(seconds: float) -> bool:
    with droid.run_lock(wait=seconds) as free:
        return free


def _stop_untracked(grace: float) -> str | None:
    """Stop a draw that holds the run lock in a process LEVI did not start:
    SIGINT (it stops its threads and records ``interrupted``), then SIGTERM,
    then SIGKILL, each only while the process is still the one its draw
    recorded. None when nothing holds the lock."""
    from .. import children

    if _lock_frees(1.0):
        return None
    holder = None
    deadline = time.monotonic() + 2.0
    while True:
        # The holder records its pid right after taking the lock.
        ledger = droid.read_json(droid.ledger_path(), {})
        holder = next(
            (
                d
                for d in ledger.get("draws", [])
                if d.get("pid") and d["status"] in droid.ACTIVE
            ),
            None,
        )
        if holder or time.monotonic() > deadline:
            break
        if _lock_frees(0.1):
            return None
    refusal = (
        "A DROID sample is being drawn by a process LEVI did not start and "
        "cannot identify{}; stop it where it runs (Ctrl-C in its terminal), "
        "then cancel again. Nothing was changed."
    )
    if (
        holder is None
        or not holder.get("identity")
        or children.identity(holder["pid"]) != holder["identity"]
    ):
        where = f" (pid {holder['pid']})" if holder else ""
        raise RuntimeError(refusal.format(where))
    pid, who = holder["pid"], holder["identity"]
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        if children.identity(pid) != who:
            break
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            break
        except PermissionError as exc:
            raise RuntimeError(refusal.format(f" (pid {pid}, another user)")) from exc
        if _lock_frees(grace):
            return f"{holder['name']} (pid {pid})"
    if _lock_frees(grace):
        return f"{holder['name']} (pid {pid})"
    raise RuntimeError(f"The draw in pid {pid} did not stop; nothing was changed.")


def on_service_start() -> None:
    """A new workspace's first draw, or a draw to resume (:func:`resumable`).
    Never fails the service."""
    try:
        ledger = droid.read_json(droid.ledger_path(), {})
        if not ledger or setting() == "off" or running():
            return
        ledger = droid.update_ledger(droid.reconcile)
        current = open_draw(ledger)
        if ledger.get("auto") == "pending" and not ledger.get("draws"):
            start(reason="new workspace", automatic=True)
        elif current and resumable(current):
            start(current["size"], reason="resume after restart", automatic=True)
        elif current:
            log.info(
                "DROID sample %s (%s) is not resumed automatically; "
                "`levi sample draw` resumes it",
                current["name"],
                current["status"],
            )
    except Exception:
        log.exception("DROID sample start failed")


def _workers() -> int:
    try:
        return min(8, max(1, int(os.environ.get("LEVI_DROID_SAMPLE_WORKERS", "4"))))
    except ValueError:
        return 4


def cli(argv=None) -> int:
    """``levi sample``: status, draw in the foreground, or cancel."""
    import argparse
    import json

    parser = argparse.ArgumentParser(
        prog="levi sample",
        description=(
            "DROID raw test samples: 500 episodes of the public release per draw, "
            "taken from one seeded order and never overlapping this workspace's "
            "earlier draws."
        ),
    )
    parser.add_argument(
        "action", nargs="?", default="status", choices=["status", "draw", "cancel"]
    )
    parser.add_argument(
        "--size", type=int, default=droid.SIZE, help="episodes per draw (default 500)"
    )
    parser.add_argument(
        "--workers", type=int, default=None, help="parallel downloads, 1-8 (default 4)"
    )
    parser.add_argument(
        "--discard",
        action="store_true",
        help="with cancel: delete the unfinished draw's partial folder (once it stopped)",
    )
    args = parser.parse_args(argv)
    if args.workers is not None and not 1 <= args.workers <= 8:
        parser.error("--workers must be 1-8")
    if not 1 <= args.size <= 5000:
        parser.error("--size must be 1-5000")
    if args.action == "status":
        print(json.dumps(status(), indent=1))
        return 0
    if args.action == "cancel":
        try:
            print(json.dumps(cancel(args.discard), indent=1))
        except RuntimeError as exc:
            print(f"ERROR: {exc}")
            return 1
        return 0
    if running():
        print("A DROID sample is being drawn already (levi sample status)")
        return 1
    draw = plan(args.size, reason="levi sample draw")
    if draw.get("attempts", 1) > 1:
        print(f"Resuming the unfinished draw {draw['name']} (status {draw['status']}).")
    print(
        f"Drawing {draw['name']} ({draw['size']} episodes); Ctrl-C stops, drawing again resumes."
    )
    stop = threading.Event()

    def report():
        last = None
        while not stop.wait(5):
            p = droid.read_json(droid.progress_path(draw["name"]), None)
            if not p:
                continue
            line = (
                f"{p['stage']}: {p['done']}/{p['total']} episodes, "
                f"{droid.gib(p['bytes_done'])} of {droid.gib(p['bytes_total'])}"
                + (
                    f", ~{max(1, round(p['eta_seconds'] / 60))} min left"
                    if p.get("eta_seconds")
                    else ""
                )
                + (f" ({p['current']})" if p.get("current") else "")
            )
            if line != last:
                print(line, flush=True)
                last = line

    threading.Thread(target=report, daemon=True).start()
    try:
        result = droid.run(draw["draw"], args.workers or _workers())
    except KeyboardInterrupt:
        print("Stopped; `levi sample draw` resumes it.")
        return 130
    except Exception as exc:  # noqa: BLE001 - the ledger keeps it too
        print(f"ERROR: {exc}")
        return 1
    finally:
        stop.set()
    print(json.dumps(result, indent=1))
    return 0 if result["status"] == "ready" else 3
