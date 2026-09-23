"""Persistent dataset registration and review manifests, with atomic writes.

Registered datasets are keyed by a human-readable **catalog name** (the id
used in URLs is ``local/<name>``): the folder name made URL-safe, or its
parent's name for the legacy generic ``…/dataset`` layout, with a timestamp
appended only on a real clash. Registering an already-registered path returns
its existing entry. Ids from before names replaced hashes live on as
aliases (``dataset_aliases.json``) so old links still resolve.

**Namespaces** reuse one input for independent experiments without copying
it: ``<dataset>--<namespace>`` is a catalog entry of its own (``base`` and
``namespace`` fields) over the same source folder and browsing view. Every
product LEVI keys by catalog name -- annotations, outcome labels, reviews,
agent revisions and the active head, dataset memory, teaching, examples,
eval records, cost profiles -- is therefore separate per namespace, and
nothing one experiment commits reaches another one's model context. Path
lookups always answer the base dataset.
"""

import contextlib
import fcntl
import json
import os
import re
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
    """The dataset registered at ``root`` -- never one of its namespaces."""
    text = str(root)
    for item in items.values():
        if item.get("base"):
            continue
        if item.get("path") == text or item.get("view") == text:
            return item
    return None


SEPARATOR = "--"
NAMESPACE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._]|-(?!-))*$")


def namespace_name(base: str, namespace: str) -> str:
    return f"{base}{SEPARATOR}{namespace}"


def is_namespace(name: str) -> bool:
    item = datasets().get(name)
    return bool(item and item.get("base"))


def create_namespace(base: str, namespace: str) -> dict:
    """A namespace of a registered dataset (idempotent). Its products start
    empty; the source stays one folder."""
    if len(namespace) > 64 or not NAMESPACE.match(namespace):
        raise ValueError(
            "A namespace is 1-64 letters, digits, '.', '_' or single '-', "
            "starting with a letter or digit"
        )
    with locked():
        items = datasets()
        source = items.get(base)
        if source is None:
            raise ValueError(f"Dataset {base!r} is not registered")
        if source.get("base"):
            raise ValueError("A namespace cannot have namespaces of its own")
        name = namespace_name(base, namespace)
        item = items.get(name)
        if item is not None and item.get("base") != base:
            raise ValueError(f"{name!r} is already a dataset of its own")
        if item is None:
            item = {
                "id": "local/" + name,
                "name": name,
                "base": base,
                "namespace": namespace,
                "created_at": time.time(),
            }
        for key in ("kind", "path", "view", "info", "input_format", "revision"):
            if key in source:
                item[key] = source[key]
        items[name] = item
        atomic(STATE / "datasets.json", items)
    return item


def namespaces(base: str | None = None) -> list[dict]:
    return [
        i
        for i in datasets().values()
        if i.get("base") and (base is None or i["base"] == base)
    ]


def follow_base(items: dict) -> bool:
    """Copy each namespace's shared fields (path, view, info...) from its
    base, so a rebuilt view reaches every namespace. True if anything changed."""
    changed = False
    for item in items.values():
        source = items.get(item.get("base") or "")
        if not source:
            continue
        for key in ("kind", "path", "view", "info", "input_format", "revision"):
            if key in source and item.get(key) != source[key]:
                item[key] = source[key]
                changed = True
    return changed


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
        follow_base(items)
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
    items = datasets()
    item = items[name]
    # A namespace reads its base's source and view.
    item = items.get(item.get("base") or "", item)
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
    otherwise its folder name (local) or ``org__name`` (Hub). A ``local/``
    id wins over the path: a namespace shares its base's folder."""
    if repo_id and repo_id.startswith("local/"):
        try:
            name = resolve_name(repo_id)
        except ValueError:
            name = None
        if name:
            return name
    if local_path:
        return name_for_path(local_path) or _base_name(Path(local_path))
    if repo_id:
        name = resolve_name(repo_id) if repo_id.startswith("local/") else None
        return name or catalog_name(repo_id.replace("/", "__"))
    return "dataset"


def review_path(repo: str):
    from .agent.store import resolve

    return resolve(STATE, display_name(repo, None), "reviews")
