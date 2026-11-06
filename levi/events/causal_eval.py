"""Replay raw captures step by step through the causal trigger and score it.

    python -m levi.events.causal_eval --root <dir> --gold <dir> [--gold <dir> ...]
        --split <split.json> --role dev|confirm
        --exclusion-list <list.json> [--exclusion-list <list.json> ...]
        [--params <dev-report.json>] [--budget-per-minute 6]
        [--max-latency 3] [--change-point-penalty P]
        [--timing] [--pace recorded|fast]
        [--map OLD=NEW ...] [--exclude-source TEXT ...] [--out report.json]

Each episode is fed, one recorded row at a time, to ``stream.CausalSignals``
and the ``causal.Trigger`` over it (the row's time as both the observation
and the current time). Its looks are scored against the reference
boundaries with ``boundary_metrics`` at ``TOLERANCES``; the whole-episode
``candidates.read`` of the same episodes is scored the same way, as the
offline upper bound. Reads, never writes (except ``--out``).

Inputs are those of ``calibrate`` (one gold JSON per episode with
``source``, ``frames`` and ``segments``; captures through
``levi.conversion.raw.load``; never video, except hashing it for the
exclusion check). Development and confirmation never mix:

- ``--split``: ``{"dev": [names], "confirm": [names]}``, a name being the
  gold file's name without ``.json`` (so another role's gold file is never
  opened). Only the ``--role`` list is loaded; an id in both lists, or a
  role that is not in the file, refuses the run.
- ``--role dev`` refuses ``--params``; ``--role confirm`` refuses a dev
  report made with another split (by the split file's sha256).
- ``--role dev`` derives the parameters (``derive``) and chooses the score
  threshold (``choose``) on these episodes; ``--role confirm`` requires
  ``--params``, a report of a dev run, uses its parameters unchanged and
  chooses nothing; it refuses when an episode of the dev run (by id or by
  capture sha256) is among the confirmation episodes.
- Test sets are refused as ``calibrate.Guard`` refuses them: a gold folder
  or source path containing a ``--refuse`` pattern (``frozen``, ``heldout``,
  ``held-out``), or a source on an exclusion list (by path or by the sha256
  of one of its videos). At least one ``--exclusion-list`` is required (the
  frozen and held-out lists, ``gold/frozen/*.json``, in the
  ``levi.pool.heldout.load`` format); the ``LEVI_POOL_HELDOUT`` lists are
  added. The refusal of a source names only the gold file
  (``refused: source <name> is on a list``): nothing of the refused gold
  file's content reaches the output, the report or the error.

Report fields (``schema_version`` ``levi.causal_trigger_eval.v1``):
``role``, ``split`` (path, sha256), ``guards``, ``params`` (and
``params_from``), ``episodes``, ``skipped``, ``causal`` (``live``: scores of
the decision times -- what a running harness would see; ``retro``: scores
of the rules' event times; ``per_reason``: count, per minute and rule delay;
``latency_vs_reference`` and ``latency_vs_offline``: how long after a
reference boundary or an offline candidate the first look came;
``rejected_samples``, ``clipped_gripper``), ``offline`` (the same scores of
``candidates.read``), ``sweep`` (dev: one row per threshold) and ``cpu``
(with ``--timing``).

``--timing`` measures the CPU time of every ``Trigger.feed`` (stream
included): per step, its share of one core at the recorded step period, the
mean and the peak, against ``causal.CPU_BUDGET_PERCENT``. Meaningful only
with ``--pace recorded`` (rows fed at their recorded pace, by sleeping; no
thread), on the host and at the time the measurement is meant for.
"""

import argparse
import hashlib
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np

from . import boundary_metrics, calibrate, candidates, gripper, motion
from .causal import CPU_BUDGET_PERCENT, REASONS, SCORES, Trigger, TriggerConfig
from .signal_profiles import ChannelSpec, SignalProfile
from .stream import CausalSignals, Sample, StreamConfig

SCHEMA = "levi.causal_trigger_eval.v1"
TOLERANCES = tuple(sorted({*boundary_metrics.TOLERANCES, *calibrate.TOLERANCES}))
CHOOSE_AT = calibrate.CHOOSE_AT
BUDGET_PER_MINUTE = 6.0
MAX_LATENCY = 3.0
# Recorded times start at 0; the stream wants positive nanoseconds.
ORIGIN_NS = 1_000_000_000
STATE, COMMAND = "observation.state", "observation.gripper_command"


