"""A confirm episode is confirmed once (design U8): the append-only claim ledger.

Same semantics and the same line format (``levi.causal_confirm_ledger.v1``) as
``levi.events.causal_eval.claim_ledger``, whose ``confirmed_overlap`` this reuses. The
core, ``claim``, is a pure function over rows; ``claim_ledger`` adds the locked append.
A claim whose episodes (id, or any capture sha256) were already confirmed is refused with
``ConfirmReused`` (exit code ``CONFIRM_REUSED_EXIT_CODE``), and nothing is written.
"""

from __future__ import annotations

import fcntl
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from levi.events.causal_eval import LEDGER_SCHEMA, confirmed_overlap

CONFIRM_REUSED_EXIT_CODE = 5  # 3 is ConfirmDataError (calibration), 4 is LeakFound


class ConfirmReused(Exception):
    """A confirm episode was already confirmed (exit code 5)."""

    exit_code = CONFIRM_REUSED_EXIT_CODE


class LedgerCorrupt(ValueError):
    """A ledger line is not a confirm record: refuse rather than guess."""


def check_entry(entry: Mapping) -> None:
    """``entry`` must be a confirm record; raises ``LedgerCorrupt`` otherwise."""
    # Explicit checks, not assert: python -O strips an assert (same rule as
    # causal_eval.read_ledger, see commit 69f34780).
    try:
        ok = (
            entry["schema"] == LEDGER_SCHEMA
            and len(entry["episodes"]) > 0
            and all(
                isinstance(e["id"], str) and isinstance(e["sha256"], dict)
                for e in entry["episodes"]
            )
        )
    except (KeyError, TypeError):
        ok = False
    if not ok:
        raise LedgerCorrupt("not a confirm record")


def claim(rows: Sequence[Mapping], entry: Mapping) -> list[Mapping]:
    """The ledger after claiming ``entry``; ``ConfirmReused`` if any of its episodes is in ``rows``."""
    check_entry(entry)
    for row in rows:
        check_entry(row)
    taken = confirmed_overlap(
        rows,
        [e["id"] for e in entry["episodes"]],
        [d for e in entry["episodes"] for d in e["sha256"].values()],
    )
    if taken is not None:
        raise ConfirmReused(f"{taken} was already confirmed")
    return [*rows, entry]


def read_rows(path) -> list[Mapping]:
    file = Path(path)
    if not file.exists():
        return []
    rows = []
    for n, line in enumerate(file.read_text().splitlines(), 1):
        if line.strip():
            try:
                row = json.loads(line)
                check_entry(row)
            except (ValueError, LedgerCorrupt):
                raise LedgerCorrupt(
                    f"line {n} of {path} is not a confirm record"
                ) from None
            rows.append(row)
    return rows


def claim_ledger(path, entry: Mapping) -> None:
    """Append ``entry`` under a lock unless one of its episodes is already confirmed."""
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        rows = []
        for n, line in enumerate(handle.read().splitlines(), 1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    raise LedgerCorrupt(f"line {n} of {path} is not JSON") from None
        claim(rows, entry)
        handle.seek(0, 2)
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
        handle.flush()
