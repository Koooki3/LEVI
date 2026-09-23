"""Token and time cost of a run, one shape for every kind of agent.

Whoever did the work -- an API model or a local Ollama VLM that LEVI ran
itself, or an external MCP agent spending its own tokens -- a closed run gets
the same cost record, so runs can be compared and the harness can learn what
makes this dataset cheaper. Three token figures, never mixed silently:

- ``metered``: LEVI ran the model and counted every token (API, Ollama);
- ``reported``: the agent's own account (covers its reasoning; external);
- ``delivered``: what LEVI sent the agent -- a measured lower bound.

The best available one is ``tokens.value`` and ``tokens.source`` says which.
Time is split into wall, LEVI tool time, model time (metered runs) and the
remainder spent on the agent's side.

Profiles are kept per dataset and per agent key (see ``usage.agent_key``) in
the dataset's memory, so a change in model, provider or harness parameter
shows up as a change against *this data's* history, not a global average.
"""

import statistics
from collections import Counter

# Keep this many runs per agent key in a dataset's cost profile.
HISTORY = 12


def provider_kind(run):
    context = run["context"]
    if context.get("pilot_runtime"):
        return "pilot"
    provider = run.get("provider_config") or {}
    kind = provider.get("kind") or context.get("provider")
    if kind in {"external", "local-tools", "ollama"}:
        return kind
    return "api"


def _duplicates(events):
    """Evidence pages read more than once with the same parameters."""
    seen = Counter()
    for event in events:
        if (
            event.get("type") != "action.started"
            or event.get("tool") != "evidence.read"
        ):
            continue
        if event.get("channel", "agent") != "agent":
            continue
        p = event.get("parameters") or {}
        seen[
            (
                p.get("episode"),
                p.get("offset"),
                p.get("limit"),
                p.get("layout"),
                p.get("tile_width"),
            )
        ] += 1
    return sum(count - 1 for count in seen.values() if count > 1)


def _refused(events):
    """The agent's calls LEVI refused, per capability: each one is a round
    trip the agent paid for and then repeated. In the full-dataset runs they
    were validation refusals that a clearer contract removed."""
    counts = Counter(
        event.get("tool")
        for event in events
        if event.get("type") == "action.failed"
        and event.get("channel", "agent") == "agent"
    )
    return dict(counts.most_common())


def of(ledger, run, events):
    from levi.agent.usage import agent_key

    from .ledger import BYTES_PER_TOKEN, PIXELS_PER_TOKEN

    completed = max(1, ledger["episodes_completed"])
    tools = ledger["tools"]
    delivered = ledger["tokens"]["delivered"]["estimated_input_tokens"]
    reported = (ledger["tokens"].get("reported") or {}).get("tokens")
    metered = run.get("tokens") or None
    if metered:
        value, source = metered, "metered"
    elif reported:
        value, source = reported, "reported"
    else:
        value, source = delivered, "delivered_lower_bound"
    tool_seconds = round(sum(t["seconds"] for t in tools.values()), 2)
    model_seconds = round(run.get("elapsed_seconds") or 0, 2) if metered else None
    wall = ledger.get("wall_seconds")
    text_tokens = {
        name: row["response_bytes"] // BYTES_PER_TOKEN for name, row in tools.items()
    }
    image_tokens = sum(t["image_pixels"] for t in tools.values()) // PIXELS_PER_TOKEN
    total_delivered = max(1, delivered)
    breakdown = sorted(
        (
            {
                "part": f"{name} text",
                "tokens": tokens,
                "share": round(tokens / total_delivered, 3),
            }
            for name, tokens in text_tokens.items()
            if tokens
        ),
        key=lambda row: -row["tokens"],
    )
    if image_tokens:
        breakdown.insert(
            0,
            {
                "part": "evidence images",
                "tokens": image_tokens,
                "share": round(image_tokens / total_delivered, 3),
            },
        )
        breakdown.sort(key=lambda row: -row["tokens"])
    frames = sum(e.get("evidence_frames", 0) for e in ledger["episodes"])
    return {
        "schema": "levi.harness.cost.v1",
        "run_id": ledger["run_id"],
        "agent_key": agent_key(run),
        "provider_kind": provider_kind(run),
        "model": (run.get("provider_config") or {}).get("model"),
        "workflow": ledger["workflow"],
        "harness": ledger.get("harness", {}).get("parameters", {}),
        "episodes": ledger["episodes_completed"],
        "evidence_frames": frames,
        "tokens": {
            "value": value,
            "source": source,
            "metered": metered,
            "reported": reported,
            "delivered": delivered,
            "per_episode": value // completed if value else None,
            "per_frame": round(value / frames, 1) if value and frames else None,
        },
        "seconds": {
            "wall": wall,
            "tool": tool_seconds,
            "model": model_seconds,
            "agent_side": round(wall - tool_seconds - (model_seconds or 0), 1)
            if wall
            else None,
            "per_episode": round(wall / completed, 1) if wall else None,
        },
        "breakdown": breakdown[:8],
        "waste": {
            "duplicate_evidence_reads": _duplicates(events),
            "refused_calls": _refused(events),
        },
    }


