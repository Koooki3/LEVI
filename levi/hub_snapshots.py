"""Hub dataset snapshots for diagnostics, kept in a cache with a hard size limit.

A diagnostic of a Hugging Face dataset downloads its metadata and parquet files (and, when asked, its videos)
into the workspace cache. Those copies used to stay there for good, one folder per dataset, until someone ran
``levi clean`` with the service stopped. They now live in a ``DiskBudget`` (``levi/performance/budget.py``):
least recently used snapshots are evicted to stay under the limit, a snapshot being read is never evicted, and
a snapshot is rebuilt by downloading it again.

A snapshot is one Hub commit seen by one credential: the caller resolves the dataset's current commit (an
authenticated call, so a credential without access still fails as before) and passes it with the sizes of the
files to download. A new commit is a new snapshot, and the reservation is the exact size, so a snapshot that
cannot fit is known before anything is downloaded.

A snapshot that cannot fit (larger than the whole limit, no room while other writers hold theirs, or a cache
that cannot be opened) is downloaded once into the old unbounded folder, so no diagnostic is lost; the caller
says so in its response.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from pathlib import Path

from .performance.budget import META_MAX, BudgetRejected, DiskBudget

VERSION = "hub-snapshot-2"
FOLDER = "hub-snapshots"
FALLBACK_REASONS = {"too_large", "busy", "unavailable", "lock_timeout"}
# huggingface_hub's own bookkeeping under <folder>/.cache/huggingface: a metadata file per downloaded file
# and a listing of the whole repository tree (every file, matched or not, about 300 bytes each)
ALLOWANCE = 64 * 1024
PER_FILE = 4 * 1024
PER_LISTED_FILE = 512


def reserve_for(sizes: list[int], listed: int = 0) -> int:
    """Bytes to reserve for downloading files of these sizes from a repository of ``listed`` files."""
    return sum(sizes) + ALLOWANCE + PER_FILE * len(sizes) + PER_LISTED_FILE * listed


@contextlib.contextmanager
def snapshot(
    repo_id: str,
    patterns: list[str],
    download: Callable[[Path], None],
    cache: Path,
    *,
    revision: str | None,
    sizes: list[int | None],
    listed: int = 0,
    scope: str = "anonymous",
    cap_gb: float | None = None,
) -> Iterator[tuple[Path, str]]:
    """Yield ``(folder, note)`` holding ``repo_id`` at ``revision`` downloaded with ``patterns``.

    ``download(folder)`` fills a folder with that revision. ``sizes`` are the sizes of the files it will
    download (``None`` when the Hub did not say), ``listed`` the number of files in the whole repository, ``scope``
    names the credential (``auth.credential_scope``). ``note`` is "" for a cached snapshot, or says why the old
    unbounded folder under ``cache`` was used instead. Without a revision or with a size unknown the snapshot is
    not cached: neither a key nor a reservation would be right.
    """
    kwargs = {} if cap_gb is None else {"cap_gb": cap_gb}
    key = f"{repo_id}@{revision}|{scope}|" + ",".join(sorted(patterns))
    lease = None
    fallback = "no commit from the Hub" if not revision else ""
    if None in sizes:
        fallback = "size unknown"
    if not fallback:
        try:
            lease, fallback = _cached(key, sizes, listed, download, cache, kwargs)
        except BudgetRejected as rejected:
            if rejected.reason not in FALLBACK_REASONS:
                raise
            fallback = rejected.reason
    if lease is None:
        folder = cache / repo_id.replace("/", "__")
        download(folder)
        yield folder, f"not cached ({fallback}): kept in {folder}"
        return
    with lease:
        yield lease.path, ""


def _cached(key, sizes, listed, download, cache, kwargs):
    """(lease, "") for the entry ``key``, downloading it first when missing; (None, why) if it went at once."""
    reserve = reserve_for(sizes, listed)
    budget = DiskBudget.from_cap_gb(cache / FOLDER, version=VERSION, **kwargs)
    lease = budget.get(key)
    if lease is not None:
        return lease, ""
    if reserve > budget.max_bytes - META_MAX:
        raise BudgetRejected("too_large", f"{reserve} bytes > limit {budget.max_bytes}")
    with budget.writing(key, reserve=reserve) as folder:
        download(folder)
    lease = budget.get(key)
    return (
        lease,
        "" if lease is not None else "evicted at once",
    )  # evicted between commit and read
