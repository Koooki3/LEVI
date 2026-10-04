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
3. else the ``workspace`` the live service last wrote into its status file,
   ``<LEVI_LIVE_HOME or ~/.levi-live>/status.json``.

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


@dataclass(frozen=True)
class Found:
    workspace: Path | None
    # own: this LEVI is the live workspace; env / status: another one, named
    # by LEVI_LIVE_WORKSPACE or by the live service's status file.
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


def _resolve(path) -> Path | None:
    try:
        return Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


ENV_CHECKOUT = "LEVI_LIVE_PROTECT_CHECKOUT"


def checkout_root() -> Path:
    """The LEVI checkout whose worktrees' ``.state`` are protected: the one
    this code runs from, unless ``LEVI_LIVE_PROTECT_CHECKOUT`` names another
    (the test suite points it at a scratch folder: its temporary folders lie
    under this checkout's own ``.state``)."""
    named = (os.environ.get(ENV_CHECKOUT) or "").strip()
    return Path(named).expanduser() if named else Path(__file__).resolve().parents[2]


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
    if explicit:
        how, named = "env", explicit
    else:
        status = jsonio.read(home() / "status.json")
        named = status.get("workspace") if isinstance(status, dict) else None
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
    return Found(candidate, how)
