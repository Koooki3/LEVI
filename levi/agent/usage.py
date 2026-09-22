"""Measured annotation cost, and an estimate built from it.

LEVI cannot meter an external agent's context: Codex, Claude Code or an MCP
client spends its own tokens outside this process. So the cost model is built
from what each agent reports plus what LEVI itself measured (episodes,
evidence frames, how they were read). Nothing is inferred when there is no
history — an unknown cost is reported as unknown, never as zero.

Samples are grouped per *agent key* (a provider model id, a pilot runtime or
an external connection), because a mosaic-reading MCP client and a
single-frame API model do not cost the same per frame.
"""

import statistics
import time

SCHEMA = "levi.agent.usage.v1"
# Used only until an agent key has its own samples, and as the *shape* that
# history calibrates: a run has a fixed cost (reading the plan, writing the
# proposal) plus a marginal cost per episode and per evidence frame. The frame
# rate depends on how evidence is read -- eight frames in one contact sheet
# cost far less than eight separate images -- so it is split by reading mode.
DEFAULTS = {
    "temporal": {
        "per_run": 6000,
        "per_episode": 2500,
        "per_evidence_frame": {"mosaic": 300, "single": 900},
    },
    "objects": {
        "per_run": 6000,
        "per_episode": 1500,
        "per_evidence_frame": {"mosaic": 250, "single": 700},
    },
    "review": {
        "per_run": 4000,
        "per_episode": 1200,
        "per_evidence_frame": {"mosaic": 300, "single": 900},
    },
}
# Beyond this multiple of the largest recorded run, the estimate is an
# extrapolation and says so instead of pretending to the same accuracy.
EXTRAPOLATION = 3.0


def agent_key(run):
    """Which cost profile this run belongs to."""
    context = run["context"]
    if context.get("pilot_runtime"):
        return f"pilot:{context['pilot_runtime']}"
    provider = run.get("provider_config") or {}
    if provider.get("kind") == "external" or context.get("provider") == "external":
        return "external:" + str(provider.get("name") or "mcp")
    return "model:" + str(provider.get("model") or provider.get("name") or "unknown")


def measured(store, run):
    """What LEVI itself can count for a run, whatever the agent was."""
    episodes = sorted(set(run.get("prepared", []) + run.get("completed", [])))
    frames = 0
    for episode in episodes:
        try:
            frames += len(store.get("evidence", f"{run['id']}:{episode}")["items"])
        except KeyError:
            continue
    sheets = len(run.get("sheets", []))
    return {
        "episodes": len(episodes),
        "evidence_frames": frames,
        "evidence_sheets": sheets,
        "reads": "mosaic" if sheets else "single",
        "workflow": run["context"]["workflow"]["kind"],
    }


def record(store, run, report):
    """Store one completed-task sample; the agent supplies its own token use."""
    sample = {
        "schema": SCHEMA,
        # One readable id per report: run, then how many it has reported.
        "id": f"{run['id']}:reported-"
        f"{sum(1 for key in store.ids('usage_samples') if key.startswith(run['id'] + ':reported')) + 1}",
        "run_id": run["id"],
        "agent_key": agent_key(run),
        "dataset": run["context"]["repo_id"],
        "at": time.time(),
        **measured(store, run),
        "input_tokens": report.get("input_tokens"),
        "output_tokens": report.get("output_tokens"),
        "tokens": (report.get("input_tokens") or 0) + (report.get("output_tokens") or 0)
        or report.get("tokens"),
        "requests": report.get("requests"),
        "seconds": report.get("seconds"),
        "model": report.get("model"),
        "note": report.get("note", ""),
        "source": "self_reported",
    }
    store.put("usage_samples", sample["id"], sample)
    return sample


def measure_run(store, run_id):
    """Record what LEVI itself metered, for runs it can meter.

    An API-model run is billed through LEVI, so its token count is already
    known exactly and no self-report is needed or trusted in its place. The
    sample id is derived from the run, so resuming a run updates its sample
    instead of counting the work twice.
    """
    run = store.get("runs", run_id)
    if not run.get("tokens"):
        # External and Pilot runs spend tokens outside this process; only the
        # agent can report those.
        return None
    sample = {
        "schema": SCHEMA,
        "id": f"{run_id}:measured",
        "run_id": run_id,
        "agent_key": agent_key(run),
        "dataset": run["context"]["repo_id"],
        "at": time.time(),
        **measured(store, run),
        "input_tokens": None,
        "output_tokens": None,
        "tokens": run["tokens"],
        "requests": run.get("requests"),
        "seconds": round(run.get("elapsed_seconds") or 0, 1),
        "model": (run.get("provider_config") or {}).get("model"),
        "note": "Metered by LEVI while running the model.",
        "source": "measured",
    }
    store.put("usage_samples", sample["id"], sample)
    return sample


def samples(store, key=None, workflow=None):
    rows = [
        row
        for row in store.list("usage_samples")
        if row.get("tokens")
        and (key is None or row["agent_key"] == key)
        and (workflow is None or row.get("workflow") == workflow)
    ]
    return sorted(rows, key=lambda row: row["at"])[-50:]


