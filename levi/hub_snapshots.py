"""Hub dataset snapshots for diagnostics, kept in a cache with a hard size limit.

A diagnostic of a Hugging Face dataset downloads its metadata and parquet files (and, when asked, its videos)
into the workspace cache. Those copies used to stay there for good, one folder per dataset, until someone ran
``levi clean`` with the service stopped. They now live in a ``DiskBudget`` (``levi/performance/budget.py``):
least recently used snapshots are evicted to stay under the limit, a snapshot being read is never evicted, and
a snapshot is rebuilt by downloading it again.

A snapshot that cannot fit (larger than the whole limit, or no room while other writers hold theirs) falls back
to the old unbounded folder, so no diagnostic is lost; the caller says so in its report.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from pathlib import Path

from .performance.budget import META_MAX, BudgetRejected, DiskBudget

VERSION = "hub-snapshot-1"
FOLDER = "hub-snapshots"
FALLBACK_REASONS = {"too_large", "busy"}


@contextlib.contextmanager
def snapshot(
    repo_id: str,
    patterns: list[str],
    download: Callable[[Path], None],
    cache: Path,
    *,
    whole_budget: bool = False,
    cap_gb: float | None = None,
) -> Iterator[tuple[Path, str]]:
    """Yield ``(folder, note)`` holding ``repo_id`` downloaded with ``patterns``.

    ``download(folder)`` fills an empty folder. ``whole_budget`` reserves the whole limit for the write (a
    video download whose size is unknown). ``note`` is "" for a cached snapshot, or says why the old unbounded
    folder under ``cache`` was used instead.
    """
    kwargs = {} if cap_gb is None else {"cap_gb": cap_gb}
    budget = DiskBudget.from_cap_gb(cache / FOLDER, version=VERSION, **kwargs)
    key = repo_id + "|" + ",".join(sorted(patterns))
    lease = budget.get(key)
    fallback = ""
    if lease is None:
        reserve = budget.max_bytes - META_MAX if whole_budget else None
        try:
            with budget.writing(key, reserve=reserve) as folder:
                download(folder)
        except BudgetRejected as rejected:
            if rejected.reason not in FALLBACK_REASONS:
                raise
            fallback = rejected.reason
        else:
            lease = budget.get(key)
            if lease is None:  # evicted between commit and read
                fallback = "evicted at once"
    if lease is None:
        folder = cache / repo_id.replace("/", "__")
        download(folder)
        yield folder, f"not cached ({fallback}): kept in {folder}"
        return
    with lease:
        yield lease.path, ""
