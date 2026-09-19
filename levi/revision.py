"""A dataset's *revision*: a cheap signature of the metadata files that
change whenever episodes are added, removed or rewritten.

Used by the sync (to refresh catalog entries), the annotation backend (to
drop a cached episode table) and the viewer (to offer a reload). Built from
``stat`` only — inode, size and nanosecond mtime of each file, so a rewritten
file differs even within one mtime tick — never from file contents.
"""

import os
from pathlib import Path

META_FILES = ("info.json", "episodes.jsonl", "tasks.jsonl", "episodes_stats.jsonl")


def _stat(path: Path) -> str:
    try:
        st = path.stat()
    except OSError:
        return "-"
    return f"{st.st_ino:x}.{st.st_size:x}.{st.st_mtime_ns:x}"


def dataset_revision(root: Path) -> str:
    meta = Path(root) / "meta"
    parts = [_stat(meta / name) for name in META_FILES]
    # v3 keeps episode metadata in meta/episodes/chunk-*/file-*.parquet.
    episodes = meta / "episodes"
    if episodes.is_dir():
        for dirpath, _dirs, files in os.walk(episodes):
            for name in sorted(files):
                parts.append(_stat(Path(dirpath) / name))
    return "-".join(parts)
