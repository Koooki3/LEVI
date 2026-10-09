"""Human-requested removal of finished evaluation sessions and live pipelines.

Sessions are hidden by a local tombstone in ``live/management.json``; their
robot-side JSON is never changed. A fresh session id or a changed session
record is visible again. Pipelines keep an ``archived`` state file so a
scanner cannot immediately discover them again. Deleting pipeline files
only unlinks its workspace capture and browsing view, never the rollout.

Each removal requires the fingerprint of a fresh preview. The catalogue
builder, worker and remover share the dataset management lock; state writes
also use the normal JSON lock. Directories are renamed to a staging folder
before committing the archive, and restored if committing fails.
"""

from __future__ import annotations

import contextlib
import fcntl
import hmac
import os
import stat
import time
import uuid
from pathlib import Path

from .. import dataset_management as data_management
from . import criteria, jsonio, locate, mirror, sessions

SCHEMA = "levi.live.management.v1"
FILE = "management.json"
TERMINAL = {"stopped", "finished"}


def _workspace(config) -> Path:
    root = config.workspace.absolute()
    if root.is_symlink():
        raise data_management.ManagementError(
            409, "The live workspace is a symbolic link"
        )
    root = root.resolve()
    if locate.refused(root):
        raise data_management.ManagementError(
            409, "The product workspace cannot be managed as a live workspace"
        )
    for path in (root / "live", root / "live/datasets", root / "live" / FILE):
        data_management.confined(path, root)
    return root


def _component(value, label) -> str:
    if not isinstance(value, str) or not data_management.NAME.fullmatch(value):
        raise data_management.ManagementError(400, f"Invalid {label}")
    return value


def _root(config, value) -> Path:
    if not isinstance(value, str) or not value:
        raise data_management.ManagementError(400, "An evaluation root is required")
    root = Path(value).expanduser().absolute().resolve()
    allowed = {Path(r).expanduser().absolute().resolve() for r in config.watch.roots}
    if root not in allowed:
        raise data_management.ManagementError(404, "Unknown evaluation root")
    return root


def is_archived(state) -> bool:
    return isinstance(state, dict) and bool(state.get("archived"))


def archived(config, name) -> bool:
    """Read-only archive check for API, catalogue and statistics readers."""
    if not isinstance(name, str) or not data_management.NAME.fullmatch(name):
        return False
    return is_archived(mirror.load_state(config, name))


def removed_sessions(config) -> list:
    """Local tombstones, for session lists and historical statistics."""
    value = jsonio.read(config.live_dir / FILE, {})
    return list(value.get("sessions") or []) if isinstance(value, dict) else []


def session_deleted(config, session: sessions.Session) -> bool:
    """Hide an unchanged deleted record; a resumed or new session is visible."""
    candidates = [
        row
        for row in removed_sessions(config)
        if isinstance(row, dict)
        and mirror.same_root(row.get("root"), session.root)
        and row.get("group") == session.group
        and row.get("task_folder") == session.task_folder
        and row.get("session_id", "") == session.session_id
        and row.get("deleted_updated_at") == session.updated_at
    ]
    if not candidates:
        return False
    value = jsonio.read(session.path)
    if not isinstance(value, dict):
        return False
    revision = data_management.fingerprint(value)
    return any(row.get("source_revision") == revision for row in candidates)


