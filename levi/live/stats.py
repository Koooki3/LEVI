"""Per-episode statistics of the background labelling: ``live/stats.jsonl``.

The worker appends one record per demo it has labelled (or failed to label)
when a batch ends. The file is the quantitative history of the service: how
long each stage took, what the model cost, whether the gate got in the way,
what came out. It rotates like the other logs (``log_max_mb``,
``log_backups``), carries no tokens, keys or paths of the person's data, and
is read back by ``read`` (damaged lines are skipped, missing fields read as
``None``) for aggregation and the page. Documented in docs/LIVE.md.

Schema ``levi.live.episode_stats.v1``; field names and units are fixed, new
fields may be added, none is renamed (``batch``, ``timeline.completed_at``,
``timeline.first_request_at`` and ``model.tokens`` were added later: a record
written before reads them as ``None``). All seconds are plain numbers (float);
a value that could not be measured is ``null``.

    at                 epoch seconds the record was written
    dataset            the live dataset name (``<group>__<task>``)
    demo               rollout folder name, e.g. ``demo_0003``
    episode_index      the episode's index in the dataset view
    session            the evaluation run id the demo came from (metadata)
    attempts           how many times labelling this demo was tried
    excluded           true when a person had removed the episode from the
                       dataset (``exclusion.py``) by the time the record was
                       written; normally false (the episode of a running batch
                       cannot be removed, and a removed one is not labelled)
    backfilled         true on a record rebuilt afterwards from the ledger and
                       the run journals (``levi live stats backfill``); what
                       could not be recovered is null
    batch:     id (epoch seconds the batch started), size (demos in it)
    episode:   frames, episode_seconds
    timeline:  to_mirror_s, to_plan_s, to_first_request_s, to_commit_s,
               to_verdict_s -- seconds after the demo's ``.complete``;
               completed_at, first_request_at -- the same two moments as
               epoch seconds (the first request's start; ``None`` when the
               episode made no request)
    model:     requests{coarse,refine,review,probe}, model_seconds{same},
               tokens{same} (reported tokens by kind; ``probe`` is the
               calibration cost), prompt_tokens, completion_tokens,
               total_tokens (what the server reported for the steps it
               reported; total = prompt + completion), probe_tokens,
               reserved_tokens, unreported_steps, images, external_tokens
               (always 0: no external model is used)
    gate:      closed_wait_s, interruptions (for the whole batch the demo was
               in), vllm_wake_s, vllm_cold_start_s (set on the first demo of
               the batch the wake or cold start was for)
    result:    state, reason, segments, segment_labels{label: count},
               verdict{outcome,events,valid_events,undecided} (plus rule and
               place_outcome when the release review uses a rule beyond "any
               valid release"; the template does not list them), review,
               spec{guideline,release_review,sha256{file: hash}}, provider,
               model

Standard library only.
"""

import contextlib
import json
import math
import time
from pathlib import Path

from . import jsonio

SCHEMA = "levi.live.episode_stats.v1"
FILE = "stats.jsonl"
KINDS = ("coarse", "refine", "review", "probe")
# ``probe`` (request-cost calibration) belongs to a batch, not to an episode:
# per-episode requests and tokens leave it out.
PER_EPISODE_KINDS = ("coarse", "refine", "review")
TAIL_BYTES = 8 * 1024 * 1024  # the most that ``read`` takes from one file

