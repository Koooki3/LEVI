"""Episode lists that a person or an agent supplies as JSON files.

The hold-back lists (``holdback.py``) and the verified-outcome lists
(``verified.py``) share this shape: ``{"name"?, "note"?, "episodes": [{"path",
...}, ...]}`` where ``path`` is absolute or relative to a pool root. Other keys
are ignored. A file that cannot be read, is not JSON or does not have this shape
raises ``ValueError`` naming the file (the scan and the export then stop: a
list that cannot be read must not pass as an empty one).

The scan records each list's path and the sha256 of its content (``digests``),
so a list edited after the scan is noticed (``recipe.find_warnings``).
"""

import hashlib
import json
from pathlib import Path


def read(file: Path, what: str) -> tuple[str, list[dict]]:
    """``(name, items)`` of one list file; every item is a dict with a
    ``path``. ``what`` names the kind of list in error messages."""
    try:
        data = json.loads(Path(file).read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read {what} list {file}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("episodes"), list):
        raise ValueError(  # noqa: TRY004 -- callers catch ValueError
            f'{what} list {file}: expected a JSON object with an "episodes" list'
        )
    for i, item in enumerate(data["episodes"]):
        path = item.get("path") if isinstance(item, dict) else None
        if not isinstance(path, str) or not path.strip():
            raise ValueError(f"{what} list {file}: episode {i} has no path")
    return str(data.get("name") or Path(file).stem), data["episodes"]


def digests(files: list[Path]) -> list[dict]:
    """``[{"path", "sha256"}]`` of the lists now (``sha256`` is None for a file
    that cannot be read)."""
    out = []
    for file in files:
        try:
            digest = hashlib.sha256(Path(file).read_bytes()).hexdigest()
        except OSError:
            digest = None
        out.append({"path": str(file), "sha256": digest})
    return out


def stamp(lists) -> list[tuple]:
    """``(path, sha256)`` of each list, as ``digests`` returns them or as the
    scan summary records them."""
    return [(d.get("path"), d.get("sha256")) for d in lists or []]


def unchanged(scanned, files: list[Path]) -> bool:
    """The lists now are the ones the scan recorded (same files, same content)."""
    return stamp(scanned) == stamp(digests(files))


def locate(path: str, roots: list[Path], by_key: dict):
    """The index row an entry's ``path`` names: the path itself when absolute,
    else under each pool root; also its resolved form (a symbolic link)."""
    given = Path(path).expanduser()
    for candidate in [given] if given.is_absolute() else [r / given for r in roots]:
        for text in (str(candidate), str(candidate.resolve())):
            if text in by_key:
                return by_key[text]
    return None


def candidates(path: str, roots: list[Path]) -> set[str]:
    """Every folder text an entry's ``path`` can stand for (no index needed)."""
    given = Path(path).expanduser()
    out = set()
    for candidate in [given] if given.is_absolute() else [r / given for r in roots]:
        out.update((str(candidate), str(candidate.resolve())))
    return out
