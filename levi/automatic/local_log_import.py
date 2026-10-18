"""Import an explicitly staged, redacted agent export; never discover sessions.

This is our versioned interchange format, not a Codex/Claude transcript parser.
Output is optional self-reported evidence, never identity or billing proof.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path

from . import external_report as report

EXPORT_SCHEMA = "levi.aeri.agent_log_export.v1"
MAX_EXPORT_BYTES = 20 * 1024
MAX_ANSWER_BYTES = 1024 * 1024
MAX_DEPTH = 16
SOURCES = frozenset({"codex", "claude", "other", "synthetic"})


class ImportRefused(ValueError):
    """Stable error code only: never expose imported text or filesystem paths."""


def _json(raw):
    try:
        value = report.strict_json(raw.decode("utf-8"))
        pending = [(value, 0)]
        while pending:
            item, depth = pending.pop()
            if depth > MAX_DEPTH:
                raise ImportRefused("invalid_json")
            if isinstance(item, dict):
                pending.extend((v, depth + 1) for v in item.values())
            elif isinstance(item, list):
                pending.extend((v, depth + 1) for v in item)
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ImportRefused("invalid_json") from exc


def parse_export(raw: bytes, answer: bytes, request_id: str) -> dict:
    """Bind one normalized export to independently supplied id and raw answer.

    Answer bytes are the bridge's answer.json assistant content, not a chat
    completion envelope. Business answer validation remains with the provider.
    """
    if len(raw) > MAX_EXPORT_BYTES or len(answer) > MAX_ANSWER_BYTES:
        raise ImportRefused("input_too_large")
    value = _json(raw)
    content = _json(answer)
    if not isinstance(content, dict) or set(content) & {"choices", "usage"}:
        raise ImportRefused("raw_answer_required")
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "source", "redacted", "report"}
        or value["schema"] != EXPORT_SCHEMA
        or not isinstance(value["source"], str)
        or value["source"] not in SOURCES
        or value["redacted"] is not True
    ):
        raise ImportRefused("invalid_export")
    try:
        normalized = report.normalize_report(
            value["report"], request_id, answer.decode("utf-8")
        )
        if len(serialize_report(normalized)) > report.MAX_REPORT_BYTES:
            raise ImportRefused("invalid_report")
        return normalized
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ImportRefused(
            "report_mismatch"
            if getattr(exc, "status", None) == "mismatch"
            else "invalid_report"
        ) from exc


def serialize_report(value):
    """The exact stdout bytes, including the publication newline."""
    return (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode(
        "utf-8"
    )


ALLOWED_DOT_DIRS = frozenset({".state"})


def _directory(path):
    # Walk each directory with O_NOFOLLOW: checking only the final filename
    # would still permit an intermediate symlink into a private session tree.
    path = Path(path)
    # Hidden directories are refused (private agent sessions such as ~/.claude or
    # ~/.codex live there) except LEVI's own workspace folder ``.state``: the
    # default LEVI_WORKSPACE and the maintainers' pytest basetemp are under it
    # (L2-AERI-IMPORT-DOTDIR, 2026-10-18).
    if not path.is_absolute() or any(
        p.startswith(".") and p not in ALLOWED_DOT_DIRS for p in path.parts[1:]
    ):
        raise ImportRefused("export_directory_required")
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read(directory, name, maximum):
    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_size > maximum
        ):
            raise ImportRefused("bounded_owned_export_required")
        raw = handle.read(maximum + 1)
    if len(raw) > maximum:
        raise ImportRefused("input_too_large")
    return raw


def import_directory(path, request_id):
    """Read exactly two fixed files. Never write the staging directory or queue."""
    try:
        directory = _directory(path)
        try:
            raw = _read(directory, "agent-export.json", MAX_EXPORT_BYTES)
            answer = _read(directory, "answer.json", MAX_ANSWER_BYTES)
        finally:
            os.close(directory)
        return parse_export(raw, answer, request_id)
    except ImportRefused:
        raise
    except (OSError, ValueError) as exc:
        raise ImportRefused("unreadable_export") from exc


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes bad arguments, which could contain secrets.
        self.exit(2, "import refused: invalid_arguments\n")


def main(argv=None):
    parser = _Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--export-dir", required=True, help="absolute owned redacted staging directory"
    )
    parser.add_argument(
        "--request-id", required=True, help="independently recorded bridge request ID"
    )
    args = parser.parse_args(argv)
    try:
        value = import_directory(args.export_dir, args.request_id)
    except ImportRefused as exc:
        print(f"import refused: {exc}", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(serialize_report(value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