def _session(config, identity):
    if not isinstance(identity, dict):
        raise data_management.ManagementError(400, "An evaluation session is required")
    root = _root(config, identity.get("root"))
    group = _component(identity.get("group"), "evaluation group")
    task = _component(identity.get("task_folder"), "task folder")
    path = data_management.confined(
        root / sessions.SESSION_DIR / f"{group}__{task}.json", root
    )
    value = data_management.read_json(path)
    if not isinstance(value, dict):
        raise data_management.ManagementError(404, "Unknown evaluation session")
    session = sessions._session(str(path), path.name, value, time.time())
    if session is None or session.group != group or session.task_folder != task:
        raise data_management.ManagementError(
            409, "The session identity changed; reload the list"
        )
    session.root = str(root)
    session_id = identity.get("session_id") or ""
    if not isinstance(session_id, str) or session_id != session.session_id:
        raise data_management.ManagementError(
            409, "A new evaluation session replaced this one"
        )
    # Old clients have no session_id. If the caller supplied its update time,
    # it must still identify the same record; the confirmation handles callers
    # with an older public Session shape that lacks updated_at.
    if (
        "updated_at" in identity
        and sessions.parse_time(identity["updated_at"]) != session.updated_at
    ):
        raise data_management.ManagementError(
            409, "The evaluation session changed; reload the list"
        )
    return session, value


def _assert_finished(session, value):
    if session.raw_state in TERMINAL:
        return
    # Being merely non-inferencing (standby, fault) is insufficient: the
    # client can still collect another episode. Unknown remote/missing PIDs
    # also cannot prove that the evaluation has ended.
    if sessions._alive(value.get("pid"), value.get("host")) is not False:
        raise data_management.Busy("The evaluation client may still be running")


def _related_states(config, session) -> dict:
    return {
        name: state
        for name, state in mirror.list_states(config, include_archived=True).items()
        if state.get("group") == session.group
        and state.get("task_folder") == session.task_folder
        and mirror.same_root(state.get("root"), session.root)
    }


def _session_names(config, session):
    # Even before its first state file exists, the builder of this session's
    # pipeline takes this name's management lock. A session removal must not
    # miss a view build that starts just after looking for related states.
    return {
        *(_related_states(config, session)),
        mirror.resolve_name(config, session.root, session.group, session.task_folder),
    }


@contextlib.contextmanager
def _dataset_locks(workspace, names):
    with contextlib.ExitStack() as stack:
        for name in sorted(names):
            if not stack.enter_context(
                data_management.lock_for(workspace, name, blocking=False)
            ):
                raise data_management.Busy(
                    "The dataset is being updated; try again after the task finishes"
                )
        yield


def _session_snapshot(config, identity):
    session, value = _session(config, identity)
    _assert_finished(session, value)
    related = _related_states(config, session)
    names = _session_names(config, session)
    source = data_management.confined(
        Path(session.root) / session.group / session.task_folder, Path(session.root)
    )
    workspace = _workspace(config)
    data_management.assert_idle(
        workspace, names, [source, *[workspace / "captures" / name for name in names]]
    )
    source_manifest = _assert_quiet_source(source, config)
    scope = {
        "root": session.root,
        "group": session.group,
        "task_folder": session.task_folder,
        "session_id": session.session_id,
    }
    plan = {
        "kind": "session",
        "identity": scope,
        "files": 0,
        "bytes": 0,
        "paths": [],
        "datasets": sorted(names),
        "source_retained": True,
        "delete_files": False,
        "already_deleted": session_deleted(config, session),
    }
    plan["confirmation"] = data_management.fingerprint(
        {"scope": scope, "source": value, "related": related, "files": source_manifest}
    )
    return plan, session, value


def session_plan(config, identity: dict) -> dict:
    """Preview a record-only deletion, refusing a live or uncertain client."""
    workspace = _workspace(config)
    session, _ = _session(config, identity)
    names = _session_names(config, session)
    with _dataset_locks(workspace, names):
        return _session_snapshot(config, identity)[0]


def _confirm(actual, supplied):
    if not isinstance(supplied, str) or not supplied:
        raise data_management.ManagementError(
            400, "A deletion preview confirmation is required"
        )
    if not hmac.compare_digest(actual, supplied):
        raise data_management.ManagementError(
            409, "Data changed since the deletion preview; preview it again"
        )


