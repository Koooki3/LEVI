"""Held-back episodes: loaded from ``LEVI_POOL_HOLDBACK`` lists.

A person sets episodes aside for now (not a frozen test set, not a deletion):
they stay indexed and visible, but no recipe selects them and no export carries
them unless the recipe says ``include_holdback``. A list is JSON with
``episodes``: ``[{"path"}, ...]`` (``path`` absolute or relative to a pool
root), an optional ``name`` (default: the file's stem) and a free ``note``.
The hold-back applies to the whole group of copies of a listed episode, the way
a held-out mark does. Held-out wins: an episode that is both is never exported
whatever the recipe says.
"""

from pathlib import Path

from . import manifest


def load(files: list[Path]) -> list[dict]:
    entries = []
    for file in files:
        name, items = manifest.read(file, "hold-back")
        for i, item in enumerate(items):
            entries.append(
                {"set": name, "id": f"{name}:{i}", "path": item["path"].strip()}
            )
    return entries