def shape(workflow, episodes, frames, reads="mosaic"):
    """Expected cost structure of a run: fixed + per episode + per frame."""
    table = DEFAULTS.get(workflow) or DEFAULTS["review"]
    per_frame = table["per_evidence_frame"]
    rate = per_frame.get(reads, per_frame["single"])
    return table["per_run"] + table["per_episode"] * episodes + rate * frames


def _calibration(rows):
    """How this agent compares to the published shape, as a median factor."""
    factors = []
    for row in rows:
        expected = shape(
            row.get("workflow") or "review",
            row.get("episodes") or 0,
            row.get("evidence_frames") or 0,
            row.get("reads") or "single",
        )
        if expected > 0:
            factors.append(row["tokens"] / expected)
    return statistics.median(factors) if factors else None


def principal_key(store, principal):
    """The cost profile a caller belongs to, without it having to know its own."""
    if principal is None or getattr(principal, "human", False):
        return None
    for run in reversed(store.list("runs")):
        if (
            run.get("principal") == principal.id
            or run.get("connection") == principal.id
        ):
            return agent_key(run)
    return None


def estimate(
    store,
    run=None,
    *,
    key=None,
    workflow=None,
    episodes=1,
    frames=None,
    reads=None,
    principal=None,
):
    """Token estimate for a planned scope, with its basis and its limits stated.

    Returns a range, not a price: LEVI cannot see an external agent's billing.
    The estimate keeps the fixed/marginal structure of a run rather than
    multiplying one observed per-frame average, because a five-episode sample
    says little about the per-frame cost of a hundred-episode job.
    """
    if run is not None:
        key = key or agent_key(run)
        workflow = workflow or run["context"]["workflow"]["kind"]
        counted = measured(store, run)
        episodes = counted["episodes"] or len(run["context"]["episodes"])
        frames = counted["evidence_frames"] or frames
        reads = reads or counted["reads"]
    key = key or principal_key(store, principal)
    workflow = workflow or "review"
    if frames is None:
        step = (
            max(0.05, run["context"]["workflow"]["coarse_step_seconds"]) if run else 2.0
        )
        frames = int(episodes * max(3, round(20 / step)))
    history = samples(store, key, workflow) or samples(store, key)
    scoped = bool(key) and bool(history)
    if not history and key:
        history = samples(store, None, workflow)
        scoped = False
    reads = reads or (
        max({row.get("reads") or "single" for row in history}, default="mosaic")
        if history
        else "mosaic"
    )
    base = shape(workflow, episodes, frames, reads)
    factor = _calibration(history)
    notes = []
    if factor:
        centre = base * factor
        spread = 0.45 if len(history) < 5 else 0.3
        whose = key if scoped else "all recorded agents"
        basis = (
            f"{len(history)} recorded run(s) for {whose} on {workflow} work, "
            f"applied to the published cost shape ({reads} evidence reading)"
        )
        widest = max(
            (
                max(
                    (row.get("episodes") or 1) / max(episodes, 1),
                    (row.get("evidence_frames") or 1) / max(frames, 1),
                )
                for row in history
            ),
            default=1.0,
        )
        if widest and widest < 1 / EXTRAPOLATION:
            spread += 0.25
            notes.append(
                f"the scope is about {round(1 / widest)}x the largest recorded run, "
                "so this is an extrapolation"
            )
    else:
        centre = base
        spread = 0.6
        basis = (
            "no recorded run yet; published default rates for "
            f"{reads} evidence reading, refined automatically once an agent "
            "reports its first run"
        )
    if reads == "single" and frames >= 16:
        notes.append(
            "reading evidence as contact sheets instead of separate frames is "
            f"worth about {int(base - shape(workflow, episodes, frames, 'mosaic')):,} "
            "tokens here"
        )
    return {
        "agent_key": key,
        "workflow": workflow,
        "episodes": episodes,
        "evidence_frames": frames,
        "reads": reads,
        "tokens": {
            "low": int(centre * (1 - spread)),
            "expected": int(centre),
            "high": int(centre * (1 + spread)),
        },
        "basis": basis,
        "samples": len(history),
        "measured_samples": sum(
            1 for row in history if row.get("source") == "measured"
        ),
        "notes": notes,
        "caveat": "Estimate of the agent's own token use, not a price; LEVI "
        "measures scope, the agent reports what it spent.",
    }


def report_table(store):
    """Per-agent summary for the human CLI."""
    table = {}
    for row in samples(store):
        entry = table.setdefault(
            row["agent_key"],
            {"runs": 0, "tokens": 0, "frames": 0, "episodes": 0, "workflows": set()},
        )
        entry["runs"] += 1
        entry["tokens"] += row["tokens"]
        entry["frames"] += row.get("evidence_frames") or 0
        entry["episodes"] += row.get("episodes") or 0
        entry["workflows"].add(row.get("workflow"))
    for entry in table.values():
        entry["workflows"] = sorted(w for w in entry["workflows"] if w)
        entry["tokens_per_frame"] = (
            round(entry["tokens"] / entry["frames"]) if entry["frames"] else None
        )
        entry["tokens_per_episode"] = (
            round(entry["tokens"] / entry["episodes"]) if entry["episodes"] else None
        )
    return table
