"""Pick a task's episodes: how many, what success share, and which ones.

``recipe.select`` filters first (held-out, excluded, nonstandard, unsupported,
duplicates, outcome filter). This module then chooses, per task and in the
recipe's task order, the episodes that go in (docs/TRAINING_POOL.md,
"Choosing how many episodes a task contributes").

Nothing here is task specific: it reads the index columns only, and the same
(recipe, seed, index) always gives the same list. Preview and export both
call it, so they cannot differ.

The quality score of an episode, in 0..1::

    score = 0.40 * trust + 0.30 * completeness + 0.30 * fit

* trust: a human label 1.0, a demonstration 0.8, the robot's flag 0.6, no
  outcome 0.3, conflicting human labels 0.0;
* completeness: 1 for a frame count between half and twice the task's median,
  falling to 0 at 0.15x (an aborted recording) and at 4x (a run that never
  ended);
* fit, for a success: how efficient it is. Frames are ranked among the task's
  successful candidates; ranks 15%..55% (the lower middle) score 1, the
  shortest (often truncated) a little less, the longest (slow, hesitant) down
  to 0. For a failure: whether it is long enough to hold an attempt, that is
  frames / (half the task's median success length), at most 1.

RECAP advantage labels (a LEVI workspace's ``recap_values``) only break ties
between episodes whose scores are within 0.02.
"""

import random
import re
import statistics
from collections import Counter, defaultdict, deque

W_TRUST, W_COMPLETE, W_FIT = 0.40, 0.30, 0.30
TRUST = {"human": 1.0, "sft_demonstration": 0.8, "robot_flag": 0.6}
TRUST_UNKNOWN = 0.3
COMPLETE_LOW = (0.15, 0.5)  # ratio to the median: 0 at 0.15, 1 from 0.5
COMPLETE_HIGH = (2.0, 4.0)  # 1 up to 2x, 0 at 4x
EFFICIENT_BAND = (0.15, 0.55)
ATTEMPT_FRACTION = 0.5
QUALITY_FLOOR = 0.4  # a stratum's next episode below this waits for a second pass
TIE_BIN = 0.02
DEFAULT_COUNT = 100
STRATEGIES = ("quality", "random", "first")

MESSAGES = {
    "fewer_available": "Fewer episodes are available than requested",
    "success_short": "Not enough successes for the requested share; failures fill in",
    "failure_short": "Not enough failures for the requested share; successes fill in",
    "unknown_outcomes_used": "Episodes with no reliable outcome were needed to reach "
    "the count",
    "no_outcomes": "No episode of this task has a recorded outcome; the success "
    "share is not applied",
    "already_used": "Episodes already picked for an earlier task were skipped",
}


def _natural(text):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", str(text))]


def frames_of(row) -> int:
    return int(row.get("frames") or 0)


def outcome_class(row: dict, *, reliable: bool) -> str:
    """``success``, ``failure`` or ``unknown``. With ``reliable`` an episode
    whose human labels disagree counts as unknown (its outcome is a guess)."""
    if reliable and row.get("label_conflict"):
        return "unknown"
    return (
        row.get("outcome")
        if row.get("outcome") in ("success", "failure")
        else "unknown"
    )


def stratum_of(row: dict) -> str:
    date = str(row.get("date") or "")[:7] or "-"
    return " | ".join(
        [
            str(row.get("policy_method") or "-"),
            str(row.get("policy_checkpoint") or "-"),
            str(row.get("source") or "-"),
            date,
        ]
    )


def _ramp(x: float, lo: float, hi: float) -> float:
    return 0.0 if x <= lo else 1.0 if x >= hi else (x - lo) / (hi - lo)


def completeness(frames: int, median: float) -> float:
    if frames <= 0:
        return 0.0
    if median <= 0:
        return 1.0
    ratio = frames / median
    if ratio < COMPLETE_LOW[1]:
        return _ramp(ratio, *COMPLETE_LOW)
    return 1.0 - _ramp(ratio, *COMPLETE_HIGH)


def efficiency(percentile: float) -> float:
    """1 in the lower middle of the successful episodes' frame counts, down
    to 0 at the longest and about 0.7 at the shortest."""
    low, high = EFFICIENT_BAND
    if low <= percentile <= high:
        return 1.0
    distance = low - percentile if percentile < low else percentile - high
    return max(0.0, 1.0 - distance / (1 - high))


def trust_of(row: dict) -> tuple[float, str]:
    if row.get("label_conflict"):
        return 0.0, "conflicting_labels"
    source = row.get("outcome_source")
    if source in TRUST:
        return TRUST[source], source
    return TRUST_UNKNOWN, "no_outcome"


