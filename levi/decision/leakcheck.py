"""Gate before an escalation request leaves: it must not carry a confirm, frozen or
held-out fragment id or its sha256.

The deny list is injected by the caller (built from the confirm ledger, see
``Denylist.from_ledger_rows``); this module reads no gold, frozen or held-out folder.
A hit refuses the request and has its own exit code, ``LEAK_EXIT_CODE``.

What is scanned: every key and string value of the teacher-visible wire (the request id
too), as a case-insensitive substring match; the sha256 of every string value; and, for a
``data:`` URL, the sha256 of the decoded bytes (images are addressed by content hash).
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

LEAK_EXIT_CODE = 4  # 3 is ConfirmDataError (calibration), 5 is ConfirmReused
MIN_ID_LENGTH = 4  # a shorter id would match half the text: refuse such a list
_HEX = frozenset("0123456789abcdef")


class LeakFound(Exception):
    """A request carries a denied identifier (exit code ``LEAK_EXIT_CODE``)."""

    exit_code = LEAK_EXIT_CODE

    def __init__(self, hits: list[tuple[str, str]]):
        self.hits = hits  # (json path, kind); the matched text is not echoed
        super().__init__(
            "request refused, it carries a denied identifier: "
            + ", ".join(f"{path} ({kind})" for path, kind in hits)
        )


@dataclass(frozen=True)
class Denylist:
    ids: frozenset[str]
    sha256: frozenset[str]

    def __post_init__(self):
        for i in self.ids:
            if not isinstance(i, str) or len(i) < MIN_ID_LENGTH:
                raise ValueError(
                    f"deny id must be a string of >= {MIN_ID_LENGTH} chars"
                )
        for s in self.sha256:
            if not (isinstance(s, str) and len(s) == 64 and set(s) <= _HEX):
                raise ValueError("deny sha256 must be 64 lowercase hex characters")

    @classmethod
    def build(cls, ids: Iterable[str] = (), sha256: Iterable[str] = ()) -> Denylist:
        return cls(frozenset(ids), frozenset(s.lower() for s in sha256))

    @classmethod
    def from_ledger_rows(cls, rows: Iterable[Mapping]) -> Denylist:
        """Every id and capture hash a confirm ledger (``confirm.read_rows``) holds."""
        ids, shas = set(), set()
        for row in rows:
            for e in row["episodes"]:
                ids.add(e["id"])
                shas.update(e["sha256"].values())
        return cls.build(ids, shas)


def _strings(node, path: str):
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, Mapping):
        for k, v in node.items():
            yield from _strings(str(k), f"{path}.<key>")
            yield from _strings(v, f"{path}.{k}")
    elif isinstance(node, (list, tuple)):
        for n, v in enumerate(node):
            yield from _strings(v, f"{path}[{n}]")


def _data_url_sha(text: str) -> str | None:
    if not text.startswith("data:") or ";base64," not in text:
        return None
    try:
        return hashlib.sha256(
            base64.b64decode(text.split(";base64,", 1)[1])
        ).hexdigest()
    except (binascii.Error, ValueError):
        return None


def scan(wire, deny: Denylist) -> list[tuple[str, str]]:
    """Every (json path, kind) where ``wire`` carries a denied id or sha256."""
    hits = []
    for path, text in _strings(wire, "$"):
        low = text.lower()
        if any(i.lower() in low for i in deny.ids):
            hits.append((path, "id"))
        if any(s in low for s in deny.sha256):
            hits.append((path, "sha256"))
        digests = {
            hashlib.sha256(text.encode("utf-8")).hexdigest(),
            _data_url_sha(text),
        }
        if digests & deny.sha256:
            hits.append((path, "content_sha256"))
    return hits


def check(wire, deny: Denylist) -> None:
    """Raise ``LeakFound`` unless ``wire`` is clean."""
    hits = scan(wire, deny)
    if hits:
        raise LeakFound(hits)


def main(argv: list[str] | None = None) -> int:
    """``python -m levi.decision.leakcheck --request wire.json --denylist list.json``:
    0 clean, ``LEAK_EXIT_CODE`` on a hit, 2 on unreadable input."""
    ap = argparse.ArgumentParser(prog="python -m levi.decision.leakcheck")
    ap.add_argument("--request", required=True, help="teacher-visible request, JSON")
    ap.add_argument(
        "--denylist", required=True, help='JSON {"ids": [...], "sha256": [...]}'
    )
    args = ap.parse_args(argv)
    try:
        wire = json.loads(Path(args.request).read_text())
        raw = json.loads(Path(args.denylist).read_text())
        deny = Denylist.build(raw.get("ids", ()), raw.get("sha256", ()))
        check(wire, deny)
    except LeakFound as e:
        print(f"leakcheck: {e}", file=sys.stderr)
        return e.exit_code
    except (OSError, ValueError, AttributeError) as e:
        print(f"leakcheck: unreadable input: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
