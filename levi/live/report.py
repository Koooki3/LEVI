"""Reports of a finished evaluation session: ``<live>/reports/``.

A session's report is written (and updated when more of its records arrive)
once the evaluation session has ended and every one of its episodes has been
labelled or given up on. Each report is three files, named after the dataset
and the session id: ``<stem>.md`` (English), ``<stem>.zh-CN.md`` and
``<stem>.json`` (the same figures and the per-episode rows). Writing is
idempotent (a report whose inputs did not change is left alone), atomic
(``.partial`` then rename) and bounded (only the newest ``resources.report_keep``
reports are kept). ``levi live report`` renders the same thing on demand.

The report holds the settings that were in force (never a secret or a path),
the figures of ``statsview``, a comparison with the previous report of the same
dataset, and the definitions. Standard library only.
"""

import contextlib
import hashlib
import json
import os
import re
import time
from pathlib import Path

from . import exclusion, jsonio, mirror, statsfmt, statsview

SCHEMA = "levi.live.report.v1"
DIR = "reports"
ENDED = ("stopped", "finished", "crashed")
DONE = ("done", "failed")
OPEN = ("mirrored", "annotating")
# The non-secret settings a report prints: ``{table: [keys]}`` of live.toml.
SETTINGS = {
    "provider": ("name", "prompt_style", "requests_in_flight"),
    "pipeline": (
        "auto_approve",
        "coarse_step_seconds",
        "refine",
        "guideline",
        "vocabulary",
        "anchored",
        "anchored_spec",
        "anchored_min_valid",
        "max_attempts",
    ),
    "vllm": (
        "served_model",
        "max_model_len",
        "min_model_len",
        "max_images",
        "max_num_seqs",
        "max_num_batched_tokens",
        "gpu_memory_utilization",
        "gpu_memory_utilization_min",
        "gpu_memory_utilization_max",
        "idle_action",
        "sleep_mode",
        "prewarm",
        "temperature",
    ),
    "gpu": ("mode", "lead_s", "lead_grace_s", "policy_budget_mib"),
}


def reports_dir(config) -> Path:
    return config.live_dir / DIR


def safe(text) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(text)).strip("_.")[:100] or "x"


def stem(dataset, session) -> str:
    """The file stem of a report: dataset and session, filename-safe. A name
    that had to be changed gets a short hash so two sessions never collide."""
    raw = f"{dataset or 'all'}__{session or 'all'}"
    name = f"{safe(dataset or 'all')}__{safe(session or 'all')}"
    if name != raw:
        name += "-" + hashlib.sha256(raw.encode()).hexdigest()[:6]
    return name


def settings(config) -> dict:
    """The configuration a report states: scalar, non-secret values only (no
    script path, port or directory)."""
    out = {}
    for table, keys in SETTINGS.items():
        section = getattr(config, table)
        out[table] = {
            key: getattr(section, key)
            for key in keys
            if isinstance(getattr(section, key), (str, int, float, bool))
        }
    return out


def spec_of(rows) -> dict | None:
    """The guideline and release-review spec of the newest record that names
    them, with the first 12 hex digits of each file's hash."""
    for row in reversed(rows):
        spec = (row.get("result") or {}).get("spec")
        if isinstance(spec, dict) and spec.get("guideline"):
            hashes = spec.get("sha256") if isinstance(spec.get("sha256"), dict) else {}
            return {
                "guideline": spec.get("guideline"),
                "release_review": spec.get("release_review"),
                "files": {
                    str(name): str(value)[:12] for name, value in sorted(hashes.items())
                },
            }
    return None


