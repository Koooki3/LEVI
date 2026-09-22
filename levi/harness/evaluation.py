"""Evaluate a harness candidate against evidence LEVI already holds.

The agent names the probe cases (where a known event is); LEVI runs the
measurement itself on the stored evidence, so a candidate qualifies on what
LEVI computed, not on what the agent says it computed.
"""

import time


def refine_top_k(store, candidate, probes):
    """Share of known events that fall inside the top-k ranked intervals."""
    from levi.agent.observations import rank_intervals

    k = candidate["target"]["value"]
    rows = []
    for probe in probes:
        run = store.get("runs", probe["run_id"])
        if run["dataset_key"] != candidate["dataset_key"]:
            raise ValueError("Probes must come from the candidate's own dataset")
        record = store.get("evidence", f"{run['id']}:{probe['episode']}")
        ranked = rank_intervals(
            store.run_dir(run["id"]) / "evidence",
            record["items"],
            run["context"]["workflow"]["coarse_step_seconds"],
        )
        hit = next(
            (
                position
                for position, row in enumerate(ranked)
                if row["from"] < probe["end"] and row["to"] > probe["start"]
            ),
            None,
        )
        rows.append(
            {
                **probe,
                "episode": f"episode_{probe['episode']:06d}",
                "intervals": len(ranked),
                "rank": hit,
                "covered": hit is not None and hit < k,
            }
        )
    covered = sum(row["covered"] for row in rows)
    rate = covered / len(rows) if rows else 0.0
    return {
        "method": "rank coarse intervals by frame change; an event is covered "
        "when one of the top-k intervals overlaps it",
        "k": k,
        "probes": rows,
        "covered": covered,
        "total": len(rows),
        "hit_rate": round(rate, 3),
        "threshold": 0.9,
        "passed": bool(rows) and rate >= 0.9,
        "at": time.time(),
    }


def tile_floor(store, key):
    """Narrowest contact-sheet tile width a committed run on this dataset was
    read at, among runs whose changeset had no rejected proposal: widths this
    data has been shown to be legible at, by a reviewer's acceptance."""
    import re

    widths = []
    for run in store.list("runs"):
        if run.get("dataset_key") != key or not run.get("changes"):
            continue
        try:
            change = store.get("changes", run["changes"])
        except KeyError:
            continue
        if (
            change.get("status") != "committed"
            or "rejected" in (change.get("decisions") or {}).values()
        ):
            continue
        for sheet in run.get("sheets", []):
            found = re.search(r"-w(\d+)\.png$", sheet)
            if found:
                widths.append(int(found.group(1)))
    return min(widths) if widths else None


def mosaic_tile_width(store, candidate, probes):
    from .improvements import PARAMETERS

    value = candidate["target"]["value"]
    floor = tile_floor(store, candidate["dataset_key"])
    default = PARAMETERS["evidence.mosaic_tile_width"]["default"]
    savings = 1 - (value / default) ** 2
    return {
        "method": "the value must be no narrower than a width a committed, "
        "unrejected run on this dataset was read at, and save >= 15% of image "
        "tokens against the default",
        "value": value,
        "legible_floor": floor,
        "image_token_savings": round(savings, 3),
        "passed": floor is not None and value >= floor and savings >= 0.15,
        "at": time.time(),
    }


EVALUATORS = {
    "evidence.refine_top_k": refine_top_k,
    "evidence.mosaic_tile_width": mosaic_tile_width,
}
NEEDS_PROBES = {"evidence.refine_top_k"}


def evaluate(store, candidate, probes):
    parameter = (candidate.get("target") or {}).get("parameter")
    evaluator = EVALUATORS.get(parameter)
    if evaluator is None:
        raise ValueError("This candidate has no deterministic evaluator")
    if parameter in NEEDS_PROBES and not probes:
        raise ValueError("Name at least one probe case with a known event")
    return evaluator(store, candidate, probes)
