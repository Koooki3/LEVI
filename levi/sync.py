"""Keep the catalog in step with the workspace while LEVI runs.

A background thread in the service scans every ``LEVI_SYNC_INTERVAL``
seconds (default 5; 0 disables; ``POST /api/levi/sync`` scans at once):

- **Registered LeRobot datasets**: when their metadata changes (episodes
  added, removed or rewritten) the cached ``info`` and ``revision`` are
  refreshed; the annotation backend and open viewers notice the new revision.
- **Registered raw captures**: when the capture changes (demos added,
  removed or modified) its browsing view is rebuilt and annotations are
  re-keyed by demo — once the capture has stopped changing for
  ``LEVI_SYNC_SETTLE`` seconds (default 10), so a demo still being recorded
  is not built half-written. A capture whose view failed is retried only
  after it changes again.
- **Removed datasets**: entries whose directory is gone are dropped (a raw
  capture's generated view with them). Annotations, outcome labels, reviews
  and SAM3 revisions stay on disk under the name and re-attach if the
  dataset comes back.
- **New datasets** (``LEVI_SYNC_DISCOVER``: ``all`` default, ``lerobot`` or
  ``off``): LeRobot datasets and raw captures placed anywhere up to three
  levels under the workspace are registered once they are stable. LEVI's own
  folders (``outputs``, ``checkpoints``, ``tmp``), hidden folders and
  in-progress conversions (``.name.partial``) are skipped.

Polling, not inotify: no extra dependency, works on network filesystems,
and a scan is only ``stat`` calls. Several LEVI processes may sync the same
workspace; catalog writes take the cross-process catalog lock and view
builds are de-duplicated through the shared job records.
"""

import os
import shutil
import threading
import time
from collections import deque
from pathlib import Path

from . import catalog, views
from .conversion import registry
from .conversion.engine import fingerprint
from .paths import ROOT
from .revision import dataset_revision

SKIP_TOP = {"outputs", "checkpoints", "tmp"}
MAX_DEPTH = 3


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


