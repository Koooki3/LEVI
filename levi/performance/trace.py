"""One trace per labelled episode, merged from the records LEVI already keeps.

Read-only and standard library only. It adds no store: the inputs are

- ``live/stats.jsonl`` (``levi.live.stats``): stage timeline, model time and
  tokens per episode;
- ``live/gate.jsonl`` (``levi.live.gating``): when the GPU gate was closed,
  used to say how much of an episode's wall time labelling was not allowed;
- the agent usage samples (``levi.agent.usage``, table ``usage_samples`` of
  ``<state>/agent/workbench.sqlite3``, opened read-only; plain SQL because
  ``Store()`` creates directories and sets WAL, which a reader must not do);
- the harness cost records (``levi.harness.cost``, the ``cost`` key of
  ``layout.outputs(<state>)/datasets/*/tasks/*/ledger.json``).

``<state>`` is the workbench directory ``<workspace>/outputs/LEVI/workbench``
(``levi.paths.STATE``); the ledgers sit beside it, in ``outputs/LEVI/datasets``.

Paths are passed in; this module never imports ``levi.paths`` (that reads the
checkout's ``.env``). Stage figures LEVI does not record today (frame decode,
presentation-timestamp scan, hashing, peak memory) are listed under
``unmeasured`` instead of being invented.
"""

import hashlib
import json
import sqlite3
from pathlib import Path

from levi.harness import layout
from levi.live import stats

SCHEMA = "levi.performance.trace.v1"
# What the trace would like to show and no record carries yet.
UNMEASURED = (
    "decode_s",
    "pts_scan_s",
    "hash_s",
    "png_encode_s",
    "queue_s",
    "cpu_rss_peak",
    "gpu_memory_peak",
)
_TAIL = 8 * 1024 * 1024


def trace_id(row) -> str:
    """A stable id from fields the record already has (nothing is stored)."""
    parts = [
        str(row.get("dataset")),
        str(row.get("demo")),
        str(row.get("session")),
        str(stats.dig(row, "batch", "id")),
        str(row.get("episode_index")),
    ]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:16]