# The record with every field, all ``None``: what ``normalize`` fills in.
TEMPLATE = {
    "schema": SCHEMA,
    "at": None,
    "dataset": None,
    "demo": None,
    "episode_index": None,
    "session": None,
    "attempts": None,
    "excluded": None,
    "backfilled": None,
    "batch": {"id": None, "size": None},
    "episode": {"frames": None, "episode_seconds": None},
    "timeline": {
        "to_mirror_s": None,
        "to_plan_s": None,
        "to_first_request_s": None,
        "to_commit_s": None,
        "to_verdict_s": None,
        "completed_at": None,
        "first_request_at": None,
    },
    "model": {
        "requests": {kind: None for kind in KINDS},
        "model_seconds": {kind: None for kind in KINDS},
        "tokens": {kind: None for kind in KINDS},
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "probe_tokens": None,
        "reserved_tokens": None,
        "unreported_steps": None,
        "images": None,
        "external_tokens": None,
    },
    "gate": {
        "closed_wait_s": None,
        "interruptions": None,
        "vllm_wake_s": None,
        "vllm_cold_start_s": None,
    },
    "result": {
        "state": None,
        "reason": None,
        "segments": None,
        "segment_labels": None,
        "verdict": {
            "outcome": None,
            "events": None,
            "valid_events": None,
            "undecided": None,
        },
        "review": None,
        "spec": None,
        "provider": None,
        "model": None,
    },
}


def _fill(template, value):
    """``value`` shaped like ``template``: missing keys are ``None``, extra
    keys are kept, a wrong type for a nested group becomes the empty group."""
    if not isinstance(template, dict):
        return value
    value = value if isinstance(value, dict) else {}
    out = {key: _fill(sub, value.get(key)) for key, sub in template.items()}
    out.update({k: v for k, v in value.items() if k not in template})
    return out


def normalize(row) -> dict | None:
    """A record in the current shape, or None when it is not a record."""
    if not isinstance(row, dict):
        return None
    out = _fill(TEMPLATE, row)
    out["schema"] = out["schema"] or SCHEMA  # a record without one is the first
    return out


def record(live_dir, row, max_bytes=None, keep=3):
    """Append one record. Never raises: the statistics are a record, not part
    of the labelling."""
    with contextlib.suppress(OSError, TypeError, ValueError):
        jsonio.append_line(
            Path(live_dir) / FILE, normalize(row), max_bytes=max_bytes, keep=keep
        )


def num_or_zero(value):
    return (
        value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
    )


def leaves(value, prefix=""):
    """Every dotted path of a nested dict (the schema's fields)."""
    if not isinstance(value, dict):
        return [prefix]
    return [p for k, v in value.items() for p in leaves(v, f"{prefix}.{k}".strip("."))]


def _reject(constant):
    raise ValueError(f"{constant} is not JSON")


def _lines(path):
    try:
        with Path(path).open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - TAIL_BYTES))
            data = handle.read()
    except OSError:
        return []
    lines = data.splitlines()
    return lines[1:] if size > TAIL_BYTES else lines  # a tail starts mid-line


def read(live_dir, limit=None, since=None) -> list:
    """Records oldest first, from the rotated files and the current one.

    ``limit`` keeps the newest that many; ``since`` (epoch seconds) drops older
    ones. Lines that do not parse, are not objects or carry another schema are
    skipped; missing fields are ``None`` (``normalize``)."""
    base = Path(live_dir) / FILE
    names = sorted(
        (p for p in base.parent.glob(FILE + ".*") if p.suffix[1:].isdigit()),
        key=lambda p: -int(p.suffix[1:]),
    )
    rows = []
    for path in [*names, base]:
        for line in _lines(path):
            try:
                # NaN and Infinity are not JSON: a line that holds one is damaged.
                value = json.loads(line, parse_constant=_reject)
            except ValueError:
                continue
            row = normalize(value)
            if row is None or not str(row["schema"]).startswith(
                "levi.live.episode_stats."
            ):
                continue
            if since is not None and (row["at"] or 0) < since:
                continue
            rows.append(row)
    # A backfilled record is written later than the moment it describes: order
    # by that moment (the sort is stable, so equal times keep the file order).
    # A record without a time keeps its place after the one before it.
    keys, carried = [], 0
    for row in rows:
        carried = num_or_zero(row["at"]) if row["at"] is not None else carried
        keys.append(carried)
    rows = [row for _, row in sorted(zip(keys, rows, strict=True), key=lambda p: p[0])]
    return rows[-limit:] if limit else rows


# --- building a record from a run's journal ----------------------------------


