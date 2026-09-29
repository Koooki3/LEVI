"""Pool settings and the path guards that keep pool sources read-only.

- ``LEVI_POOL_ROOTS``: comma-separated folders the pool reads (never writes).
  Unset: the training pool is idle.
- ``LEVI_EXPORT_ROOTS``: comma-separated folders an export may be written
  under. Default: the workspace.
- ``LEVI_POOL_HELDOUT``: comma-separated JSON lists of held-out episodes
  (``{"episodes": [{"path", "sha256": {...}}, ...]}``), never exported.
"""

import os
from pathlib import Path

from .. import paths


def _paths(value: str | None) -> list[Path]:
    return [
        Path(p.strip()).expanduser().resolve()
        for p in (value or "").split(",")
        if p.strip()
    ]


def workspace() -> Path:
    return paths.workspace()


def pool_dir() -> Path:
    """Where the index, sources, recipes and pool jobs live."""
    return workspace() / "pool"


def default_export_parent() -> Path:
    return workspace() / "exports" / "pool"


def pool_roots() -> list[Path]:
    return _paths(os.getenv("LEVI_POOL_ROOTS"))


def export_roots() -> list[Path]:
    return _paths(os.getenv("LEVI_EXPORT_ROOTS")) or [workspace()]


def heldout_files() -> list[Path]:
    return _paths(os.getenv("LEVI_POOL_HELDOUT"))


def enabled() -> bool:
    return bool(pool_roots())


def require_enabled() -> list[Path]:
    roots = pool_roots()
    if not roots:
        raise ValueError(
            "The training pool is idle: set LEVI_POOL_ROOTS to the folders it may read"
        )
    return roots


def inside_any(path: Path, bases: list[Path]) -> Path | None:
    path = Path(path).resolve()
    for base in bases:
        if path == base or path.is_relative_to(base):
            return base
    return None


def check_export_target(target: str | Path, sources: list[str | Path]) -> Path:
    """Where an export may be written: inside an export root, outside every
    source dataset it reads (and every indexed source), and new."""
    target = Path(target).expanduser()
    if not target.is_absolute():
        target = default_export_parent() / target
    target = target.resolve()
    roots = export_roots()
    if not inside_any(target, roots):
        raise PermissionError(
            f"Export directory {target} is outside LEVI_EXPORT_ROOTS "
            f"({', '.join(map(str, roots))})"
        )
    for source in sources:
        source = Path(source).resolve()
        if target == source or target.is_relative_to(source):
            raise PermissionError(
                f"Export directory {target} lies inside the source dataset {source}"
            )
        if source.is_relative_to(target):
            raise PermissionError(
                f"Export directory {target} would contain the source dataset {source}"
            )
    if target.exists() or target.is_symlink():
        raise ValueError(f"Export directory already exists: {target}")
    staging = target.parent / f".{target.name}.partial"
    if staging.exists():
        raise ValueError(f"An unfinished export is in the way: {staging}")
    return target


def guard_write(path: Path, sources: list[str | Path]) -> Path:
    """Refuse a write that would land inside a pool source dataset."""
    path = Path(path).resolve()
    for source in sources:
        source = Path(source).resolve()
        if path == source or path.is_relative_to(source):
            raise PermissionError(f"Refusing to write inside pool source {source}")
    return path
