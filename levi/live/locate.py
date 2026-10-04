"""Which live workspace a LEVI page shows.

The live service keeps a workspace of its own (``levi live start`` refuses the
product LEVI's ``.state``): the automatic approver, its runs, the mirror and
the state files never enter the workspace people work in. Its *page* does not
need a LEVI of its own, though: the product LEVI shows the same ``/live`` page
by reading the live workspace's files (``levi/live/api.py``).

``find(own)`` answers, for a LEVI whose workspace is ``own``:

1. ``own`` itself when it is a live workspace (``live/workspace.json``): the
   live service's own core;
2. else ``LEVI_LIVE_WORKSPACE`` when set (the same variable ``levi live``
   reads for its workspace);
3. else the workspace the last ``levi live start`` with the live home ran on,
   ``<LEVI_LIVE_HOME or ~/.levi-live>/started.json`` (``remembered``, the same
   answer ``levi live`` commands use when given no workspace). A home without
   that record (a service started before it existed) falls back to the
   ``workspace`` in its status file, ``status.json``, but only for a workspace
   a ``start`` has used (``started_here``): ``levi live once`` also writes the
   status file, and its target is never the live service's workspace.

A candidate is used only when it is an absolute path, carries the live
marker in a ``live/`` folder that really lies inside it (not a link to
elsewhere), and neither is, lies inside nor holds ``own`` or the ``.state``
of any checkout of this repository (``protected_workspaces``). Otherwise the
answer is no workspace and a reason: ``not_configured`` (nothing names one),
``not_live`` (what is named is not a live workspace, or does not exist) or
``product_workspace`` (what is named is a product LEVI's workspace). The
reason is shown, never the path.

Nothing here writes, and nothing reads a token or a key. Standard library only.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from . import auto, jsonio
from . import config as live_config

ENV_WORKSPACE = "LEVI_LIVE_WORKSPACE"
ENV_HOME = "LEVI_LIVE_HOME"
STARTED = "started.json"  # in the home and in live/: the workspace `start` ran on


@dataclass(frozen=True)
class Found:
    workspace: Path | None
    # own: this LEVI is the live workspace; env / status: another one, named
    # by LEVI_LIVE_WORKSPACE or by the live home's records (``remembered``).
    how: str
    problem: str | None = None

    @property
    def embedded(self) -> bool:
        """Shown by a LEVI that is not the live workspace itself."""
        return self.workspace is not None and self.how != "own"


def home() -> Path:
    """The live service's home (status file, pid file, lock)."""
    return Path(os.environ.get(ENV_HOME) or live_config.DEFAULT_HOME).expanduser()


def is_live(workspace) -> bool:
    try:
        return (Path(workspace) / "live" / auto.MARKER).is_file()
    except OSError:
        return False


def started_here(workspace) -> bool:
    """Has a ``levi live start`` run on this workspace? Its record, or the
    service log only ``start`` writes (services started before the record
    existed)."""
    live = Path(workspace) / "live"
    return (live / STARTED).is_file() or (live / "logs" / "live.log").is_file()


def remembered(home_dir) -> tuple:
    """What the live home names as the service's workspace: ``(named,
    legacy)``. ``named`` is the ``workspace`` of ``<home>/started.json`` when
    that record exists, else the one in ``<home>/status.json`` (or None);
    ``legacy`` is true for the status-file fallback, which counts only for a
    workspace a ``start`` used (``started_here``, checked by the caller once
    the path is known to be safe)."""
    home_dir = Path(home_dir).expanduser()
    record = jsonio.read(home_dir / STARTED)
    if isinstance(record, dict):
        return record.get("workspace"), False
    status = jsonio.read(home_dir / "status.json")
    return (status.get("workspace") if isinstance(status, dict) else None), True


def _resolve(path) -> Path | None:
    try:
        return Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


# The checkout this code runs from.
THIS_CHECKOUT = Path(__file__).resolve().parents[2]


def checkout_root() -> Path:
    """The LEVI checkout whose worktrees' ``.state`` are protected: always the
    one this code runs from. No setting changes it (a value read from a
    ``.env`` must never unprotect the product's ``.state``); the test suite
    replaces this function, since its temporary folders lie under the
    checkout's own ``.state``."""
    return THIS_CHECKOUT


def _git_common_dir(top: Path) -> Path | None:
    marker = top / ".git"
    try:
        if marker.is_dir():
            return marker
        line = marker.read_text().strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    git_dir = Path(line.split(":", 1)[1].strip())
    if not git_dir.is_absolute():
        git_dir = top / git_dir
    try:
        common = (git_dir / "commondir").read_text().strip()
        return (git_dir / common).resolve()
    except OSError:
        # <main>/.git/worktrees/<name> -> <main>/.git
        return git_dir.parent.parent if git_dir.parent.name == "worktrees" else None


def checkouts(top: Path | None = None) -> list:
    """Every checkout of the repository ``top`` belongs to: the main one and
    each worktree ``git worktree list`` would show (read from the git
    directory's own files, no subprocess)."""
    top = Path(top) if top is not None else checkout_root()
    found = [top]
    common = _git_common_dir(top)
    if common is not None:
        if common.name == ".git":
            found.append(common.parent)
        try:
            entries = sorted((common / "worktrees").iterdir())
        except OSError:
            entries = []
        for entry in entries:
            try:
                found.append(Path((entry / "gitdir").read_text().strip()).parent)
            except OSError:
                continue
    unique = []
    for folder in found:
        resolved = _resolve(folder)
        if resolved and resolved not in unique:
            unique.append(resolved)
    return unique


def protected_workspaces(top: Path | None = None) -> list:
    """The ``.state`` of every LEVI checkout (the main one, where the product
    LEVI runs, and every worktree): never a live workspace, nor inside one,
    nor holding one (``overlaps``)."""
    return [p for p in (_resolve(c / ".state") for c in checkouts(top)) if p]


def overlaps(a: Path, b: Path) -> bool:
    """One folder is the other, lies inside it or holds it."""
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def refused(candidate: Path, own: Path | None = None) -> bool:
    """A product workspace, or one that overlaps it: a checkout's ``.state``
    (``protected_workspaces``), or this LEVI's own workspace ``own``."""
    guarded = list(protected_workspaces())
    mine = _resolve(own) if own is not None else None
    if mine:
        guarded.append(mine)
    return any(overlaps(candidate, p) for p in guarded)


def confined(path, workspace) -> bool:
    """``path`` resolves (symbolic links followed) to somewhere inside
    ``workspace``: a ``live/`` that is a link to elsewhere is not."""
    target, root = _resolve(path), _resolve(workspace)
    return bool(target and root and target.is_relative_to(root))


def find(own) -> Found:
    own = Path(own)
    if is_live(own):
        return Found(own, "own")
    explicit = (os.environ.get(ENV_WORKSPACE) or "").strip()
    legacy = False
    if explicit:
        how, named = "env", explicit
    else:
        named, legacy = remembered(home())
        how = "status"
        if not isinstance(named, str) or not named.strip():
            return Found(None, how, "not_configured")
    if not Path(named.strip()).expanduser().is_absolute():
        return Found(None, how, "not_live")
    candidate = _resolve(named.strip())
    if candidate is None:
        return Found(None, how, "not_live")
    if refused(candidate, own):
        return Found(None, how, "product_workspace")
    if (
        not candidate.is_dir()
        or not is_live(candidate)
        or not confined(candidate / "live", candidate)
    ):
        return Found(None, how, "not_live")
    if legacy and not started_here(candidate):
        # Only a `levi live once` ran there: not the live service's workspace.
        return Found(None, how, "not_configured")
    return Found(candidate, how)