def profile(gripper_range):
    """The declared FR3-style profile of a capture: xyz in metres in the
    base frame, the measured width (open = wide) on ``gripper_range`` and
    the last gripper command (open = 1)."""
    channels = [
        ChannelSpec(
            role="position",
            feature=STATE,
            index=i,
            axis=axis,
            units="m",
            frame="base",
            kind="measured",
        )
        for i, axis in enumerate("xyz")
    ]
    channels.append(
        ChannelSpec(
            role="gripper",
            feature=STATE,
            index=6,
            units="m",
            kind="measured",
            open_level="high",
            valid_range=tuple(gripper_range),
        )
    )
    channels.append(
        ChannelSpec(
            role="gripper",
            feature=COMMAND,
            index=0,
            units="binary",
            kind="commanded",
            open_level="high",
            valid_range=(0.0, 1.0),
        )
    )
    return SignalProfile(channels=channels)


def derive(episodes):
    """The dataset-level parameters, from development episodes only: the
    offline readers' rules with every whole-episode quantity replaced by one
    fixed for the dataset -- the gripper range (min and max width over the
    episodes, as ``gripper.read`` with the dataset's range), the still speed
    (``motion.STILL`` of the median over episodes of each one's 95th
    percentile speed) and the height turn (``signals.TURN`` of the median
    height span)."""
    from ..agent.signals import TURN

    widths = np.concatenate([np.asarray(e["width"], float) for e in episodes])
    widths = widths[np.isfinite(widths)]
    fast, spans = [], []
    for e in episodes:
        times = np.asarray(e["times"], float)
        steps = np.diff(times)
        steps = steps[np.isfinite(steps) & (steps > 0)]
        if not len(steps):
            continue
        fps = 1 / float(np.median(steps))
        speed = motion.speeds(times, np.asarray(e["xyz"], float), fps)
        speed = speed[np.isfinite(speed)]
        if len(speed):
            fast.append(float(np.percentile(speed, 95)))
        z = np.asarray(e["xyz"], float)[:, 2]
        z = z[np.isfinite(z)]
        if len(z):
            spans.append(float(z.max() - z.min()))
    if not len(widths) or not fast or not spans:
        raise ValueError("no usable development signal to derive parameters from")
    lo, hi = float(widths.min()), float(widths.max())
    if hi - lo <= 1e-9:
        raise ValueError("the gripper width never changes on the development set")
    still = motion.STILL * float(np.median(fast))
    turn = TURN * float(np.median(spans))
    return {
        "gripper_range": [lo, hi],
        "gripper_low_high": [gripper.LOW, gripper.HIGH],
        "motion_speed_m_s": max(still, 1e-6),
        "still_ns": round(motion.STILL_SECONDS * 1e9),
        "height_turn_m": max(turn, 1e-6),
        "trigger": asdict(TriggerConfig()),
        "derived_from": [e["id"] for e in episodes],
    }


def _stream_config(params):
    base = StreamConfig()
    return StreamConfig(
        max_history=base.max_history,
        max_age_ns=base.max_age_ns,
        still_ns=int(params["still_ns"]),
        motion_speed_m_s=float(params["motion_speed_m_s"]),
        max_gap_ns=base.max_gap_ns,
    )


def replay(episode, params, *, min_score=None, timing=False, pace=False, index=0):
    """Feed one episode's rows in order. Returns the looks (as dicts), the
    rows the stream rejected (a repeated timestamp), the gripper samples
    clipped into the range, and per-step CPU seconds when ``timing``."""
    trigger_cfg = dict(params["trigger"])
    if min_score is not None:
        trigger_cfg["min_score"] = float(min_score)
    lo, hi = params["gripper_range"]
    signals = CausalSignals(
        profile((lo, hi)),
        episode_index=index,
        config=_stream_config(params),
        height_turn_m=float(params["height_turn_m"]),
    )
    trigger = Trigger(signals, TriggerConfig(**trigger_cfg))
    times = np.asarray(episode["times"], float)
    xyz = np.asarray(episode["xyz"], float)
    width = np.asarray(episode["width"], float)
    command = episode.get("command")
    looks, cpu, rejected, clipped, step = [], [], 0, 0, 0
    start_wall = time.monotonic()
    for row, t in enumerate(times):
        if pace:
            wait = start_wall + (t - times[0]) - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        values = {(STATE, i): float(xyz[row, i]) for i in range(3)}
        w = float(width[row])
        if np.isfinite(w) and not lo <= w <= hi:
            clipped += 1
            w = min(max(w, lo), hi)
        values[(STATE, 6)] = w
        if command is not None:
            values[(COMMAND, 0)] = float(command[row])
        ns = ORIGIN_NS + round(float(t) * 1e9)
        began = time.process_time_ns() if timing else 0
        try:
            decision = trigger.feed(Sample(step, ns, values), now_ns=ns)
        except ValueError:
            rejected += 1
            continue
        if timing:
            cpu.append((time.process_time_ns() - began) / 1e9)
        step += 1
        if decision.look:
            out = decision.as_dict()
            # Back on the episode's own clock.
            shift = float(times[0])
            for key in ("time_s", "event_time_s"):
                if out[key] is not None:
                    out[key] = round(out[key] + shift, 6)
            out["row"] = row
            looks.append(out)
    return {
        "looks": looks,
        "rejected": rejected,
        "clipped": clipped,
        "cpu": cpu,
        "steps": step,
    }


