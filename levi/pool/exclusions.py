"""Episodes a person removed on a live workspace's page, read-only.

``levi live`` mirrors the rollouts it labels into ``<workspace>/captures/
<dataset>/demo_NNNN`` and records, in ``<workspace>/live/datasets/<dataset>.
json``, which episodes a person removed (``levi/live/exclusion.py``: a soft
delete, the mirror and the annotations stay). When that workspace lies under
a pool root the scan indexes its mirror like any raw capture, and the same
recording is usually indexed a second time from the rollout folder the mirror
was linked from (one fingerprint, one *group*; the rollout copy is the
canonical one). Removing the episode must keep the recording out of every
export, so the pool reads those files **at query time** (not at scan time:
a click on the live page counts at once, without a new scan) and treats the
whole group as removed, the same way a label or a held-out mark on one copy
applies to every copy.

The key set holds, for each removed episode, **both** the mirror's folder and
the rollout folder the state file says it was linked from
(``state["source"]``): the rollout copy is recognised even when the index has
no mirror of it (the scan ran before the demo was mirrored) or the mirror is
no longer a copy of it (the source was replaced after mirroring, so the
fingerprints differ and the two are in different groups). The paths are only
compared with the index's keys, never opened, so a state file can at worst
make the pool leave out more.

Only workspaces the last scan found under the pool roots, the pool's own
workspace and the live workspace this LEVI's live page shows
(``levi/live/locate.py``) are consulted; any other live workspace the scan
has not seen is not.

Nothing here writes into a live workspace. The pool keeps one small file of
its own, ``<pool>/live_workspaces.json``: every live workspace the product
LEVI's live page has shown (``remember``; entries are only ever added). They
are consulted too (those that no longer exist are skipped), so an episode
removed on that page stays out of the pool after the page finds another live
workspace, and whether or not the live workspace lies under a pool root.
"""

import json
import logging
from pathlib import Path

from . import settings

LIVE = Path("live")
STATES = LIVE / "datasets"
MARKER = LIVE / "workspace.json"


REMEMBERED = "live_workspaces.json"
# At most this many remembered live workspaces; past it a new one is not
# added (it is still read while the page shows it), and the page and the log
# say so: `levi pool live-workspaces forget` makes room.
MAX_REMEMBERED = 20
LOG = logging.getLogger("levi.pool.exclusions")
# Parsed state files by path, kept while the file's stamp (inode, size,
# modification time) is unchanged: the pool asks on every listing.
_CACHE: dict[str, tuple] = {}


def _read(path: Path):
    try:
        st = path.stat()
        stamp = (st.st_ino, st.st_size, st.st_mtime_ns)
    except OSError:
        _CACHE.pop(str(path), None)
        return None
    cached = _CACHE.get(str(path))
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    _CACHE[str(path)] = (stamp, value)
    return value


def remembered(pool: Path | None = None) -> list[Path]:
    """The live workspaces this pool was ever shown (``remember``)."""
    value = _read(Path(pool or settings.pool_dir()) / REMEMBERED)
    rows = value.get("workspaces") if isinstance(value, dict) else None
    return [Path(w) for w in rows or [] if isinstance(w, str) and w]


def is_full(workspace, pool: Path | None = None) -> bool:
    """The list is at ``MAX_REMEMBERED`` and does not hold ``workspace``."""
    known = {str(w) for w in remembered(pool)}
    return str(Path(workspace)) not in known and len(known) >= MAX_REMEMBERED


def _store(pool: Path, change) -> None:
    from levi.live import jsonio

    def apply(value):
        value = value if isinstance(value, dict) else {}
        rows = [w for w in value.get("workspaces") or [] if isinstance(w, str)]
        return {"schema": "levi.pool.live_workspaces.v1", "workspaces": change(rows)}

    jsonio.update(pool / REMEMBERED, apply, default=dict)