def kind_of(stage, phase) -> str:
    """Which request kind a ``model_step`` is: the temporal run's ``coarse``
    and ``refine`` (``refine-2``...) passes, or any step of a release review."""
    if stage == "review":
        return "review"
    return "refine" if str(phase).startswith("refine") else "coarse"


def usage_of(journals, episode, probe=False) -> dict:
    """The model's cost for one episode from the runs' journals.

    ``journals`` is ``[(stage, events)]`` with ``stage`` ``temporal`` or
    ``review``. ``probe`` adds the batch's request-cost calibrations (they
    belong to a run, not an episode: the first demo carries them). Also gives
    the earliest request's start time for the timeline."""
    requests = {k: 0 for k in KINDS}
    seconds = {k: 0.0 for k in KINDS}
    prompt = completion = images = reserved = unreported = probes = 0
    reported = probed = False
    tokens = {k: 0 for k in KINDS}
    counted = set()  # kinds that have a reported token figure
    first = None
    for stage, events in journals:
        for event in events:
            kind = event.get("type")
            if kind == "request_cost_calibrated" and probe and stage == "temporal":
                requests["probe"] += 2
                probes += int(event.get("tokens") or 0)
                tokens["probe"] += int(event.get("tokens") or 0)
                counted.add("probe")
                probed = True
                continue
            if kind != "model_step" or event.get("episode") != episode:
                continue
            usage = event.get("usage") or {}
            if usage.get("cached"):
                continue
            which = kind_of(stage, event.get("phase"))
            took = float(usage.get("elapsed_seconds") or 0.0)
            requests[which] += 1
            seconds[which] += took
            images += int(usage.get("images") or 0)
            seen = usage.get("reported_tokens")
            asked = usage.get("prompt_tokens")
            if isinstance(seen, int) and isinstance(asked, int) and seen >= asked:
                # The server said what it used: split into prompt and answer.
                prompt += asked
                completion += seen - asked
                tokens[which] += seen
                counted.add(which)
                reported = True
            else:
                # No usage from the server: ``tokens`` is the reservation
                # LEVI held for the call, not something that was spent.
                unreported += 1
                reserved += int(usage.get("tokens") or 0)
            if event.get("time") is not None:
                begun = float(event["time"]) - took
                first = begun if first is None else min(first, begun)
    return {
        "requests": requests,
        "model_seconds": {
            k: (None if k == "probe" else round(v, 2)) for k, v in seconds.items()
        },
        # Reported tokens by kind (``probe``: the calibration cost); a kind with
        # no reported step is None.
        "tokens": {k: (v if k in counted else None) for k, v in tokens.items()},
        "prompt_tokens": prompt if reported else None,
        "completion_tokens": completion if reported else None,
        # None when no step reported usage (or there was no request): "not
        # measured", never 0.
        "total_tokens": prompt + completion if reported else None,
        "probe_tokens": probes if probed else None,
        "reserved_tokens": reserved if unreported else None,
        "unreported_steps": unreported,
        "images": images,
        "external_tokens": 0,
        "first_request_at": first,
    }


def after(moment, base):
    """Seconds from ``base`` to ``moment``; None when either is unknown."""
    if moment is None or base is None:
        return None
    return round(float(moment) - float(base), 2)


def stamp(row, now=None) -> dict:
    row["at"] = round(time.time() if now is None else now, 3)
    return row


# --- aggregation ---------------------------------------------------------------
#
# Pure functions over the records ``read`` returns. They never raise on a
# damaged or partial record: a value that is not a finite number is skipped, a
# group with nothing to measure gives ``None`` (or ``n = 0``), never a made-up
# zero. Definitions are in docs/LIVE.md ("Statistics and reports").

LATENCIES = (
    "to_mirror_s",
    "to_plan_s",
    "to_first_request_s",
    "to_commit_s",
    "to_verdict_s",
)
BATCH_GAP_S = 5.0  # records this close, same dataset and gate figures: one batch