def delete_session(config, identity: dict, confirmation: str) -> dict:
    """Atomically add a local tombstone without writing a robot-side file."""
    workspace = _workspace(config)
    session, _ = _session(config, identity)
    names = _session_names(config, session)
    path = data_management.confined(workspace / "live" / FILE, workspace)
    data_management.confined(path.with_name(path.name + ".lock"), workspace)
    with _dataset_locks(workspace, names), jsonio.locked(path):
        plan, session, source = _session_snapshot(config, identity)
        if set(plan["datasets"]) != names:
            raise data_management.ManagementError(
                409, "The pipeline changed; preview the deletion again"
            )
        _confirm(plan["confirmation"], confirmation)
        value = data_management.read_json(path, {"schema": SCHEMA, "sessions": []})
        if not isinstance(value, dict) or not isinstance(
            value.get("sessions", []), list
        ):
            raise data_management.Busy("The session removal records cannot be read")
        tombstone = {
            **plan["identity"],
            "run_id": session.run_id,
            "deleted_updated_at": session.updated_at,
            "source_revision": data_management.fingerprint(source),
            "at": time.time(),
            "by": "person",
        }
        if not plan["already_deleted"]:
            value.setdefault("sessions", []).append(tombstone)
            value["schema"] = SCHEMA
            jsonio.write(path, value, indent=1)
    return {**plan, "deleted": True}


def _assert_quiet_source(source: Path, config):
    """A fresh unclosed capture can still be written without a session file."""
    if not source.exists():
        return []
    quiet_s = max(60.0, float(config.watch.settle_s))
    now = time.time()
    manifest = [data_management.file_stamp(source)]
    try:
        entries = list(os.scandir(source))
        for entry in entries:
            if criteria.kind_of(entry.name) not in {"demo", "incomplete"}:
                continue
            if entry.is_symlink():
                raise data_management.ManagementError(
                    409, "A capture contains a symbolic link"
                )
            if not entry.is_dir(follow_symlinks=False):
                continue
            demo = Path(entry.path)
            children = list(os.scandir(demo))
            manifest.append(data_management.file_stamp(demo))
            manifest.extend(
                [
                    [
                        item.path,
                        item.stat(follow_symlinks=False).st_size,
                        item.stat(follow_symlinks=False).st_mtime_ns,
                        item.stat(follow_symlinks=False).st_ino,
                    ]
                    for item in children
                ]
            )
            newest = max(
                [
                    entry.stat(follow_symlinks=False).st_mtime,
                    *[item.stat(follow_symlinks=False).st_mtime for item in children],
                ]
            )
            if now - newest >= quiet_s or any(
                item.name == ".complete" for item in children
            ):
                continue
            raw = any(item.name.endswith("_raw.avi") for item in children)
            meta_path = demo / "metadata.json"
            if meta_path.is_symlink():
                raise data_management.ManagementError(
                    409, "A capture contains a symbolic link"
                )
            metadata = jsonio.read(meta_path) or {}
            stopped = isinstance(metadata, dict) and bool(metadata.get("stopped_at"))
            if raw or not stopped:
                raise data_management.Busy(
                    "A rollout is still being written; wait for it to finish"
                )
    except FileNotFoundError as exc:
        raise data_management.Busy(
            "The rollout files changed; preview the deletion again"
        ) from exc
    return sorted(manifest)


