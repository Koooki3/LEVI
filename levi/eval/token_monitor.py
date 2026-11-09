"""Monitor and tally external-agent tokens from named Claude session transcripts.

The user's decision (2026-11-06): external tokens have **no cap**, but they are
**monitored and counted**. This module is the monitor. It sits beside
``levi.eval.tokens`` and reuses it: the totals, the ``exact`` flag, the
sub-agent result-line rule (a result line is counted, its aggregate usage is
never added, an uncovered sub-agent makes the count a lower bound) and the
time-window rule all come from ``usage_from_files``. What this module adds is
the per-message view, the two totals, the estimate alert and the per-task
roll-up. A consistency guard raises if the per-message sums ever disagree with
``usage_from_files`` for the same files.

Rules:

- The caller names every transcript file and tags it with a task label
  (``LABEL=PATH``). Nothing here lists a directory, expands a glob or looks into
  a home folder. A path given under two labels is refused (it would be counted
  twice).
- One row per message id (the last usage record of that id wins; a later file
  replaces an earlier one's copy), with the four fields of
  ``levi.aeri.external_report.v1`` kept apart.
- Two totals side by side, the definitions of ``levi.eval.compare``:
  ``fresh`` = ``input_new + output + cache_write`` and ``with_cache`` = the four
  fields. A total is ``None`` (unknown) as soon as one of its fields was never
  seen; a message that lacks a field is counted in ``missing_fields`` and makes
  ``exact`` false. Unknown is never 0.
- Alert: a task whose ``fresh`` total exceeds ``2 x estimate`` (the caller
  passes the estimate, in ``fresh`` tokens). A task with no estimate is not
  alerted and says so (``alert_state: "no_estimate"``); one whose ``fresh`` is
  unknown is ``"fresh_unknown"``. A lower bound that already exceeds the line is
  still an alert (the true value is at least that).
- The output holds **counts, ids, times and codes only**: no transcript text, no
  file path. A task label is limited to ``[A-Za-z0-9_.:-]``.

Standard library only; no network, no GPU. ``--selftest`` runs the rules against
hand-written synthetic transcripts.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from levi.eval import tokens
from levi.eval.tokens import FIELDS, USAGE_KEYS

SCHEMA = "levi.eval.token_monitor.v1"
ALERT_FACTOR = 2.0
LABEL_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
FRESH_FIELDS = ("input_new", "output", "cache_write")


def _total(values: Mapping[str, int | None], names: Sequence[str]) -> int | None:
    parts = [values[name] for name in names]
    if any(part is None for part in parts):
        return None
    return sum(parts)  # type: ignore[arg-type]


def _messages(files: Sequence[Path], lo, hi) -> tuple[list[dict], int]:
    """Per-message rows, in first-seen order, with the rules of
    ``tokens.usage_from_files`` (key = message.id, else uuid; last usage wins;
    window by the first line's time). Returns (rows, unplaced)."""
    table: dict[str, list] = {}
    for path in files:
        with path.open("rb") as handle:
            for raw in handle:
                raw = raw.strip()
                if b'"usage"' not in raw:
                    continue
                try:
                    row = json.loads(raw)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(row, dict) or row.get("type") != "assistant":
                    continue
                message = row.get("message")
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                by_uuid = not message.get("id")
                key = row.get("uuid") if by_uuid else message.get("id")
                if not isinstance(key, str) or not key:
                    continue
                stamp = tokens._line_time(row.get("timestamp"))
                entry = table.get(key)
                if entry is None:
                    table[key] = [stamp, usage, message.get("model"), by_uuid]
                    continue
                if stamp is not None and (entry[0] is None or stamp < entry[0]):
                    entry[0] = stamp
                entry[1] = usage
                entry[2] = message.get("model") or entry[2]
    rows, unplaced = [], 0
    for key, (first, usage, model, by_uuid) in table.items():
        if lo is not None or hi is not None:
            if first is None:
                unplaced += 1
                continue
            if (lo is not None and first < lo) or (hi is not None and first > hi):
                continue
        counts = {n: tokens._count(usage.get(k)) for n, k in USAGE_KEYS.items()}
        rows.append(
            {
                "id": key,
                "id_kind": "uuid" if by_uuid else "message_id",
                "first_time": first,
                "model": model if isinstance(model, str) and model else None,
                **counts,
                "fresh": _total(counts, FRESH_FIELDS),
                "with_cache": _total(counts, FIELDS),
            }
        )
    return rows, unplaced


def _alert(
    fresh: int | None, estimate: int | None
) -> tuple[bool | None, str, float | None]:
    if estimate is None:
        return None, "no_estimate", None
    if fresh is None:
        return None, "fresh_unknown", None
    ratio = fresh / estimate
    over = fresh > ALERT_FACTOR * estimate
    return over, "over_2x" if over else "within_2x", ratio


def monitor(
    tasks: Mapping[str, Sequence[str | Path]],
    *,
    estimates: Mapping[str, int] | None = None,
    since=None,
    until=None,
) -> dict:
    """Count the named transcripts per task label.

    ``tasks`` maps a label to its transcript files (oldest first).
    ``estimates`` maps a label to the caller's estimate in ``fresh`` tokens;
    a label without one is not alerted. Returns a JSON-able dict.
    """
    estimates = dict(estimates or {})
    if not tasks:
        raise ValueError("no tasks given")
    unknown = set(estimates) - set(tasks)
    if unknown:
        raise ValueError(f"estimate for a label with no files: {sorted(unknown)}")
    seen_paths: dict[Path, str] = {}
    for label, paths in tasks.items():
        if not LABEL_RE.fullmatch(label):
            raise ValueError(
                f"bad task label {label!r}: use [A-Za-z0-9_.:-], 1-64 chars"
            )
        for path in paths:
            real = Path(path).resolve()
            if real in seen_paths and seen_paths[real] != label:
                raise ValueError(
                    f"one file under two labels ({seen_paths[real]!r}, {label!r}): it would count twice"
                )
            seen_paths[real] = label
    for label, value in estimates.items():
        if type(value) is not int or value <= 0:
            raise ValueError(f"estimate for {label!r} must be a positive integer")
    lo = tokens.parse_time(since) if since is not None else None
    hi = tokens.parse_time(until) if until is not None else None

    out_tasks: dict[str, dict] = {}
    for label, paths in tasks.items():
        files = [Path(p) for p in paths]
        usage = tokens.usage_from_files(files, since=since, until=until)
        rows, _unplaced = _messages(files, lo, hi)
        for name in FIELDS:  # guard: the per-message view must agree with tokens.py
            have = [r[name] for r in rows if r[name] is not None]
            total = usage.tokens[name]
            if (sum(have) if have else None) != total or len(rows) != usage.messages:
                raise RuntimeError(
                    f"per-message sums disagree with levi.eval.tokens for task {label!r} field {name}"
                )
        total = dict(usage.tokens)
        fresh = _total(total, FRESH_FIELDS)
        with_cache = _total(total, FIELDS)
        over, state, ratio = _alert(fresh, estimates.get(label))
        out_tasks[label] = {
            "files": usage.files,
            "messages": usage.messages,
            "models": list(usage.models),
            "tokens": total,
            "fresh": fresh,
            "with_cache": with_cache,
            "exact": usage.exact,
            "reasons": list(usage.reasons),
            "missing_fields": dict(usage.missing_fields),
            "subagent_results": usage.subagent_results,
            "subagents_uncovered": usage.subagents_uncovered,
            "estimate_fresh": estimates.get(label),
            "ratio_to_estimate": ratio,
            "alert": over,
            "alert_state": state,
            "per_message": rows,
        }
    grand = {
        name: _sum_known([t["tokens"][name] for t in out_tasks.values()])
        for name in FIELDS
    }
    return {
        "schema": SCHEMA,
        "rule": tokens.RULE,
        "alert_factor": ALERT_FACTOR,
        "fresh_definition": "+".join(FRESH_FIELDS),
        "with_cache_definition": "+".join(FIELDS),
        "window": {"since": lo, "until": hi},
        "tasks": out_tasks,
        "total": {
            "tokens": grand,
            "fresh": _total(grand, FRESH_FIELDS),
            "with_cache": _total(grand, FIELDS),
            "messages": sum(t["messages"] for t in out_tasks.values()),
            "exact": all(t["exact"] for t in out_tasks.values()),
            "alerts": sorted(k for k, t in out_tasks.items() if t["alert"]),
        },
    }


def _sum_known(values: Sequence[int | None]) -> int | None:
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def _cell(value) -> str:
    return "unknown" if value is None else f"{value:,}"


def to_markdown(report: dict) -> str:
    lines = [
        "# External token monitor",
        "",
        (
            f"Rule: {report['rule']}. fresh = {report['fresh_definition']}; "
            f"with_cache = {report['with_cache_definition']}. `unknown` = no counted message carried the field."
        ),
        "",
        "| task | files | msgs | input_new | output | cache_write | cache_read | fresh | with_cache | exact | estimate | alert |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for label, t in report["tasks"].items():
        k = t["tokens"]
        alert = {
            "over_2x": "ALERT >2x",
            "within_2x": "ok",
            "no_estimate": "no estimate",
            "fresh_unknown": "fresh unknown",
        }[t["alert_state"]]
        lines.append(
            f"| {label} | {t['files']} | {t['messages']} | {_cell(k['input_new'])} | {_cell(k['output'])} "
            f"| {_cell(k['cache_write'])} | {_cell(k['cache_read'])} | {_cell(t['fresh'])} | {_cell(t['with_cache'])} "
            f"| {'yes' if t['exact'] else 'NO (lower bound)'} | {_cell(t['estimate_fresh'])} | {alert} |"
        )
    tot = report["total"]
    lines.append(
        f"| **total** | | {tot['messages']} | {_cell(tot['tokens']['input_new'])} | {_cell(tot['tokens']['output'])} "
        f"| {_cell(tot['tokens']['cache_write'])} | {_cell(tot['tokens']['cache_read'])} | {_cell(tot['fresh'])} "
        f"| {_cell(tot['with_cache'])} | {'yes' if tot['exact'] else 'NO (lower bound)'} | | |"
    )
    for label, t in report["tasks"].items():
        if t["reasons"]:
            lines += [
                "",
                f"Not exact, task `{label}`: "
                + ", ".join(t["reasons"])
                + f"; missing fields {t['missing_fields']}.",
            ]
    lines += [
        "",
        "## Per message",
        "",
        "| task | id | input_new | output | cache_write | cache_read | fresh | with_cache |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for label, t in report["tasks"].items():
        for r in t["per_message"]:
            lines.append(
                f"| {label} | {r['id']} | {_cell(r['input_new'])} | {_cell(r['output'])} | {_cell(r['cache_write'])} "
                f"| {_cell(r['cache_read'])} | {_cell(r['fresh'])} | {_cell(r['with_cache'])} |"
            )
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------- selftest


def _line(
    mid,
    out,
    *,
    at="2026-11-06T09:00:00Z",
    i=1,
    cr=10,
    cw=100,
    uuid=None,
    usage=None,
    kind="assistant",
):
    message = {
        "model": "m",
        "usage": usage
        if usage is not None
        else {
            "input_tokens": i,
            "output_tokens": out,
            "cache_read_input_tokens": cr,
            "cache_creation_input_tokens": cw,
        },
    }
    if mid:
        message["id"] = mid
    row = {
        "type": kind,
        "timestamp": at,
        "uuid": uuid or f"u-{mid}-{out}",
        "message": message,
    }
    return json.dumps(row)


def _write(path: Path, lines: Sequence[str]) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def selftest() -> list[str]:
    """Check every rule against hand-written transcripts; return the names of the
    checks that passed. Raises AssertionError naming the first that failed."""
    done: list[str] = []

    def check(name: str, ok: bool) -> None:
        assert ok, f"selftest failed: {name}"
        done.append(name)

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        # A: msg a (two lines: 5 then 50 output), msg b (one line, 7 output).
        a = _write(
            d / "a.jsonl",
            [
                _line("a", 5),
                _line("a", 50, at="2026-11-06T09:00:05Z"),
                _line("b", 7, at="2026-11-06T09:10:00Z"),
            ],
        )
        rep = monitor({"t1": [a]})
        t = rep["tasks"]["t1"]
        check("last_usage_per_id", [r["output"] for r in t["per_message"]] == [50, 7])
        # i=1, cr=10, cw=100 per message; 2 messages.
        check(
            "cumulative",
            t["tokens"]
            == {"input_new": 2, "output": 57, "cache_read": 20, "cache_write": 200},
        )
        check("fresh_and_with_cache_apart", (t["fresh"], t["with_cache"]) == (259, 279))
        check(
            "per_message_totals",
            [(r["fresh"], r["with_cache"]) for r in t["per_message"]]
            == [(151, 161), (108, 118)],
        )
        check("exact_when_complete", t["exact"] and t["reasons"] == [])

        # B: unknown is not zero.
        b = _write(
            d / "b.jsonl",
            [
                _line("c", 4, usage={"input_tokens": 1, "output_tokens": 4}),
                _line(
                    "d",
                    6,
                    usage={
                        "input_tokens": 2,
                        "output_tokens": 6,
                        "cache_creation_input_tokens": 9,
                    },
                ),
            ],
        )
        tb = monitor({"t2": [b]})["tasks"]["t2"]
        check(
            "missing_not_zero",
            tb["tokens"]["cache_read"] is None and tb["with_cache"] is None,
        )
        check(
            "missing_cache_write_partial",
            tb["tokens"]["cache_write"] == 9
            and tb["missing_fields"]["cache_write"] == 1,
        )
        check(
            "missing_makes_inexact",
            (not tb["exact"]) and "missing_fields" in tb["reasons"],
        )
        check("fresh_lower_bound_kept", tb["fresh"] == 22)
        check(
            "per_message_unknown",
            [r["with_cache"] for r in tb["per_message"]] == [None, None]
            and tb["per_message"][0]["fresh"] is None,
        )

        # C: alert at more than 2x the estimate, fresh basis.
        r = monitor({"t1": [a]}, estimates={"t1": 129})  # 259 > 258
        check(
            "alert_over",
            r["tasks"]["t1"]["alert"] is True and r["total"]["alerts"] == ["t1"],
        )
        r = monitor({"t1": [a]}, estimates={"t1": 130})  # 259 <= 260
        check(
            "no_alert_within",
            r["tasks"]["t1"]["alert"] is False
            and r["tasks"]["t1"]["alert_state"] == "within_2x",
        )
        r = monitor({"t1": [a]})
        check(
            "no_estimate_no_alert",
            r["tasks"]["t1"]["alert"] is None
            and r["tasks"]["t1"]["alert_state"] == "no_estimate",
        )
        r = monitor({"t2": [b]}, estimates={"t2": 1})
        check(
            "alert_on_lower_bound",
            r["tasks"]["t2"]["alert"] is True and not r["tasks"]["t2"]["exact"],
        )
        only_out = _write(d / "o.jsonl", [_line("z", 3, usage={"output_tokens": 3})])
        r = monitor({"t3": [only_out]}, estimates={"t3": 1})
        check(
            "fresh_unknown_no_alert",
            r["tasks"]["t3"]["alert"] is None
            and r["tasks"]["t3"]["alert_state"] == "fresh_unknown",
        )

        # D: per-label roll-up.
        r = monitor({"t1": [a], "t4": [_write(d / "c.jsonl", [_line("e", 1)])]})
        check(
            "by_label", set(r["tasks"]) == {"t1", "t4"} and r["total"]["messages"] == 3
        )
        check(
            "grand_total",
            r["total"]["tokens"]["output"] == 58 and r["total"]["fresh"] == 259 + 102,
        )
        try:
            monitor({"x": [a], "y": [a]})
        except ValueError:
            check("same_file_two_labels_refused", True)
        else:
            check("same_file_two_labels_refused", False)

        # E: window by the first line.
        w = monitor({"t1": [a]}, since="2026-11-06T09:05:00Z")
        check(
            "window_first_line",
            [x["id"] for x in w["tasks"]["t1"]["per_message"]] == ["b"],
        )
        w = monitor({"t1": [a]}, until="2026-11-06T09:00:02Z")
        check(
            "window_straddle_final_usage",
            [x["output"] for x in w["tasks"]["t1"]["per_message"]] == [50],
        )

        # F: sub-agent result line (tokens.py rule): counted, not added, uncovered -> lower bound.
        res = json.dumps(
            {
                "type": "user",
                "timestamp": "2026-11-06T09:00:00Z",
                "toolUseResult": {"agentId": "ag1", "status": "async_launched"},
            }
        )
        main = _write(d / "main.jsonl", [_line("m", 2), res])
        tm = monitor({"s": [main]})["tasks"]["s"]
        check(
            "subagent_uncovered_inexact",
            tm["subagents_uncovered"] == 1
            and not tm["exact"]
            and tm["tokens"]["output"] == 2,
        )
        sub = _write(d / "agent-ag1.jsonl", [_line("q", 8)])
        tm = monitor({"s": [main, sub]})["tasks"]["s"]
        check(
            "subagent_covered_exact",
            tm["subagents_uncovered"] == 0
            and tm["exact"]
            and tm["tokens"]["output"] == 10,
        )

        # G: counts only, no content.
        canary = "SECRET-PROMPT-TEXT"
        leaky = _write(
            d / "leak.jsonl",
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": "2026-11-06T09:00:00Z",
                        "uuid": "u9",
                        "cwd": "/private/dir",
                        "message": {
                            "id": "L",
                            "model": "m",
                            "content": [{"type": "text", "text": canary}],
                            "usage": {
                                "input_tokens": 1,
                                "output_tokens": 1,
                                "cache_read_input_tokens": 1,
                                "cache_creation_input_tokens": 1,
                            },
                        },
                    }
                )
            ],
        )
        rep = monitor({"leak": [leaky]})
        blob = json.dumps(rep) + to_markdown(rep)
        check(
            "no_content_no_path",
            canary not in blob and "/private/dir" not in blob and tmp not in blob,
        )
        md = to_markdown(monitor({"t2": [b]}, estimates={"t2": 1}))
        check(
            "markdown_marks_unknown_and_alert",
            "unknown" in md and "ALERT >2x" in md and "NO (lower bound)" in md,
        )
    return done


