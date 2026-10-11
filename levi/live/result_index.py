"""Read-only, source-pinned result links; statistics stay in analysis.summarize.

A configured root plus canonical session metadata authorizes the scope. A
record's group/task/session alone never authorizes reading another source.
Downloads are current snapshots, not a publication of a catalog version.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from fastapi import HTTPException

from levi import evaluation_results as catalog
from levi.automatic.api import _open_below, _read_below

from . import analysis, exclusion, sessions, stats

SCHEMA = "levi.live.result-index.v1"
PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,159}$")
SESSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,159}$")
DATASET = re.compile(r"^(?![.])[^/\\\x00-\x1f\x7f]{1,200}$")
LIMIT = 16 * 1024 * 1024
MAX_SESSIONS = 1000
MAX_STATES = 1000
MAX_CATALOG = 1000
MISSING = (
    "incomplete",
    "fr3_fault",
    "discarded",
    "failed",
    "excluded",
    "unlabelled",
    "done_without_record",
)


class _Snapshot:
    def __init__(self):
        self.files = {}
        self.directories = {}
        self.artifacts = {}
        self.parsed = {}
        self.truncated = set()

    def read(self, path, *, optional=False):
        path = catalog._safe(Path(path))
        if path in self.files:
            return self.files[path]
        try:
            fd = _open_below(Path(path.anchor), path.parts[1:])
            try:
                before = os.fstat(fd)
                raw = os.read(fd, LIMIT + 1)
                after = os.fstat(fd)
                if len(raw) > LIMIT or (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                ) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    raise ValueError("Source changed or exceeds the read limit")
                self.stat_file(path, after)
            finally:
                os.close(fd)
        except FileNotFoundError:
            if not optional:
                raise
            raw = None
        self.files[path] = raw
        return raw

    def json(self, path, *, optional=False):
        key = ("json", Path(path))
        if key not in self.parsed:
            raw = self.read(path, optional=optional)
            self.parsed[key] = json.loads(raw) if raw is not None else None
        return self.parsed[key]

    def pin(self, path):
        path = catalog._safe(Path(path))
        if path in self.directories:
            return
        info = path.stat()
        if not path.is_dir():
            raise ValueError("Missing source directory")
        self.directories[path] = (
            info.st_dev,
            info.st_ino,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    def stat_file(self, path, info):
        self.artifacts[catalog._safe(path)] = (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    def valid(self):
        try:
            for path, original in self.files.items():
                try:
                    now = _read_below(
                        Path(path.anchor), catalog._safe(path).parts[1:], LIMIT
                    )
                except FileNotFoundError:
                    now = None
                if now != original:
                    return False
            for path, original in self.artifacts.items():
                info = catalog._safe(path).stat()
                if (
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                ) != original:
                    return False
            for path, original in self.directories.items():
                info = catalog._safe(path).stat()
                if (
                    info.st_dev,
                    info.st_ino,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                ) != original or not path.is_dir():
                    return False
        except (OSError, ValueError):
            return False
        return True

    def digest(self):
        parts = {
            str(p): hashlib.sha256(raw).hexdigest() if raw is not None else None
            for p, raw in self.files.items()
        }
        parts.update({str(p): list(v) for p, v in self.directories.items()})
        parts.update({str(p): list(v) for p, v in self.artifacts.items()})
        return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def _component(value, pattern=PART):
    if (
        not isinstance(value, str)
        or not pattern.fullmatch(value)
        or value in (".", "..")
    ):
        raise HTTPException(400, "Invalid result identity")
    return value


def _roots(config):
    return list(dict.fromkeys(str(Path(p).expanduser()) for p in config.watch.roots))


def _root(config, root):
    # Exact configured spellings only. Do not resolve an arbitrary client path.
    if root not in _roots(config):
        raise HTTPException(403, "Result root is not configured")
    try:
        return catalog._safe(Path(root))
    except ValueError:
        raise HTTPException(403, "Configured source is unsafe") from None


def _base(config, root, group, task, session):
    return {
        "schema": SCHEMA,
        "identity": {
            "root": root,
            "workspace": str(config.workspace),
            "group": group,
            "task": task,
            "session_id": None,
            "run_id": session,
            "datasets": [],
        },
        "status": "unknown",
        "reason": "canonical_session_missing",
        "result_visibility": "unknown",
        "analysis": None,
        "records": [],
        "runs": [],
        "reports": [],
        "viewers": [],
        "policy": {"config": None, "checkpoint": None},
        "label_mode": None,
        "source": {
            "kind": "current_session",
            "snapshot": None,
            "basis": "operator_and_automatic_separate",
            "missing": [],
        },
        "comparison": {
            "status": "unknown",
            "campaign_id": None,
            "missing": ["explicit_campaign", "frozen_settings", "policy_mapping"],
            "conditions": None,
        },
    }


def _stop(item, status, reason):
    item.update(
        status=status,
        reason=reason,
        analysis=None,
        records=[],
        runs=[],
        reports=[],
        viewers=[],
    )
    item["comparison"] = {
        "status": "unknown",
        "campaign_id": item["comparison"].get("campaign_id"),
        "missing": ["result_unavailable"],
        "conditions": None,
    }
    item["policy"] = {"config": None, "checkpoint": None}
    item["result_visibility"] = (
        "withheld" if status in ("active", "blocked") else "unknown"
    )
    item["source"]["snapshot"] = None
    return item


def _session_rows(snapshot, root):
    directory = catalog._safe(root / sessions.SESSION_DIR)
    key = ("sessions", directory)
    if key in snapshot.parsed:
        return snapshot.parsed[key]
    rows = []
    if directory.is_dir():
        snapshot.pin(directory)
        paths = sorted(directory.glob("*.json"))
        if len(paths) > MAX_SESSIONS:
            snapshot.truncated.add("sessions")
        for path in paths[:MAX_SESSIONS]:
            if path.name.startswith("."):
                continue
            try:
                value = snapshot.json(path)
                if isinstance(value, dict):
                    rows.append((path, value))
            except (OSError, ValueError):
                continue
    grouped = {}
    for path, value in rows:
        identity = (value.get("group"), value.get("task_folder"))
        if all(isinstance(part, str) for part in identity):
            grouped.setdefault(identity, []).append((path, value))
    snapshot.parsed[key] = (rows, grouped)
    return rows, grouped


def _find(snapshot, root, group, task, requested):
    _rows, grouped = _session_rows(snapshot, root)
    matched = grouped.get((group, task), [])
    exact = [
        value
        for value in matched
        if requested in (value[1].get("run_id"), value[1].get("session_id"))
    ]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None
    return matched[0] if len(matched) == 1 else None


def _states(config, snapshot):
    directory = catalog._safe(config.live_dir / "datasets")
    key = ("states", directory)
    if key in snapshot.parsed:
        return snapshot.parsed[key]
    if not directory.is_dir():
        return {}
    snapshot.pin(directory)
    found = {}
    paths = sorted(directory.glob("*.json"))
    if len(paths) > MAX_STATES:
        snapshot.truncated.add("datasets")
    for path in paths[:MAX_STATES]:
        if not DATASET.fullmatch(path.stem):
            continue
        try:
            value = snapshot.json(path)
        except (OSError, ValueError):
            continue
        if isinstance(value, dict):
            found[path.stem] = value
    snapshot.parsed[key] = found
    return found


def _header_visibility(snapshot, source, matching, run, session):
    """Older C2 writers omit AERI; canonical capture headers must vouch for legacy data."""
    for state in matching.values():
        for demo, row in (state.get("demos") or {}).items():
            if (
                not isinstance(row, dict)
                or row.get("run_id") != run
                or row.get("deleted")
                or exclusion.is_excluded(row)
            ):
                continue
            if not isinstance(demo, str) or not re.fullmatch(r"demo_[0-9]+", demo):
                return "source_demo_identity_unknown"
            try:
                meta = snapshot.json(source / demo / "metadata.json", optional=True)
            except (OSError, ValueError):
                return "source_header_unreadable"
            if not isinstance(meta, dict) or not isinstance(meta.get("eval"), dict):
                return "source_header_unknown"
            if row.get("source_changed"):
                return "source_episode_changed"
            signature = row.get("sig")
            if isinstance(signature, dict):
                snapshot.read(source / demo / ".complete", optional=True)
                current = {
                    name: [
                        snapshot.artifacts[source / demo / name][1],
                        snapshot.artifacts[source / demo / name][3],
                    ]
                    if source / demo / name in snapshot.artifacts
                    else None
                    for name in (".complete", "metadata.json")
                }
                if current != signature:
                    return "source_episode_changed"
            evaluation = meta["eval"]
            if evaluation.get("run_id") != run:
                return "source_header_session_changed"
            marker = meta.get("aeri") or evaluation.get("aeri") or {}
            if not isinstance(marker, dict):
                return "source_aeri_visibility_unknown"
            if (
                marker.get("campaign_id")
                or marker.get("blinded")
                or marker.get("operator_blind")
                or meta.get("blinded")
                or evaluation.get("blinded")
            ):
                return "source_campaign_uses_gated_report"
            if (
                "aeri" in meta
                or "aeri" in evaluation
                or "episode_role" in meta
                or "episode_role" in evaluation
                or evaluation.get("verdict_by") == "aeri"
            ) and session.get("analysis_visibility") != "public":
                return "source_aeri_visibility_unknown"
    return None


def _records(snapshot, live_dir):
    key = ("records", live_dir)
    if key in snapshot.parsed:
        return snapshot.parsed[key]
    raw = snapshot.read(live_dir / analysis.FILE, optional=True)
    rows = []
    for line in (raw or b"").splitlines():
        try:
            row = analysis.normalize(json.loads(line))
            if row:
                rows.append(row)
        except (ValueError, TypeError):
            continue
    snapshot.parsed[key] = analysis.latest(rows)
    return snapshot.parsed[key]


def _state_scope(config, snapshot, group, task):
    key = ("state_scope", config.live_dir)
    if key not in snapshot.parsed:
        grouped = {}
        for name, state in _states(config, snapshot).items():
            identity = (state.get("group"), state.get("task_folder"))
            if all(isinstance(v, str) for v in identity):
                grouped.setdefault(identity, {})[name] = state
        snapshot.parsed[key] = grouped
    return snapshot.parsed[key].get((group, task), {})


def _record_scope(snapshot, live_dir, group, task, run):
    key = ("record_scope", live_dir)
    if key not in snapshot.parsed:
        grouped = {}
        for row in _records(snapshot, live_dir):
            identity = (row.get("group"), row.get("task"), row.get("session"))
            if all(isinstance(v, str) for v in identity):
                grouped.setdefault(identity, []).append(row)
        snapshot.parsed[key] = grouped
    return snapshot.parsed[key].get((group, task, run), [])


def _runs(selected, matching):
    grouped = {}
    missing = []
    for record in selected:
        name, demo = record["dataset"], record["demo"]
        row = matching[name]["demos"][demo]
        episode = row.get("episode_index")
        for kind in ("temporal", "verdict"):
            ref = row.get(kind)
            if not isinstance(ref, dict):
                continue
            run = ref.get("run_id")
            change = ref.get("changeset") if kind == "temporal" else None
            if not isinstance(run, str) or not SESSION.fullmatch(run):
                missing.append(name + ":" + demo + ":" + kind + "_run_id")
                continue
            key = (name, kind, run, change)
            slot = grouped.setdefault(
                key,
                {
                    "dataset": name,
                    "kind": kind,
                    "run_id": run,
                    "changeset": change,
                    "episodes": [],
                },
            )
            if isinstance(episode, int) and not isinstance(episode, bool):
                slot["episodes"].append(episode)
            else:
                missing.append(name + ":" + demo + ":episode_index")
            if kind == "temporal" and not change:
                missing.append(name + ":" + demo + ":changeset")
    return [
        {**r, "episodes": sorted(set(r["episodes"]))} for r in grouped.values()
    ], missing


def _published(config, snapshot, item, source):
    """Catalog metadata is provenance only; never serve its stored paths."""
    root = config.workspace / "outputs/LEVI/evaluation-results"
    try:
        rows = _catalog_rows(config, snapshot)
    except (OSError, ValueError):
        return []
    out = []
    identity = item["identity"]
    key = ("catalog_scope", config.workspace)
    if key not in snapshot.parsed:
        grouped = {}
        for row in rows:
            rollout = row.get("rollout") or {}
            scope = (
                row.get("model"),
                row.get("task"),
                row.get("session"),
                rollout.get("root"),
                rollout.get("directory"),
            )
            if all(isinstance(v, str) for v in scope):
                grouped.setdefault(scope, []).append(row)
        snapshot.parsed[key] = grouped
    scope = (
        identity["group"],
        identity["task"],
        identity["run_id"],
        str(source.parent.parent),
        str(source),
    )
    for row in snapshot.parsed[key].get(scope, []):
        rollout = row.get("rollout") or {}
        if (row.get("model"), row.get("task"), row.get("session")) != (
            identity["group"],
            identity["task"],
            identity["run_id"],
        ):
            continue
        if (rollout.get("root"), rollout.get("directory")) != (
            str(source.parent.parent),
            str(source),
        ):
            continue
        if row.get("progress_only") is not False or row.get("campaign_id"):
            continue
        report = row.get("report") or {}
        try:
            directory = catalog._safe(Path(report["directory"]))
            # Only live-export versions physically inside the configured derived catalog.
            expected = (
                root
                / "models"
                / identity["group"]
                / identity["task"]
                / "sessions"
                / identity["run_id"]
                / "versions"
            )
            if directory.parent != expected or not re.fullmatch(
                r"[0-9a-f]{64}", directory.name
            ):
                continue
            raw = snapshot.read(directory / "manifest.json")
            manifest = json.loads(raw)
            if hashlib.sha256(raw).hexdigest() != report.get("manifest_sha256"):
                continue
            if manifest.get("schema") != "levi.live.analysis.export.v1":
                continue
            if any(
                manifest.get(k) != row.get(k)
                for k in ("model", "task", "session", "basis")
            ) or (manifest.get("source") or {}).get("rollout_directory") != str(source):
                raise ValueError("Published source identity changed")
            files = manifest.get("files") or {}
            valid = []
            for name, meta in files.items():
                if name not in (
                    "data/analysis.json",
                    "data/records.json",
                    "summary.en.md",
                    "summary.zh-CN.md",
                    "figures/success.svg",
                ):
                    continue
                data = snapshot.read(directory / name)
                if hashlib.sha256(data).hexdigest() != meta.get("sha256"):
                    raise ValueError("Published artifact changed")
                valid.append(name)
            provenance = manifest.get("source") or {}
            checks = {
                provenance.get("session_file"): provenance.get("session_sha256"),
                provenance.get("analysis_file"): provenance.get("analysis_sha256"),
                **(provenance.get("dataset_states") or {}),
            }
            current = bool(checks) and all(
                str(path) in checks
                and hashlib.sha256(data).hexdigest() == checks[str(path)]
                for path, data in snapshot.files.items()
                if data is not None and str(path) in checks
            )
            current = current and all(Path(path) in snapshot.files for path in checks)
            out.append(
                {
                    "basis": row["basis"],
                    "version": directory.name,
                    "files": sorted(valid),
                    "current": current,
                    "status": "current" if current else "stale",
                    "download_url": None,
                }
            )
        except (OSError, ValueError, KeyError, TypeError):
            out.append(
                {
                    "basis": str(row.get("basis") or "unknown"),
                    "version": None,
                    "files": [],
                    "current": False,
                    "status": "missing",
                    "download_url": None,
                }
            )
    return out


def _campaign(item, raw, root, snapshot):
    """Use the existing report gate; a terminal child cannot unblind a campaign."""
    from levi.automatic.campaign import api as campaign_api

    campaign = raw.get("campaign_id")
    item["comparison"]["campaign_id"] = campaign if isinstance(campaign, str) else None
    try:
        c = campaign_api._locate(campaign)
        campaign_api._require_reportable(c)
        if (
            catalog._safe(Path(c.host_record.get("rollout_root") or "")) != root
            or not c.plan
        ):
            return _stop(item, "blocked", "campaign_source_unknown")
        arms = [
            arm
            for arm, spec in c.plan["campaign"]["arms"].items()
            if spec.get("group") == raw.get("group")
        ]
        if len(arms) != 1:
            return _stop(item, "blocked", "campaign_policy_mapping_unknown")
        expected = c.plan["campaign"]["arms"][arms[0]]["policy_forward"]
        policy = raw.get("policy") or {}
        if policy.get("config") != expected.get("config") or policy.get(
            "checkpoint_dir"
        ) != expected.get("checkpoint_dir"):
            return _stop(item, "blocked", "campaign_policy_mismatch")
        chosen = campaign_api._basis_of(c, None)
        snapshot.pin(c.folder)
        snapshot.read(c.folder / "journal.jsonl", optional=True)
        snapshot.read(c.folder / "plan.json", optional=True)
        snapshot.read(c.folder / "report" / chosen / "manifest.json")
        snapshot.read(c.folder / "report" / chosen / "data/analysis.json")
        report = campaign_api.get_report(campaign)
        manifest = report["manifest"]
        if (
            manifest.get("schema") != "levi.aeri.campaign_report_manifest.v1"
            or manifest.get("campaign_id") != campaign
            or manifest.get("progress_only") is not False
            or manifest.get("campaign_state") not in campaign_api.FINISHED
        ):
            return _stop(item, "blocked", "campaign_analysis_not_published")
        if raw.get("run_id") not in {r.get("run_id") for r in manifest.get("runs", [])}:
            return _stop(item, "blocked", "campaign_session_link_unknown")
        hashes = [manifest.get(k) for k in ("campaign_sha256", "settings_sha256")]
        if any(
            manifest.get(k) != c.plan.get(k)
            for k in ("campaign_sha256", "settings_sha256")
        ):
            return _stop(item, "blocked", "campaign_frozen_config_mismatch")
        if raw.get("run_id") not in {
            child.get("run_id")
            for child in c.plan.get("children", [])
            if child.get("arm") == arms[0]
        }:
            return _stop(item, "blocked", "campaign_child_mapping_unknown")
        if not all(
            isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v) for v in hashes
        ):
            return _stop(item, "blocked", "campaign_frozen_config_unknown")
        basis = report["basis"]
        item["comparison"] = {
            "status": "comparable",
            "campaign_id": campaign,
            "missing": [],
            "cohort": hashes[0],
            "settings_sha256": hashes[1],
        }
        existing = report.get("analysis") or {}
        header = existing.get("header") or {}
        block = c.plan["campaign"]
        conditions = {
            "reset_mode": c.plan.get("reset_mode"),
            "label_basis": basis,
            "stop_rules": block.get("stop_rules"),
            "coverage": header.get("coverage"),
            "early_stop": existing.get("early_stop"),
            "runs": [
                {
                    key: r.get(key)
                    for key in (
                        "run_id",
                        "reset_mode",
                        "scene_check",
                        "levi_commit",
                        "plan_sha256",
                    )
                }
                for r in manifest.get("runs", [])
            ],
            "arms": [
                {
                    key: a.get(key)
                    for key in (
                        "id",
                        "config",
                        "checkpoint",
                        "versions",
                        "not_verified",
                    )
                }
                for a in manifest.get("arms", [])
            ],
        }
        missing = [
            key
            for key in ("reset_mode", "stop_rules", "coverage", "early_stop", "arms")
            if conditions[key] is None or conditions[key] == []
        ]
        if any(not r.get("levi_commit") for r in conditions["runs"]):
            missing.append("run_commit")
        if any(not r.get("scene_check") for r in conditions["runs"]):
            missing.append("scene_check")
        if any(not a.get("versions") for a in conditions["arms"]):
            missing.append("environment_versions")
        if any(a.get("not_verified") for a in conditions["arms"]):
            missing.append("unverified_arms")
        differences = []
        versions = {
            a["versions"]
            for a in conditions["arms"]
            if isinstance(a.get("versions"), str) and a["versions"]
        }
        if len(versions) > 1:
            differences.append("environment_versions")
        for key in ("reset_mode", "scene_check"):
            values = {r[key] for r in conditions["runs"] if isinstance(r.get(key), str)}
            if len(values) > 1 or (
                key == "reset_mode"
                and conditions["reset_mode"] is not None
                and values != {conditions["reset_mode"]}
            ):
                differences.append(key)
        item["comparison"].update(
            conditions=conditions,
            missing=missing,
            differences=differences,
            status="incompatible"
            if differences
            else "unknown"
            if missing
            else "comparable",
        )
        item["policy"] = {
            "config": policy.get("config"),
            "checkpoint": Path(str(policy.get("checkpoint_dir") or "")).name or None,
        }
        item["reports"] = [
            {
                "basis": basis,
                "version": hashes[0],
                "files": [],
                "current": True,
                "status": "current",
                "download_url": None,
                "url": "/automatic/campaigns/" + campaign,
                "api_url": "/api/levi/automatic/campaigns/" + campaign + "/report",
            }
        ]
        item.update(
            status="reported",
            reason="gated_campaign_report",
            result_visibility="visible",
        )
        # The campaign has its own analysis schema and denominator; do not
        # pass it off as this session's live summary or re-aggregate it.
        return item
    except (HTTPException, OSError, ValueError, KeyError, TypeError):
        return _stop(item, "blocked", "campaign_report_unavailable")


def session(
    config,
    *,
    root,
    group,
    task,
    session,
    session_id=None,
    run_id=None,
    linker=None,
    _snapshot=None,
    _validate=True,
):
    group, task = _component(group), _component(task)
    session = _component(session, SESSION)
    if session_id is not None:
        _component(session_id, SESSION)
    if run_id is not None:
        _component(run_id, SESSION)
    allowed = _root(config, root)
    item = _base(config, root, group, task, session)
    snapshot = _snapshot or _Snapshot()
    try:
        if not allowed.is_dir():
            return _stop(item, "missing_source", "configured_root_missing")
        snapshot.pin(allowed)
        found = _find(snapshot, allowed, group, task, session)
        if found is None:
            return item
        _path, raw = found
        if raw.get("schema") != "levi.eval.session.v1":
            return _stop(item, "unknown", "canonical_session_unknown")
        current_run, current_id = raw.get("run_id"), raw.get("session_id")
        if not isinstance(current_run, str) or not SESSION.fullmatch(current_run):
            return _stop(item, "unknown", "canonical_run_id_missing")
        if (
            session not in (current_run, current_id)
            or (run_id is not None and run_id != current_run)
            or (session_id is not None and session_id != current_id)
        ):
            return _stop(item, "rebound", "session_identity_changed")
        item["identity"].update(session_id=current_id, run_id=current_run)
        if raw.get("state") not in ("finished", "stopped"):
            return _stop(item, "active", "session_not_terminal")
        source = catalog._safe(allowed / group / task)
        if not source.is_dir():
            return _stop(item, "missing_source", "rollout_source_missing")
        snapshot.pin(source)
        if raw.get("campaign_id"):
            result = _campaign(item, raw, allowed, snapshot)
            return (
                result
                if not _validate or snapshot.valid()
                else _stop(item, "stale", "source_changed_during_read")
            )
        if (
            raw.get("blinded")
            or raw.get("operator_blind")
            or (
                ("aeri" in raw or "episode_role" in raw)
                and raw.get("analysis_visibility") != "public"
            )
        ):
            return _stop(item, "blocked", "analysis_visibility_unknown_or_blind")
        source = catalog._safe(allowed / group / task)
        if not source.is_dir():
            return _stop(item, "missing_source", "rollout_source_missing")
        snapshot.pin(source)
        states = _state_scope(config, snapshot, group, task)
        matching = {}
        archived = False
        rebound = False
        for name, state in states.items():
            if (state.get("group"), state.get("task_folder")) != (group, task):
                continue
            state_source = state.get("source")
            if not isinstance(state_source, str):
                continue
            if catalog._safe(Path(state_source)) != source:
                # A state claiming this root but pointing elsewhere was rebound.
                rebound = rebound or state.get("root") == root
                continue
            if state.get("archived"):
                archived = True
                continue
            matching[name] = state
        item["identity"]["datasets"] = sorted(matching)
        if not matching:
            return _stop(
                item,
                "archived" if archived else "rebound" if rebound else "unknown",
                "dataset_archived"
                if archived
                else "dataset_source_changed"
                if rebound
                else "dataset_source_unknown",
            )
        withheld = _header_visibility(snapshot, source, matching, current_run, raw)
        if withheld:
            return _stop(
                item,
                "stale"
                if withheld
                in ("source_header_session_changed", "source_episode_changed")
                else "blocked",
                withheld,
            )
        selected = []
        have = set()
        stale = False
        for record in _record_scope(
            snapshot, config.live_dir, group, task, current_run
        ):
            if record["dataset"] not in matching or (
                record.get("group"),
                record.get("task"),
                record.get("session"),
            ) != (group, task, current_run):
                continue
            row = (matching[record["dataset"]].get("demos") or {}).get(record["demo"])
            if not isinstance(row, dict) or row.get("run_id") != current_run:
                stale = True
                continue
            if (
                row.get("deleted")
                or exclusion.is_excluded(row)
                or row.get("state") != "done"
            ):
                continue
            if record.get("episode_index") != row.get("episode_index"):
                stale = True
                continue
            temporal = (
                row.get("temporal") if isinstance(row.get("temporal"), dict) else {}
            )
            if isinstance(temporal.get("committed_at"), (int, float)) and temporal[
                "committed_at"
            ] > (record.get("labelled_at") or 0):
                stale = True
                continue
            # Compare labels to their current canonical source. Historical
            # analysis is not allowed to keep a replaced label visible.
            if (
                record.get("operator")
                != stats.operator_brief(row.get("operator_label"))
                or record.get("agent") != analysis._agent(row.get("verdict"))
                or record.get("online") != analysis._online(row)
            ):
                stale = True
                continue
            selected.append(record)
            have.add((record["dataset"], record["demo"]))
        missing = dict.fromkeys(MISSING, 0)
        for name, state in matching.items():
            for demo, row in (state.get("demos") or {}).items():
                if (
                    not isinstance(row, dict)
                    or row.get("run_id") != current_run
                    or row.get("deleted")
                ):
                    continue
                if exclusion.is_excluded(row):
                    missing["excluded"] += 1
                elif row.get("state") == "failed":
                    missing["failed"] += 1
                elif row.get("state") == "done" and (name, demo) not in have:
                    missing["done_without_record"] += 1
                elif row.get("state") != "done":
                    missing["unlabelled"] += 1
        if stale:
            return _stop(item, "stale", "analysis_source_changed")
        if not selected:
            return _stop(item, "waiting_annotation", "no_current_analysis_records")
        item["analysis"] = analysis.summarize(
            selected,
            missing={(group, task): missing},
            scope={
                "root": root,
                "workspace": str(config.workspace),
                "group": group,
                "task": task,
                "session": current_run,
            },
        )
        item["records"] = selected
        item["runs"], item["source"]["missing"] = _runs(selected, matching)
        # Session totals cannot be reconstructed from task-level incomplete counters.
        item["source"]["missing"].append("session_incomplete_discarded_coverage")
        policy = raw.get("policy") if isinstance(raw.get("policy"), dict) else {}
        item["policy"] = {
            "config": policy.get("config"),
            "checkpoint": Path(str(policy.get("checkpoint_dir") or "")).name or None,
        }
        item["label_mode"] = (
            (raw.get("levi") or {}).get("mode")
            if isinstance(raw.get("levi"), dict)
            else None
        )
        item["reports"] = _published(config, snapshot, item, source)
        if linker is not None:
            linker(config, item, matching, snapshot)
        item.update(
            status="partial" if sum(missing.values()) else "reported",
            reason=None,
            result_visibility="visible",
        )
        if snapshot.truncated:
            return _stop(item, "unknown", "index_scan_truncated")
        if _validate and not snapshot.valid():
            return _stop(item, "stale", "source_changed_during_read")
        if _validate:
            item["source"]["snapshot"] = snapshot.digest()
        return item
    except (OSError, ValueError, TypeError, KeyError):
        return _stop(item, "unknown", "source_unreadable")


def build(config, *, dataset=None, run_id=None, linker=None):
    if dataset is not None and not DATASET.fullmatch(dataset):
        raise HTTPException(400, "Invalid dataset identity")
    if run_id is not None:
        _component(run_id, SESSION)
    items = []
    snapshot = _Snapshot()
    for root in _roots(config):
        try:
            allowed = _root(config, root)
            rows, _grouped = _session_rows(snapshot, allowed)
            identities = [
                (raw.get("group"), raw.get("task_folder"), raw.get("run_id"))
                for _path, raw in rows
                if all(
                    isinstance(raw.get(k), str)
                    for k in ("group", "task_folder", "run_id")
                )
            ]
            for group, task, run in sorted(set(identities)):
                try:
                    item = session(
                        config,
                        root=root,
                        group=group,
                        task=task,
                        session=run,
                        linker=linker,
                        _snapshot=snapshot,
                        _validate=False,
                    )
                except HTTPException:
                    continue
                if (
                    dataset is not None
                    and dataset not in item["identity"]["datasets"]
                    and dataset
                    not in {
                        repo.removeprefix("local/")
                        for viewer in item["viewers"]
                        for repo in (
                            viewer.get("repo_id"),
                            viewer.get("native_repo_id"),
                        )
                        if isinstance(repo, str)
                    }
                ):
                    continue
                if run_id is not None and run_id not in {
                    r["run_id"] for r in item["runs"]
                }:
                    continue
                items.append(item)
        except (OSError, ValueError):
            continue
    seen = {
        (
            r["identity"]["root"],
            r["identity"]["group"],
            r["identity"]["task"],
            r["identity"]["run_id"],
        )
        for r in items
    }
    if run_id is None:
        for item in _history(config, seen, snapshot):
            if dataset is None or dataset in item["identity"]["datasets"]:
                items.append(item)
    valid = snapshot.valid()
    digest = snapshot.digest() if valid else None
    for item in items:
        if not valid:
            _stop(item, "stale", "source_changed_during_read")
        elif snapshot.truncated:
            _stop(item, "unknown", "index_scan_truncated")
        elif item["result_visibility"] == "visible":
            item["source"]["snapshot"] = digest
    return {
        "schema": SCHEMA,
        "available": True,
        "items": items,
        "complete": not bool(snapshot.truncated),
        "truncated": sorted(snapshot.truncated),
        "limits": {
            "sessions_per_root": MAX_SESSIONS,
            "datasets": MAX_STATES,
            "catalog_entries": MAX_CATALOG,
            "file_bytes": LIMIT,
        },
    }


def _catalog_rows(config, snapshot):
    root = config.workspace / "outputs/LEVI/evaluation-results"
    key = ("catalog", root)
    if key not in snapshot.parsed:
        rows = []
        if root.is_dir():
            snapshot.pin(root)
            references = sorted(
                root.glob("models/*/*/sessions/*/reports/*/reference.json")
            )
            legacy = sorted(
                p
                for p in root.glob("models/*/*/sessions/*/reference.json")
                if not (p.parent / "reports").exists()
            )
            paths = [*references, *legacy]
            if len(paths) > MAX_CATALOG:
                snapshot.truncated.add("catalog")
            for path in paths[:MAX_CATALOG]:
                parent = path.parent
                while parent != root:
                    snapshot.pin(parent)
                    parent = parent.parent
                value = snapshot.json(path)
                if isinstance(value, dict) and value.get("schema") == catalog.SCHEMA:
                    rows.append(value)
        snapshot.parsed[key] = rows
    return snapshot.parsed[key]


def _history(config, seen, snapshot):
    """Keep catalog identities discoverable without reopening historical outcomes."""
    try:
        rows = _catalog_rows(config, snapshot)
    except (OSError, ValueError):
        return []
    found = {}
    for row in rows:
        try:
            if row.get("progress_only") is not False:
                continue
            group, task, run = (
                _component(row.get(k), SESSION if k == "session" else PART)
                for k in ("model", "task", "session")
            )
            rollout = row.get("rollout") or {}
            root = rollout.get("root")
            if root not in _roots(config):
                continue
            source = catalog._safe(Path(root) / group / task)
            if rollout.get("directory") != str(source):
                continue
            key = (root, group, task, run)
            if key in seen:
                continue
            item = found.setdefault(key, _base(config, root, group, task, run))
            item["source"].update(
                kind="catalog_reference",
                missing=["historical_session_visibility", "canonical_session_metadata"],
            )
            item["status"] = "unknown" if source.is_dir() else "missing_source"
            item["reason"] = "historical_session_metadata_unavailable"
            # A basis/version is provenance, never a downloadable path or outcome.
            directory = Path((row.get("report") or {}).get("directory") or "")
            version = (
                directory.name
                if re.fullmatch(r"[0-9a-f]{64}", directory.name)
                else None
            )
            item["source"].setdefault("catalog_versions", []).append(
                {"basis": row.get("basis"), "version": version, "status": "unverified"}
            )
            for name, state in _state_scope(config, snapshot, group, task).items():
                if state.get("source") == str(source) and (
                    state.get("group"),
                    state.get("task_folder"),
                ) == (group, task):
                    item["identity"]["datasets"].append(name)
                    if state.get("archived"):
                        item["status"] = "archived"
                        item["reason"] = "historical_dataset_archived"
            if source.is_dir():
                current = _find(snapshot, Path(root), group, task, run)
                if (
                    current
                    and run != current[1].get("run_id")
                    and item["status"] != "archived"
                ):
                    item["status"] = "stale"
                    item["reason"] = "historical_session_metadata_replaced"
            item["identity"]["datasets"] = sorted(set(item["identity"]["datasets"]))
        except (OSError, ValueError, HTTPException, TypeError):
            continue
    return list(found.values())


def download(item, *, format="json", lang="en"):
    if format not in ("json", "markdown") or lang not in ("en", "zh"):
        raise HTTPException(400, "Unknown result download format or language")
    if (
        item["analysis"] is None
        or item["status"] not in ("partial", "reported")
        or not item["source"]["snapshot"]
    ):
        raise HTTPException(409, "Current session analysis is unavailable")
    if format == "json":
        return (
            json.dumps(
                {
                    "schema": SCHEMA,
                    "identity": item["identity"],
                    "source": item["source"],
                    "analysis": item["analysis"],
                    "records": item["records"],
                    "runs": item["runs"],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
    prefix = (
        "当前会话只读快照；不是已发布报告版本。"
        if lang == "zh"
        else "Read-only current-session snapshot; not a published report version."
    )
    return (
        prefix
        + "\n\n"
        + "Snapshot: "
        + item["source"]["snapshot"]
        + "\n\n"
        + analysis.to_markdown(item["analysis"], lang)
    )
