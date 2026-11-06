"""External-agent token usage from Claude session transcripts.

The teacher arm of a teacher/student round (``levi.eval.compare``) is an
external agent, so its cost is that agent's own token usage. A Claude Code
session file is JSON lines; an assistant message is written as several lines
(one per content block) that all carry the same ``message.id`` and whose
``message.usage`` grows line by line. **The last line of a message holds the
final count**; keeping the first line under-counts a message several times
over (measured on a sub-agent transcript: output 16 on the first line, 415 on
the last). So this module keeps the last usage record per message id.

The same rule is what the maintainers' external-agent accounting script
applies to a sub-agent transcript; it is written out here so the evaluation code
depends on no script outside the repository.

Rules:

- The caller passes the transcript files explicitly. Nothing here lists a
  directory, expands a glob or looks into a home folder, so a caller cannot
  sweep up sessions that are not part of the round.
- **Exact holds only for the files given.** A teacher that answers through
  sub-agents leaves a result line (``type: user`` with a ``toolUseResult`` that
  carries an ``agentId``) in the main session and the sub-agent's own
  messages in ``<session>/subagents/agent-<id>.jsonl``. Such a result line is
  counted (``subagent_results``); its aggregate usage is never added, because
  the sub-agent's file holds the per-message counts and adding both would count
  twice. A sub-agent whose file was not given is ``subagents_uncovered``: its
  tokens are missing from the totals, ``exact`` is false and ``source`` says so.
- Fields are reported apart, never summed: ``input_new`` (``input_tokens``),
  ``output`` (``output_tokens``), ``cache_read``
  (``cache_read_input_tokens``) and ``cache_write``
  (``cache_creation_input_tokens``), the names of
  ``levi.aeri.external_report.v1``.
- **Unknown is not zero.** A field that no counted message carried is ``None``
  in the result. A message whose final usage lacks a field (an empty ``{}``
  usage lacks all four), a line that cannot be parsed, a half-written last line
  (``truncated_tail``), a message keyed by ``uuid`` because it has no
  ``message.id`` (``uuid_fallback``: lines of one message then count apart) and
  a message that cannot be placed in the time window are counted and reported
  (``Usage.exact`` is then false), never dropped silently.
- A time window keeps a message when its *first* line's timestamp lies in
  ``[since, until]``; its usage is then the final one, so a message that
  straddles the window's end is neither cut in half nor counted twice.

Standard library only; no network, no GPU.
"""

from __future__ import annotations

import argparse
import json
import math
import re
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
SOURCE_CODE_PREFIX = "claude_jsonl.last_usage"
MAX_SOURCE = 200  # the longest external_tokens_source an ArmRecord takes


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


def _subagent_result(row: dict) -> tuple[bool, str | None]:
    """(is a sub-agent result line, its agent id). The main session records a
    finished sub-agent as a ``user`` line whose ``toolUseResult`` names the
    agent (``agentId``) and carries its aggregate ``totalTokens`` / ``usage``;
    other tools' results have neither."""
    result = row.get("toolUseResult")
    if not isinstance(result, dict):
        return False, None
    agent_id = result.get("agentId")
    if isinstance(agent_id, str) and agent_id:
        return True, agent_id
    if "totalTokens" in result:
        return True, None
    return False, None


def _outside(stamp: float | None, lo: float | None, hi: float | None) -> bool:
    """True when ``stamp`` is a time and lies outside the window (a line
    without a time is kept: it cannot be ruled out)."""
    if stamp is None:
        return False
    return (lo is not None and stamp < lo) or (hi is not None and stamp > hi)