def score_rows(rows: list[dict]) -> dict[str, dict]:
    """Quality parts and score for every candidate of one task, by key."""
    lengths = [frames_of(r) for r in rows if frames_of(r) > 0]
    median = statistics.median(lengths) if lengths else 0
    wins = sorted(
        frames_of(r) for r in rows if outcome_class(r, reliable=True) == "success"
    )
    win_median = statistics.median(wins) if wins else median
    out = {}
    for row in rows:
        frames = frames_of(row)
        trust, trust_why = trust_of(row)
        complete = completeness(frames, median)
        kind = outcome_class(row, reliable=True)
        if kind == "success" and wins:
            below = sum(1 for w in wins if w < frames)
            equal = sum(1 for w in wins if w == frames)
            fit = efficiency((below + 0.5 * equal) / len(wins))
            fit_why = (
                "efficient"
                if fit >= 0.999
                else "slow"
                if ((below + 0.5 * equal) / len(wins) > EFFICIENT_BAND[1])
                else "very_short"
            )
        elif kind == "failure":
            need = ATTEMPT_FRACTION * win_median
            fit = min(1.0, frames / need) if need > 0 else 1.0
            fit_why = "full_attempt" if fit >= 0.999 else "short_attempt"
        else:
            fit, fit_why = 0.5, "unscored"
        why = [trust_why, fit_why]
        if complete < 0.999:
            why.append("odd_length")
        out[row["key"]] = {
            "score": round(W_TRUST * trust + W_COMPLETE * complete + W_FIT * fit, 4),
            "trust": round(trust, 3),
            "completeness": round(complete, 3),
            "fit": round(fit, 3),
            "reason": why,
        }
    return out


def _rank_key(row, info, kind, recap):
    """Best first: score in coarse bins, then the RECAP signal, then key."""
    bins = -round(info["score"] / TIE_BIN)
    signal = recap.get(row["key"]) if recap else None
    tie = 0.0 if signal is None else (-signal if kind == "success" else signal)
    return (bins, tie, -info["score"], row["key"])


def _quality_pick(rows, k, scores, kind, recap):
    """Best-scoring episodes, spread over strata: the stratum with the fewest
    picks so far goes first (ties: the better next episode); an episode
    below the quality floor waits until no stratum has a better one."""
    if k <= 0:
        return []
    if k >= len(rows):
        return sorted(rows, key=lambda r: _rank_key(r, scores[r["key"]], kind, recap))
    strata = defaultdict(list)
    for row in rows:
        strata[stratum_of(row)].append(row)
    queues = {
        name: deque(
            sorted(members, key=lambda r: _rank_key(r, scores[r["key"]], kind, recap))
        )
        for name, members in strata.items()
    }
    taken: Counter = Counter()
    picked = []
    for floor in (QUALITY_FLOOR, -1.0):
        while len(picked) < k:
            best = None
            for name, queue in queues.items():
                if not queue or scores[queue[0]["key"]]["score"] < floor:
                    continue
                order = (
                    taken[name],
                    _rank_key(queue[0], scores[queue[0]["key"]], kind, recap),
                    name,
                )
                if best is None or order < best[0]:
                    best = (order, name)
            if best is None:
                break
            name = best[1]
            picked.append(queues[name].popleft())
            taken[name] += 1
    return picked


def _pick(rows, k, strategy, scores, kind, recap, seed_text, order_key):
    if k <= 0 or not rows:
        return []
    if strategy == "first":
        return sorted(rows, key=order_key)[:k]
    if strategy == "random":
        ordered = sorted(rows, key=lambda r: r["key"])
        return random.Random(seed_text).sample(ordered, min(k, len(ordered)))
    return _quality_pick(rows, k, scores, kind, recap)


