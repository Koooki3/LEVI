"""Improvement candidates and the self-improvement state machine (plan §6.6).

A candidate is one file, ``workbench/improvements/<dataset>/<slug>.json``,
named for what it changes. Recurrence of the same finding updates that file
(``duplicate``); a finding with nothing new is a ``no_op``. Only a harness
parameter in ``PARAMETERS`` can be published, within its bounds: validators,
permissions and held-out data are out of reach by construction. A software
defect is filed as a ``software`` candidate with its reproduction facts and
can never be published by the harness -- it waits for a developer.

Publication needs a human. A running task keeps the parameters it was planned
with; a published value reaches the next task through ``snapshot``.
"""

import time

from .layout import improvements_dir, read_json, write_json

TRANSITIONS = {
    "proposed": {"evaluating", "rejected"},
    "evaluating": {"qualified", "rejected"},
    "qualified": {"awaiting_authorization", "rejected"},
    "awaiting_authorization": {"published", "rejected"},
    "published": {"observing"},
    "observing": {"retained", "rolled_back"},
}
# A software candidate is not a harness change: a developer fixes the code
# and a person marks it resolved (or rejects it as not a defect).
SOFTWARE_TRANSITIONS = {
    "proposed": {"evaluating", "resolved", "rejected"},
    "evaluating": {"resolved", "rejected"},
}
HUMAN_ONLY = {"published", "retained", "rolled_back", "resolved"}
ACTIVE = {"published", "observing", "retained"}
CLOSED = {"rejected", "rolled_back", "resolved"}

# What a candidate may change, with the value a task gets when nothing is
# published. Anything else is refused.
PARAMETERS = {
    "evidence.refine_top_k": {
        "type": int,
        "min": 0,
        "max": 4,
        "default": 0,
        "means": "Refine this many coarse intervals per episode, ranked by how "
        "much the picture changes across them, before proposing segments.",
    },
    "evidence.mosaic_tile_width": {
        "type": int,
        "min": 160,
        "max": 480,
        "default": 320,
        "means": "Default tile width of an evidence contact sheet. Image tokens "
        "grow with the square of it; below what this dataset was read at "
        "without rejected proposals, legibility is unproven.",
    },
}

# Triage thresholds. A pattern must recur across episodes before it is worth a
# candidate; one odd episode is a note in the ledger, not a harness change.
SAMPLING_GAP_SHARE = 0.3
SAMPLING_GAP_MIN_EPISODES = 3
OVERSIZED_RESPONSE_BYTES = 16_000
# Cost rules. A run is compared with the same agent's earlier runs on the same
# dataset; one run is not a baseline.
COST_RISE = 1.5
COST_BASELINE_RUNS = 2
SLOW_TOOL_SECONDS = 5.0


def path(state, key, slug):
    return improvements_dir(state, key) / f"{slug}.json"


def load(state, key, slug):
    value = read_json(path(state, key, slug))
    if value is None:
        raise KeyError(f"No improvement candidate {slug!r} for {key}")
    return value


def listing(state, key):
    folder = improvements_dir(state, key)
    return [read_json(p) for p in sorted(folder.glob("*.json"))]


def _check_target(target):
    spec = PARAMETERS.get(target.get("parameter"))
    if spec is None:
        raise ValueError(
            "Only these harness parameters can change: " + ", ".join(PARAMETERS)
        )
    value = target.get("value")
    if not isinstance(value, spec["type"]) or not (spec["min"] <= value <= spec["max"]):
        raise ValueError(
            f"{target['parameter']} must be {spec['type'].__name__} in "
            f"[{spec['min']}, {spec['max']}]"
        )


def _history(candidate, state, by, note=""):
    candidate["state"] = state
    candidate["history"].append(
        {"state": state, "at": time.time(), "by": by, "note": note}
    )


