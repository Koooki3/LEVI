"""Per-episode statistics of the background labelling: ``live/stats.jsonl``.

The worker appends one record per demo it has labelled (or failed to label)
when a batch ends. The file is the quantitative history of the service: how
long each stage took, what the model cost, whether the gate got in the way,
what came out. It rotates like the other logs (``log_max_mb``,
``log_backups``), carries no tokens, keys or paths of the person's data, and
is read back by ``read`` (damaged lines are skipped, missing fields read as
``None``) for aggregation and the page. Documented in docs/LIVE.md.

Schema ``levi.live.episode_stats.v1``; field names and units are fixed, new
fields may be added, none is renamed. All seconds are plain numbers (float);
a value that could not be measured is ``null``.

    at                 epoch seconds the record was written
    dataset            the live dataset name (``<group>__<task>``)
    demo               rollout folder name, e.g. ``demo_0003``
    episode_index      the episode's index in the dataset view
    session            the evaluation run id the demo came from (metadata)
    attempts           how many times labelling this demo was tried
    excluded           false (reserved for episodes a person set aside)
    episode:   frames, episode_seconds
    timeline:  to_mirror_s, to_plan_s, to_first_request_s, to_commit_s,
               to_verdict_s -- seconds after the demo's ``.complete``
    model:     requests{coarse,refine,review,probe}, model_seconds{same},
               prompt_tokens, completion_tokens, total_tokens (what the
               server reported for the steps it reported; total = prompt +
               completion), probe_tokens, reserved_tokens, unreported_steps,
               images, external_tokens (always 0: no external model is used)
    gate:      closed_wait_s, interruptions (for the whole batch the demo was
               in), vllm_wake_s, vllm_cold_start_s (set on the first demo of
               the batch the wake or cold start was for)
    result:    state, reason, segments, segment_labels{label: count},
               verdict{outcome,events,valid_events,undecided}, review,
               spec{guideline,release_review,sha256{file: hash}}, provider,
               model

Standard library only.
"""

import contextlib
import json
import time
from pathlib import Path

from . import jsonio

SCHEMA = "levi.live.episode_stats.v1"
FILE = "stats.jsonl"
KINDS = ("coarse", "refine", "review", "probe")
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
    "episode": {"frames": None, "episode_seconds": None},
    "timeline": {
        "to_mirror_s": None,
        "to_plan_s": None,
        "to_first_request_s": None,
        "to_commit_s": None,
        "to_verdict_s": None,
    },
    "model": {
        "requests": {kind: None for kind in KINDS},
        "model_seconds": {kind: None for kind in KINDS},
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
                value = json.loads(line)
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
    first = None
    for stage, events in journals:
        for event in events:
            kind = event.get("type")
            if kind == "request_cost_calibrated" and probe and stage == "temporal":
                requests["probe"] += 2
                probes += int(event.get("tokens") or 0)
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
        "prompt_tokens": prompt if reported else None,
        "completion_tokens": completion if reported else None,
        "total_tokens": prompt + completion,
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
