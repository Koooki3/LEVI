"""Human-requested local data management, separate from annotation writes.

Destructive operations use a preview fingerprint and a per-dataset lock.
The live catalogue builder and worker take the same lock, including when
the live workspace is being managed by the product's linked viewer.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

NAME = re.compile(r"^(?![.])[^/\\\x00-\x1f\x7f]{1,200}$")
ACTIVE = {"planned", "queued", "running", "stalled", "cancelling", "executing"}


class ManagementError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class Busy(ManagementError):
    def __init__(
        self, detail="The dataset is in use; try again after the task finishes"
    ):
        super().__init__(409, detail)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise Busy("A data or job record cannot be read; nothing was deleted") from exc


def confined(path: Path, root: Path) -> Path:
    """Require an existing physical path below its allowed root."""
    root = Path(root).absolute()
    path = Path(path).absolute()
    if not path.is_relative_to(root) or path == root:
        raise ManagementError(409, "The deletion target is outside its data folder")
    current = path
    while current != root:
        if current.is_symlink():
            raise ManagementError(409, "Deleting symbolic links is not supported")
        current = current.parent
    if root.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ManagementError(409, "The deletion target is outside its data folder")
    return path


@contextlib.contextmanager
def lock_for(workspace: Path, name: str, *, blocking: bool = True):
    """Yield whether the lock was acquired (False for a nonblocking miss).

    Lock order: this lock, then the live state/catalog lock. Live datasets
    use their source name, not the product's ``live.`` alias.
    """
    if not NAME.fullmatch(name):
        raise ManagementError(404, "Unknown dataset")
    workspace = Path(workspace)
    folder = (
        workspace / "live/datasets"
        if (workspace / "live").is_dir()
        else workspace / "outputs/LEVI/workbench/.management"
    )
    folder.mkdir(parents=True, exist_ok=True)
    path = confined(folder / f"{name}.catalogue.lock", workspace)
    with path.open("a") as handle:
        acquired = False
        try:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                acquired = True
            except BlockingIOError:
                pass
            yield acquired
        finally:
            if acquired:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _matches(value, names: set[str], roots: list[Path]) -> bool:
    if isinstance(value, dict):
        return any(_matches(v, names, roots) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_matches(v, names, roots) for v in value)
    if not isinstance(value, str):
        return False
    if value in names or value.removeprefix("local/") in names:
        return True
    if value.startswith("/"):
        p = Path(value)
        return any(
            p == root or p.is_relative_to(root) or root.is_relative_to(p)
            for root in roots
        )
    return False


def assert_idle(workspace: Path, names: set[str], roots: list[Path]):
    """Refuse any relevant live batch, session, or active local job.

    Only explicit job records and the runs table are read. No provider,
    connection credential or agent grant is opened.
    """
    workspace = Path(workspace)
    state = workspace / "outputs/LEVI/workbench"
    roots = [Path(p).absolute() for p in roots]
    for name in names:
        live = read_json(workspace / "live/datasets" / f"{name}.json", {})
        if isinstance(live, dict) and live.get("current"):
            raise Busy("The dataset is being labelled; try again after the batch")
    folders = [state / "jobs", workspace / "pool/jobs"]
    for parent in ("datasets", "recap_values", "segmentation"):
        for name in names:
            folders.append(state / parent / name / "jobs")
    for folder in folders:
        for path in folder.glob("*.json"):
            if path.name.endswith(".progress.json"):
                continue
            job = read_json(path, {})
            if not isinstance(job, dict) or job.get("status") not in ACTIVE:
                continue
            if _matches(job, names, roots) or folder not in folders[:2]:
                raise Busy(
                    "A conversion, export, segmentation or value job uses this dataset"
                )
    _assert_segmentation_idle(state, names)
    db_path = state / "agent/workbench.sqlite3"
    if db_path.is_file():
        try:
            with contextlib.closing(
                sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
            ) as db:
                records = db.execute("SELECT body FROM records WHERE kind='runs'")
                for (body,) in records:
                    run = json.loads(body)
                    if run.get("status") in ACTIVE and _matches(run, names, roots):
                        raise Busy("An annotation run uses this dataset")
        except (sqlite3.Error, ValueError) as exc:
            raise Busy(
                "Annotation jobs cannot be checked; nothing was deleted"
            ) from exc
    config_file = next(
        (
            p
            for p in (workspace / "live/effective.toml", workspace / "live.toml")
            if p.is_file()
        ),
        None,
    )
    if config_file:
        from .live import config, sessions

        c = config.load(config_file)
        for session in sessions.read_sessions(c.watch.roots).values():
            source = Path(session.root) / session.group / session.task_folder
            if session.raw_state not in {"stopped", "finished"} and _matches(
                str(source), names, roots
            ):
                value = read_json(Path(session.path), {})
                if sessions._alive(value.get("pid"), value.get("host")) is not False:
                    raise Busy("An evaluation session may still be using this dataset")


def _assert_segmentation_idle(state: Path, names: set[str]):
    """Live overlays are sessions, not jobs. Check memory and their durable plans."""
    from . import children
    from .segmentation import live

    for name in names:
        for session in live.sessions(name):
            if session.stopped_at is None or live.awaiting_publish(session):
                raise Busy("A live segmentation session uses this dataset")
    processes = read_json(state / "processes.json", [])
    if not isinstance(processes, list):
        raise Busy("The worker process records cannot be checked")
    for name in names:
        for plan in (state / "segmentation" / name / "live").glob("*/plan.json"):
            session_id = plan.parent.name
            groups = [
                p
                for p in processes
                if p.get("kind") == "segmentation-live" and p.get("label") == session_id
            ]
            for group in groups:
                if (
                    not isinstance(group.get("pid"), int)
                    or not group.get("identity")
                    or children._members(group)
                ):
                    raise Busy("A live segmentation worker uses this dataset")
            if any((plan.parent / "rows").rglob("*.jsonl")) or any(
                (plan.parent / "rows").rglob("*.parquet")
            ):
                raise Busy(
                    "Live segmentation results are still waiting to be published"
                )
            if not groups and not (plan.parent / "result.json").is_file():
                raise Busy("A live segmentation session has an uncertain state")


def fingerprint(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def file_stamp(path: Path):
    st = path.stat()
    return [str(path), st.st_size, st.st_mtime_ns, st.st_ino]


@dataclass
class Target:
    workspace: Path
    state: Path
    name: str
    item: dict
    root: Path
    browse: Path
    names: set[str]


def target(name: str) -> Target:
    from . import catalog, links, paths

    if not NAME.fullmatch(name):
        raise ManagementError(404, "Unknown local dataset")
    link = links.get(name)
    workspace = link.workspace if link else paths.ROOT
    state = link.state if link else catalog.STATE
    source = link.source if link else catalog.resolve_name("local/" + name)
    items = read_json(confined(state / "datasets.json", workspace), {})
    item = items.get(source)
    if not isinstance(item, dict):
        raise ManagementError(404, "Dataset is not registered")
    if item.get("base"):
        source = item["base"]
        item = items.get(source)
    if not isinstance(item, dict) or not item.get("path"):
        raise ManagementError(409, "The dataset has no local data folder")
    root = confined(Path(item["path"]), workspace)
    browse = confined(Path(item.get("view") or root), workspace)
    if not confined(browse / "meta/info.json", workspace).is_file():
        raise ManagementError(409, "The browsing view is still being prepared")
    names = {source} | {n for n, v in items.items() if v.get("base") == source}
    live_name = source
    for record in (workspace / "live/datasets").glob("*.json"):
        live = read_json(confined(record, workspace), {})
        if (
            isinstance(live, dict)
            and live.get("capture")
            and Path(live["capture"]).absolute() == root
        ):
            live_name = record.stem
            names.add(live_name)
            break
    return Target(workspace, state, live_name, item, root, browse, names)


def episode_rows(ds: Target) -> tuple[list[dict], list[Path]]:
    """Read authoritative IDs rather than assuming range(total_episodes)."""
    info = read_json(ds.browse / "meta/info.json", {})
    if str(info.get("codebase_version", "")).lstrip("v").startswith("3."):
        import pyarrow.parquet as pq

        files = sorted((ds.browse / "meta/episodes").glob("chunk-*/file-*.parquet"))
        rows = [
            row
            for path in files
            for row in pq.read_table(confined(path, ds.browse)).to_pylist()
        ]
    else:
        path = confined(ds.browse / "meta/episodes.jsonl", ds.workspace)
        files = [path] if path.is_file() else []
        if not files:
            raise ManagementError(
                409, "Episode metadata is required before deleting files"
            )
        try:
            rows = [
                json.loads(line)
                for line in path.read_text().splitlines()
                if line.strip()
            ]
        except (OSError, ValueError) as exc:
            raise ManagementError(409, "Episode metadata cannot be read") from exc
    if not all(
        isinstance(r.get("episode_index"), int) and r["episode_index"] >= 0
        for r in rows
    ):
        raise ManagementError(409, "Episode metadata contains invalid indices")
    if len({r["episode_index"] for r in rows}) != len(rows):
        raise ManagementError(409, "Episode metadata contains duplicate indices")
    return rows, files


def episodes(name: str, session: str | None = None) -> dict:
    ds = target(name)
    rows, _ = episode_rows(ds)
    live = read_json(ds.workspace / "live/datasets" / f"{ds.name}.json", {})
    known = live.get("demos") or {}
    by_index = {
        r.get("episode_index"): r for r in known.values() if not r.get("deleted")
    }
    indices = sorted(
        r["episode_index"]
        for r in rows
        if not session or by_index.get(r["episode_index"], {}).get("run_id") == session
    )
    return {
        "name": name,
        "indices": indices,
        "first_episode_index": indices[0] if indices else None,
        "session": session,
    }


def _format_path(
    ds: Target, template: str, row: dict, video_key: str | None = None
) -> Path:
    ep = row["episode_index"]
    info = read_json(ds.browse / "meta/info.json", {})
    values = {
        "episode_index": ep,
        "episode_chunk": ep // max(1, int(info.get("chunks_size") or 1000)),
        "chunk_index": row.get("data/chunk_index", 0),
        "file_index": row.get("data/file_index", 0),
        "video_key": video_key or "",
    }
    if video_key:
        values["chunk_index"] = row.get(
            f"videos/{video_key}/chunk_index", values["chunk_index"]
        )
        values["file_index"] = row.get(
            f"videos/{video_key}/file_index", values["file_index"]
        )
    try:
        return confined(ds.browse / template.format(**values), ds.browse)
    except (KeyError, ValueError) as exc:
        raise ManagementError(
            409, "The dataset declares an unsupported file path"
        ) from exc


def _raw_folders(ds: Target, selected: list[dict]) -> tuple[list[Path], list[str]]:
    if ds.item.get("kind") != "raw":
        return [], []
    live = read_json(ds.workspace / "live/datasets" / f"{ds.name}.json", {})
    known = live.get("demos") or {}
    folders, demos = [], []
    for row in selected:
        source_demo = row.get("source_demo")
        if not isinstance(source_demo, str) or not source_demo:
            raise ManagementError(409, "The source episode cannot be identified safely")
        raw = confined(ds.root / source_demo, ds.root)
        if raw.is_dir():
            folders.append(raw)
        if live:
            demo = Path(source_demo).name
            if (
                demo not in known
                or known[demo].get("episode_index") != row["episode_index"]
            ):
                raise ManagementError(
                    409, "The live episode mapping changed; reload the dataset"
                )
            demos.append(demo)
            source = Path(live.get("source") or "")
            configured_root = Path(live.get("root") or "")
            from .live import config

            config_file = next(
                (
                    p
                    for p in (
                        ds.workspace / "live/effective.toml",
                        ds.workspace / "live.toml",
                    )
                    if p.is_file()
                ),
                None,
            )
            if not config_file:
                raise ManagementError(409, "The rollout source cannot be verified")
            allowed = config.load(config_file).watch.roots
            if str(configured_root.resolve()) not in {
                str(Path(p).expanduser().resolve()) for p in allowed
            }:
                raise ManagementError(
                    409, "The rollout source is outside the configured roots"
                )
            expected = (
                configured_root / str(live.get("group")) / str(live.get("task_folder"))
            )
            if source.absolute() != expected.absolute():
                raise ManagementError(
                    409, "The rollout source does not match its recorded task"
                )
            original = confined(source / demo, configured_root)
            if original.is_dir() and original != raw:
                folders.append(original)
    return list(dict.fromkeys(folders)), demos


def _tree_files(folders: list[Path]) -> list[Path]:
    files = []
    for folder in folders:
        for p in folder.rglob("*"):
            confined(p, folder)
            if p.is_file():
                files.append(p)
    return files


def _protect(paths_to_remove: list[Path]):
    """Respect explicit frozen-data manifests, without reading gold labels."""
    from .pool import heldout, settings

    try:
        protected = heldout.load(settings.heldout_files())
    except ValueError as exc:
        raise ManagementError(
            409, "The frozen-data protection list cannot be checked"
        ) from exc
    for item in protected:
        p = Path(item["path"]).expanduser()
        candidates = (
            [p] if p.is_absolute() else [root / p for root in settings.pool_roots()]
        )
        if any(
            p == q or p.is_relative_to(q) or q.is_relative_to(p)
            for p in candidates
            for q in paths_to_remove
        ):
            raise ManagementError(409, "Frozen test data is protected from deletion")


def _snapshot(ds: Target, indices: list[int]) -> dict:
    if not indices or any(
        isinstance(i, bool) or not isinstance(i, int) or i < 0 for i in indices
    ):
        raise ManagementError(400, "Select at least one valid episode")
    requested = sorted(set(indices))
    rows, metadata_files = episode_rows(ds)
    selected = [r for r in rows if r["episode_index"] in requested]
    if len(selected) != len(requested):
        raise ManagementError(404, "One or more selected episodes no longer exist")
    folders, demos = _raw_folders(ds, selected)
    info = read_json(ds.browse / "meta/info.json", {})
    remaining = [r for r in rows if r["episode_index"] not in requested]
    files = _tree_files(folders)

    def episode_files(row):
        out = [_format_path(ds, info["data_path"], row)]
        for key, feature in (info.get("features") or {}).items():
            if feature.get("dtype") == "video":
                out.append(_format_path(ds, info["video_path"], row, key))
        return out

    retained = {p for row in remaining for p in episode_files(row)}
    shared = set()
    for row in selected:
        for path in episode_files(row):
            if path in retained:
                shared.add(path)
            else:
                files.append(path)
    files = sorted({p for p in files if p.is_file()})
    _protect([*folders, *files])
    for name in ds.names:
        for parent in (
            ds.state / "annotations" / name,
            ds.state / "datasets" / name / "annotations",
        ):
            for ep in requested:
                for path in (
                    parent / f"episode_{ep:06d}.json",
                    parent / "outcomes" / f"episode_{ep:06d}.json",
                ):
                    if path.is_file():
                        files.append(confined(path, ds.workspace))
    metadata_files = [ds.browse / "meta/info.json", *metadata_files]
    for filename in ("episodes_stats.jsonl", "levi_provenance.jsonl", "levi_view.json"):
        path = ds.browse / "meta" / filename
        if path.is_file():
            metadata_files.append(confined(path, ds.browse))
    live_path = confined(
        ds.workspace / "live/datasets" / f"{ds.name}.json", ds.workspace
    )
    if live_path.is_file():
        metadata_files.append(live_path)
    assert_idle(ds.workspace, ds.names, [ds.root, ds.browse, *folders])
    from . import paths

    if paths.ROOT != ds.workspace:
        assert_idle(
            paths.ROOT, {"live." + n for n in ds.names}, [ds.root, ds.browse, *folders]
        )
    if ds.item.get("kind") == "raw" and folders:
        from .live import config, management

        config_file = next(
            (
                p
                for p in (
                    ds.workspace / "live/effective.toml",
                    ds.workspace / "live.toml",
                )
                if p.is_file()
            ),
            None,
        )
        if config_file:
            management._assert_quiet_source(
                Path(read_json(live_path, {}).get("source") or ds.root),
                config.load(config_file),
            )
    confirmation = fingerprint(
        {
            "name": ds.name,
            "indices": requested,
            "files": [file_stamp(p) for p in files],
            "meta": [file_stamp(p) for p in metadata_files],
            "folders": [file_stamp(p) for p in folders],
            "shared": [file_stamp(p) for p in sorted(shared) if p.is_file()],
        }
    )
    return {
        "confirmation": confirmation,
        "episodes": requested,
        "files": len(files),
        "bytes": sum(p.stat().st_size for p in files),
        "paths": [str(p) for p in files[:50]],
        "source_files_included": bool(folders),
        "shared_files_retained": len(shared),
        "_files": files,
        "_folders": folders,
        "_demos": demos,
        "_remaining": remaining,
        "_metadata": metadata_files,
        "_info": info,
    }


def _public(snapshot: dict) -> dict:
    return {k: v for k, v in snapshot.items() if not k.startswith("_")}


def deletion_plan(name: str, indices: list[int]) -> dict:
    ds = target(name)
    with lock_for(ds.workspace, ds.name, blocking=False) as acquired:
        if not acquired:
            raise Busy()
        return _public(_snapshot(ds, indices))


def _jsonl_bytes(rows: list[dict]) -> bytes:
    return (
        "".join(json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n" for r in rows)
    ).encode()


@contextlib.contextmanager
def catalog_lock(ds: Target):
    from . import catalog

    confined(ds.state, ds.workspace)
    confined(ds.state / "datasets.json", ds.workspace)
    confined(ds.state / ".catalog.lock", ds.workspace)
    if ds.state == catalog.STATE:
        with catalog.locked():
            yield
    else:
        with confined(ds.state / ".catalog.lock", ds.workspace).open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def audit(workspace: Path, record: dict):
    """Audit in the workspace acted on; failure never reverses a committed action."""
    from .live import jsonio

    path = (
        workspace / "live/audit.jsonl"
        if (workspace / "live").is_dir()
        else workspace / "outputs/LEVI/workbench/management.jsonl"
    )
    try:
        jsonio.append_line(
            confined(path, workspace),
            {"time": time.time(), "principal": "person", **record},
        )
    except Exception:
        logging.getLogger(__name__).exception("Cannot append data-management audit")


def delete_episodes(name: str, indices: list[int], confirmation: str) -> dict:
    from .live import jsonio

    ds = target(name)
    live_path = confined(
        ds.workspace / "live/datasets" / f"{ds.name}.json", ds.workspace
    )
    confined(live_path.with_name(live_path.name + ".lock"), ds.workspace)
    with contextlib.ExitStack() as stack:
        if not stack.enter_context(lock_for(ds.workspace, ds.name, blocking=False)):
            raise Busy()
        stack.enter_context(catalog_lock(ds))
        if live_path.is_file():
            stack.enter_context(jsonio.locked(live_path))
        snapshot = _snapshot(ds, indices)
        if not confirmation or confirmation != snapshot["confirmation"]:
            raise ManagementError(
                409, "The dataset changed after the preview; preview the deletion again"
            )
        remaining = snapshot["_remaining"]
        info = dict(snapshot["_info"])
        info.update(
            total_episodes=len(remaining),
            total_frames=sum(int(r.get("length") or 0) for r in remaining),
            levi_episode_indices=[r["episode_index"] for r in remaining],
        )
        encode = lambda value: json.dumps(
            value, ensure_ascii=False, indent=2, allow_nan=False
        ).encode()
        replacements = {ds.browse / "meta/info.json": encode(info)}
        v3 = str(info.get("codebase_version", "")).lstrip("v").startswith("3.")
        if v3:
            import pyarrow as pa
            import pyarrow.compute as pc
            import pyarrow.parquet as pq

            for path in snapshot["_metadata"]:
                if path.suffix == ".parquet":
                    table = pq.read_table(path)
                    keep = pc.invert(
                        pc.is_in(
                            table["episode_index"],
                            value_set=pa.array(
                                snapshot["episodes"], type=table["episode_index"].type
                            ),
                        )
                    )
                    sink = pa.BufferOutputStream()
                    pq.write_table(table.filter(keep), sink)
                    replacements[path] = sink.getvalue().to_pybytes()
        else:
            replacements[ds.browse / "meta/episodes.jsonl"] = _jsonl_bytes(remaining)
        for path in snapshot["_metadata"]:
            if path.suffix == ".jsonl" and path.name != "episodes.jsonl":
                rows = [
                    json.loads(line)
                    for line in path.read_text().splitlines()
                    if line.strip()
                ]
                replacements[path] = _jsonl_bytes(
                    [
                        r
                        for r in rows
                        if r.get("episode_index") not in snapshot["episodes"]
                    ]
                )
        if live_path.is_file():
            state = read_json(live_path, {})
            for demo in snapshot["_demos"]:
                state["demos"][demo].update(
                    deleted={"at": time.time(), "by": "person"},
                    excluded={
                        "at": time.time(),
                        "by": "person",
                        "reason": "local files deleted",
                    },
                )
                state["demos"][demo].pop("episode_index", None)
            state.setdefault("catalogue", {}).update(
                first_episode_index=min(
                    (r["episode_index"] for r in remaining), default=None
                ),
                updated_at=time.time(),
            )
            replacements[live_path] = encode(state)
        items = read_json(ds.state / "datasets.json", {})
        for key in ds.names:
            if key in items:
                items[key]["info"] = info
        replacements[ds.state / "datasets.json"] = encode(items)
        backups, created = [], []
        stamp = uuid.uuid4().hex
        try:
            # Whole episode folders are staged outside the capture. Neither a
            # scanner nor the source fingerprint can mistake backups for data.
            for n, folder in enumerate(snapshot["_folders"]):
                scope = (
                    ds.root
                    if folder.is_relative_to(ds.root)
                    else Path(read_json(live_path, {})["source"])
                )
                backup = scope.parent / f".levi-delete-{stamp}-{n}"
                os.replace(folder, backup)
                backups.append((folder, backup))
            for n, path in enumerate(snapshot["_files"]):
                if any(path.is_relative_to(folder) for folder in snapshot["_folders"]):
                    continue
                backup = path.with_name(f".levi-delete-{stamp}-file-{n}")
                os.replace(path, backup)
                backups.append((path, backup))
            view_meta = ds.browse / "meta/levi_view.json"
            if ds.item.get("kind") == "raw" and view_meta.is_file():
                from .conversion.engine import fingerprint as capture_fingerprint

                meta = read_json(view_meta, {})
                meta["source_fingerprint"] = capture_fingerprint(ds.root)
                replacements[view_meta] = encode(meta)
            for n, (path, content) in enumerate(replacements.items()):
                if path.exists():
                    backup = path.with_name(f".levi-delete-{stamp}-meta-{n}")
                    os.replace(path, backup)
                    backups.append((path, backup))
                created.append(path)
                path.write_bytes(content)
        except Exception:
            for path in reversed(created):
                path.unlink(missing_ok=True)
            for path, backup in reversed(backups):
                os.replace(backup, path)
            raise
        cleanup_pending = []
        for _path, backup in backups:
            try:
                if backup.is_dir():
                    shutil.rmtree(backup)
                else:
                    backup.unlink(missing_ok=True)
            except OSError:
                cleanup_pending.append(str(backup))
        audit(
            ds.workspace,
            {
                "tool": "episode.delete_files",
                "dataset": ds.name,
                "episodes": snapshot["episodes"],
                "cleanup_pending": cleanup_pending,
            },
        )
        return {
            "deleted": snapshot["episodes"],
            "remaining": len(remaining),
            "first_episode_index": min(
                (r["episode_index"] for r in remaining), default=None
            ),
            "repo_id": "local/" + name,
            "shared_files_retained": snapshot["shared_files_retained"],
            "cleanup_pending": bool(cleanup_pending),
        }