def file_finding(state, key, *, slug, kind, title, finding, target=None, criteria=""):
    """Triage one finding: new candidate, duplicate of an open one, or no-op.

    Returns ``(outcome, candidate)`` with outcome one of proposed, duplicate,
    no_op.
    """
    if kind == "harness":
        _check_target(target)
    existing = read_json(path(state, key, slug))
    if existing:
        seen = {item["run_id"] for item in existing["evidence"]}
        if finding["run_id"] in seen:
            return "no_op", existing
        if existing["state"] in CLOSED:
            # Rejected or rolled back: the same pattern again is not new
            # evidence for the same change. Record that it recurred, only.
            existing.setdefault("recurred_after_close", []).append(finding["run_id"])
            write_json(path(state, key, slug), existing)
            return "no_op", existing
        existing["evidence"].append(finding)
        existing["updated_at"] = time.time()
        write_json(path(state, key, slug), existing)
        return "duplicate", existing
    candidate = {
        "schema": "levi.harness.improvement.v1",
        "slug": slug,
        "dataset_key": key,
        "kind": kind,
        "title": title,
        "target": target,
        "acceptance_criteria": criteria,
        "evidence": [finding],
        "evaluation": None,
        "observations": [],
        "history": [],
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    _history(candidate, "proposed", "closure", "filed by deterministic triage")
    write_json(path(state, key, slug), candidate)
    return "proposed", candidate


def transition(state, key, slug, to, *, by, human, note="", evaluation=None):
    candidate = load(state, key, slug)
    if candidate["kind"] != "harness" and to in {
        "qualified",
        "awaiting_authorization",
        "published",
    }:
        raise ValueError(
            "A software candidate is fixed in code by a developer; the harness "
            "cannot publish it"
        )
    table = TRANSITIONS if candidate["kind"] == "harness" else SOFTWARE_TRANSITIONS
    allowed = table.get(candidate["state"], set())
    if to not in allowed:
        raise ValueError(
            f"{slug} is {candidate['state']}; it can move to "
            f"{sorted(allowed) or 'nothing'}"
        )
    if to in HUMAN_ONLY and not human:
        raise PermissionError(f"Only a person can move a candidate to {to}")
    if to == "qualified":
        result = evaluation or candidate.get("evaluation")
        if not result or not result.get("passed"):
            raise ValueError("Qualify only with an evaluation that passed")
    if evaluation is not None:
        candidate["evaluation"] = evaluation
    _history(candidate, to, by, note)
    if to == "published":
        _history(candidate, "observing", "levi", "applies to tasks planned from now")
    candidate["updated_at"] = time.time()
    write_json(path(state, key, slug), candidate)
    if to in {"published", "retained", "rolled_back"}:
        from .memory import record_lesson

        record_lesson(
            state,
            key,
            {
                "slug": slug,
                "text": candidate["title"],
                "status": candidate["state"],
                "target": candidate["target"],
                "at": time.time(),
            },
        )
    return candidate


MAX_REVISIONS = 1


def revise(state, key, slug, value, *, by, note=""):
    """Change the candidate's value once, while it is being evaluated.

    Plan §6.6: at most one revision, then a retest. The previous evaluation is
    kept in the history and cleared, so the revised value must pass its own.
    """
    candidate = load(state, key, slug)
    if candidate["state"] != "evaluating":
        raise ValueError("Only a candidate being evaluated can be revised")
    if len(candidate.get("revisions", [])) >= MAX_REVISIONS:
        raise ValueError(
            "This candidate was already revised once; reject it or file a new one"
        )
    target = {**candidate["target"], "value": value}
    _check_target(target)
    candidate.setdefault("revisions", []).append(
        {
            "from": candidate["target"]["value"],
            "to": value,
            "previous_evaluation": candidate.get("evaluation"),
            "by": by,
            "note": note,
            "at": time.time(),
        }
    )
    candidate["target"] = target
    candidate["evaluation"] = None
    candidate["updated_at"] = time.time()
    write_json(path(state, key, slug), candidate)
    return candidate


def snapshot(state, key):
    """Parameters a task planned now runs with, and where each came from."""
    values = {name: spec["default"] for name, spec in PARAMETERS.items()}
    sources = {}
    for candidate in listing(state, key):
        if candidate["kind"] == "harness" and candidate["state"] in ACTIVE:
            parameter = candidate["target"]["parameter"]
            values[parameter] = candidate["target"]["value"]
            sources[parameter] = candidate["slug"]
    return {"parameters": values, "sources": sources}


def observe(state, key, slug, observation):
    """A task that ran with a published candidate reports how it went."""
    candidate = load(state, key, slug)
    if observation["run_id"] in {o["run_id"] for o in candidate["observations"]}:
        return candidate
    candidate["observations"].append(observation)
    candidate["updated_at"] = time.time()
    write_json(path(state, key, slug), candidate)
    return candidate


def triage(state, ledger, before=None, tile_floor=None):
    """Deterministic triage of a closed task: no model call, bounded work.

    ``before`` is this agent's cost profile on the dataset before the run;
    ``tile_floor`` the narrowest tile width a committed, unrejected run on this
    dataset was read at (see evaluation.tile_floor).
    """
    key = ledger["dataset_key"]
    outcomes = []
    processed = [e for e in ledger["episodes"] if e.get("processed")]
    from .memory import classify

    gap_episodes = [
        e["episode"]
        for e in processed
        if any("sampling_gap" in classify(w) for w in e.get("warnings", []))
    ]
    if (
        len(gap_episodes) >= SAMPLING_GAP_MIN_EPISODES
        and len(gap_episodes) >= SAMPLING_GAP_SHARE * max(1, len(processed))
        and ledger["harness"].get("parameters", {}).get("evidence.refine_top_k", 0) == 0
    ):
        outcome, candidate = file_finding(
            state,
            key,
            slug="refine-at-coarse-change",
            kind="harness",
            title="Refine where the picture changes most between coarse samples, "
            "instead of where the agent guesses",
            target={"parameter": "evidence.refine_top_k", "value": 2},
            criteria="On episodes whose missed events are known, the top-k ranked "
            "intervals contain at least 90% of those events.",
            finding={
                "run_id": ledger["run_id"],
                "observation": f"{len(gap_episodes)} of {len(processed)} episodes "
                "carry warnings that an event may fall between coarse samples",
                "episodes": gap_episodes,
            },
        )
        outcomes.append({"slug": candidate["slug"], "outcome": outcome})
    for tool, row in ledger["tools"].items():
        if (
            row["calls"]
            and row["response_bytes"] / row["calls"] > OVERSIZED_RESPONSE_BYTES
        ):
            outcome, candidate = file_finding(
                state,
                key,
                slug=f"oversized-{tool.replace('.', '-')}-responses",
                kind="software",
                title=f"{tool} returns about {row['response_bytes'] // row['calls']} "
                "bytes per call; the agent pays for every byte",
                finding={
                    "run_id": ledger["run_id"],
                    "observation": f"{row['calls']} calls, "
                    f"{row['response_bytes']} response bytes",
                    "reproduce": f"call {tool} {row['calls']} times in one run and "
                    "compare response sizes",
                },
            )
            outcomes.append({"slug": candidate["slug"], "outcome": outcome})
    cost = ledger.get("cost") or {}
    before = before or {}
    outcomes += _cost_findings(state, key, ledger, cost, before, tile_floor)
    for parameter, slug in ledger["harness"].get("sources", {}).items():
        observe(
            state,
            key,
            slug,
            {
                "run_id": ledger["run_id"],
                "parameter": parameter,
                "episodes_completed": ledger["episodes_completed"],
                "sampling_gap_episodes": len(gap_episodes),
                "processed_episodes": len(processed),
                "wall_seconds": ledger["wall_seconds"],
                # The cost side of the trade, against this agent's history here.
                "agent_key": cost.get("agent_key"),
                "tokens_per_episode": (cost.get("tokens") or {}).get("per_episode"),
                "token_source": (cost.get("tokens") or {}).get("source"),
                "seconds_per_episode": (cost.get("seconds") or {}).get("per_episode"),
                "baseline_tokens_per_episode": before.get("tokens_per_episode"),
                "baseline_seconds_per_episode": before.get("seconds_per_episode"),
            },
        )
        outcomes.append({"slug": slug, "outcome": "observed"})
    return outcomes or [{"outcome": "no_op"}]


def _slug(text):
    import re

    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:60]


