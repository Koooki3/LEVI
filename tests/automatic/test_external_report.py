"""Pure optional-report validation: no sockets, sessions or model calls."""

import hashlib
import json

import pytest

from levi.automatic import external_report as report

CONTENT = '{ "判定": true }\r\n'
REQUEST_ID = "request-test"


def valid(**extra):
    return {
        "schema": report.REPORT_SCHEMA,
        "request_id": REQUEST_ID,
        "answer_sha256": hashlib.sha256(CONTENT.encode("utf-8")).hexdigest(),
        **extra,
    }


def test_exact_utf8_binding_preserves_unknown_and_true_zero():
    value = report.normalize_report(
        valid(tokens={"input_new": 0, "output": 4}), REQUEST_ID, CONTENT
    )
    assert value["tokens"] == {
        "input_new": 0,
        "output": 4,
        "cache_read": None,
        "cache_write": None,
    }
    assert value["agent_id"] is None and value["elapsed_ms"] is None
    assert value["agent_notes"] is None
    for content in (CONTENT.strip(), json.dumps(json.loads(CONTENT))):
        with pytest.raises(report.ReportError, match="mismatch"):
            report.normalize_report(valid(), REQUEST_ID, content)


@pytest.mark.parametrize(
    "changes",
    [
        {"schema": "future"},
        {"request_id": "request-other"},
        {"answer_sha256": "0" * 64},
        {"agent_id": "../someone"},
        {"agent_id": "x" * 65},
        {"agent_notes": "图" * 1366},
        {"agent_notes": []},
        {"elapsed_ms": True},
        {"elapsed_ms": -1},
        {"elapsed_ms": 86400001},
        {"tokens": None},
        {"tokens": {"total": 10}},
        {"tokens": {"input_new": False}},
        {"tokens": {"output": 1.0}},
        {"tokens": {"cache_read": -1}},
        {"tokens": {"cache_write": 2**53}},
        {"tokens": {"output": "10"}},
        {"context": {"round_id": "pretend"}},
        {"operator_label": "success"},
        {"path": "/outside/report.json"},
        {"agent_notes": "\ud800"},
    ],
)
def test_invalid_self_reports_are_rejected(changes):
    with pytest.raises((report.ReportError, UnicodeError)):
        report.normalize_report(valid(**changes), REQUEST_ID, CONTENT)


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b"[]",
        b"\xff",
        b'{"schema":1,"schema":2}',
        b"NaN",
        b'{"x":1e400}',
        b"x" * (16 * 1024 + 1),
    ],
)
def test_bad_snapshot_becomes_unknown(raw):
    value = report.evidence(("candidate", raw), REQUEST_ID, CONTENT, 4)
    assert value["report_status"] == "invalid"
    assert value["report"] is None


def test_client_revalidates_binding_without_promoting_self_report():
    raw = json.dumps(
        valid(
            agent_id="worker-7",
            elapsed_ms=0,
            tokens={"cache_read": 0, "cache_write": 1},
        )
    ).encode()
    value = report.evidence(("candidate", raw), REQUEST_ID, CONTENT, 20)
    checked = report.verify_evidence(value, REQUEST_ID, CONTENT)
    assert checked["binding_verified"] is True
    assert checked["report_source"] == "external_self_reported"
    assert checked["bridge_wait_ms"] == 20
    assert checked["report"]["elapsed_ms"] == 0
    assert checked["report"]["tokens"]["input_new"] is None
    assert report.verify_evidence(value, "request-other", CONTENT) is None
    assert report.verify_evidence(value, REQUEST_ID, CONTENT.strip()) is None
    assert (
        report.verify_evidence({**value, "bridge_wait_ms": True}, REQUEST_ID, CONTENT)
        is None
    )
    assert (
        report.verify_evidence(
            {**value, "report_source": "authenticated"}, REQUEST_ID, CONTENT
        )
        is None
    )
    assert report.verify_evidence({**value, "extra": "x"}, REQUEST_ID, CONTENT) is None


def test_no_report_is_not_zero_usage_and_missing_extension_stays_none():
    value = report.evidence(("unavailable", None), REQUEST_ID, CONTENT, 0)
    checked = report.verify_evidence(value, REQUEST_ID, CONTENT)
    assert checked["report"] is None and checked["binding_verified"] is False
    assert report.verify_evidence(None, REQUEST_ID, CONTENT) is None


def test_text_snapshot_hashes_full_content_before_truncation():
    text = "图" * 20001
    snapshot = report.text_snapshot(text)
    assert snapshot["text"] == text[:20000]
    assert snapshot["bytes"] == len(text.encode("utf-8"))
    assert snapshot["sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert snapshot["truncated"] is True
    assert snapshot["sha256"] != hashlib.sha256(snapshot["text"].encode()).hexdigest()