@dataclass(frozen=True)
class Usage:
    """Token usage summed over the counted messages, with what was left out."""

    tokens: dict[str, int | None]
    messages: int
    models: tuple[str, ...]
    files: int
    lines: int  # non-blank lines read
    other_lines: int  # lines that carry no assistant usage record
    bad_lines: int  # mention "usage" or "toolUseResult" but are not a parseable record
    no_id: int  # assistant usage lines with neither message.id nor uuid
    unplaced: int  # messages without a valid timestamp, left out of a window
    outside_window: int  # messages whose first line is outside the window
    missing_fields: dict[str, int] = field(default_factory=dict)
    uuid_fallback: int = 0  # counted messages keyed by uuid (no message.id)
    truncated_tail: int = 0  # files whose last line is half written
    subagent_results: int = 0  # sub-agent result lines in the given files
    subagents_uncovered: int = 0  # ... whose own transcript was not given

    @property
    def reasons(self) -> tuple[str, ...]:
        """Why the count is not exact (empty = exact). Codes, not text."""
        found = []
        if not self.messages:
            found.append("nothing_counted")
        for code, value in (
            ("bad_lines", self.bad_lines),
            ("no_id", self.no_id),
            ("unplaced", self.unplaced),
            ("missing_fields", sum(self.missing_fields.values())),
            ("uuid_fallback", self.uuid_fallback),
            ("truncated_tail", self.truncated_tail),
            ("subagent_uncovered", self.subagents_uncovered),
        ):
            if value:
                found.append(code)
        return tuple(found)

    @property
    def exact(self) -> bool:
        """True only if nothing was set aside, every field was present on every
        counted message and no sub-agent's transcript is missing. It speaks
        for the files given, not for everything the round cost. Otherwise a
        total is a lower bound (a ``uuid`` fallback can also count a message
        that spans several lines more than once)."""
        return not self.reasons

    @property
    def source(self) -> str:
        """One line for ``ArmRecord.external_tokens_source`` (at most 200
        characters, the record's limit, even when every reason applies). A lower
        bound lists the reason codes of ``reasons`` with their counts."""
        text = (
            f"claude session jsonl ({RULE}); {self.files} files, "
            f"{self.messages} messages"
        )
        if self.exact:
            return text
        if not self.messages:
            return text + "; nothing counted, tokens unknown"
        counts = {
            "bad_lines": self.bad_lines,
            "no_id": self.no_id,
            "unplaced": self.unplaced,
            "missing_fields": sum(self.missing_fields.values()),
            "uuid_fallback": self.uuid_fallback,
            "truncated_tail": self.truncated_tail,
            "subagent_uncovered": self.subagents_uncovered,
        }
        counts = {name: value for name, value in counts.items() if value}
        text += "; LOWER BOUND: " + ", ".join(f"{n}={v}" for n, v in counts.items())
        if len(text) > MAX_SOURCE:  # absurd counts: keep the codes, drop the numbers
            text = text[: text.index("; LOWER BOUND")] + "; LOWER BOUND: "
            text += "+".join(counts)
        return text

    @property
    def source_code(self) -> str:
        """The same as ``source`` as one code (``[A-Za-z0-9_.:+-]``, under 64
        characters), for a confirm record, where ``source``'s sentence is not
        allowed. ``reasons`` has the details."""
        if self.exact:
            state = "exact"
        elif not self.messages:
            state = "nothing_counted"
        elif self.uuid_fallback:
            state = "not_exact"
        else:
            state = "lower_bound"
        return f"{SOURCE_CODE_PREFIX}:{state}"

    def external_tokens(self) -> dict[str, int | None]:
        return dict(self.tokens)

    def record_fields(self, *, confirm: bool = False) -> dict:
        """The three ``levi.eval.compare.ArmRecord`` fields this count fills:
        ``external_tokens``, ``external_tokens_exact`` (false = a lower bound;
        the table then marks the teacher's total and will not call a student
        with a lower bound "fewer") and ``external_tokens_source`` (the code
        for a confirm record, the sentence otherwise)."""
        return {
            "external_tokens": self.external_tokens(),
            "external_tokens_exact": self.exact,
            "external_tokens_source": self.source_code if confirm else self.source,
        }

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
            "uuid_fallback": self.uuid_fallback,
            "truncated_tail": self.truncated_tail,
            "subagent_results": self.subagent_results,
            "subagents_uncovered": self.subagents_uncovered,
            "reasons": list(self.reasons),
            "source_code": self.source_code,
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

    lines = other = bad = no_id = truncated = 0
    # message id -> [first timestamp or None, last usage dict, model, keyed by uuid]
    messages: dict[str, list] = {}
    results: set[str] = set()  # agent ids of sub-agent result lines
    results_without_id = 0
    covered: set[str] = set()  # agent ids whose own transcript was given
    for path in files:
        file_agents: set[str] = set()
        file_messages = 0
        with path.open("rb") as handle:
            for raw in handle:
                half_written = not raw.endswith(b"\n")
                raw = raw.strip()
                if not raw:
                    continue
                lines += 1
                if half_written:
                    # Only the last line of a file can lack its newline. A
                    # complete record without one is fine; a cut one is not.
                    try:
                        json.loads(raw)
                    except (ValueError, RecursionError):
                        truncated += 1
                        continue
                if b'"usage"' not in raw and b'"toolUseResult"' not in raw:
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
                if row.get("type") == "user":
                    is_result, agent_id = _subagent_result(row)
                    if not is_result or _outside(
                        _line_time(row.get("timestamp")), lo, hi
                    ):
                        other += 1
                    elif agent_id is None:
                        results_without_id += 1
                    else:
                        results.add(agent_id)
                    continue
                message = row.get("message")
                if row.get("type") != "assistant" or message is None:
                    other += 1
                    continue
                if not isinstance(message, dict):
                    bad += 1
                    continue
                if "usage" not in message:
                    other += 1
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    bad += 1
                    continue
                by_uuid = not message.get("id")
                key = row.get("uuid") if by_uuid else message.get("id")
                if not isinstance(key, str) or not key:
                    no_id += 1
                    continue
                file_messages += 1
                if isinstance(row.get("agentId"), str) and row["agentId"]:
                    file_agents.add(row["agentId"])
                stamp = _line_time(row.get("timestamp"))
                entry = messages.get(key)
                if entry is None:
                    messages[key] = [stamp, usage, message.get("model"), by_uuid]
                    continue
                if stamp is not None and (entry[0] is None or stamp < entry[0]):
                    entry[0] = stamp
                entry[1] = usage  # the last line of a message is the final count
                entry[2] = message.get("model") or entry[2]
        covered |= file_agents
        named = re.fullmatch(r"agent-(.+)", path.stem)
        if named and file_messages:
            covered.add(named.group(1))

    unplaced = outside = by_uuid_counted = 0
    totals = dict.fromkeys(FIELDS, 0)
    seen = dict.fromkeys(FIELDS, 0)
    missing = dict.fromkeys(FIELDS, 0)
    models, counted = set(), 0
    windowed = lo is not None or hi is not None
    for first, usage, model, by_uuid in messages.values():
        if windowed:
            if first is None:
                unplaced += 1
                continue
            if (lo is not None and first < lo) or (hi is not None and first > hi):
                outside += 1
                continue
        counted += 1
        by_uuid_counted += by_uuid
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
        uuid_fallback=by_uuid_counted,
        truncated_tail=truncated,
        subagent_results=len(results) + results_without_id,
        subagents_uncovered=len(results - covered) + results_without_id,
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
