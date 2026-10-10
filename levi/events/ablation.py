"""Equal-budget ablation of event candidates in the boundary refinement.

Scaffolding: nothing here calls a model, decodes a video or reads a dataset.
Two questions, two parts:

1. **Where would each arm look?** (``plan``, model-free.) For each episode,
   the frames each arm's refinement reads under the *same* budget -- the
   plan's frame cap and the model's image limit -- and how many reference
   boundaries have a refined frame within a tolerance. Arms:

   - ``off``: today's rule without event intelligence: the draft's
     boundaries plus the published change windows, the least changed
     dropped first until the rest fit (``sampling.legacy``);
   - ``candidates``: the draft's boundaries, then event candidate windows,
     then the change windows (``sampling.plan``, as a plan with
     ``workflow.event_intelligence`` runs);
   - ``candidates_only``: the draft's boundaries and event candidate
     windows, no change windows.

2. **Did it annotate better?** (``score``.) Given each arm's finished
   annotation of the same episodes (from runs made with the same model,
   decoding, episodes and budget; only ``event_intelligence`` differs), the
   multi-tolerance boundary scores and segment F1 per arm and each arm's
   difference from ``off`` (``boundary_metrics``). Producing those
   annotations needs the local model: that is the GPU-window part
   (docs/EVENTS.md, "Pending GPU window").

Inputs are JSON (formats below and in docs/EVENTS.md); the command line
refuses any input path naming a frozen or held-out set, and writes only to
stdout or ``--out``. Tune nothing on a test set: compare on development
episodes, report on episodes that were not used to choose.

Plan spec::

    {"settings": {"window": 1.0, "spacing": 0.1, "cameras": 1, "cap": 96,
                  "limit": null, "max_windows": 4, "tolerances": [0.2, 0.5]},
     "episodes": {"<name>": {"times": [...], "draft": [s, ...],
                             "candidates": [{"at": s, "id": "..."}, ...],
                             "visual": [s, ...], "reference": [s, ...]}}}

``candidates`` and ``visual`` in priority order; ``reference`` the
boundaries the arms are judged against.

Score input: ``--reference`` and each ``--arm name=<file>`` map an episode
to its segments (``start``, ``end``, ``subtask``); ``--durations`` maps an
episode to its length in seconds.
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np

from . import boundary_metrics, sampling

ARMS = ("off", "candidates", "candidates_only")
REFUSE = ("frozen", "heldout", "held-out")
DEFAULTS = {
    "window": 1.0,
    "spacing": 0.1,
    "cameras": 1,
    "cap": 96,
    "limit": None,
    "max_windows": 4,
    "tolerances": [0.2, 0.5],
}


def arm_plan(arm, episode, settings):
    """``{"windows", "positions", "images", "fits"}``: what ``arm``'s
    refinement of one episode reads. ``fits`` is False (and nothing is
    read) when the draft's own boundaries do not fit the budget -- the run
    would coarsen or batch then, the same for every arm."""
    from levi.agent.observations import Overflow

    s = {**DEFAULTS, **settings}
    times = np.asarray(episode["times"], dtype=float)
    common = {
        "required": list(episode.get("draft") or []),
        "window": s["window"],
        "spacing": s["spacing"],
        "cameras": s["cameras"],
        "cap": s["cap"],
        "limit": s["limit"],
    }
    candidates = [
        (c["at"], c.get("id", f"c{i}"), {})
        for i, c in enumerate(episode.get("candidates") or [])
    ]
    visual = [(t, f"v{i}", {}) for i, t in enumerate(episode.get("visual") or [])]
    try:
        if arm == "off":
            windows, positions = sampling.legacy(
                times, ranked=list(episode.get("visual") or []), **common
            )
        elif arm in {"candidates", "candidates_only"}:
            tiers = [(sampling.CANDIDATES, candidates)]
            if arm == "candidates":
                tiers.append((sampling.UNCERTAIN, visual))
            planned = sampling.plan(
                times,
                tiers=tiers,
                max_windows={sampling.CANDIDATES: s["max_windows"]},
                **common,
            )
            windows, positions = planned.windows(), planned.positions
        else:
            raise ValueError(f"Unknown arm {arm!r}; arms: {', '.join(ARMS)}")
    except Overflow:
        return {"windows": [], "positions": [], "images": 0, "fits": False}
    return {
        "windows": [float(w) for w in windows],
        "positions": sorted(int(p) for p in positions),
        "images": len(positions) * max(1, int(s["cameras"])),
        "fits": True,
    }


def coverage(reference, times, positions, tolerance):
    """``(covered, total)``: reference boundaries with a refined frame within
    ``tolerance`` seconds."""
    seen = np.asarray([times[p] for p in positions], dtype=float)
    hits = sum(
        1
        for r in reference
        if len(seen) and float(np.min(np.abs(seen - float(r)))) <= tolerance + 1e-9
    )
    return hits, len(reference)


def plan(spec, arms=ARMS):
    """Per arm, over the spec's episodes: images read (total, mean, most),
    episodes whose draft did not fit, and reference coverage at each
    tolerance (``covered / total``); per episode, each arm's windows."""
    settings = {**DEFAULTS, **(spec.get("settings") or {})}
    episodes = spec.get("episodes") or {}
    out = {"settings": settings, "arms": {}, "episodes": {}}
    for arm in arms:
        images, unfit = [], 0
        covered = {str(t): [0, 0] for t in settings["tolerances"]}
        for name, episode in sorted(episodes.items()):
            result = arm_plan(arm, episode, settings)
            out["episodes"].setdefault(name, {})[arm] = {
                "windows": [round(w, 3) for w in result["windows"]],
                "images": result["images"],
                "fits": result["fits"],
            }
            images.append(result["images"])
            unfit += not result["fits"]
            times = np.asarray(episode["times"], dtype=float)
            for t in settings["tolerances"]:
                hit, total = coverage(
                    episode.get("reference") or [], times, result["positions"], t
                )
                covered[str(t)][0] += hit
                covered[str(t)][1] += total
        out["arms"][arm] = {
            "images_total": int(sum(images)),
            "images_mean": float(np.mean(images)) if images else 0.0,
            "images_most": int(max(images)) if images else 0,
            "draft_did_not_fit": unfit,
            "reference_covered": {
                t: {
                    "covered": c,
                    "total": n,
                    "share": c / n if n else None,
                }
                for t, (c, n) in covered.items()
            },
        }
    return out