@contextlib.contextmanager
def _catalog_lock(workspace):
    folder = data_management.confined(workspace / "outputs/LEVI/workbench", workspace)
    lock = data_management.confined(folder / ".catalog.lock", workspace)
    folder.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _targets(workspace, state, name, catalogue):
    capture = workspace / "captures" / name
    views = workspace / "outputs/LEVI/workbench/views"
    view = views / name
    recorded_capture = Path(state.get("capture") or capture)
    data_management.confined(recorded_capture, workspace)
    if recorded_capture.resolve() != capture.resolve():
        raise data_management.ManagementError(
            409, "The pipeline capture is outside its data folder"
        )
    for key, entry in catalogue.items():
        if not isinstance(entry, dict):
            continue
        related = (
            key == name
            or entry.get("name") == name
            or entry.get("path") == str(capture)
            or entry.get("base") == name
        )
        if (
            related
            and entry.get("path")
            and Path(entry["path"]).resolve() != capture.resolve()
        ):
            raise data_management.ManagementError(
                409, "The pipeline catalogue entry belongs to another capture"
            )
        if related and entry.get("view"):
            recorded_view = Path(entry["view"])
            data_management.confined(recorded_view, workspace)
            if recorded_view.resolve() != view.resolve():
                raise data_management.ManagementError(
                    409, "The pipeline view is outside its data folder"
                )
    return [
        data_management.confined(capture, workspace),
        data_management.confined(view, workspace),
    ]


def _tree(paths):
    """Stat-only manifest, refusing links and special files before staging."""
    rows = []
    for target in paths:
        if not target.exists():
            rows.append([str(target), None])
            continue
        if not target.is_dir():
            raise data_management.ManagementError(
                409, "The pipeline data is not a directory"
            )
        for folder, dirs, files in os.walk(target, followlinks=False):
            parent = Path(folder)
            for item in [parent, *[parent / p for p in sorted(dirs + files)]]:
                st = item.lstat()
                if stat.S_ISLNK(st.st_mode) or not (
                    stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)
                ):
                    raise data_management.ManagementError(
                        409, "A pipeline file is a symbolic link or special file"
                    )
                rows.append(
                    [str(item), st.st_size, st.st_mtime_ns, st.st_ino, st.st_mode]
                )
    # Each directory occurs once as a child and once as the next parent;
    # de-duplicate to keep a stable manifest and count files exactly once.
    return sorted({row[0]: row for row in rows}.values())


def _source(config, state) -> Path:
    source = Path(state.get("source") or "")
    root = _root(config, state.get("root") or str(source.parent.parent))
    group = _component(state.get("group"), "evaluation group")
    task = _component(state.get("task_folder"), "task folder")
    expected = root / group / task
    data_management.confined(source, root)
    if source.resolve() != expected.resolve():
        raise data_management.ManagementError(
            409, "The pipeline source does not match its evaluation root"
        )
    return expected


def _dataset_snapshot(config, name, delete_files):
    workspace = _workspace(config)
    path = data_management.confined(mirror.state_path(config, name), workspace)
    state = data_management.read_json(path)
    if not isinstance(state, dict) or state.get("name") != name or is_archived(state):
        raise data_management.ManagementError(404, "Unknown live pipeline")
    source = _source(config, state)
    catalogue_path = data_management.confined(
        workspace / "outputs/LEVI/workbench/datasets.json", workspace
    )
    catalogue = data_management.read_json(catalogue_path, {})
    if not isinstance(catalogue, dict):
        raise data_management.Busy("The dataset catalogue cannot be checked")
    targets = _targets(workspace, state, name, catalogue)
    paths = {str(p) for p in targets}
    entries = {
        key: item
        for key, item in catalogue.items()
        if isinstance(item, dict)
        and (
            key == name
            or item.get("base") == name
            or item.get("path") in paths
            or item.get("view") in paths
        )
    }
    names = {name, *entries}
    data_management.assert_idle(workspace, names, [source, *targets])
    live_sessions = []
    identity = {
        "root": str(source.parent.parent),
        "group": state["group"],
        "task_folder": state["task_folder"],
    }
    session_path = (
        source.parent.parent
        / sessions.SESSION_DIR
        / f"{state['group']}__{state['task_folder']}.json"
    )
    if session_path.exists() or session_path.is_symlink():
        data_management.confined(session_path, source.parent.parent)
        value = data_management.read_json(session_path)
        if not isinstance(value, dict):
            raise data_management.Busy("The evaluation session cannot be checked")
        identity["session_id"] = str(value.get("session_id") or "")
        session, value = _session(config, identity)
        _assert_finished(session, value)
        live_sessions.append(value)
    source_manifest = _assert_quiet_source(source, config)
    manifest = _tree(targets) if delete_files else []
    files = [row for row in manifest if len(row) > 2 and stat.S_ISREG(row[4])]
    plan = {
        "kind": "dataset",
        "name": name,
        "delete_files": bool(delete_files),
        "files": len(files),
        "bytes": sum(row[1] for row in files),
        "paths": [str(p) for p in targets if p.exists()] if delete_files else [],
        "datasets": sorted(names),
        "source_retained": True,
        "source_files": 0,
        "annotations_retained": True,
    }
    plan["confirmation"] = data_management.fingerprint(
        {
            "name": name,
            "delete_files": bool(delete_files),
            "state": state,
            "catalogue": entries,
            "sessions": live_sessions,
            "manifest": manifest,
            "source_files": source_manifest,
        }
    )
    return plan, state, catalogue, entries, targets, catalogue_path


