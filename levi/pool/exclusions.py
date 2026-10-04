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

Nothing here writes.
"""

import json
from pathlib import Path

from . import settings

LIVE = Path("live")
STATES = LIVE / "datasets"
MARKER = LIVE / "workspace.json"


def _read(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


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
    """The workspaces to consult: those the last scan listed, the pool's, and
    the live workspace the live page of this LEVI shows (``levi/live/
    locate.py``): a person removes episodes on the product LEVI's page too,
    and that must reach this pool even when the live workspace lies outside
    the pool roots (its states name the rollout folders they came from)."""
    seen = {Path(w) for w in scanned or []}
    seen.add(settings.workspace())
    try:
        from levi.live import locate

        found = locate.find(settings.workspace()).workspace
    except Exception:  # noqa: BLE001 - never let the live side break the pool
        found = None
    if found is not None:
        seen.add(Path(found))
    return sorted(seen)


def current(scanned) -> dict[str, dict]:
    """Every removed episode's key over ``workspaces(scanned)`` (the scan
    summary's ``workspaces``)."""
    found: dict[str, dict] = {}
    for workspace in workspaces(scanned):
        for key, info in workspace_exclusions(workspace).items():
            found.setdefault(key, info)
    return found
