"""An explicit workspace: the folders one LEVI instance reads and writes.

Importing this module reads no ``.env``, creates no directory and starts
nothing. ``levi.paths`` still owns the process-wide default (it loads the
``.env`` and fixes ``ROOT``/``STATE`` when imported); a function that takes a
``Workspace`` works on exactly the folders it is given, so two workspaces can be
used in one process without patching module globals.

The layout below is the single definition of where the pieces live under the
root; ``levi.paths`` builds its constants from the same strings.
"""

from dataclasses import dataclass
from pathlib import Path

STATE_REL = "outputs/LEVI/workbench"
CACHE_REL = ".cache/levi"


@dataclass(frozen=True)
class Workspace:
    root: Path
    state: Path
    cache: Path

    @classmethod
    def at(cls, root: str | Path) -> "Workspace":
        """The workspace rooted at ``root`` (resolved), with the standard layout."""
        base = Path(root).expanduser().resolve()
        return cls(
            root=base,
            state=(base / STATE_REL).resolve(),
            cache=(base / CACHE_REL).resolve(),
        )

    @classmethod
    def default(cls) -> "Workspace":
        """The process-wide workspace named by ``LEVI_WORKSPACE`` (reads ``levi.paths``)."""
        from . import paths

        return cls(root=paths.ROOT, state=paths.STATE, cache=paths.CACHE)
