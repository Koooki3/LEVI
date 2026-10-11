"""Link live datasets to their canonical derived workbench without copying.

Original captures and browsing views remain read-only. Annotation, review and
Value operations use the registered source dataset key in its live workbench;
the product keeps its own catalog and checkpoint/provider registry.
"""

import contextlib
import contextvars
import functools
import hashlib
import inspect
import json
import logging
import os
import re
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
            if (
                not isinstance(source, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]+", source)
                or source in {".", ".."}
                or not isinstance(item, dict)
                or item.get("base")
            ):
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
    """The linked datasets as catalog entries (never persisted here)."""
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
                "readonly": False,
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


def _target(name: str, own: Path | None = None) -> tuple[Path, str]:
    """Canonical derived state and dataset key; never mutate process globals.

    Linked state must really remain in its registered workspace. An escaped
    workbench symlink cannot turn a derived operation into a source write.
    """
    link = get(name)
    if link is None:
        return (Path(own) if own is not None else paths.STATE), name
    state = _resolve(link.state)
    if state is None or state != link.state or not state.is_relative_to(link.workspace):
        raise ValueError("Linked derived state must remain inside its workspace")
    if state.is_relative_to(link.workspace / "captures"):
        raise ValueError("Linked derived state must not overlap source captures")
    # Store construction and locks write before an annotation's own guard.
    # Reject derived roots redirected to captures/views or another state.
    folders = [
        state / category
        for category in (
            "agent",
            "annotations",
            "object_annotations",
            "reviews",
            "recap_values",
            "segmentation",
        )
    ]
    folders += [
        state / "agent" / "workbench.sqlite3",
        state / "agent" / "locks",
        state / "agent" / "datasets" / link.source,
        state / "annotations" / link.source,
        state / "object_annotations" / link.source,
        state / "reviews" / (link.source + ".json"),
    ]
    for folder in folders:
        if folder.resolve() != folder:
            raise ValueError("Linked derived directories must not be symbolic links")
    return state, link.source


_JOB_TARGETS = contextvars.ContextVar("linked_job_targets", default=None)


def target(name: str, own: Path | None = None) -> tuple[Path, str]:
    state, source = _target(name, own)
    pinned = (_JOB_TARGETS.get() or {}).get(name)
    if pinned and (state.resolve(), source) != pinned[:2]:
        raise pinned[2](409, "The job's linked dataset source disappeared or changed")
    return state, source


@contextlib.contextmanager
def job_scope(job, name, own, error):
    """Pin a job's canonical identity for its whole operation, without globals."""
    source = job.get("source")
    if source is None:
        if get(name) is not None:
            raise error(409, "The linked job has no canonical source identity")
        yield
        return
    if not isinstance(source, dict):
        raise error(409, "Invalid canonical job source")
    state, dataset = source.get("workbench"), source.get("dataset")
    if (
        not isinstance(state, str)
        or not Path(state).is_absolute()
        or not isinstance(dataset, str)
    ):
        raise error(409, "Invalid canonical job source")
    expected = (Path(state).resolve(), dataset, error)
    token = _JOB_TARGETS.set({**(_JOB_TARGETS.get() or {}), name: expected})
    try:
        target(name, own)
        yield
    finally:
        _JOB_TARGETS.reset(token)


def for_job(name_key, own, error):
    def decorate(function):
        @functools.wraps(function)
        def wrapped(job, *args, **kwargs):
            with job_scope(job, job[name_key], own(), error):
                return function(job, *args, **kwargs)

        return wrapped

    return decorate


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
    requested = Path(path).expanduser().absolute()
    if target is None:
        return
    for workspace in workspaces():
        if requested.is_relative_to(workspace) and not target.is_relative_to(workspace):
            raise ReadOnly(READ_ONLY)
        if not target.is_relative_to(workspace):
            continue
        state = workspace / STATE
        # Only registered datasets' derived annotation paths are writable.
        # Resolving both sides also prevents symlink escapes into captures.
        if state.resolve() == state and target.is_relative_to(state):
            parts = target.relative_to(state).parts
            sources = _catalog(workspace)
            if (
                len(parts) >= 2
                and parts[0] in {"annotations", "object_annotations"}
                and parts[1] in sources
            ):
                return
            if (
                len(parts) >= 3
                and parts[:2] == ("agent", "datasets")
                and parts[2] in sources
            ):
                return
            if (
                len(parts) == 2
                and parts[0] == "reviews"
                and parts[1].removesuffix(".json") in sources
            ):
                return
        raise ReadOnly(READ_ONLY)


def job_mark(state: Path, dataset: str) -> str:
    return hashlib.sha256((str(state.resolve()) + "/" + dataset).encode()).hexdigest()[
        :12
    ]


def reserve_job_id(directory: Path, name: str, own: Path) -> str:
    from . import naming

    if get(name) is None:
        return naming.timestamp_id(directory, ".json")
    state, source = target(name, own)
    mark = job_mark(state, source)
    return naming.timestamp_id(directory, "." + mark + ".json") + "." + mark


def is_job_id(value: str) -> bool:
    from . import naming

    base, separator, mark = value.partition(".")
    return naming.is_timestamp_id(base) and (
        not separator or bool(re.fullmatch(r"[0-9a-f]{12}", mark))
    )


def for_dataset(name_key, own, error):
    def decorate(function):
        signature = inspect.signature(function)

        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            value = signature.bind(*args, **kwargs).arguments[name_key]
            if not isinstance(value, str):
                return function(*args, **kwargs)
            name = value.removeprefix("local/")
            if get(name) is None:
                return function(*args, **kwargs)
            state, source = target(name, own())
            with job_scope(
                {"source": {"workbench": str(state), "dataset": source}},
                name,
                own(),
                error,
            ):
                return function(*args, **kwargs)

        return wrapped

    return decorate
