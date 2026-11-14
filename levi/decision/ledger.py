"""External-token ledger: one line per panel member per escalation attempt (design 3.3).

Append-only JSON lines (``levi.decision.token_ledger.v1``). Counts follow
``levi.eval.token_monitor``: ``fresh`` = input_new + output + cache_write,
``with_cache`` = all four fields, a total is ``None`` as soon as one of its fields is
unknown, and **unknown is null, never 0**. A row with an unknown field is never
``exact``. Per-field totals over rows add the known values (``None`` only when no row knew
it), as the monitor does, so a total over rows with unknowns is a lower bound and its
``exact`` is false.

The ledger holds no transcript text and no answer. ``report`` gives per-request rows only
for splits that may be shown; for ``confirm`` (and ``frozen``, ``heldout``) it gives
aggregates only.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from levi.eval.token_monitor import FRESH_FIELDS, LABEL_RE, _sum_known, _total

from .contracts import TOKEN_FIELDS, TokenRow
from .jsonl import line_problem, start_on_fresh_line

SCHEMA = "levi.decision.token_ledger.v1"
PURPOSES = ("escalation", "reference_T", "confirm_reference", "setup_paradigm", "audit")
SPLITS = ("tune", "production", "confirm", "frozen", "heldout")
AGGREGATE_ONLY_SPLITS = ("confirm", "frozen", "heldout")


class LedgerError(ValueError):
    """A ledger row or line is not valid: refuse rather than guess."""


def make_row(
    request_sha256: str,
    task: str,
    split: str,
    row: TokenRow,
    *,
    purpose: str = "escalation",
    attempt: int = 1,
    source_code: str | None = None,
    at: str | None = None,
) -> dict:
    """One ledger line. Unknown fields stay None and force ``exact`` false."""
    if not (isinstance(request_sha256, str) and len(request_sha256) == 64):
        raise LedgerError("request_sha256 must be 64 hex characters")
    if not LABEL_RE.fullmatch(task):
        raise LedgerError("task label must match [A-Za-z0-9_.:-], 1-64 chars")
    if split not in SPLITS:
        raise LedgerError(f"split must be one of {SPLITS}")
    if purpose not in PURPOSES:
        raise LedgerError(f"purpose must be one of {PURPOSES}")
    if not (isinstance(row.role, str) and LABEL_RE.fullmatch(row.role)):
        raise LedgerError("role must match [A-Za-z0-9_.:-], 1-64 chars")
    if source_code is not None and not (
        isinstance(source_code, str) and LABEL_RE.fullmatch(source_code)
    ):
        raise LedgerError("source_code must match [A-Za-z0-9_.:-], 1-64 chars, or None")
    fields = {n: getattr(row, n) for n in TOKEN_FIELDS}
    return {
        "schema": SCHEMA,
        "request_sha256": request_sha256,
        "task": task,
        "purpose": purpose,
        "split": split,
        "role": row.role,
        "attempt": attempt,
        **fields,
        "fresh": _total(fields, FRESH_FIELDS),
        "with_cache": _total(fields, TOKEN_FIELDS),
        "exact": bool(row.exact) and all(v is not None for v in fields.values()),
        "source_code": source_code,
        "at": at or datetime.now(UTC).isoformat(timespec="seconds"),
    }


def _label_ok(value) -> bool:
    return isinstance(value, str) and LABEL_RE.fullmatch(value) is not None


def _key(r: Mapping):
    return (r["request_sha256"], r["purpose"], r["role"], r["attempt"])


def _check_row(r: Mapping) -> None:
    """The checks a stored row must pass (``read_rows``); raises ValueError, KeyError
    or TypeError."""
    fields = {k: r[k] for k in TOKEN_FIELDS}
    if not (
        r["schema"] == SCHEMA
        and r["split"] in SPLITS
        and r["purpose"] in PURPOSES
        and _label_ok(r["role"])
        and (r["source_code"] is None or _label_ok(r["source_code"]))
        and all(v is None or (type(v) is int and v >= 0) for v in fields.values())
        and (r["exact"] is False or all(v is not None for v in fields.values()))
        and r["fresh"] == _total(fields, FRESH_FIELDS)
        and r["with_cache"] == _total(fields, TOKEN_FIELDS)
    ):
        raise ValueError("row")
    _key(r)


def _check_new_row(r: Mapping) -> None:
    """What ``append_rows`` demands before it writes: the stored-row checks, and the
    fields of the dedupe key and ``make_row`` (a row that reads back, never one that
    would make ``read_rows`` refuse the whole file)."""
    _check_row(r)
    if not (
        isinstance(r["request_sha256"], str)
        and len(r["request_sha256"]) == 64
        and _label_ok(r["task"])
        and type(r["attempt"]) is int
        and r["attempt"] >= 1
    ):
        raise ValueError("row")
    json.dumps(r, sort_keys=True, allow_nan=False)


def read_rows(path) -> list[dict]:
    file = Path(path)
    if not file.exists():
        return []
    text = file.read_text()
    rows = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
            _check_row(r)
        except (ValueError, KeyError, TypeError):
            raise LedgerError(line_problem(path, n, text, "ledger row")) from None
        rows.append(r)
    return rows


def append_rows(path, rows: Iterable[Mapping]) -> int:
    """Append rows that are not already there (key: request sha256, purpose, role, attempt).
    Returns how many were written; a replayed request writes none.

    Every row is checked first (``_check_new_row``) and one that would not read back
    raises ``LedgerError`` with nothing written. The append starts on a fresh line (a
    last line without its newline gets one, see ``levi.decision.jsonl``); a file whose
    existing lines are not rows is refused, never appended to."""
    rows = list(rows)
    for n, r in enumerate(rows):
        try:
            _check_new_row(r)
        except (ValueError, KeyError, TypeError):
            raise LedgerError(f"row {n} to append is not a valid ledger row") from None
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with file.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        text = handle.read()
        have = set()
        for n, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                have.add(_key(json.loads(line)))
            except (ValueError, KeyError, TypeError):
                raise LedgerError(line_problem(path, n, text, "ledger row")) from None
        handle.seek(0, 2)
        start_on_fresh_line(handle)
        for r in rows:
            if _key(r) in have:
                continue
            have.add(_key(r))
            handle.write(json.dumps(r, sort_keys=True, allow_nan=False) + "\n")
            written += 1
        handle.flush()
        os.fsync(handle.fileno())
    return written


def _aggregate(rows: Sequence[Mapping]) -> dict:
    tokens = {n: _sum_known([r[n] for r in rows]) for n in TOKEN_FIELDS}
    return {
        "rows": len(rows),
        "requests": len({r["request_sha256"] for r in rows}),
        "tokens": tokens,
        "fresh": _total(tokens, FRESH_FIELDS),
        "with_cache": _total(tokens, TOKEN_FIELDS),
        "exact": all(r["exact"] for r in rows),
    }


def report(rows: Sequence[Mapping]) -> dict:
    """Totals by split and task, both bases. Per-request lines only outside the
    aggregate-only splits (``confirm``, ``frozen``, ``heldout``)."""
    by_split: dict[str, dict] = {}
    for split in sorted({r["split"] for r in rows}):
        part = [r for r in rows if r["split"] == split]
        by_split[split] = {
            "all": _aggregate(part),
            "tasks": {
                t: _aggregate([r for r in part if r["task"] == t])
                for t in sorted({r["task"] for r in part})
            },
        }
    shown = [r for r in rows if r["split"] not in AGGREGATE_ONLY_SPLITS]
    return {
        "schema": SCHEMA + ".report",
        "fresh_definition": "+".join(FRESH_FIELDS),
        "with_cache_definition": "+".join(TOKEN_FIELDS),
        "by_split": by_split,
        "total": _aggregate(rows),
        "per_request": [
            {
                k: r[k]
                for k in (
                    "request_sha256",
                    "task",
                    "split",
                    "role",
                    "attempt",
                    "fresh",
                    "with_cache",
                    "exact",
                )
            }
            for r in shown
        ],
        "aggregate_only_splits": list(AGGREGATE_ONLY_SPLITS),
    }