def read_gate(live_dir) -> list:
    """Gate transitions (oldest first) reduced to ``{at, to: {open, code}}``.

    Rotated files are read too; a damaged line is skipped. The session list in
    the raw lines is dropped: it names rollout groups and tasks."""
    base = Path(live_dir) / "gate.jsonl"
    names = sorted(
        (p for p in base.parent.glob(base.name + ".*") if p.suffix[1:].isdigit()),
        key=lambda p: -int(p.suffix[1:]),
    )
    rows = []
    for path in [*names, base]:
        try:
            with path.open("rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(0, size - _TAIL))
                lines = handle.read().splitlines()
        except OSError:
            continue
        for line in lines[1:] if size > _TAIL else lines:
            try:
                value = json.loads(line)
            except ValueError:
                continue
            at = stats.num(value.get("at")) if isinstance(value, dict) else None
            if at is None:
                continue
            to = value.get("to") if isinstance(value.get("to"), dict) else {}
            rows.append(
                {"at": at, "to": {"open": to.get("open"), "code": to.get("code")}}
            )
    return sorted(rows, key=lambda r: r["at"])


def _diff(later, earlier):
    later, earlier = stats.num(later), stats.num(earlier)
    return None if later is None or earlier is None else round(later - earlier, 2)


def episode_trace(row, gate_rows=None) -> dict:
    """The trace of one ``stats.jsonl`` record (already ``normalize``d)."""
    line = row["timeline"]
    seconds = row["model"]["model_seconds"]
    model_s = stats.total(seconds.get(kind) for kind in stats.PER_EPISODE_KINDS)
    verdict = line["to_verdict_s"]
    start = stats.num(line["completed_at"])
    window = None
    if gate_rows and start is not None and stats.num(verdict) is not None:
        window = stats.gate_window(gate_rows, start, start + verdict)
    return {
        "trace_id": trace_id(row),
        "dataset": row["dataset"],
        "demo": row["demo"],
        "episode_index": row["episode_index"],
        "session": row["session"],
        "at": row["at"],
        "state": stats.dig(row, "result", "state"),
        "frames": stats.dig(row, "episode", "frames"),
        "stages_s": {
            "mirror": line["to_mirror_s"],
            "plan": _diff(line["to_plan_s"], line["to_mirror_s"]),
            "first_request": _diff(line["to_first_request_s"], line["to_plan_s"]),
            "commit": _diff(line["to_commit_s"], line["to_first_request_s"]),
            "verdict": _diff(verdict, line["to_commit_s"]),
            "total": verdict,
        },
        "model": {
            "seconds": model_s,
            "seconds_by_kind": {k: seconds.get(k) for k in stats.PER_EPISODE_KINDS},
            "requests": stats.total(
                row["model"]["requests"].get(k) for k in stats.PER_EPISODE_KINDS
            ),
            "prompt_tokens": row["model"]["prompt_tokens"],
            "completion_tokens": row["model"]["completion_tokens"],
            "images": row["model"]["images"],
        },
        "non_model_s": _diff(verdict, model_s) if model_s is not None else None,
        "gate": {
            "closed_wait_s": stats.dig(row, "gate", "closed_wait_s"),
            "interruptions": stats.dig(row, "gate", "interruptions"),
            "window": window,
        },
    }


def summarize(traces) -> dict:
    """Distributions over the traces (``stats.dist``: n, min, median, p90, max, mean)."""
    pick = lambda *path: [stats.dig(t, *path) for t in traces]
    return {
        "episodes": len(traces),
        "stages_s": {
            name: stats.dist(pick("stages_s", name))
            for name in (
                "mirror",
                "plan",
                "first_request",
                "commit",
                "verdict",
                "total",
            )
        },
        "model_s": stats.dist(pick("model", "seconds")),
        "non_model_s": stats.dist(pick("non_model_s")),
        "prompt_tokens": stats.dist(pick("model", "prompt_tokens")),
        "images": stats.dist(pick("model", "images")),
        "gate_closed_s": stats.dist(pick("gate", "window", "closed_s")),
    }


def workbench_state(workspace) -> Path:
    """The workbench directory of a LEVI workspace. A path that already is one
    (it holds ``agent/``) is taken as it is."""
    root = Path(workspace)
    state = root / "outputs" / "LEVI" / "workbench"
    if not state.is_dir() and (root / "agent").is_dir():
        return root
    return state


def read_usage_samples(workspace, limit=200) -> list:
    """Agent usage samples (``levi.agent.usage``), newest last; [] when the
    workspace has no store. The database is opened read-only."""
    path = workbench_state(workspace) / "agent" / "workbench.sqlite3"
    if not path.is_file():
        return []
    try:
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            found = db.execute(
                "SELECT body FROM records WHERE kind='usage_samples'"
            ).fetchall()
        finally:
            db.close()
    except sqlite3.Error:
        return []
    rows = []
    for (body,) in found:
        try:
            value = json.loads(body)
        except ValueError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    rows.sort(key=lambda r: stats.num(r.get("at")) or 0)
    return rows[-limit:]


def read_costs(workspace, limit=200) -> list:
    """Closed-run cost records (``levi.harness.cost``) from the task ledgers."""
    rows = []
    datasets = layout.outputs(workbench_state(workspace)) / "datasets"
    for path in datasets.glob("*/tasks/*/ledger.json"):
        try:
            cost = json.loads(path.read_text()).get("cost")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(cost, dict):
            rows.append(cost)
    rows.sort(key=lambda r: str(r.get("run_id")))
    return rows[-limit:]


def _usage_brief(row) -> dict:
    keys = (
        "run_id", "agent_key", "workflow", "episodes", "evidence_frames",
        "tokens", "requests", "seconds", "source",
    )  # fmt: skip
    return {key: row.get(key) for key in keys}


def _cost_brief(row) -> dict:
    return {
        "run_id": row.get("run_id"),
        "agent_key": row.get("agent_key"),
        "provider_kind": row.get("provider_kind"),
        "workflow": row.get("workflow"),
        "episodes": row.get("episodes"),
        "evidence_frames": row.get("evidence_frames"),
        "tokens": (row.get("tokens") or {}).get("value"),
        "token_source": (row.get("tokens") or {}).get("source"),
        "seconds": row.get("seconds"),
    }


def build(live_dir=None, workspace=None, since=None, limit=None) -> dict:
    """The merged view. Each source is optional; a missing one is ``null``."""
    out = {
        "schema": SCHEMA,
        "sources": {},
        "unmeasured": list(UNMEASURED),
    }
    if live_dir is not None:
        rows = stats.read(live_dir, limit=limit, since=since)
        gate = read_gate(live_dir)
        traces = [episode_trace(row, gate) for row in rows]
        out["sources"]["live"] = {
            "stats_records": len(rows),
            "gate_transitions": len(gate),
        }
        out["episodes"] = traces
        out["summary"] = summarize(traces)
    if workspace is not None:
        usage = read_usage_samples(workspace)
        costs = read_costs(workspace)
        out["sources"]["workspace"] = {
            "usage_samples": len(usage),
            "cost_records": len(costs),
        }
        out["usage"] = [_usage_brief(r) for r in usage]
        out["cost"] = [_cost_brief(r) for r in costs]
    return out
