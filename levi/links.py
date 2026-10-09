"""Read-only links to the live service's workspace.

The live service keeps its own workspace (``levi/live/locate.py``): the
mirrored rollouts (``captures/<name>``), the browsing views LEVI builds from
them and the annotations, outcome labels and automatic verdicts written for
them all live there, never in the workspace a person works in. Every path of
this LEVI is confined to its own workspace (``levi/paths.py``), so its "Local
datasets" could neither list nor open them.

A link makes the live workspace's datasets readable here without copying them
and without the live service running (it is normally stopped between
evaluations, so its HTTP interface cannot be the way in):

- ``entries()``: the live catalog's datasets as read-only entries named
  ``live.<name>`` with a ``linked`` field. They are computed when asked and
  never written into this LEVI's catalog, so they cannot be mistaken for, or
  overwrite, a dataset of its own, and a dataset the live workspace drops
  disappears here by itself (that is the sync: there is no copy to keep in
  step).
- ``inside(path)``: a path is readable when it lies inside a linked
  workspace's ``captures/`` or ``outputs/LEVI/workbench/views/``. Nothing else
  of the live workspace is opened as a file, and nothing here is writable:
  every write path of this LEVI still confines to its own workspace and
  refuses a linked dataset (``read_only``).
- ``state_of(name)``: the live workspace's workbench state, where the
  annotations, labels and the agent store of a linked dataset are read.

Which workspaces are linked: the one this LEVI's live page shows
(``levi/live/locate.py``), the ones the training pool remembers the page
showed, and the absolute paths in ``LEVI_LINKED_WORKSPACES`` (comma
separated). A candidate must be a live workspace that really contains its
``live/`` marker and is neither this LEVI's own workspace nor any checkout's
``.state``. Nothing here writes, and nothing reads a token or a key.
"""

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from . import paths

LOG = logging.getLogger("levi.links")
PREFIX = "live."
ENV = "LEVI_LINKED_WORKSPACES"
CATALOG = Path("outputs/LEVI/workbench/datasets.json")
STATE = Path("outputs/LEVI/workbench")
READABLE = (Path("captures"), Path("outputs/LEVI/workbench/views"))

# Parsed catalogs by path while the file's stamp is unchanged.
_CACHE: dict[str, tuple] = {}


@dataclass(frozen=True)
class Link:
    name: str  # the name here: ``live.<source>``
    source: str  # the name in the linked workspace's catalog
    workspace: Path

    @property
    def state(self) -> Path:
        return self.workspace / STATE


def _resolve(path) -> Path | None:
    try:
        return Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _usable(candidate: Path | None) -> bool:
    from .live import locate

    if candidate is None or not candidate.is_absolute() or not candidate.is_dir():
        return False
    if not locate.is_live(candidate):
        return False
    if not locate.confined(candidate / "live", candidate):
        return False
    return not locate.refused(candidate, paths.ROOT)


_WORKSPACES: dict = {}
TTL = 1.0  # seconds a discovery is reused (a request reads annotations often)


def workspaces() -> list[Path]:
    """The live workspaces linked now, in a stable order, without duplicates."""
    key = (
        str(paths.ROOT),
        os.environ.get(ENV),
        os.environ.get("LEVI_LIVE_HOME"),
        os.environ.get("LEVI_LIVE_WORKSPACE"),
    )
    cached = _WORKSPACES.get(key)
    if cached and time.monotonic() - cached[0] < TTL:
        return list(cached[1])
    found = _discover()
    _WORKSPACES.clear()
    _WORKSPACES[key] = (time.monotonic(), found)
    return list(found)


def _discover() -> list[Path]:
    from .live import locate

    found: list = []
    try:
        shown = locate.find(paths.ROOT)
        if shown.workspace is not None and shown.embedded:
            found.append(shown.workspace)
    except Exception as exc:  # noqa: BLE001  a broken live home never hides the datasets
        LOG.debug("live page workspace not linked: %s", exc)
    try:
        from .pool import exclusions, settings

        found += list(exclusions.remembered(settings.pool_dir()))
    except Exception as exc:  # noqa: BLE001
        LOG.debug("remembered live workspaces not linked: %s", exc)
    for text in (os.environ.get(ENV) or "").split(","):
        if text.strip():
            found.append(Path(text.strip()))
    out: list[Path] = []
    for item in found:
        path = _resolve(item)
        if _usable(path) and path not in out:
            out.append(path)
    return out


def _stamp(path: Path):
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns)


def _catalog(workspace: Path) -> dict:
    path = workspace / CATALOG
    stamp = _stamp(path)
    cached = _CACHE.get(str(path))
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        value = json.loads(path.read_text()) if stamp else {}
    except (OSError, ValueError):
        value = {}
    value = value if isinstance(value, dict) else {}
    _CACHE[str(path)] = (stamp, value)
    return value


