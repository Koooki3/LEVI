"""A provenance catalog over existing evaluation reports; no statistics or raw-data writes."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

SCHEMA = "levi.evaluation.results.v1"
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,191}$")
ANALYZED = {"ANALYZING", "REPORTED"}


class CatalogError(ValueError):
    """Invalid identity, incomplete publication or unsafe path."""


def _name(value: str) -> str:
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise CatalogError("Identity must be one explicit filename-safe component")
    return value


def _safe(path: Path) -> Path:
    path = Path(os.path.abspath(path.expanduser()))
    for parent in (*reversed(path.parents), path):
        if parent.is_symlink():
            raise CatalogError("Catalog and source paths must not traverse symlinks")
    return path


def _json(value) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode()


def _read(path: Path) -> bytes:
    path = _safe(path)
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > 100 * 1024 * 1024:
            raise CatalogError("Artifact exceeds the catalog hashing limit")
        raw = stream.read()
        after = os.fstat(stream.fileno())
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise CatalogError(
            "Source changed during hashing; retry after report publication"
        )
    return raw


def _atomic(path: Path, raw: bytes) -> None:
    _safe(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".catalog-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def entries(root: Path) -> list[dict]:
    root = _safe(root)
    rows = []
    for path in sorted(root.glob("models/*/*/sessions/*/reference.json")):
        value = json.loads(_read(path))
        if value.get("schema") == SCHEMA:
            rows.append(value)
    return rows


def _index(root: Path) -> None:
    rows = entries(root)
    _atomic(root / "index.json", _json({"schema": SCHEMA, "entries": rows}))
    lines = [
        "# 评测结果 / Evaluation results",
        "",
        "按模型、任务和会话查阅。数字、图表与结论以来源报告为准。",
        "Browse by model, task and session. Statistics remain in the source report.",
        "",
        "| 模型 / Model | 任务 / Task | 会话 / Session | 标签依据 / Basis | 状态 / State | 报告 / Report |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        source = row["report"]["directory"]
        label = "进度 / Progress" if row["progress_only"] else "报告 / Report"
        lines.append(
            f"| {row['model']} | {row['task']} | {row['session']} | "
            f"{row['basis']} | {row['campaign_state']} | [{label}](<{source}>) |"
        )
    progress = []
    for path in sorted(root.glob("campaigns/*/*/reference.json")):
        row = json.loads(_read(path))
        if row.get("progress_only") is True:
            progress.append(row)
    if progress:
        lines += ["", "## 进行中 / In progress", ""]
        for row in progress:
            directory = row["report"]["directory"]
            lines.append(
                f"- {row['campaign_id']} / {row['basis']}: [进度 / Progress](<{directory}>)"
            )
    _atomic(root / "INDEX.md", ("\n".join(lines) + "\n").encode())


def register(
    root: Path,
    *,
    report: Path,
    rollout_root: Path,
    rollout_dir: Path,
    model: str,
    task: str,
    session: str,
) -> dict:
    """Publish a model/task/session reference to a fully published source manifest.

    report is one existing label-basis report, not a campaign folder. Caller
    supplies the exact model/task mapping; checkpoint paths are never guessed.
    Only the derived catalog is written. Source report and rollout stay intact.
    """
    root, report = _safe(root), _safe(report)
    rollout_root, rollout_dir = _safe(rollout_root), _safe(rollout_dir)
    model, task, session = map(_name, (model, task, session))
    if not rollout_root.is_dir() or not rollout_dir.is_dir():
        raise CatalogError("Existing rollout root and model/task folder are required")
    if rollout_dir != rollout_root / model / task:
        raise CatalogError("Rollout folder must exactly match root/model/task")
    if (
        root == report
        or root.is_relative_to(report)
        or root.is_relative_to(rollout_root)
        or report.is_relative_to(root)
        or rollout_root.is_relative_to(root)
    ):
        raise CatalogError("Catalog must be separate from reports and rollout sources")
    manifest_path = report / "manifest.json"
    raw = _read(manifest_path)
    manifest = json.loads(raw)
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != "levi.aeri.campaign_report_manifest.v1"
    ):
        raise CatalogError("Expected an existing campaign report manifest")
    campaign = _name(manifest.get("campaign_id"))
    basis = _name(manifest.get("basis"))
    state = manifest.get("campaign_state")
    if not isinstance(state, str) or not NAME.fullmatch(state):
        raise CatalogError("Manifest has no valid campaign state")
    # Fail closed: an in-progress/blinded report never reveals analysis paths,
    # assignments, labels, checkpoint names or arm metadata in the catalog.
    if not isinstance(manifest.get("progress_only"), bool):
        raise CatalogError("Manifest must explicitly declare progress_only")
    progress = manifest["progress_only"]
    if not progress and state not in ANALYZED:
        raise CatalogError("Analysis is not permitted before ANALYZING or REPORTED")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise CatalogError("Manifest must declare artifact hashes")
    artifacts = {}
    for name, expected in files.items():
        if not isinstance(name, str) or not isinstance(expected, dict):
            raise CatalogError("Invalid artifact hash entry")
        expected_hash = expected.get("sha256")
        if not isinstance(expected_hash, str) or not re.fullmatch(
            r"[0-9a-f]{64}", expected_hash
        ):
            raise CatalogError("Invalid artifact sha256")
        if progress and name not in (
            "progress.json",
            "summary.en.md",
            "summary.zh-CN.md",
        ):
            raise CatalogError("Progress manifest contains an analysis artifact")
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise CatalogError("Artifact path escapes source report")
        candidate = _safe(report / relative)
        if not candidate.is_relative_to(report):
            raise CatalogError("Artifact path escapes source report")
        digest = hashlib.sha256(_read(candidate)).hexdigest()
        if digest != expected_hash:
            raise CatalogError("Source artifact differs from its manifest: " + name)
        if not progress or name in (
            "progress.json",
            "summary.en.md",
            "summary.zh-CN.md",
        ):
            artifacts[name] = digest
    if _read(manifest_path) != raw:
        raise CatalogError("Report changed during indexing; retry")
    row = {
        "schema": SCHEMA,
        "model": model,
        "task": task,
        "session": session,
        "campaign_id": campaign,
        "campaign_state": state,
        "basis": basis,
        "progress_only": progress,
        "rollout": {"root": str(rollout_root), "directory": str(rollout_dir)},
        "report": {
            "directory": str(report),
            "manifest_sha256": hashlib.sha256(raw).hexdigest(),
            "files": artifacts,
        },
    }
    # An explicit mapping is provenance, not proof that this model is an arm.
    # Record the mapping source; callers must use frozen campaign mappings.
    row["mapping"] = "explicit caller-supplied model/task; not inferred from checkpoint"
    if not progress:
        row["frozen_config"] = {
            key: manifest.get(key)
            if isinstance(manifest.get(key), str)
            and re.fullmatch(r"[0-9a-f]{64}", manifest[key])
            else None
            for key in ("campaign_sha256", "settings_sha256")
        }
        row["levi_commits"] = [
            value
            for value in manifest.get("levi_commits", [])
            if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{7,64}", value)
        ]
    digest = hashlib.sha256(_json(row)).hexdigest()
    root.mkdir(parents=True, exist_ok=True)
    lock = _safe(root / ".catalog.lock")
    with lock.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        if progress:
            existing = root / "campaigns" / campaign / basis / "reference.json"
            if (
                existing.exists()
                and json.loads(_read(existing)).get("progress_only") is False
            ):
                raise CatalogError(
                    "A published analysis cannot be re-blinded in the same catalog identity"
                )
            limited = {
                "schema": SCHEMA,
                "campaign_id": campaign,
                "basis": basis,
                "campaign_state": state,
                "progress_only": True,
                "report": row["report"],
            }
            _atomic(
                root / "campaigns" / campaign / basis / "reference.json", _json(limited)
            )
            _index(root)
            return limited
        target = _safe(root / "models" / model / task / "sessions" / session)
        version = target / "versions" / (digest + ".json")
        _atomic(version, _json(row))
        _atomic(target / "reference.json", _json(row))
        # A campaign has one report per basis, shared by all model/task references.
        campaign_ref = root / "campaigns" / campaign / basis / "reference.json"
        _atomic(
            campaign_ref,
            _json(
                {
                    "schema": SCHEMA,
                    "campaign_id": campaign,
                    "basis": basis,
                    "progress_only": progress,
                    "report": row["report"],
                }
            ),
        )
        _index(root)
    return row


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    write = commands.add_parser("register", help="Index an existing published report")
    for flag in ("root", "report", "rollout-root", "rollout-dir"):
        write.add_argument("--" + flag, type=Path, required=True)
    for flag in ("model", "task", "session"):
        write.add_argument("--" + flag, required=True)
    read = commands.add_parser(
        "list", help="Read current model/task/session references"
    )
    read.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            result = entries(args.root)
        else:
            result = register(
                args.root,
                report=args.report,
                rollout_root=args.rollout_root,
                rollout_dir=args.rollout_dir,
                model=args.model,
                task=args.task,
                session=args.session,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (CatalogError, OSError, ValueError) as exc:
        parser.exit(2, str(exc) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