def previous_of(config, dataset, before_at, skip=None) -> dict | None:
    """The newest report of ``dataset`` whose last record is older than
    ``before_at`` (and that is not ``skip``'s own file), or None."""
    best = None
    for path in reports_dir(config).glob("*.json"):
        if skip and path.stem == skip:
            continue
        found = jsonio.read(path)
        if not isinstance(found, dict) or found.get("schema") != SCHEMA:
            continue
        if found.get("dataset") != dataset:
            continue
        last = ((found.get("summary") or {}).get("window") or {}).get("last_at")
        if not isinstance(last, (int, float)) or (
            before_at is not None and last >= before_at
        ):
            continue
        if best is None or last > best[0]:
            best = (last, found)
    return best[1] if best else None


def build(
    config,
    dataset=None,
    session=None,
    *,
    now=None,
    policy=None,
    include_excluded=False,
) -> dict:
    """Everything a report says, as one dict (the JSON file's content).
    ``policy`` is ``{config, checkpoint}`` of the evaluation session when the
    robot side's file is at hand."""
    now = time.time() if now is None else now
    payload = statsview.build(
        config,
        dataset=dataset,
        session=session,
        limit=None,
        include_excluded=include_excluded,
        now=now,
    )
    rows, _ = statsview.scoped_rows(
        config, dataset, session, include_excluded=include_excluded
    )
    last = [r.get("at") for r in rows if isinstance(r.get("at"), (int, float))]
    report = {
        "schema": SCHEMA,
        "dataset": dataset,
        "session": session,
        "generated_at": round(now, 3),
        # Inputs of the report: when they are the same the report is too.
        "signature": {
            "records": len(rows),
            "last_at": max(last) if last else None,
            "excluded_demos": payload["excluded_demos"],
        },
        "include_excluded": bool(include_excluded),
        "excluded_demos": payload["excluded_demos"],
        "settings": {
            **settings(config),
            **({"spec": spec_of(rows)} if spec_of(rows) else {}),
            **({"policy": policy} if policy else {}),
        },
        "previous": None,
        **{k: payload[k] for k in ("scope", "summary", "sessions", "episodes")},
    }
    first = (payload["summary"].get("window") or {}).get("first_at")
    before = previous_of(config, dataset, first, skip=stem(dataset, session))
    if before:
        report["previous"] = {
            "session": before.get("session"),
            "generated_at": before.get("generated_at"),
            "summary": before.get("summary"),
        }
    return report


def render(report, lang="en", max_rows=None) -> str:
    """The Markdown of a report dict (``build``'s answer)."""
    t = statsfmt.TEXT.get(lang) or statsfmt.TEXT["en"]
    label = " / ".join(x for x in (report.get("dataset"), report.get("session")) if x)
    settings_ = {
        table: {k: ("-" if v is None else v) for k, v in items.items()}
        for table, items in (report.get("settings") or {}).items()
        if isinstance(items, dict)
    }
    if isinstance(settings_.get("spec"), dict):
        spec = settings_.pop("spec")
        flat = spec.pop("files", {}) if isinstance(spec.get("files"), dict) else {}
        settings_["spec"] = {
            **{k: v for k, v in spec.items() if k != "files"},
            **{f"sha256 {name}": value for name, value in flat.items()},
        }
    return statsfmt.to_markdown(
        report,
        lang,
        title=f"{t['title']}: {label}" if label else None,
        settings=settings_,
        previous=report.get("previous"),
        max_rows=max_rows,
    )


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".partial")
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)
    finally:
        with contextlib.suppress(OSError):
            temp.unlink()


def write(config, dataset, session, *, force=False, now=None, policy=None) -> dict:
    """Write (or update) the report of one session. Returns ``{"stem",
    "written", "files"}``; ``written`` is False when the stored report was
    built from the same records."""
    name = stem(dataset, session)
    folder = reports_dir(config)
    existing = jsonio.read(folder / f"{name}.json")
    report = build(config, dataset, session, now=now, policy=policy)
    files = [f"{name}.md", f"{name}.zh-CN.md", f"{name}.json"]
    if (
        not force
        and isinstance(existing, dict)
        and existing.get("signature") == report["signature"]
        and all((folder / f).is_file() for f in files)
    ):
        return {"stem": name, "written": False, "files": files}
    # Markdown first, JSON last: the JSON is what marks a report complete.
    _write(folder / files[0], render(report, "en"))
    _write(folder / files[1], render(report, "zh"))
    _write(
        folder / files[2],
        json.dumps(report, ensure_ascii=False, indent=1, allow_nan=False),
    )
    prune(config)
    return {"stem": name, "written": True, "files": files}


