"""External-agent token usage from Claude session transcripts.

The teacher arm of a teacher/student round (``levi.eval.compare``) is an
external agent, so its cost is that agent's own token usage. A Claude Code
session file is JSON lines; an assistant message is written as several lines
(one per content block) that all carry the same ``message.id`` and whose
``message.usage`` grows line by line. **The last line of a message holds the
final count**; keeping the first line under-counts a message several times
over (measured on a sub-agent transcript: output 16 on the first line, 415 on
the last). So this module keeps the last usage record per message id.

The rule is the one of ``lab/aeri-external/tools/agent_tokens.py`` (the
sub-agent branch, lines 76-96), rewritten here so the evaluation code does not
depend on the git-ignored lab folder.

Rules:

- The caller passes the transcript files explicitly. Nothing here lists a
  directory, expands a glob or looks into a home folder, so a caller cannot
  sweep up sessions that are not part of the round.
- Fields are reported apart, never summed: ``input_new`` (``input_tokens``),
  ``output`` (``output_tokens``), ``cache_read``
  (``cache_read_input_tokens``) and ``cache_write``
  (``cache_creation_input_tokens``), the names of
  ``levi.aeri.external_report.v1``.
- **Unknown is not zero.** A field that no counted message carried is ``None``
  in the result. A message whose final usage lacks a field, a line that cannot
  be parsed and a message that cannot be placed in the time window are counted
  and reported (``Usage.exact`` is then false), never dropped silently.
- A time window keeps a message when its *first* line's timestamp lies in
  ``[since, until]``; its usage is then the final one, so a message that
  straddles the window's end is neither cut in half nor counted twice.

Standard library only; no network, no GPU.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

FIELDS = ("input_new", "output", "cache_read", "cache_write")
USAGE_KEYS = {
    "input_new": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cache_read_input_tokens",
    "cache_write": "cache_creation_input_tokens",
}
# The largest count levi.aeri.external_report.v1 accepts.
MAX_COUNT = 2**53 - 1
RULE = "last usage record per message id"


def parse_time(value) -> float:
    """A window bound as epoch seconds: a number, or an ISO 8601 time with a
    zone (``2026-11-06T09:00:00Z``). A time without a zone is refused rather
    than guessed."""
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            raise ValueError(f"not a time: {value!r}")
        return float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            pass
        else:
            return parse_time(number)
        try:
            moment = datetime.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValueError(f"not a time: {value!r}") from exc
    else:
        raise TypeError(f"not a time: {value!r}")
    if moment.tzinfo is None:
        raise ValueError(f"time without a zone is ambiguous: {value!r}")
    return moment.timestamp()


def _line_time(value) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return parse_time(value)
    except ValueError:
        return None


def _count(value) -> int | None:
    if type(value) is int and 0 <= value <= MAX_COUNT:
        return value
    return None


@dataclass(frozen=True)
class Usage:
    """Token usage summed over the counted messages, with what was left out."""

    tokens: dict[str, int | None]
    messages: int
    models: tuple[str, ...]
    files: int
    lines: int  # non-blank lines read
    other_lines: int  # lines that carry no assistant usage record
    bad_lines: int  # mention "usage" but are not a parseable record
    no_id: int  # assistant usage lines with neither message.id nor uuid
    unplaced: int  # messages without a valid timestamp, left out of a window
    outside_window: int  # messages whose first line is outside the window
    missing_fields: dict[str, int] = field(default_factory=dict)

    @property
    def exact(self) -> bool:
        """True only if nothing was set aside and every field was present on
        every counted message. Otherwise a total is a lower bound."""
        return (
            self.messages > 0
            and self.bad_lines == 0
            and self.no_id == 0
            and self.unplaced == 0
            and not any(self.missing_fields.values())
        )

    @property
    def source(self) -> str:
        """One line for ``ArmRecord.external_tokens_source``."""
        text = (
            f"claude session jsonl ({RULE}); {self.files} files, "
            f"{self.messages} messages"
        )
        if self.exact:
            return text
        if not self.messages:
            return text + "; nothing counted, tokens unknown"
        return (
            text
            + f"; LOWER BOUND: {self.bad_lines} bad lines, {self.no_id} without id, "
            f"{self.unplaced} unplaced, {sum(self.missing_fields.values())} missing fields"
        )

    def external_tokens(self) -> dict[str, int | None]:
        return dict(self.tokens)

    def to_dict(self) -> dict:
        return {
            "rule": RULE,
            "tokens": dict(self.tokens),
            "exact": self.exact,
            "messages": self.messages,
            "models": list(self.models),
            "files": self.files,
            "lines": self.lines,
            "other_lines": self.other_lines,
            "bad_lines": self.bad_lines,
            "no_id": self.no_id,
            "unplaced": self.unplaced,
            "outside_window": self.outside_window,
            "missing_fields": dict(self.missing_fields),
        }


def usage_from_files(
    paths: Iterable[str | Path],
    *,
    since: float | str | datetime | None = None,
    until: float | str | datetime | None = None,
) -> Usage:
    """Sum the final usage of every message in the given transcript files.

    ``paths`` are files, in the order they should win a duplicate message id
    (a later file replaces an earlier one's copy; resumed sessions repeat
    earlier messages, so pass the oldest first). A directory or a missing file
    raises; an empty list raises. ``since`` / ``until`` bound the window
    (inclusive); either may be left open.
    """
    files = [Path(p) for p in paths]
    if not files:
        raise ValueError("no transcript files given: pass the session files explicitly")
    lo = parse_time(since) if since is not None else None
    hi = parse_time(until) if until is not None else None
    if lo is not None and hi is not None and lo > hi:
        raise ValueError("since is after until")

    lines = other = bad = no_id = 0
    # message id -> [first timestamp or None, last usage dict, model]
    messages: dict[str, list] = {}
    for path in files:
        with path.open("rb") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                lines += 1
                if b'"usage"' not in raw:
                    other += 1
                    continue
                try:
                    row = json.loads(raw)
                except (ValueError, RecursionError):
                    bad += 1
                    continue
                if not isinstance(row, dict):
                    bad += 1
                    continue
                message = row.get("message")
                if row.get("type") != "assistant" or message is None:
                    other += 1
                    continue
                if not isinstance(message, dict):
                    bad += 1
                    continue
                usage = message.get("usage")
                if not usage:
                    other += 1
                    continue
                if not isinstance(usage, dict):
                    bad += 1
                    continue
                key = message.get("id") or row.get("uuid")
                if not isinstance(key, str) or not key:
                    no_id += 1
                    continue
                stamp = _line_time(row.get("timestamp"))
                entry = messages.get(key)
                if entry is None:
                    messages[key] = [stamp, usage, message.get("model")]
                    continue
                if stamp is not None and (entry[0] is None or stamp < entry[0]):
                    entry[0] = stamp
                entry[1] = usage  # the last line of a message is the final count
                entry[2] = message.get("model") or entry[2]

    unplaced = outside = 0
    totals = dict.fromkeys(FIELDS, 0)
    seen = dict.fromkeys(FIELDS, 0)
    missing = dict.fromkeys(FIELDS, 0)
    models, counted = set(), 0
    windowed = lo is not None or hi is not None
    for first, usage, model in messages.values():
        if windowed:
            if first is None:
                unplaced += 1
                continue
            if (lo is not None and first < lo) or (hi is not None and first > hi):
                outside += 1
                continue
        counted += 1
        if isinstance(model, str) and model:
            models.add(model)
        for name, key in USAGE_KEYS.items():
            value = _count(usage.get(key))
            if value is None:
                missing[name] += 1
            else:
                totals[name] += value
                seen[name] += 1
    return Usage(
        tokens={name: (totals[name] if seen[name] else None) for name in FIELDS},
        messages=counted,
        models=tuple(sorted(models)),
        files=len(files),
        lines=lines,
        other_lines=other,
        bad_lines=bad,
        no_id=no_id,
        unplaced=unplaced,
        outside_window=outside,
        missing_fields=missing,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m levi.eval.tokens",
        description=(
            "Sum the final token usage (last usage record per message id) of the "
            "given Claude session transcript files. Reads only the files named."
        ),
    )
    parser.add_argument("files", nargs="+", help="session .jsonl files, oldest first")
    parser.add_argument(
        "--since", help="window start: epoch seconds or ISO time with zone"
    )
    parser.add_argument(
        "--until", help="window end: epoch seconds or ISO time with zone"
    )
    args = parser.parse_args(argv)
    try:
        usage = usage_from_files(args.files, since=args.since, until=args.until)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(usage.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
