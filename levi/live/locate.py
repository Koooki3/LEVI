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

A candidate is used only when it carries the live marker and is not ``own``
nor a checkout's ``.state`` (``protected_workspaces``). Otherwise the
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


def protected_workspaces(top: Path | None = None) -> list:
    """The `.state` of the checkout ``top`` (this one by default) and, when it
    is a git worktree, of the main checkout it belongs to (the product LEVI
    runs there): never a live workspace."""
    top = Path(top) if top is not None else Path(__file__).resolve().parents[2]
    found = [(top / ".state").resolve()]
    marker = top / ".git"
    try:
        if marker.is_file():
            line = marker.read_text().strip()
            if line.startswith("gitdir:"):
                git_dir = Path(line.split(":", 1)[1].strip())
                # <main>/.git/worktrees/<name> -> <main>
                if git_dir.parent.name == "worktrees":
                    found.append((git_dir.parent.parent.parent / ".state").resolve())
    except OSError:
        pass
    return found


def _protected(own: Path) -> set:
    found = {p for p in (_resolve(x) for x in protected_workspaces()) if p}
    mine = _resolve(own)
    if mine:
        found.add(mine)
    return found


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
    candidate = _resolve(named)
    if candidate is None:
        return Found(None, how, "not_live")
    if candidate in _protected(own):
        return Found(None, how, "product_workspace")
    if not candidate.is_dir() or not is_live(candidate):
        return Found(None, how, "not_live")
    return Found(candidate, how)