def _pairs(items: Sequence[str], what: str) -> list[tuple[str, str]]:
    out = []
    for item in items:
        label, sep, rest = item.partition("=")
        if not sep or not label or not rest:
            raise ValueError(f"{what} wants LABEL=VALUE, got {item!r}")
        out.append((label, rest))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m levi.eval.token_monitor",
        description="Count and monitor external tokens per task from the named Claude transcript files (counts only).",
    )
    parser.add_argument(
        "--task",
        action="append",
        default=[],
        metavar="LABEL=FILE",
        help="a transcript file under a task label; repeat (oldest file first within a label)",
    )
    parser.add_argument(
        "--estimate",
        action="append",
        default=[],
        metavar="LABEL=N",
        help="estimate in fresh tokens (input_new+output+cache_write); alert above 2x",
    )
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--out", help="write the output here instead of stdout")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="check the rules on synthetic transcripts and exit",
    )
    args = parser.parse_args(argv)
    if args.selftest:
        try:
            names = selftest()
        except AssertionError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"selftest ok: {len(names)} checks")
        return 0
    try:
        if not args.task:
            raise ValueError("no --task given")
        tasks: dict[str, list[str]] = {}
        for label, path in _pairs(args.task, "--task"):
            tasks.setdefault(label, []).append(path)
        estimates = {}
        for label, value in _pairs(args.estimate, "--estimate"):
            estimates[label] = int(value)
        report = monitor(tasks, estimates=estimates, since=args.since, until=args.until)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    text = (
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.format == "json"
        else to_markdown(report)
    )
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