def first_after(reference, looks, before, after):
    """For each reference time ``r``, ``t - r`` of the first look ``t`` in
    ``[r - before, r + after]``; None where there is none."""
    looks = np.sort(np.asarray(list(looks), float))
    out = []
    for r in reference:
        lo = np.searchsorted(looks, r - before - boundary_metrics.EPS)
        if lo < len(looks) and looks[lo] <= r + after + boundary_metrics.EPS:
            out.append(float(looks[lo] - r))
        else:
            out.append(None)
    return out


def _stats(values):
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0, "mean": None, "median": None, "p90": None, "max": None}
    a = np.asarray(values, float)
    return {
        "n": len(values),
        "mean": round(float(a.mean()), 4),
        "median": round(float(np.median(a)), 4),
        "p90": round(float(np.percentile(a, 90)), 4),
        "max": round(float(a.max()), 4),
    }


def _latency(reference, looks, max_latency):
    lat, total = [], 0
    for e in reference:
        total += len(reference[e])
        lat += first_after(
            reference[e], looks.get(e, ()), float(CHOOSE_AT), max_latency
        )
    covered = sum(v is not None for v in lat)
    return {
        "references": total,
        "covered": covered,
        "share": round(covered / total, 4) if total else None,
        "window_seconds": [-float(CHOOSE_AT), max_latency],
        **{k: v for k, v in _stats(lat).items() if k != "n"},
    }


def offline(episodes, penalty=None):
    """``{index: [(time, source)]}`` of ``candidates.read``'s kept
    candidates, the whole-episode upper bound."""
    out = {}
    for i, e in enumerate(episodes):
        found = candidates.proposed(
            candidates.read(
                e["table"],
                calibrate.INFO,
                calibrate._profile(),
                episode_index=i,
                change_point_penalty=penalty,
            )
        )
        out[i] = [(c.center_time_s, candidates._source_of(c.event_type)) for c in found]
    return out


def evaluate(episodes, params, *, min_score=None, timing=False, pace=False):
    """The causal section of the report, and the replays."""
    reference = {i: e["boundaries"] for i, e in enumerate(episodes)}
    durations = {i: e["duration"] for i, e in enumerate(episodes)}
    runs = {
        i: replay(e, params, min_score=min_score, timing=timing, pace=pace, index=i)
        for i, e in enumerate(episodes)
    }
    live = {i: [x["time_s"] for x in r["looks"]] for i, r in runs.items()}
    retro = {i: [x["event_time_s"] for x in r["looks"]] for i, r in runs.items()}
    minutes = sum(max(0.0, float(d)) for d in durations.values()) / 60
    per_reason = {}
    for reason in REASONS:
        looks = [x for r in runs.values() for x in r["looks"] if x["reason"] == reason]
        per_reason[reason] = {
            "count": len(looks),
            "per_minute": round(len(looks) / minutes, 3) if minutes else None,
            "rule_delay_seconds": _stats([x["delay_s"] for x in looks]),
            "cues": sorted({x["cue"] for x in looks}),
        }
    return (
        {
            "live": boundary_metrics.boundary_scores(
                reference, live, durations, TOLERANCES
            ),
            "retro": boundary_metrics.boundary_scores(
                reference, retro, durations, TOLERANCES
            ),
            "per_reason": per_reason,
            "rejected_samples": sum(r["rejected"] for r in runs.values()),
            "clipped_gripper": sum(r["clipped"] for r in runs.values()),
        },
        runs,
        live,
    )


