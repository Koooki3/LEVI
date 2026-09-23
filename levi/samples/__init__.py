"""Test samples a workspace downloads for itself.

A new workspace (``paths.configure`` creating it) is marked so that the
first time the service runs it draws a DROID raw sample of 500 episodes
(:mod:`levi.samples.droid`, 18-30 GB). ``levi sample draw`` (or ``POST /api/levi/samples/droid_raw``)
draws another 500 at any time: a new ``droid_raw_500_drawNN`` capture that
shares no episode with earlier draws. A draw that was interrupted (service
stopped, network lost) resumes where it stopped the next time it is started,
and the service resumes it on start.

``LEVI_DROID_SAMPLE=off``: LEVI never starts or resumes a download by
itself (tests and CI set it); drawing by hand still works.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time

from . import droid

log = logging.getLogger("levi")


def setting() -> str:
    value = os.environ.get("LEVI_DROID_SAMPLE", "auto").strip().lower()
    return value if value in ("auto", "off") else "auto"


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


def plan(size: int = droid.SIZE, reason: str = "manual") -> dict:
    """The draw to run now: the unfinished one, or a new one after the last."""
    if not 1 <= size <= 5000:
        raise ValueError("A draw holds 1-5000 episodes")
    chosen: dict = {}

    def change(v):
        # Any draw, by hand or automatic, answers a new workspace's offer.
        if v.get("auto") == "pending":
            v["auto"] = "started"
        current = open_draw(v)
        if current:
            current["attempts"] = current.get("attempts", 0) + 1
            chosen.update(current)
            return
        number = _next_number(v)
        name = droid.dataset_name(size, number)
        while droid.final_root(name).exists():
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
        }
        v["draws"].append(entry)
        chosen.update(entry)

    droid.update_ledger(change)
    return chosen


def start(size: int = droid.SIZE, workers: int | None = None, reason: str = "manual"):
    """Start (or resume) a draw in a worker process LEVI tracks; returns the
    draw. Refused while another process draws."""
    if running():
        raise RuntimeError("A DROID sample is being drawn already")
    draw = plan(size, reason)
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


def cancel(discard: bool = False) -> dict:
    """Stop a running draw. Its partial folder stays and a later start
    resumes it -- or, with ``discard``, the folder is deleted and the draw
    closed; the next draw then uses the same positions of the order."""
    from .. import children

    stopped = [
        row for row in children.listed() if row["kind"] == "sample" and row["running"]
    ]
    for row in stopped:
        children.terminate(row)
    ledger = droid.read_json(droid.ledger_path(), {})
    current = open_draw(ledger)
    discarded = None
    if current:
        if discard:
            import shutil

            partial = droid.partial_root(current["name"])
            shutil.rmtree(partial, ignore_errors=True)
            discarded = str(partial)
        droid.set_draw(
            current["draw"], status="discarded" if discard else "cancelled", pid=None
        )
    return {"stopped": [r["label"] for r in stopped], "discarded": discarded}


def on_service_start() -> None:
    """A new workspace's first draw, or a draw the last service left running.
    Never fails the service."""
    try:
        ledger = droid.read_json(droid.ledger_path(), {})
        if not ledger or setting() == "off" or running():
            return
        current = open_draw(ledger)
        if ledger.get("auto") == "pending" and not ledger.get("draws"):
            start(reason="new workspace")
        elif current and current["status"] in droid.ACTIVE:
            start(current["size"], reason="resume after restart")
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
            "reproducible and never overlapping earlier draws."
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
        help="with cancel: delete the unfinished draw's partial folder",
    )
    args = parser.parse_args(argv)
    if args.action == "status":
        print(json.dumps(status(), indent=1))
        return 0
    if args.action == "cancel":
        print(json.dumps(cancel(args.discard), indent=1))
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
                    f", ~{p['eta_seconds'] // 60} min left"
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