def listing(config) -> list:
    """The reports there are, newest first: ``{stem, dataset, session,
    generated_at}``."""
    out = []
    for path in reports_dir(config).glob("*.json"):
        found = jsonio.read(path)
        if isinstance(found, dict) and found.get("schema") == SCHEMA:
            out.append(
                {
                    "stem": path.stem,
                    "dataset": found.get("dataset"),
                    "session": found.get("session"),
                    "generated_at": found.get("generated_at"),
                }
            )
    out.sort(key=lambda r: -(r["generated_at"] or 0))
    return out


def prune(config, keep=None) -> list:
    """Delete the oldest reports beyond ``keep`` (default
    ``resources.report_keep``); returns the stems removed. A damaged JSON
    (not a report) is left alone."""
    keep = config.resources.report_keep if keep is None else keep
    removed = []
    for row in listing(config)[max(1, keep) :]:
        for suffix in (".json", ".md", ".zh-CN.md"):
            with contextlib.suppress(OSError):
                (reports_dir(config) / (row["stem"] + suffix)).unlink()
        removed.append(row["stem"])
    return removed


# --- when a session is over ------------------------------------------------------


def finished_sessions(config, sessions=None, busy=()) -> list:
    """``[(dataset, session)]`` whose evaluation session has ended and whose
    every episode the service has handled: no demo of it waits to be labelled,
    is being labelled, or is a retry in the queue.

    ``sessions`` is the robot side's ``{(root, group, task): Session}`` (what
    the supervisor holds); a session still ``running``, ``homing``, ``standby``
    or waiting for a reset, with the same run id, is not over. ``busy`` names
    datasets that have episodes waiting or a batch in progress."""
    live = {}
    for (_root, group, task), session in (sessions or {}).items():
        live[mirror.dataset_name(group, task)] = session
    found = []
    for name, state in mirror.list_states(config).items():
        if name in busy or state.get("current"):
            continue
        runs: dict = {}
        for row in (state.get("demos") or {}).values():
            # A removed episode is not waited for (exclusion.py).
            if (
                isinstance(row, dict)
                and row.get("run_id")
                and not exclusion.is_excluded(row)
            ):
                runs.setdefault(str(row["run_id"]), []).append(row.get("state"))
        for run, states in runs.items():
            if any(s in OPEN for s in states) or not any(s in DONE for s in states):
                continue
            current = live.get(name)
            if (
                current is not None
                and current.run_id == run
                and current.state not in ENDED
            ):
                continue
            found.append((name, run))
    return sorted(found)


def policy_of(sessions, dataset, session) -> dict | None:
    """The evaluated policy's config name and checkpoint folder name, from the
    robot side's session file (never a path)."""
    for (_root, group, task), current in (sessions or {}).items():
        if mirror.dataset_name(group, task) == dataset and current.run_id == session:
            policy = {k: v for k, v in (current.policy or {}).items() if v}
            return policy or None
    return None


def auto(config, sessions=None, busy=(), now=None) -> list:
    """Write the report of every finished session whose records changed;
    returns the results of the ones written. Never raises (a report is a
    record, not part of the labelling)."""
    done = []
    for dataset, session in finished_sessions(config, sessions, busy):
        with contextlib.suppress(Exception):
            result = write(
                config,
                dataset,
                session,
                now=now,
                policy=policy_of(sessions, dataset, session),
            )
            if result["written"]:
                done.append(result)
    return done