def _cost_findings(state, key, ledger, cost, before, tile_floor):
    """Token and time: what to watch and what the harness itself can lower."""
    found = []
    tokens = (cost.get("tokens") or {}).get("per_episode")
    seconds = (cost.get("seconds") or {}).get("per_episode")
    if before.get("runs", 0) >= COST_BASELINE_RUNS:
        rises = []
        base = before.get("tokens_per_episode")
        if tokens and base and tokens > COST_RISE * base:
            rises.append(f"tokens/episode {int(base)} → {tokens}")
        base = before.get("seconds_per_episode")
        if seconds and base and seconds > COST_RISE * base:
            rises.append(f"seconds/episode {base} → {seconds}")
        if rises:
            outcome, candidate = file_finding(
                state,
                key,
                slug=f"cost-rise-{_slug(cost['agent_key'])}",
                kind="cost",
                title=f"{cost['agent_key']} got more expensive on this dataset: "
                + "; ".join(rises),
                finding={
                    "run_id": ledger["run_id"],
                    "observation": "; ".join(rises),
                    "top_cost": (cost.get("breakdown") or [{}])[0],
                    "harness": cost.get("harness"),
                    "model": cost.get("model"),
                },
            )
            found.append({"slug": candidate["slug"], "outcome": outcome})
    for tool, row in ledger["tools"].items():
        spent = row.get("seconds") or 0.0
        if row["calls"] >= 3 and spent / row["calls"] > SLOW_TOOL_SECONDS:
            outcome, candidate = file_finding(
                state,
                key,
                slug=f"slow-{tool.replace('.', '-')}",
                kind="software",
                title=f"{tool} takes {round(spent / row['calls'], 1)} s per "
                "call; the task waits for it",
                finding={
                    "run_id": ledger["run_id"],
                    "observation": f"{row['calls']} calls, {spent} s",
                },
            )
            found.append({"slug": candidate["slug"], "outcome": outcome})
    current = (
        ledger["harness"]
        .get("parameters", {})
        .get(
            "evidence.mosaic_tile_width",
            PARAMETERS["evidence.mosaic_tile_width"]["default"],
        )
    )
    images = next(
        (r for r in cost.get("breakdown", []) if r["part"] == "evidence images"), None
    )
    spec = PARAMETERS["evidence.mosaic_tile_width"]
    if (
        tile_floor
        and images
        and spec["min"] <= tile_floor < current
        and 1 - (tile_floor / current) ** 2 >= 0.15
    ):
        outcome, candidate = file_finding(
            state,
            key,
            slug="narrower-contact-sheets",
            kind="harness",
            title=f"Read contact sheets at {tile_floor} px tiles by default instead "
            f"of {current}: this dataset was already read that way without a "
            "rejected proposal",
            target={"parameter": "evidence.mosaic_tile_width", "value": tile_floor},
            criteria="The value is no narrower than a width a committed run on this "
            "dataset was read at with no rejected proposal, and saves at least 15% "
            "of image tokens.",
            finding={
                "run_id": ledger["run_id"],
                "observation": f"evidence images were {round(images['share'] * 100)}% "
                f"of what the agent received; narrowest proven tile {tile_floor} px",
            },
        )
        found.append({"slug": candidate["slug"], "outcome": outcome})
    return found