def dataset_plan(config, name: str, delete_files=False) -> dict:
    """Preview a pipeline archive and, optionally, its workspace files."""
    name = _component(name, "pipeline name")
    workspace = _workspace(config)
    state = data_management.confined(mirror.state_path(config, name), workspace)
    data_management.confined(state.with_name(state.name + ".lock"), workspace)
    with (
        _dataset_locks(workspace, {name}),
        _catalog_lock(workspace),
        jsonio.locked(state),
    ):
        return _dataset_snapshot(config, name, delete_files)[0]


def delete_dataset(config, name: str, confirmation: str, delete_files=False) -> dict:
    """Archive a stopped pipeline; optionally unlink only its mirror/view."""
    import shutil

    name = _component(name, "pipeline name")
    workspace = _workspace(config)
    path = data_management.confined(mirror.state_path(config, name), workspace)
    data_management.confined(path.with_name(path.name + ".lock"), workspace)
    staging = workspace / "live" / f".delete-{name}-{uuid.uuid4().hex}"
    staged = []
    with (
        _dataset_locks(workspace, {name}),
        _catalog_lock(workspace),
        jsonio.locked(path),
    ):
        plan, state, catalogue, entries, targets, catalogue_path = _dataset_snapshot(
            config, name, delete_files
        )
        _confirm(plan["confirmation"], confirmation)
        try:
            if delete_files:
                data_management.confined(staging, workspace).mkdir()
                for i, target in enumerate(targets):
                    if target.exists():
                        destination = staging / str(i)
                        os.rename(target, destination)
                        staged.append((target, destination))
            archived_state = {
                **state,
                "archived": True,
                "archived_at": time.time(),
                "archived_by": "person",
                "archived_delete_files": bool(delete_files),
            }
            # Commit the tombstone before unlinking staged files. The source
            # state stays in place, preventing both scanning and registration.
            jsonio.write(path, archived_state, indent=1)
            if entries:
                jsonio.write(
                    catalogue_path,
                    {
                        key: value
                        for key, value in catalogue.items()
                        if key not in entries
                    },
                    indent=2,
                )
        except Exception:
            # No physical unlink has happened yet. Restore files and each
            # original record before allowing another catalogue build.
            for target, destination in reversed(staged):
                os.rename(destination, target)
            jsonio.write(path, state, indent=1)
            if entries:
                jsonio.write(catalogue_path, catalogue, indent=2)
            if staging.exists():
                shutil.rmtree(staging)
            raise
    cleanup_pending = False
    if staging.exists():
        try:
            shutil.rmtree(staging)
        except OSError:
            # The archive is committed. A cleanup failure leaves an isolated
            # staging folder instead of reporting a misleading failed delete.
            cleanup_pending = True
    return {**plan, "deleted": True, "cleanup_pending": cleanup_pending}