def remember(workspace, pool: Path | None = None) -> bool:
    """Add a live workspace the live page showed (never removes one; only
    ``forget`` does). Past ``MAX_REMEMBERED`` it is not added and a warning
    is logged. A failure to write is not the page's problem: the pool then
    still reads the workspace the page shows now. True when it is listed."""
    text = str(Path(workspace))
    pool = Path(pool or settings.pool_dir())
    if text in {str(w) for w in remembered(pool)}:
        return True
    if is_full(text, pool):
        LOG.warning(
            "the training pool already remembers %d live workspaces: %s is read "
            "while the live page shows it, but not remembered "
            "(levi pool live-workspaces forget <path> makes room)",
            MAX_REMEMBERED,
            text,
        )
        return False

    def add(rows):
        return rows if text in rows else [*rows, text]

    try:
        _store(pool, add)
    except OSError:
        return False
    return True


def forget(workspace, pool: Path | None = None) -> bool:
    """Stop reading a remembered live workspace (nothing of it is deleted; it
    is listed again if the live page shows it again). True when it was
    listed."""
    text = str(Path(workspace).expanduser())
    pool = Path(pool or settings.pool_dir())
    known = [str(w) for w in remembered(pool)]
    resolved = str(Path(text).resolve()) if Path(text).is_absolute() else text
    hit = next((w for w in known if w in (text, resolved)), None)
    if hit is None:
        return False
    _store(pool, lambda rows: [w for w in rows if w != hit])
    return True


def listing() -> dict:
    """What ``levi pool live-workspaces list`` prints."""
    rows = remembered()
    shown = _shown_now()
    return {
        "remembered": [{"path": str(w), "exists": w.is_dir()} for w in rows],
        "limit": MAX_REMEMBERED,
        "shown_now": str(shown) if shown else None,
    }


def _shown_now() -> Path | None:
    try:
        from levi.live import locate

        found = locate.find(settings.workspace()).workspace
    except (OSError, ValueError, RuntimeError):  # the live side never breaks the pool
        return None
    return Path(found) if found is not None else None


def workspace_exclusions(workspace: Path) -> dict[str, dict]:
    """Pool episode key (the mirror's demo folder) -> who removed it, of one
    live workspace; empty for any other workspace."""
    workspace = Path(workspace)
    if not (workspace / MARKER).is_file():
        return {}
    found: dict[str, dict] = {}
    try:
        files = sorted((workspace / STATES).glob("*.json"))
    except OSError:
        return {}
    for file in files:
        state = _read(file)
        if not isinstance(state, dict):
            continue
        name = state.get("name") or file.stem
        # Where the capture is now, and where the state says it was made (the
        # workspace may have been moved).
        folders = {workspace / "captures" / name}
        if state.get("capture"):
            folders.add(Path(state["capture"]))
        # The rollout task folder the mirror was linked from.
        if state.get("source"):
            folders.add(Path(str(state["source"])))
        for demo, row in (state.get("demos") or {}).items():
            mark = row.get("excluded") if isinstance(row, dict) else None
            if not mark:
                continue
            info = {
                "workspace": str(workspace),
                "dataset": name,
                "demo": demo,
                "at": mark.get("at") if isinstance(mark, dict) else None,
                "reason": mark.get("reason") if isinstance(mark, dict) else None,
            }
            for folder in folders:
                found[str((folder / demo).resolve())] = info
    return found


def workspaces(scanned) -> list[Path]:
    """The workspaces to consult: those the last scan listed, the pool's, the
    live workspace the live page of this LEVI shows (``levi/live/locate.py``)
    and every one it showed before (``remembered``): a person removes
    episodes on the product LEVI's page too, and that must reach this pool
    even when the live workspace lies outside the pool roots (its states name
    the rollout folders they came from) or the page has since found another."""
    seen = {Path(w) for w in scanned or []}
    seen.add(settings.workspace())
    seen.update(w for w in remembered() if w.is_dir())
    found = _shown_now()
    if found is not None and Path(found) != settings.workspace():
        seen.add(Path(found))
        remember(found)
    return sorted(seen)


def current(scanned) -> dict[str, dict]:
    """Every removed episode's key over ``workspaces(scanned)`` (the scan
    summary's ``workspaces``)."""
    found: dict[str, dict] = {}
    for workspace in workspaces(scanned):
        for key, info in workspace_exclusions(workspace).items():
            found.setdefault(key, info)
    return found
