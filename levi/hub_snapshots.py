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
# huggingface_hub keeps a small metadata file per downloaded file under <folder>/.cache/huggingface
ALLOWANCE = 64 * 1024
PER_FILE = 4 * 1024


def reserve_for(sizes: list[int]) -> int:
    """Bytes to reserve for downloading files of these sizes, with huggingface_hub's own bookkeeping."""
    return sum(sizes) + ALLOWANCE + PER_FILE * len(sizes)


@contextlib.contextmanager
def snapshot(
    repo_id: str,
    patterns: list[str],
    download: Callable[[Path], None],
    cache: Path,
    *,
    revision: str,
    sizes: list[int],
    scope: str = "anonymous",
    cap_gb: float | None = None,
) -> Iterator[tuple[Path, str]]:
    """Yield ``(folder, note)`` holding ``repo_id`` at ``revision`` downloaded with ``patterns``.

    ``download(folder)`` fills a folder with that revision. ``sizes`` are the sizes of the files it will
    download, ``scope`` names the credential (``auth.credential_scope``). ``note`` is "" for a cached snapshot,
    or says why the old unbounded folder under ``cache`` was used instead.
    """
    kwargs = {} if cap_gb is None else {"cap_gb": cap_gb}
    key = f"{repo_id}@{revision}|{scope}|" + ",".join(sorted(patterns))
    reserve = reserve_for(sizes)
    lease = None
    fallback = ""
    try:
        budget = DiskBudget.from_cap_gb(cache / FOLDER, version=VERSION, **kwargs)
        lease = budget.get(key)
        if lease is None:
            if reserve > budget.max_bytes - META_MAX:
                raise BudgetRejected(
                    "too_large", f"{reserve} bytes > limit {budget.max_bytes}"
                )
            with budget.writing(key, reserve=reserve) as folder:
                download(folder)
            lease = budget.get(key)
            if lease is None:  # evicted between commit and read
                fallback = "evicted at once"
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
