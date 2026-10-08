"""Pool settings and the path guards that keep pool sources read-only.

- ``LEVI_POOL_ROOTS``: comma-separated folders the pool reads (never writes).
  Unset: the training pool is idle.
- ``LEVI_EXPORT_ROOTS``: comma-separated folders an export may be written
  under. Default: the workspace.
- ``LEVI_POOL_HELDOUT``: comma-separated JSON lists of held-out episodes
  (``{"episodes": [{"path", "sha256": {...}}, ...]}``), never exported.
  Unset, exports are refused; ``none`` states that there is no held-out set.
- ``LEVI_POOL_HOLDBACK``: comma-separated JSON lists of episodes a person set
  aside for now (``{"episodes": [{"path"}, ...]}``): indexed and listed, but
  no recipe selects them and no export carries them unless the recipe sets
  ``include_holdback``. Unset: nothing is held back.
- ``LEVI_POOL_OUTCOMES``: comma-separated JSON lists of verified outcomes
  (``{"episodes": [{"path", "outcome", "basis"}, ...]}``) that rank above the
  robot's key press and below a human label. Unset: none.
- ``LEVI_POOL_STALL_SECONDS`` (300): a running job that has not moved for this
  long is reported ``stalled``.
- ``LEVI_POOL_PARTIAL_TTL`` (3 days) and ``LEVI_POOL_JOB_TTL`` (30 days): how
  long an interrupted or failed export's ``.partial`` folder, and a finished
  job's record and log, are kept before the sweeper removes them (``3d``,
  ``12h``, ``90m``, ``3600s``; a bare number is days).
- ``LEVI_POOL_AUTO_RESUME`` (off): ``1`` resumes interrupted exports when the
  service starts.
- ``LEVI_POOL_FREE_MARGIN_GIB`` (1): free space an export leaves on its volume.
- ``LEVI_POOL_BATCH_EPISODES`` (auto, at least 8): raw episodes converted per
  journaled part; a smaller part loses less to an interruption.
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


def heldout_disabled() -> bool:
    """``LEVI_POOL_HELDOUT=none``: the operator states there is no held-out
    set; without this an empty setting refuses exports."""
    return (os.getenv("LEVI_POOL_HELDOUT") or "").strip().lower() == "none"


def heldout_files() -> list[Path]:
    if heldout_disabled():
        return []
    return _paths(os.getenv("LEVI_POOL_HELDOUT"))


def _list_paths(value: str | None) -> list[Path]:
    """List files named by a setting. ``~name`` of a user that does not exist
    cannot be expanded: it stays as written and names a file that is not there,
    so reading it fails with the same "cannot read" error as any missing list."""
    from .manifest import expand

    return [expand(p.strip()).resolve() for p in (value or "").split(",") if p.strip()]


def holdback_files() -> list[Path]:
    """The hold-back lists; unset is no hold-back (unlike the held-out
    setting, an empty value never refuses an export)."""
    return _list_paths(os.getenv("LEVI_POOL_HOLDBACK"))


def outcome_files() -> list[Path]:
    """The verified-outcome lists; unset is none."""
    return _list_paths(os.getenv("LEVI_POOL_OUTCOMES"))


def require_heldout() -> None:
    """An export needs a held-out list, or an explicit ``none``."""
    if not heldout_files() and not heldout_disabled():
        raise ValueError(
            "No held-out list is configured: set LEVI_POOL_HELDOUT to the frozen "
            "test lists (comma-separated JSON files), or to `none` if the pool "
            "has no held-out set. Refusing to export without knowing."
        )


def _number(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.getenv(name) or default))
    except ValueError:
        return default


UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def _duration(name: str, default: float, bare: str) -> float:
    """``3d``, ``12h``, ``90m``, ``3600s``; a bare number counts in ``bare``
    units; ``0`` keeps nothing past its use."""
    value = (os.getenv(name) or "").strip().lower()
    if not value:
        return default
    unit = UNITS[bare]
    if value[-1] in UNITS:
        unit, value = UNITS[value[-1]], value[:-1]
    try:
        return max(0.0, float(value) * unit)
    except ValueError:
        return default


def stall_seconds() -> float:
    return max(5.0, _duration("LEVI_POOL_STALL_SECONDS", 300.0, "s"))


def partial_ttl_seconds() -> float:
    return _duration("LEVI_POOL_PARTIAL_TTL", 3 * 86400.0, "d")


def job_ttl_seconds() -> float:
    return _duration("LEVI_POOL_JOB_TTL", 30 * 86400.0, "d")


def auto_resume() -> bool:
    return (os.getenv("LEVI_POOL_AUTO_RESUME") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def free_margin_bytes() -> int:
    return int(_number("LEVI_POOL_FREE_MARGIN_GIB", 1.0) * 1024**3)


def batch_episodes() -> int | None:
    value = int(_number("LEVI_POOL_BATCH_EPISODES", 0))
    return value if value > 0 else None


def job_path(job_id: str) -> Path:
    if not job_id.replace("-", "").isalnum():
        raise ValueError("Invalid job ID")
    return pool_dir() / "jobs" / f"{job_id}.json"


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


def _is_export_dir(path: Path) -> bool:
    """A folder a pool export wrote (it carries the export marker)."""
    from .scanner import EXPORT_MARKERS

    return any((Path(path) / name).is_file() for name in EXPORT_MARKERS)


def check_export_target(
    target: str | Path, sources: list[str | Path], allow_partial: bool = False
) -> Path:
    """Where an export may be written: inside an export root, outside every
    source dataset it reads (and every indexed source), and new.
    ``allow_partial``: the export's own unfinished ``.partial`` folder may be
    there (a resume)."""
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
        if target == source and (_is_export_dir(source) or not source.exists()):
            # The very folder of an earlier pool export (still listed by the scan,
            # or already deleted and only listed by a stale index): not a source
            # dataset. An existing folder is refused as "already exists" below; a
            # folder that is gone is free to be written again.
            continue
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
    if staging.exists() and not allow_partial:
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