def _median(rows, path):
    values = []
    for row in rows:
        value = row
        for key in path:
            value = (value or {}).get(key)
        if value:
            values.append(value)
    return round(statistics.median(values), 1) if values else None


def fold(profiles, record):
    """Add one run to the profile of its agent key; returns (profile, previous median)."""
    profile = profiles.setdefault(record["agent_key"], {"runs": []})
    # Folding a run again (late usage report) compares it with the others only.
    profile["runs"] = [r for r in profile["runs"] if r["run_id"] != record["run_id"]]
    before = {
        "tokens_per_episode": _median(profile["runs"], ("tokens", "per_episode")),
        "seconds_per_episode": _median(profile["runs"], ("seconds", "per_episode")),
        "runs": len(profile["runs"]),
    }
    profile["runs"].append(
        {
            "run_id": record["run_id"],
            "provider_kind": record["provider_kind"],
            "model": record["model"],
            "harness": record["harness"],
            "tokens": {
                k: record["tokens"][k] for k in ("per_episode", "source", "value")
            },
            "seconds": {k: record["seconds"][k] for k in ("per_episode", "wall")},
            "top_cost": record["breakdown"][0]["part"] if record["breakdown"] else None,
        }
    )
    profile["runs"] = profile["runs"][-HISTORY:]
    profile["median"] = {
        "tokens_per_episode": _median(profile["runs"], ("tokens", "per_episode")),
        "seconds_per_episode": _median(profile["runs"], ("seconds", "per_episode")),
    }
    profile["latest"] = profile["runs"][-1]
    return profile, before


def hints(record, previous):
    """Plain advice for the next run on this dataset, from measured facts."""
    advice = []
    if record["waste"]["duplicate_evidence_reads"]:
        advice.append(
            f"{record['waste']['duplicate_evidence_reads']} evidence page(s) were "
            "read twice; keep the ids from the mosaic answer instead of re-reading."
        )
    refused = record["waste"].get("refused_calls") or {}
    if sum(refused.values()) >= 3:
        worst = ", ".join(f"{tool} {n}" for tool, n in list(refused.items())[:3])
        advice.append(
            f"{sum(refused.values())} call(s) were refused ({worst}); read the "
            "refusal once and fix every item it names before calling again."
        )
    for row in record["breakdown"][:2]:
        if row["share"] >= 0.3:
            advice.append(
                f"{row['part']} was {round(row['share'] * 100)}% of what the agent "
                "received."
            )
    per_episode = record["tokens"]["per_episode"]
    base = previous.get("tokens_per_episode")
    if per_episode and base and previous.get("runs", 0) >= 1:
        change = per_episode / base - 1
        if abs(change) >= 0.2:
            advice.append(
                f"Tokens per episode {'rose' if change > 0 else 'fell'} "
                f"{abs(round(change * 100))}% against this agent's earlier runs here "
                f"({int(base)} → {per_episode})."
            )
    return advice
