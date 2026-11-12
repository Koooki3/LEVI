"""External-token ledger: two bases, unknown is null, confirm only as aggregate (U13-adjacent, U14)."""

import json

import pytest

from levi.decision import ledger
from levi.decision.contracts import TokenRow
from levi.eval import token_monitor, tokens

SHA = "a" * 64


def row(
    split="tune",
    task="boundary",
    role="reviewer_a",
    i=1,
    o=2,
    cr=10,
    cw=100,
    exact=True,
    sha=SHA,
    **kw,
):
    return ledger.make_row(
        sha,
        task,
        split,
        TokenRow(role, i, o, cr, cw, exact),
        at="2026-11-12T00:00:00+00:00",
        **kw,
    )


def test_fresh_and_with_cache_are_the_monitors_two_bases():
    r = row()
    assert r["fresh"] == 1 + 2 + 100 and r["with_cache"] == 1 + 2 + 10 + 100
    assert (
        ledger.FRESH_FIELDS
        == token_monitor.FRESH_FIELDS
        == ("input_new", "output", "cache_write")
    )


def test_ledger_row_agrees_with_the_monitor_on_a_real_transcript_shape(tmp_path):
    f = token_monitor._write(
        tmp_path / "a.jsonl",
        [
            token_monitor._line("a", 5),
            token_monitor._line("a", 50, at="2026-11-06T09:00:05Z"),
            token_monitor._line("b", 7, at="2026-11-06T09:10:00Z"),
        ],
    )
    usage = tokens.usage_from_files([f])
    mon = token_monitor.monitor({"t1": [f]})["tasks"]["t1"]
    t = usage.tokens
    r = ledger.make_row(
        SHA,
        "t1",
        "tune",
        TokenRow(
            "reviewer_a",
            t["input_new"],
            t["output"],
            t["cache_read"],
            t["cache_write"],
            usage.exact,
        ),
    )
    assert (
        (r["fresh"], r["with_cache"], r["exact"])
        == (mon["fresh"], mon["with_cache"], mon["exact"])
        == (259, 279, True)
    )


# U14: unknown is null, never 0, and never exact
def test_unknown_field_is_null_and_not_exact():
    r = ledger.make_row(
        SHA, "t", "tune", TokenRow("reviewer_a", 1, None, 10, 100, True)
    )
    assert r["output"] is None and r["fresh"] is None and r["with_cache"] is None
    assert r["exact"] is False
    zero = row(i=0, o=0, cr=0, cw=0)  # a true zero stays 0
    assert zero["fresh"] == 0 and zero["exact"] is True


def test_a_missing_panel_member_makes_the_total_a_lower_bound_not_smaller_evidence():
    rows = [
        row(role="reviewer_a"),
        ledger.make_row(
            SHA,
            "boundary",
            "tune",
            TokenRow("reviewer_b", None, None, None, None, False),
        ),
    ]
    agg = ledger.report(rows)["by_split"]["tune"]["all"]
    assert (
        agg["exact"] is False and agg["tokens"]["output"] == 2
    )  # the known part, flagged inexact
    assert agg["fresh"] == 103  # sums known values like the monitor
    only_unknown = ledger.report([rows[1]])["by_split"]["tune"]["all"]
    assert only_unknown["tokens"]["output"] is None and only_unknown["fresh"] is None


def test_row_validation():
    with pytest.raises(ledger.LedgerError):
        ledger.make_row("short", "t", "tune", TokenRow("r", 1, 1, 1, 1, True))
    with pytest.raises(ledger.LedgerError):
        row(task="has space")
    with pytest.raises(ledger.LedgerError):
        row(split="gold")
    with pytest.raises(ledger.LedgerError):
        row(purpose="whatever")
    with pytest.raises(ValueError):
        TokenRow("r", -1, 0, 0, 0, True)


def test_append_is_idempotent_per_request_role_attempt_and_replay_reads_back(tmp_path):
    p = tmp_path / "external_ledger.jsonl"
    rows = [
        row(role="reviewer_a"),
        row(role="reviewer_b"),
        row(role="reviewer_a", attempt=2),
    ]
    assert ledger.append_rows(p, rows) == 3
    assert ledger.append_rows(p, rows) == 0  # a replayed request adds nothing
    assert ledger.read_rows(p) == rows
    assert ledger.append_rows(p, [row(sha="b" * 64)]) == 1


def test_a_hand_edited_row_that_breaks_the_unknown_rule_refuses(tmp_path):
    p = tmp_path / "l.jsonl"
    bad = row()
    bad["output"] = None  # exact stays true, fresh stays a number
    p.write_text(json.dumps(bad) + "\n")
    with pytest.raises(ledger.LedgerError):
        ledger.read_rows(p)
    zeroed = ledger.make_row(SHA, "t", "tune", TokenRow("r", 1, None, 1, 1, True))
    zeroed["output"] = 0  # unknown rewritten as 0 no longer matches fresh = null
    p.write_text(json.dumps(zeroed) + "\n")
    with pytest.raises(ledger.LedgerError):
        ledger.read_rows(p)


# confirm: aggregate only
def test_confirm_rows_appear_only_as_aggregates():
    rows = [
        row(split="tune", sha="1" * 64),
        row(split="confirm", sha="2" * 64, role="a"),
        row(split="confirm", sha="3" * 64, role="b"),
        row(split="heldout", sha="4" * 64),
    ]
    rep = ledger.report(rows)
    shown = {p["request_sha256"] for p in rep["per_request"]}
    assert shown == {"1" * 64}
    text = json.dumps(rep)
    for hidden in ("2" * 64, "3" * 64, "4" * 64):
        assert hidden not in text
    conf = rep["by_split"]["confirm"]["all"]
    assert (conf["rows"], conf["requests"], conf["fresh"]) == (2, 2, 2 * 103)
    assert rep["by_split"]["confirm"]["tasks"]["boundary"]["fresh"] == 2 * 103
    assert rep["total"]["rows"] == 4