def num(value):
    """``value`` as a float when it is a finite number, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def dig(row, *path):
    """``row[a][b]...`` or None when any step is missing or not a dict."""
    for key in path:
        if not isinstance(row, dict):
            return None
        row = row.get(key)
    return row


def quantile(values, q):
    """The ``q`` quantile (0..1) of ``values`` by linear interpolation between
    the two nearest ranks (numpy's default); None for no values."""
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _round(value, digits=2):
    return None if value is None else round(value, digits)


def dist(values) -> dict:
    """``{n, min, median, p90, max, mean}`` of the numbers in ``values``."""
    numbers = [v for v in map(num, values) if v is not None]
    if not numbers:
        return {
            "n": 0,
            "min": None,
            "median": None,
            "p90": None,
            "max": None,
            "mean": None,
        }
    return {
        "n": len(numbers),
        "min": _round(min(numbers)),
        "median": _round(quantile(numbers, 0.5)),
        "p90": _round(quantile(numbers, 0.9)),
        "max": _round(max(numbers)),
        "mean": _round(sum(numbers) / len(numbers)),
    }


def total(values):
    """Sum of the numbers in ``values``; None when there are none."""
    numbers = [v for v in map(num, values) if v is not None]
    return sum(numbers) if numbers else None


def ratio(top, bottom, digits=3):
    top, bottom = num(top), num(bottom)
    if top is None or not bottom:
        return None
    return round(top / bottom, digits)


def mark_excluded(rows, excluded=None, known=None) -> list:
    """The records with ``excluded`` decided by the datasets' state files.

    ``excluded`` is ``{(dataset, demo)}`` a person has removed, ``known`` the
    datasets whose state file could be read. For a record of a known dataset the
    state is the truth: removed means ``excluded: True``, and an episode that
    was restored since has ``excluded: False`` whatever the record said when it
    was written. A record of a dataset with no readable state keeps its own
    flag. No record is changed in place."""
    excluded = excluded or set()
    known = known if known is not None else {d for d, _ in excluded}
    out = []
    for row in rows:
        if isinstance(row, dict) and row.get("dataset") in known:
            flag = (row.get("dataset"), row.get("demo")) in excluded
            if row.get("excluded") is not flag:
                row = {**row, "excluded": flag}
        out.append(row)
    return out


def select(rows, dataset=None, session=None, since=None, include_excluded=True) -> list:
    """The records of one dataset and/or evaluation session and/or written at
    or after ``since`` (epoch seconds). ``None`` means no filter. With
    ``include_excluded`` False the records marked ``excluded`` (see
    ``mark_excluded``) are left out."""
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if dataset is not None and row.get("dataset") != dataset:
            continue
        if session is not None and row.get("session") != session:
            continue
        if since is not None and (num(row.get("at")) or 0) < since:
            continue
        if not include_excluded and row.get("excluded") is True:
            continue
        out.append(row)
    return out


def latest(rows) -> list:
    """One record per demo of a dataset: the newest (a retry replaces the
    attempt before it), in the order the demos first appear."""
    found: dict = {}
    for row in rows:
        key = (row.get("dataset"), row.get("demo"))
        found[key] = row if key not in found else _newer(found[key], row)
    return list(found.values())


def _newer(old, new):
    return new if (num(new.get("at")) or 0) >= (num(old.get("at")) or 0) else old


def batches(rows) -> list:
    """The records grouped by the batch the worker labelled them in: by the
    record's ``batch.id`` when it has one, otherwise records of one dataset
    written within ``BATCH_GAP_S`` of each other that carry the same gate
    figures (a batch's demos are all written at its end)."""
    groups: dict = {}
    loose: list = []
    for row in rows:
        ident = dig(row, "batch", "id")
        if num(ident) is not None:
            groups.setdefault((row.get("dataset"), ident), []).append(row)
        else:
            loose.append(row)
    out = list(groups.values())
    loose.sort(key=lambda r: (str(r.get("dataset")), num(r.get("at")) or 0))
    cluster: list = []
    for row in loose:
        if cluster and not _same_batch(cluster[-1], row):
            out.append(cluster)
            cluster = []
        cluster.append(row)
    if cluster:
        out.append(cluster)
    return out


def _same_batch(last, row) -> bool:
    def figures(r):
        return (dig(r, "gate", "closed_wait_s"), dig(r, "gate", "interruptions"))

    return (
        last.get("dataset") == row.get("dataset")
        and figures(last) == figures(row)
        and abs((num(row.get("at")) or 0) - (num(last.get("at")) or 0)) <= BATCH_GAP_S
    )


def _model_seconds(row):
    per_kind = dig(row, "model", "model_seconds")
    if not isinstance(per_kind, dict):
        return None
    return total(per_kind.get(k) for k in KINDS)


def _count(values, key=None) -> dict:
    out: dict = {}
    for value in values:
        name = str(value if key is None else key(value))
        out[name] = out.get(name, 0) + 1
    return out


def in_session(rows, session_ends=None) -> dict:
    """How many episodes' first model request began before the last episode of
    their evaluation session ended (the "labelled during the session" figure).

    The session's end is the latest ``timeline.completed_at`` of its episodes
    (and ``session_ends[(dataset, session)]`` when the caller knows an episode
    that has no record yet). An episode with no record of when it ended or
    asked, or without a session id, is not evaluable and is left out of both
    counts; an episode that made no request at all counts as not in session."""
    rows = latest(rows)
    ends = _session_ends(rows, session_ends)
    evaluable = inside = 0
    for row in rows:
        key = (row.get("dataset"), row.get("session"))
        if key not in ends or num(dig(row, "timeline", "completed_at")) is None:
            continue
        evaluable += 1
        first = num(dig(row, "timeline", "first_request_at"))
        if first is not None and first < ends[key]:
            inside += 1
    return {
        "count": inside,
        "evaluable": evaluable,
        "ratio": ratio(inside, evaluable),
    }


def gate_window(gate_rows, start, end) -> dict | None:
    """Seconds the gate was closed between ``start`` and ``end`` (epoch
    seconds), how many times it closed, and how long its state was unknown, from
    the transitions in ``gate.jsonl`` (oldest first). None when the window or
    the history is unknown. The gate is closed while the policy server infers,
    so this is the time labelling was *not allowed*, not time the worker waited.

    A closure is counted when the gate goes from open (or from unknown) to
    closed: a change of the reason code while it stays closed is not another
    one. A transition whose ``open`` is null (the service stopped) ends the
    interval before it: nothing is known until the next transition, so that
    time is not counted as closed and is reported as ``unknown_s``."""
    start, end = num(start), num(end)
    steps = []
    for row in gate_rows or []:
        at = num(row.get("at")) if isinstance(row, dict) else None
        opened = dig(row, "to", "open")
        if at is not None:
            steps.append((at, opened if isinstance(opened, bool) else None))
    if start is None or end is None or end <= start or not steps:
        return None
    steps.sort(key=lambda step: step[0])
    closed_s = unknown_s = 0.0
    closures = 0
    before = None  # the state before the first transition is unknown
    for index, (at, state) in enumerate(steps):
        if state is False and before is not False and start <= at <= end:
            closures += 1
        stop = steps[index + 1][0] if index + 1 < len(steps) else end
        span = max(0.0, min(stop, end) - max(at, start))
        if state is False:
            closed_s += span
        elif state is None:
            unknown_s += span
        before = state
    first = steps[0][0]
    if first > start:
        unknown_s += min(first, end) - start
    window = end - start
    return {
        "closed_s": round(closed_s, 2),
        "closures": closures,
        "unknown_s": round(unknown_s, 2),
        "window_s": round(window, 2),
        "closed_share": round(closed_s / window, 3),
    }


def summarize(rows, *, gate=None, session_ends=None) -> dict:
    """Every aggregate of the records in ``rows`` (already filtered).

    Per-episode figures (counts, latency, tokens per episode, outcome) use the
    newest record of each demo; costs that are spent per attempt (token and
    request totals, the gate's and vLLM's figures) use every record, so a
    retry's first attempt is counted as the cost it was."""
    rows = [r for r in rows if isinstance(r, dict)]
    last = latest(rows)
    states = _count(dig(r, "result", "state") or "unknown" for r in last)
    superseded = len(rows) - len(last)
    kept = {id(r) for r in last}
    tried = {(r.get("dataset"), r.get("demo")) for r in rows if id(r) not in kept}

    episodes = {
        "count": len(last),
        "records": len(rows),
        "done": states.get("done", 0),
        "failed": states.get("failed", 0),
        "retrying": states.get("mirrored", 0),
        # ``attempts`` counts the earlier tries that failed: a done episode that
        # needed a retry has 1 or more, a failed one (given up) 2 or more.
        "retried": sum(
            1
            for r in last
            if (num(r.get("attempts")) or 0)
            >= (2 if dig(r, "result", "state") == "failed" else 1)
            or (r.get("dataset"), r.get("demo")) in tried
        ),
        "superseded": superseded,
        "excluded": sum(1 for r in last if r.get("excluded") is True),
    }

    latency = {key: dist(dig(r, "timeline", key) for r in last) for key in LATENCIES}

    both = [
        (num(dig(r, "episode", "episode_seconds")), _model_seconds(r)) for r in last
    ]
    both = [(e, m) for e, m in both if e is not None and m is not None]
    episode_s = total(e for e, _ in both)
    model_s = total(m for _, m in both)
    starts = [num(dig(r, "timeline", "completed_at")) for r in last]
    ends = [
        (s + n)
        for r, s in zip(last, starts, strict=True)
        for n in [
            num(dig(r, "timeline", "to_verdict_s"))
            or num(dig(r, "timeline", "to_commit_s"))
        ]
        if s is not None and n is not None
    ]
    starts = [s for s in starts if s is not None]
    span = (max(ends) - min(starts)) if starts and ends else None
    span_episode_s = total(
        num(dig(r, "episode", "episode_seconds"))
        for r in last
        if num(dig(r, "timeline", "completed_at")) is not None
    )
    throughput = {
        "episode_seconds": _round(
            total(dig(r, "episode", "episode_seconds") for r in last)
        ),
        # The latest attempt of each episode, like every figure in this group.
        "model_seconds": _round(total(_model_seconds(r) for r in last)),
        "realtime_factor": ratio(episode_s, model_s),
        "realtime_factor_n": len(both),
        "span_s": _round(span),
        "wall_factor": ratio(span_episode_s, span) if span and span > 0 else None,
    }

    def kind_total(field, kind, source):
        return total(dig(r, "model", field, kind) for r in source)

    by_kind = {}
    kind_seconds = {k: kind_total("model_seconds", k, rows) for k in KINDS}
    kind_tokens = {k: kind_total("tokens", k, rows) for k in KINDS}
    seconds_sum = total(kind_seconds.values())
    # Shares are of the tokens in ``tokens_total``: the calibration (``probe``)
    # is not part of it and has no share.
    tokens_sum = total(kind_tokens[k] for k in PER_EPISODE_KINDS)
    for kind in KINDS:
        by_kind[kind] = {
            "requests": kind_total("requests", kind, rows),
            "seconds": _round(kind_seconds[kind]),
            "seconds_share": ratio(kind_seconds[kind], seconds_sum),
            "tokens": kind_tokens[kind],
            "tokens_share": ratio(kind_tokens[kind], tokens_sum)
            if kind in PER_EPISODE_KINDS
            else None,
        }
    prompts = [
        (
            num(dig(r, "model", "prompt_tokens")),
            num(dig(r, "model", "completion_tokens")),
        )
        for r in rows
    ]
    prompts = [(p, c) for p, c in prompts if p is not None and c is not None]
    prompt_sum = total(p for p, _ in prompts)
    completion_sum = total(c for _, c in prompts)
    request_totals = [
        total(dig(r, "model", "requests", k) for k in PER_EPISODE_KINDS)
        for r in last
        if any(
            num(dig(r, "model", "requests", k)) is not None for k in PER_EPISODE_KINDS
        )
    ]
    model = {
        "requests_per_episode": dist(request_totals),
        "requests": {k: by_kind[k]["requests"] for k in KINDS},
        # What the server reported for the steps it reported (total = prompt +
        # completion). The calibration cost and the reservations LEVI holds for
        # steps whose server gave no usage are kept apart, never added in.
        "tokens_total": total(dig(r, "model", "total_tokens") for r in rows),
        "probe_tokens": total(dig(r, "model", "probe_tokens") for r in rows),
        "reserved_tokens": total(dig(r, "model", "reserved_tokens") for r in rows),
        "unreported_steps": total(dig(r, "model", "unreported_steps") for r in rows),
        "tokens_per_episode": dist(dig(r, "model", "total_tokens") for r in last),
        "prompt_tokens": prompt_sum,
        "completion_tokens": completion_sum,
        "prompt_share": ratio(prompt_sum, (prompt_sum or 0) + (completion_sum or 0))
        if prompts
        else None,
        "images_total": total(dig(r, "model", "images") for r in rows),
        "images_per_episode": dist(dig(r, "model", "images") for r in last),
        "external_tokens": total(dig(r, "model", "external_tokens") for r in rows),
        "by_kind": by_kind,
    }

    waits = [num(dig(r, "gate", "vllm_wake_s")) for r in rows]
    colds = [num(dig(r, "gate", "vllm_cold_start_s")) for r in rows]
    waits = [w for w in waits if w is not None]
    colds = [c for c in colds if c is not None]
    groups = batches(rows)
    figures = [
        (
            num(dig(g[0], "gate", "closed_wait_s")),
            num(dig(g[0], "gate", "interruptions")),
        )
        for g in groups
    ]
    stamps = [num(r.get("at")) for r in rows]
    stamps = [s for s in stamps if s is not None]
    begun = [num(dig(r, "timeline", "completed_at")) for r in rows]
    begun = [b for b in begun if b is not None]
    gpu = {
        "batches": len(groups),
        "closed_wait_s": _round(total(c for c, _ in figures)),
        "interruptions": total(i for _, i in figures),
        "vllm_wakes": {"count": len(waits), **_short(waits)},
        "vllm_cold_starts": {"count": len(colds), **_short(colds)},
        # Not recorded anywhere: a sleep leaves no line in gate.jsonl or in a
        # demo's record. Shown as unknown rather than guessed.
        "vllm_sleeps": None,
        "gate_window": gate_window(gate, min(begun), max(stamps))
        if begun and stamps and gate
        else None,
    }

    verdicts = _count((dig(r, "result", "verdict", "outcome") or "none") for r in last)
    segments = [num(dig(r, "result", "segments")) for r in last]
    labels: dict = {}
    for r in last:
        found = dig(r, "result", "segment_labels")
        if isinstance(found, dict):
            for label, count in found.items():
                if num(count) is not None:
                    labels[str(label)] = labels.get(str(label), 0) + int(count)
    outcome = {
        "segments_total": total(segments),
        "segments_per_episode": dist(segments),
        "segment_histogram": _count(
            [s for s in segments if s is not None],
            key=lambda n: "5+" if n >= 5 else int(n),
        ),
        "labels": dict(sorted(labels.items(), key=lambda kv: (-kv[1], kv[0]))),
        "verdicts": verdicts,
        "undecided": sum(
            1 for r in last if dig(r, "result", "verdict", "undecided") is True
        ),
        "review": _count((dig(r, "result", "review") or "unknown") for r in last),
    }

    return {
        "episodes": episodes,
        "latency": latency,
        "throughput": throughput,
        "model": model,
        "gpu": gpu,
        "outcome": outcome,
        "in_session": in_session(rows, session_ends),
        "window": {
            "first_at": _round(min(begun), 3) if begun else None,
            "last_at": _round(max(stamps), 3) if stamps else None,
        },
    }


def _short(values) -> dict:
    return {
        "total_s": _round(total(values)),
        "median_s": _round(quantile(values, 0.5)),
        "max_s": _round(max(values)) if values else None,
    }


def _episode_row(row, ends) -> dict:
    key = (row.get("dataset"), row.get("session"))
    started = num(dig(row, "timeline", "completed_at"))
    first = num(dig(row, "timeline", "first_request_at"))
    during = None
    if key in ends and started is not None:
        during = first is not None and first < ends[key]
    return {
        "dataset": row.get("dataset"),
        "demo": row.get("demo"),
        "episode_index": row.get("episode_index"),
        "session": row.get("session"),
        "at": row.get("at"),
        "state": dig(row, "result", "state"),
        "attempts": row.get("attempts"),
        "episode_seconds": dig(row, "episode", "episode_seconds"),
        "to_mirror_s": dig(row, "timeline", "to_mirror_s"),
        "to_first_request_s": dig(row, "timeline", "to_first_request_s"),
        "to_commit_s": dig(row, "timeline", "to_commit_s"),
        "to_verdict_s": dig(row, "timeline", "to_verdict_s"),
        "requests": total(dig(row, "model", "requests", k) for k in PER_EPISODE_KINDS),
        "model_seconds": _round(_model_seconds(row)),
        "total_tokens": dig(row, "model", "total_tokens"),
        "prompt_tokens": dig(row, "model", "prompt_tokens"),
        "completion_tokens": dig(row, "model", "completion_tokens"),
        "images": dig(row, "model", "images"),
        "closed_wait_s": dig(row, "gate", "closed_wait_s"),
        "segments": dig(row, "result", "segments"),
        "verdict": dig(row, "result", "verdict", "outcome"),
        "review": dig(row, "result", "review"),
        "in_session": during,
        "excluded": row.get("excluded") is True,
    }


def _session_ends(rows, session_ends=None) -> dict:
    ends: dict = {}
    for row in rows:
        stamp = num(dig(row, "timeline", "completed_at"))
        if stamp is not None and row.get("session"):
            key = (row.get("dataset"), row.get("session"))
            ends[key] = max(ends.get(key, stamp), stamp)
    for key, stamp in (session_ends or {}).items():
        if num(stamp) is not None and key in ends:
            ends[key] = max(ends[key], float(stamp))
    return ends


def episode_rows(rows, session_ends=None) -> list:
    """One flat dict per demo (its newest record), oldest first: the table of
    the page and the CSV export."""
    last = latest([r for r in rows if isinstance(r, dict)])
    ends = _session_ends(last, session_ends)
    return [_episode_row(r, ends) for r in last]


def session_rows(rows, *, session_ends=None) -> list:
    """One dict per (dataset, evaluation session), newest session first."""
    groups: dict = {}
    for row in rows:
        if isinstance(row, dict):
            groups.setdefault((row.get("dataset"), row.get("session")), []).append(row)
    out = []
    for (dataset, session), members in groups.items():
        # No gate history here: the per-session table does not show it, and
        # walking the whole history once per session costs far more than the
        # rest of the table.
        found = summarize(members, session_ends=session_ends)
        verdicts = found["outcome"]["verdicts"]
        out.append(
            {
                "dataset": dataset,
                "session": session,
                "episodes": found["episodes"]["count"],
                "done": found["episodes"]["done"],
                "failed": found["episodes"]["failed"],
                "first_at": found["window"]["first_at"],
                "last_at": found["window"]["last_at"],
                "to_commit_median_s": found["latency"]["to_commit_s"]["median"],
                "to_verdict_median_s": found["latency"]["to_verdict_s"]["median"],
                "to_verdict_p90_s": found["latency"]["to_verdict_s"]["p90"],
                "tokens": found["model"]["tokens_total"],
                "model_seconds": found["throughput"]["model_seconds"],
                "realtime_factor": found["throughput"]["realtime_factor"],
                "in_session_ratio": found["in_session"]["ratio"],
                "closed_wait_s": found["gpu"]["closed_wait_s"],
                "success": verdicts.get("success", 0),
                "failure": verdicts.get("failure", 0),
            }
        )
    out.sort(key=lambda r: -(r["last_at"] or 0))
    return out