def _mark(workspace: Path) -> str:
    return hashlib.sha256(str(workspace).encode()).hexdigest()[:6]


def links() -> dict[str, Link]:
    """Every linked dataset, by its name here."""
    from . import catalog

    own = catalog.datasets()  # a dataset of this LEVI's own keeps its name
    out: dict[str, Link] = {}
    for workspace in workspaces():
        for source, item in sorted(_catalog(workspace).items()):
            if not isinstance(item, dict) or item.get("base"):
                continue  # a namespace is another experiment of one dataset
            try:
                state = json.loads(
                    (workspace / "live/datasets" / f"{source}.json").read_text()
                )
            except (OSError, ValueError):
                state = {}
            if state.get("archived"):
                continue
            name = PREFIX + source
            if name in own:
                continue
            if name in out:  # the same name in two live workspaces
                name = f"{PREFIX}{_mark(workspace)}.{source}"
            out[name] = Link(name, source, workspace)
    return out


def entries() -> dict[str, dict]:
    """The linked datasets as catalog entries (read-only, never persisted)."""
    out: dict[str, dict] = {}
    for name, link in links().items():
        item = dict(_catalog(link.workspace).get(link.source) or {})
        # The live workspace may have lost the rollouts (they were removed) and
        # failed to rebuild the view, while the view it built earlier is whole:
        # that is still readable, and says it is stale.
        stale = False
        if item.get("kind") == "raw" and item.get("view_status") != "ready":
            view = item.get("view")
            if (
                view
                and (_resolve(view) or Path(view)).joinpath("meta/info.json").is_file()
            ):
                stale = True
                item["view_status"] = "ready"
                item.pop("view_error", None)
        item.update(
            id="local/" + name,
            name=name,
            linked={
                "kind": "live",
                "source": link.source,
                "workspace": link.workspace.name,
                "readonly": True,
                **({"stale": True} if stale else {}),
            },
        )
        out[name] = item
    return out


def get(name: str | None) -> Link | None:
    if not name or not name.startswith(PREFIX):
        return None
    return links().get(name)


def product_id(workspace, source: str) -> str | None:
    """The id (``local/live.<name>``) under which this LEVI shows dataset
    ``source`` of live workspace ``workspace``, or None when it does not link
    it (the live page then has no viewer to open here)."""
    target = _resolve(workspace)
    for name, link in links().items():
        if link.source == source and link.workspace == target:
            return "local/" + name
    return None


def is_linked(name: str | None) -> bool:
    return get(name) is not None


def state_of(name: str | None) -> Path | None:
    """The workbench state to read a linked dataset's annotations from, or
    None for a dataset of this LEVI's own."""
    link = get(name)
    return link.state if link else None


def source_name(name: str) -> str:
    """The name a linked dataset has in its own workspace (the key of its
    annotations, labels and reviews there)."""
    link = get(name)
    return link.source if link else name


def inside(path) -> Path:
    """``path`` resolved, if it is a place a linked workspace lets be read."""
    result = _resolve(path)
    if result is not None:
        for workspace in workspaces():
            for part in READABLE:
                if result.is_relative_to((workspace / part).resolve()):
                    return result
    raise ValueError("Path must remain inside the configured workspace")


def name_for_path(path) -> str | None:
    """The linked dataset whose folder (or view) is ``path``."""
    target = _resolve(path)
    if target is None:
        return None
    for name, item in entries().items():
        for key in ("path", "view"):
            if item.get(key) and _resolve(item[key]) == target:
                return name
    return None


def status() -> list[dict]:
    """What is linked, for the catalog answer (names, never paths)."""
    return [
        {
            "kind": "live",
            "workspace": w.name,
            "datasets": len(
                [
                    1
                    for it in _catalog(w).values()
                    if isinstance(it, dict) and not it.get("base")
                ]
            ),
        }
        for w in workspaces()
    ]


READ_ONLY = (
    "This dataset belongs to the live evaluation workspace and is read-only "
    "here; review and label it where the live service keeps it"
)


class ReadOnly(PermissionError):
    """A write was asked of a linked dataset."""


def refuse_write(name: str | None) -> None:
    if is_linked(name):
        raise ReadOnly(READ_ONLY)


def refuse_path(path) -> None:
    """The lowest guard: whatever asks, a path inside a linked workspace is
    never written. Called by every function that writes annotations, labels
    and sidecars, so a route, an agent or a command that bypasses the HTTP
    checks still cannot reach the live workspace."""
    target = _resolve(path)
    if target is None:
        return
    for workspace in workspaces():
        if target.is_relative_to(workspace):
            raise ReadOnly(READ_ONLY)