class Synchronizer:
    def __init__(self, root: Path | None = None):
        self.root = Path(root or ROOT)
        self.interval = _env_float("LEVI_SYNC_INTERVAL", 5)
        self.settle = _env_float("LEVI_SYNC_SETTLE", 10)
        self.discover = os.environ.get("LEVI_SYNC_DISCOVER", "all")
        self.changes: deque = deque(maxlen=50)
        self.pending: dict[str, tuple[str, float]] = {}
        self.last_scan: float | None = None
        self.last_error: str | None = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._made: list[dict] = []
        self.rejected: dict[str, str] = {}

    # --- lifecycle ------------------------------------------------------------

    def start(self):
        if self.interval <= 0 or self._thread:
            return
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="levi-sync"
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.scan()
            except Exception as exc:  # noqa: BLE001  keep syncing
                self.last_error = f"{type(exc).__name__}: {exc}"
            self._wake.wait(self.interval)
            self._wake.clear()

    def status(self) -> dict:
        return {
            "enabled": self.interval > 0,
            "running": bool(self._thread and self._thread.is_alive()),
            "interval_seconds": self.interval,
            "settle_seconds": self.settle,
            "discover": self.discover,
            "last_scan": self.last_scan,
            "last_error": self.last_error,
            "pending": sorted(self.pending),
            "changes": list(self.changes),
        }

    # --- scanning ---------------------------------------------------------------

    def _note(self, kind: str, name: str, detail: str = ""):
        self._made.append({"kind": kind, "name": name, "detail": detail})
        self.changes.appendleft(
            {"time": time.time(), "kind": kind, "name": name, "detail": detail}
        )

    def _stable(self, key: str, signature: str, now: float) -> bool:
        """True once ``signature`` has stayed the same for ``settle`` s."""
        seen = self.pending.get(key)
        if seen is None or seen[0] != signature:
            self.pending[key] = (signature, now)
            return self.settle <= 0
        return now - seen[1] >= self.settle

    def scan(self) -> list[dict]:
        """One pass; returns the changes it made."""
        with self._lock:
            self._made: list[dict] = []
            now = time.time()
            live = set()
            for name, entry in list(catalog.datasets().items()):
                live.add(name)
                self._check_entry(name, entry, now)
            if self.discover != "off":
                self._discover(now)
            # Forget pending state of things that no longer exist.
            self.pending = {
                k: v
                for k, v in self.pending.items()
                if k in live or (k.startswith("/") and Path(k).exists())
            }
            self.last_scan = time.time()
            self.last_error = None
            return self._made

    def _check_entry(self, name: str, entry: dict, now: float):
        path = Path(entry["path"])
        if not path.exists():
            catalog.remove_entry(name)
            if entry.get("kind") == "raw" and entry.get("view"):
                view = Path(entry["view"])
                # Only ever delete LEVI's own generated view.
                if view.is_relative_to(catalog.STATE / "views"):
                    shutil.rmtree(view, ignore_errors=True)
            self.pending.pop(name, None)
            self._note("removed", name, "directory no longer exists; annotations kept")
            return
        if entry.get("kind") == "raw":
            self._check_raw(name, entry, path, now)
            return
        revision = dataset_revision(path)
        if revision == entry.get("revision"):
            return
        try:
            info = catalog.read(path / "meta/info.json", None)
        except (OSError, ValueError):
            info = None
        if not isinstance(info, dict) or "features" not in info:
            return  # being rewritten; next scan
        previous = (entry.get("info") or {}).get("total_episodes")
        catalog.add_entry(path, {"info": info, "revision": revision})
        if entry.get("revision") is not None:
            self._note(
                "updated",
                name,
                f"episodes {previous} → {info.get('total_episodes')}",
            )

    def _check_raw(self, name: str, entry: dict, path: Path, now: float):
        view = Path(entry["view"]) if entry.get("view") else None
        if view and (view / "meta/info.json").exists():
            revision = dataset_revision(view)
            if revision != entry.get("revision"):
                catalog.add_entry(path, {"revision": revision})
        try:
            current = fingerprint(path)
        except OSError:
            return
        built = None
        if view and views.is_view(view):
            built = catalog.read(view / "meta/levi_view.json", {}).get(
                "source_fingerprint"
            )
        if current == built:
            self.pending.pop(name, None)
            return
        if (
            entry.get("view_status") == "failed"
            and entry.get("view_failed_fingerprint") == current
        ):
            return  # unchanged since it failed; wait for the capture to change
        if not self._stable(name, current, now):
            return
        self.pending.pop(name, None)
        try:
            # Returns at once while a build job for this capture is running
            # (possibly started by another LEVI process); relaunches one that
            # was interrupted.
            updated = views.request(path)
            if updated.get("view_job") != entry.get("view_job"):
                self._note("rebuilding", name, "capture changed; rebuilding its view")
        except ValueError as exc:
            catalog.add_entry(
                path,
                {
                    "view_status": "failed",
                    "view_error": str(exc),
                    "view_failed_fingerprint": current,
                },
            )
            self._note("failed", name, str(exc))

    # --- discovery --------------------------------------------------------------

    def _discover(self, now: float):
        items = catalog.datasets().values()
        known = [Path(i["path"]) for i in items] + [
            Path(i["view"]) for i in items if i.get("view")
        ]
        for candidate, kind in self._candidates(known):
            key = str(candidate)
            try:
                signature = (
                    dataset_revision(candidate)
                    if kind == "lerobot"
                    else fingerprint(candidate)
                )
            except OSError:
                continue
            if self.rejected.get(key) == signature:
                continue  # already reported; unchanged since
            if not self._stable(key, signature, now):
                continue
            self.pending.pop(key, None)
            try:
                if kind == "lerobot":
                    entry = catalog.register(key)
                    catalog.add_entry(
                        candidate,
                        {
                            "registered_by": "sync",
                            "revision": dataset_revision(candidate),
                        },
                    )
                else:
                    entry = views.request(candidate)
                    catalog.add_entry(candidate, {"registered_by": "sync"})
                self._note("added", entry["name"], f"{kind} found at {candidate}")
            except ValueError as exc:
                if kind == "raw":
                    # Listed with its reason; retried when the capture changes.
                    fmt = registry.detect(candidate)
                    entry = catalog.add_entry(
                        candidate,
                        {
                            "kind": "raw",
                            "input_format": fmt.id if fmt else None,
                            "view_status": "failed",
                            "view_error": str(exc),
                            "view_failed_fingerprint": signature,
                            "registered_by": "sync",
                        },
                    )
                    self._note("failed", entry["name"], str(exc))
                else:
                    self.rejected[key] = signature
                    self._note("skipped", candidate.name, str(exc))

    def _candidates(self, known: list[Path]):
        def covered(path: Path) -> bool:
            return any(path == k or path.is_relative_to(k) for k in known)

        stack = [
            (child, 1)
            for child in self._children(self.root)
            if child.name not in SKIP_TOP
        ]
        while stack:
            path, depth = stack.pop()
            if covered(path):
                continue
            if (path / "meta/info.json").is_file():
                yield path, "lerobot"
                continue
            if self.discover == "all" and self._looks_raw(path):
                fmt = registry.detect(path)
                if fmt is not None and fmt.viewable:
                    yield path, "raw"
                    continue
            if depth < MAX_DEPTH:
                stack.extend((child, depth + 1) for child in self._children(path))

    @staticmethod
    def _children(path: Path):
        try:
            return [
                p
                for p in path.iterdir()
                if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")
            ]
        except OSError:
            return []

    @staticmethod
    def _looks_raw(path: Path) -> bool:
        # Demos directly inside, or one task level down.
        return any(path.glob("demo_*")) or any(path.glob("*/demo_*"))


SYNC = Synchronizer()