def _boundaries(segments_by_episode):
    return {
        e: boundary_metrics.boundaries(segments)
        for e, segments in segments_by_episode.items()
    }


def score(reference, arms, durations, tolerances=(0.1, 0.2, 0.5)):
    """Per arm, its boundary scores and segment F1 against ``reference``
    (episode -> segments), and its difference from ``off`` when ``off`` is
    one of the arms (positive = better than off)."""
    ref_bounds = _boundaries(reference)
    out = {}
    for name, segments in arms.items():
        out[name] = {
            "boundaries": boundary_metrics.boundary_scores(
                ref_bounds, _boundaries(segments), durations, tolerances
            ),
            "segments": boundary_metrics.segment_f1(reference, segments),
        }

    def minus(a, b):
        return None if a is None or b is None else round(a - b, 4)

    if "off" in out:
        base = out["off"]
        for name, value in out.items():
            if name == "off":
                continue
            value["minus_off"] = {
                "segment_f1_mean": {
                    k: minus(value["segments"][k]["mean"], base["segments"][k]["mean"])
                    for k in value["segments"]
                },
                "boundary_f1": {
                    k: minus(
                        value["boundaries"]["at"][k]["f1"],
                        base["boundaries"]["at"][k]["f1"],
                    )
                    for k in value["boundaries"]["at"]
                },
            }
    return out


def _refuse(path):
    lowered = str(Path(path).resolve()).lower()
    hit = next((word for word in REFUSE if word in lowered), None)
    if hit:
        raise SystemExit(
            f"Refusing {path}: its path names a {hit} set; an ablation never "
            "reads a test set"
        )
    return Path(path)


def _load(path):
    return json.loads(_refuse(path).read_text())


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m levi.events.ablation",
        description="Equal-budget ablation of event candidates (no model call)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan", help="Where each arm's refinement would look")
    p.add_argument("--spec", required=True)
    p.add_argument("--arm", action="append", choices=ARMS)
    s = sub.add_parser("score", help="Compare finished annotations of the arms")
    s.add_argument("--reference", required=True)
    s.add_argument("--durations", required=True)
    s.add_argument("--arm", action="append", required=True, metavar="NAME=FILE")
    for each in (p, s):
        each.add_argument("--out")
    args = parser.parse_args(argv)
    if args.command == "plan":
        result = plan(_load(args.spec), tuple(args.arm or ARMS))
    else:
        arms = {}
        for item in args.arm:
            name, _, path = item.partition("=")
            if not name or not path:
                parser.error("--arm takes NAME=FILE")
            arms[name] = _load(path)
        durations = {k: float(v) for k, v in _load(args.durations).items()}
        result = score(_load(args.reference), arms, durations)
    text = json.dumps(result, indent=1, sort_keys=True) + "\n"
    if args.out:
        _refuse(args.out)
        Path(args.out).write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
