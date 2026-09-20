"""Persistent dataset registration and review manifests, with atomic writes.

Registered datasets are keyed by a human-readable **catalog name** (the id
used in URLs is ``local/<name>``): the folder name made URL-safe, or its
parent's name for the legacy generic ``…/dataset`` layout, with a timestamp
appended only on a real clash. Registering an already-registered path returns
its existing entry. Ids from before names replaced hashes live on as
aliases (``dataset_aliases.json``) so old links still resolve.
"""

import contextlib
import fcntl
import json
import os
import threading
import time
from pathlib import Path

from .naming import catalog_name, unique_name
from .paths import STATE, inside

LOCK = threading.RLock()
_DEPTH = threading.local()
# Public LeRobot datasets, checked reachable on 2026-09-20. A demo that has
# been removed upstream answers 401 to an anonymous fetch, which reads as a
# LEVI permission error, so these are worth re-checking when they change.
DEMOS = ["lerobot/svla_so101_pickplace", "lerobot/aloha_static_coffee"]
GENERIC_FOLDER_NAMES = {"dataset", "data", "output", "outputs"}


def read(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def atomic(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + str(time.time_ns()) + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
    os.replace(temp, path)


@contextlib.contextmanager
def locked():
    """Exclusive catalog access across threads *and* LEVI processes sharing
    the workspace (the background sync writes it too). Re-entrant within a
    thread: only the outermost level takes the file lock."""
    with LOCK:
        depth = getattr(_DEPTH, "value", 0)
        if depth:
            _DEPTH.value = depth + 1
            try:
                yield
            finally:
                _DEPTH.value = depth
            return
        STATE.mkdir(parents=True, exist_ok=True)
        with (STATE / ".catalog.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            _DEPTH.value = 1
            try:
                yield
            finally:
                _DEPTH.value = 0
                fcntl.flock(handle, fcntl.LOCK_UN)


def datasets():
    return read(STATE / "datasets.json", {})


def aliases():
    return read(STATE / "dataset_aliases.json", {})


def _base_name(root: Path) -> str:
    name = root.name
    if name.lower() in GENERIC_FOLDER_NAMES and root.parent.name:
        name = root.parent.name
    return catalog_name(name)


def _entry_for_path(items: dict, root: Path):
    text = str(root)
    for item in items.values():
        if item.get("path") == text or item.get("view") == text:
            return item
    return None


def add_entry(root: Path, fields: dict) -> dict:
    """Insert or refresh the catalog entry for ``root`` (idempotent by path)."""
    with locked():
        items = datasets()
        existing = _entry_for_path(items, root)
        if existing:
            existing.update(fields)
            item = existing
        else:
            name = unique_name(_base_name(root), items.keys())
            item = {"id": "local/" + name, "name": name, "path": str(root), **fields}
        items[item["name"]] = item
        atomic(STATE / "datasets.json", items)
    return item


def remove_entry(name: str) -> dict | None:
    """Drop a catalog entry. Its annotations, labels, reviews and SAM3
    revisions stay on disk under the name, and re-attach if a dataset with
    that name is registered again."""
    with locked():
        items = datasets()
        item = items.pop(name, None)
        if item is not None:
            atomic(STATE / "datasets.json", items)
    return item


def register(path: str):
    root = inside(path)
    info = read(inside("meta/info.json", root), None)
    if not info or "features" not in info or "fps" not in info:
        raise ValueError("A LeRobot dataset needs meta/info.json with features and fps")
    return add_entry(root, {"kind": "lerobot", "info": info})


def resolve_name(repo: str) -> str | None:
    """Catalog name for a ``local/…`` id, following legacy hash aliases."""
    if not repo.startswith("local/"):
        return None
    key = repo.split("/", 1)[1]
    items = datasets()
    if key in items:
        return key
    target = aliases().get(key)
    if target and target in items:
        return target
    raise ValueError("Local dataset is not registered")


def canonical_id(repo: str) -> str:
    name = resolve_name(repo)
    return repo if name is None else "local/" + name


def local_root(repo: str):
    name = resolve_name(repo)
    if name is None:
        return None
    item = datasets()[name]
    # A raw capture is browsed through its generated view (see levi/views.py).
    if item.get("kind") == "raw":
        if not item.get("view"):
            raise ValueError(
                "The browsing view of this raw capture is not ready yet "
                f"({item.get('view_status', 'missing')})"
            )
        return inside(item["view"])
    return inside(item["path"])


def name_for_path(path) -> str | None:
    if not path:
        return None
    item = _entry_for_path(datasets(), Path(path))
    return item["name"] if item else None


def display_name(repo_id: str | None, local_path: str | None) -> str:
    """The on-disk key for a dataset's sidecars, reviews, diagnostics and
    exports: its catalog name when registered (unique by construction),
    otherwise its folder name (local) or ``org__name`` (Hub)."""
    if local_path:
        return name_for_path(local_path) or _base_name(Path(local_path))
    if repo_id:
        name = resolve_name(repo_id) if repo_id.startswith("local/") else None
        return name or catalog_name(repo_id.replace("/", "__"))
    return "dataset"


def review_path(repo: str):
    from .agent.store import resolve

    return resolve(STATE, display_name(repo, None), "reviews")