def cpu_report(runs, episodes):
    per_step, periods = [], []
    for i, r in runs.items():
        per_step += r["cpu"]
        dt = np.diff(np.asarray(episodes[i]["times"], float))
        dt = dt[np.isfinite(dt) & (dt > 0)]
        if len(dt):
            periods.append(float(np.median(dt)))
    if not per_step or not periods:
        return None
    period = float(np.median(periods))
    a = np.asarray(per_step, float)
    mean_pct = float(a.mean()) / period * 100
    return {
        "steps": len(a),
        "step_period_seconds": round(period, 6),
        "cpu_seconds_per_step": {
            "mean": float(a.mean()),
            "p99": float(np.percentile(a, 99)),
            "max": float(a.max()),
        },
        "core_percent_mean": round(mean_pct, 4),
        "core_percent_peak": round(float(a.max()) / period * 100, 4),
        "budget_percent": CPU_BUDGET_PERCENT,
        # The rule: the mean share of one core stays below the budget.
        "within_budget": mean_pct < CPU_BUDGET_PERCENT,
    }


def _thresholds():
    return sorted({0.0, *SCORES.values()})


def choose(sweep, budget):
    """The threshold with the highest live recall at ``CHOOSE_AT`` among
    those making at most ``budget`` looks per minute (ties: the higher
    threshold, fewer looks). When none is within the budget, the highest
    threshold, flagged."""
    rows = [r for r in sweep if (r["looks_per_minute"] or 0) <= budget]
    if not rows:
        return max(r["min_score"] for r in sweep), False
    best = max(r[f"recall@{CHOOSE_AT}"] or 0 for r in rows)
    return max(
        r["min_score"] for r in rows if (r[f"recall@{CHOOSE_AT}"] or 0) >= best
    ), True


def sweep_rows(episodes, params):
    rows = []
    for thr in _thresholds():
        section, _, _ = evaluate(episodes, params, min_score=thr)
        at = section["live"]["at"]
        rows.append(
            {
                "min_score": thr,
                "looks_per_minute": section["live"]["candidates_per_minute"],
                **{f"recall@{k}": at[k]["recall"] for k in at},
                **{f"precision@{k}": at[k]["precision"] for k in at},
            }
        )
    return rows


def load_split(path, role):
    data = json.loads(Path(path).read_text())
    if role not in data or not isinstance(data[role], list):
        raise SystemExit(f"refused: split {path} has no {role!r} list")
    dev, confirm = set(data.get("dev") or []), set(data.get("confirm") or [])
    both = sorted(dev & confirm)
    if both:
        raise SystemExit(f"refused: split lists {both[:5]} as both dev and confirm")
    return set(data[role])


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def collect(folders, root, mapping, exclude, refuse, guard, wanted):
    """Episodes of the ``wanted`` ids, loaded as ``calibrate`` loads them,
    with the capture's gripper command added."""
    from ..conversion import raw

    episodes, skipped = [], []
    for folder in folders:
        for path in calibrate.gold_files(folder, refuse):
            # Matched by file name, so another role's gold is never opened.
            if path.stem not in wanted:
                skipped.append({"gold": str(path), "reason": "not in this role"})
                continue
            try:
                record, reason = calibrate.load_episode(
                    path, root, mapping, exclude, guard
                )
            except SystemExit as exc:
                if not str(exc).startswith("refused"):
                    raise
                # Nothing of a refused gold file -- its source, labels,
                # subtasks or boundaries -- reaches the output: only the
                # gold file's own name. ``from None`` drops the original
                # message from any traceback too.
                raise SystemExit(f"refused: source {path.stem} is on a list") from None
            if record is None:
                skipped.append({"gold": str(path), "reason": reason})
                continue
            *_, command = raw.load(Path(root) / record["source"])
            state = np.stack(record["table"]["observation.state"].to_numpy())
            record.update(
                xyz=state[:, :3], width=state[:, 6], command=np.asarray(command, float)
            )
            episodes.append(record)
    return episodes, skipped