def choose(
    rows: list[dict],
    *,
    task: str,
    count: int | None,
    success_ratio: float | None,
    strategy: str,
    seed: int,
    order_key,
    recap: dict | None = None,
    taken: set | None = None,
) -> tuple[list[dict], dict]:
    """The picked rows (annotated with ``sel_stratum``, ``quality_score``,
    ``selection_reason``), and the task's report. ``taken``: keys and groups
    of episodes an earlier task already picked (not picked again)."""
    taken = taken or set()
    fresh = [
        r
        for r in rows
        if r["key"] not in taken and (r.get("group") or r["key"]) not in taken
    ]
    skipped = len(rows) - len(fresh)
    scores = score_rows(fresh)
    quality = strategy == "quality"
    sides: dict[str, list[dict]] = {"success": [], "failure": [], "unknown": []}
    for row in fresh:
        sides[outcome_class(row, reliable=quality)].append(row)
    successes, failures, unknown = (
        sides["success"],
        sides["failure"],
        sides["unknown"],
    )
    wanted = len(fresh) if count is None else min(count, len(fresh))
    notes: list[str] = []
    if count is not None and count > len(fresh):
        notes.append("fewer_available")
    if skipped:
        notes.append("already_used")
    decided = len(successes) + len(failures)
    seed_text = f"{seed}:{task}"
    picked: list[tuple[dict, str]] = []
    shortfall = {"successes": 0, "failures": 0}
    if success_ratio is None and strategy != "quality":
        # Plain draws over the whole pool: the natural share in expectation.
        chosen = _pick(
            fresh, wanted, strategy, scores, None, recap, seed_text, order_key
        )
        picked = [(r, outcome_class(r, reliable=False)) for r in chosen]
    elif wanted:
        if decided == 0:
            s_want = f_want = 0
            if success_ratio is not None:
                notes.append("no_outcomes")
        elif success_ratio is None:
            nd = min(wanted, decided)
            s_want = round(nd * len(successes) / decided)
            f_want = nd - s_want
        else:
            s_want = round(wanted * success_ratio)
            f_want = wanted - s_want
        take = {
            "success": min(s_want, len(successes)),
            "failure": min(f_want, len(failures)),
        }
        short_s, short_f = s_want - take["success"], f_want - take["failure"]
        if success_ratio is not None and decided:
            shortfall = {"successes": short_s, "failures": short_f}
            if short_s:
                notes.append("success_short")
            if short_f:
                notes.append("failure_short")
        lack = wanted - take["success"] - take["failure"]
        pools = {"success": successes, "failure": failures, "unknown": unknown}
        # The side that has enough fills the gap of the other.
        for kind in ("failure", "success") if short_s else ("success", "failure"):
            extra = min(lack, len(pools[kind]) - take[kind])
            take[kind] += extra
            lack -= extra
        take["unknown"] = min(lack, len(unknown))
        if take["unknown"] and decided:
            notes.append("unknown_outcomes_used")
        for kind in ("success", "failure", "unknown"):
            chosen = _pick(
                pools[kind],
                take[kind],
                strategy,
                scores,
                kind,
                recap,
                f"{seed_text}:{kind}",
                order_key,
            )
            picked += [(r, kind) for r in chosen]
    out = []
    for row, kind in picked:
        info = scores[row["key"]]
        out.append(
            {
                **row,
                "sel_stratum": stratum_of(row),
                "quality_score": info["score"],
                "selection_reason": info["reason"],
                "selection_parts": {
                    k: info[k] for k in ("trust", "completeness", "fit")
                },
            }
        )
    achieved = Counter(kind for _, kind in picked)
    report = {
        "task": task,
        "strategy": strategy,
        "requested": count,
        "success_ratio": success_ratio,
        "available": len(fresh),
        "successes": len(successes),
        "failures": len(failures),
        "unknown": len(unknown),
        "already_used": skipped,
        "selected": len(picked),
        "selected_successes": achieved["success"],
        "selected_failures": achieved["failure"],
        "selected_unknown": achieved["unknown"],
        "shortfall": (count - len(picked))
        if count is not None and count > len(picked)
        else 0,
        "shortfall_successes": shortfall["successes"],
        "shortfall_failures": shortfall["failures"],
        "notes": list(dict.fromkeys(notes)),
    }
    report["note"] = "; ".join(MESSAGES[n] for n in report["notes"])
    return out, report


def suggest_count(chosen_counts: list[int], available: int) -> int:
    """The default count for a task added to a composition: the median of
    the counts of the tasks already in it (100 when none), at most what is
    available."""
    base = (
        int(statistics.median_high(chosen_counts)) if chosen_counts else DEFAULT_COUNT
    )
    return max(1, min(base, available)) if available else 0


def mix(chosen: list[dict]) -> dict:
    """The composition's overall mix: outcomes, categories and methods."""
    total = len(chosen)
    outcomes = Counter(outcome_class(r, reliable=False) for r in chosen)
    decided = outcomes["success"] + outcomes["failure"]
    categories = Counter(r["category"] for r in chosen)
    methods = Counter(r["policy_method"] for r in chosen if r.get("policy_method"))
    lean = None
    for dimension, counter in (("category", categories), ("policy_method", methods)):
        if counter and total >= 20:
            value, n = counter.most_common(1)[0]
            if n / total >= 0.8 and len(counter) > 1:
                lean = {
                    "dimension": dimension,
                    "value": value,
                    "share": round(n / total, 3),
                }
                break
    return {
        "episodes": total,
        "successes": outcomes["success"],
        "failures": outcomes["failure"],
        "unknown": outcomes["unknown"],
        "success_share": round(outcomes["success"] / decided, 3) if decided else None,
        "categories": dict(categories),
        "policy_methods": dict(methods),
        "lean": lean,
    }
