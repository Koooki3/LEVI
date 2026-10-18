"""Bounded, optional external self-reports. No identity or billing authentication.

This module parses evidence only. Its failures must never decide whether an AERI
answer is accepted, cause an action, or supply an operator label.
"""

from __future__ import annotations

import hashlib
import json
import re

REPORT_SCHEMA = "levi.aeri.external_report.v1"
EVIDENCE_SCHEMA = "levi.aeri.external_evidence.v1"
MAX_REPORT_BYTES = 16 * 1024
MAX_NOTES_BYTES = 4096
MAX_TEXT_CHARS = 20000
MAX_ELAPSED_MS = 86400000
MAX_TOKEN_COUNT = 2**53 - 1
TOKEN_FIELDS = ("input_new", "output", "cache_read", "cache_write")
STATUSES = frozenset(
    {"reported", "unavailable", "invalid", "mismatch", "unsupported_version"}
)


class ReportError(ValueError):
    def __init__(self, status="invalid"):
        super().__init__(status)
        self.status = status


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ReportError()
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=pairs)
        json.dumps(value, allow_nan=False)
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ReportError() from exc


def text_snapshot(text):
    """Hash complete UTF-8 content before keeping a bounded display prefix."""
    empty = {"text": None, "sha256": None, "bytes": None, "truncated": False}
    if not isinstance(text, str):
        return empty
    try:
        raw = text.encode("utf-8")
    except UnicodeError:
        return empty
    return {
        "text": text[:MAX_TEXT_CHARS],
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "truncated": len(text) > MAX_TEXT_CHARS,
    }


def _integer(value, maximum):
    if value is not None and (type(value) is not int or not 0 <= value <= maximum):
        raise ReportError()
    return value


def _bounded_json(value, maximum):
    try:
        if (
            len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            > maximum
        ):
            raise ReportError()
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ReportError() from exc


def normalize_report(value, request_id, content):
    """Bind an untrusted report to the full, unmodified assistant content."""
    _bounded_json(value, MAX_REPORT_BYTES)
    required = {"schema", "request_id", "answer_sha256"}
    allowed = required | {"agent_id", "agent_notes", "elapsed_ms", "tokens"}
    if not isinstance(value, dict) or not required <= value.keys() <= allowed:
        raise ReportError()
    if value["schema"] != REPORT_SCHEMA:
        raise ReportError("unsupported_version")
    if not isinstance(request_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,128}", request_id
    ):
        raise ReportError("mismatch")
    digest = text_snapshot(content)["sha256"]
    if (
        digest is None
        or value["request_id"] != request_id
        or value["answer_sha256"] != digest
    ):
        raise ReportError("mismatch")
    agent_id = value.get("agent_id")
    if agent_id is not None and (
        not isinstance(agent_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", agent_id)
    ):
        raise ReportError()
    notes = value.get("agent_notes")
    if notes is not None and (
        not isinstance(notes, str) or len(notes.encode("utf-8")) > MAX_NOTES_BYTES
    ):
        raise ReportError()
    tokens = value.get("tokens", {})
    if not isinstance(tokens, dict) or not tokens.keys() <= set(TOKEN_FIELDS):
        raise ReportError()
    return {
        "schema": REPORT_SCHEMA,
        "request_id": request_id,
        "answer_sha256": digest,
        "agent_id": agent_id,
        "agent_notes": notes,
        "elapsed_ms": _integer(value.get("elapsed_ms"), MAX_ELAPSED_MS),
        "tokens": {
            key: _integer(tokens.get(key), MAX_TOKEN_COUNT) for key in TOKEN_FIELDS
        },
    }


def evidence(snapshot, request_id, content, wait_ms):
    """Build bridge metadata from a frozen (status, raw-bytes) report snapshot."""
    status, raw = snapshot
    report = None
    if status == "candidate":
        try:
            if len(raw) > MAX_REPORT_BYTES:
                raise ReportError()
            report = normalize_report(
                strict_json(raw.decode("utf-8")), request_id, content
            )
            status = "reported"
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            status = exc.status if isinstance(exc, ReportError) else "invalid"
    return {
        "schema": EVIDENCE_SCHEMA,
        "bridge_wait_ms": wait_ms,
        "report_status": status,
        "report_source": "external_self_reported",
        "report": report,
    }


def verify_evidence(value, request_id, content):
    """Client boundary: invalid extensions disappear, never replace the answer.

    binding_verified only attests to matching id/content, never who authored it,
    whether costs are billable, or whether the answer is semantically correct.
    """
    if value is None:
        return None
    try:
        _bounded_json(value, MAX_REPORT_BYTES + 1024)
        keys = {"schema", "bridge_wait_ms", "report_status", "report_source", "report"}
        if not isinstance(value, dict) or set(value) != keys:
            raise ReportError()
        if (
            value["schema"] != EVIDENCE_SCHEMA
            or value["report_source"] != "external_self_reported"
        ):
            raise ReportError()
        status = value["report_status"]
        if not isinstance(status, str) or status not in STATUSES:
            raise ReportError()
        wait = _integer(value["bridge_wait_ms"], MAX_ELAPSED_MS)
        if wait is None:
            raise ReportError()
        if status == "reported":
            report = normalize_report(value["report"], request_id, content)
        elif value["report"] is not None:
            raise ReportError()
        else:
            report = None
        return {
            "schema": EVIDENCE_SCHEMA,
            "bridge_wait_ms": wait,
            "report_status": status,
            "report_source": "external_self_reported",
            "report": report,
            "binding_verified": status == "reported",
        }
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None