def _identity(e):
    return {"id": e["id"], "source": e["source"], "sha256": e["sha256"]}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m levi.events.causal_eval")
    parser.add_argument("--root", required=True)
    parser.add_argument("--gold", action="append", required=True)
    parser.add_argument("--split", required=True)
    parser.add_argument("--role", choices=("dev", "confirm"), required=True)
    parser.add_argument("--params", help="report of a dev run (required for confirm)")
    parser.add_argument(
        "--exclusion-list",
        action="append",
        required=True,
        help="frozen/held-out list (levi.pool.heldout format); LEVI_POOL_HELDOUT is added",
    )
    parser.add_argument("--refuse", action="append", default=list(calibrate.REFUSE))
    parser.add_argument("--map", action="append", default=[])
    parser.add_argument("--exclude-source", action="append", default=[])
    parser.add_argument("--budget-per-minute", type=float, default=BUDGET_PER_MINUTE)
    parser.add_argument("--max-latency", type=float, default=MAX_LATENCY)
    parser.add_argument("--change-point-penalty", type=float)
    parser.add_argument("--timing", action="store_true")
    parser.add_argument("--pace", choices=("recorded", "fast"), default="fast")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    if args.role == "confirm" and not args.params:
        raise SystemExit("refused: --role confirm needs --params from a dev run")
    if args.role == "dev" and args.params:
        # A dev run derives and chooses its own parameters; borrowed ones
        # would break the chain a confirm run checks.
        raise SystemExit("refused: --role dev derives its parameters; drop --params")
    from ..pool import settings

    lists = [*args.exclusion_list, *map(str, settings.heldout_files())]
    guard = calibrate.Guard(args.root, args.refuse, lists)
    mapping = [tuple(m.split("=", 1)) for m in args.map]
    wanted = load_split(args.split, args.role)
    episodes, skipped = collect(
        args.gold, args.root, mapping, args.exclude_source, args.refuse, guard, wanted
    )
    if not episodes:
        raise SystemExit(f"no {args.role} episode could be read")

    params_from, sweep, chosen_by = None, None, None
    if args.params:
        dev = json.loads(Path(args.params).read_text())
        if dev.get("schema_version") != SCHEMA or dev.get("role") != "dev":
            raise SystemExit("refused: --params must be the report of a dev run")
        split_sha = _sha256(args.split)
        if (dev.get("split") or {}).get("sha256") != split_sha:
            raise SystemExit(
                f"refused: the dev run {args.params} used another split than "
                f"{args.split}"
            )
        seen_ids = {e["id"] for e in dev["episodes"]}
        seen_sha = {d for e in dev["episodes"] for d in e["sha256"].values()}
        for e in episodes:
            if e["id"] in seen_ids or set(e["sha256"].values()) & seen_sha:
                raise SystemExit(f"refused: {e['id']} was in the dev run {args.params}")
        params = dev["params"]
        params_from = {"path": args.params, "sha256": _sha256(args.params)}
    else:
        params = derive(episodes)
        sweep = sweep_rows(episodes, params)
        threshold, within = choose(sweep, args.budget_per_minute)
        params["trigger"]["min_score"] = threshold
        chosen_by = (
            f"highest live recall at {CHOOSE_AT} s with at most "
            f"{args.budget_per_minute:g} looks per minute on the dev episodes"
            + ("" if within else " (none within the budget: highest threshold)")
        )

    section, runs, live = evaluate(
        episodes, params, timing=args.timing, pace=args.pace == "recorded"
    )
    reference = {i: e["boundaries"] for i, e in enumerate(episodes)}
    durations = {i: e["duration"] for i, e in enumerate(episodes)}
    base = offline(episodes, args.change_point_penalty)
    base_times = {i: [t for t, _ in v] for i, v in base.items()}
    section["latency_vs_reference"] = _latency(reference, live, args.max_latency)
    section["latency_vs_offline"] = {
        source: _latency(
            {i: [t for t, s in v if s == source] for i, v in base.items()},
            live,
            args.max_latency,
        )
        for source in candidates.SOURCES
    }
    out = {
        "schema_version": SCHEMA,
        "role": args.role,
        "split": {"path": args.split, "sha256": _sha256(args.split)},
        "guards": guard.record(),
        "params": params,
        "params_from": params_from,
        "chosen_by": chosen_by,
        "tolerances": list(TOLERANCES),
        "episodes": [_identity(e) for e in episodes],
        "skipped": skipped,
        "causal": section,
        "offline": {
            "change_point_penalty": args.change_point_penalty,
            "scores": boundary_metrics.boundary_scores(
                reference, base_times, durations, TOLERANCES
            ),
            "latency_vs_reference": _latency(reference, base_times, args.max_latency),
        },
        "sweep": sweep,
        "cpu": cpu_report(runs, episodes) if args.timing else None,
        "pace": args.pace,
    }
    text = json.dumps(out, indent=1)
    if args.out:
        Path(args.out).write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
