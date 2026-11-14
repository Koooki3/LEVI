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


@pytest.mark.parametrize("field", ["role", "source_code"])
@pytest.mark.parametrize(
    "value", ["x" * 65, "has space", "line\nbreak", "tok=sk-abc/def", "", "é"]
)
def test_role_and_source_code_are_label_shaped_or_refused(field, value):
    with pytest.raises(ledger.LedgerError, match=field):
        if field == "role":
            row(role=value)
        else:
            row(source_code=value)


@pytest.mark.parametrize(
    "value", ["claude_jsonl.last_usage:exact", "reviewer_a", "x" * 64, "a-b.c:d_1"]
)
def test_normal_role_and_source_code_still_pass(value):
    r = row(role=value, source_code=value)
    assert r["role"] == value and r["source_code"] == value
    assert row(source_code=None)["source_code"] is None


def test_a_stored_row_with_a_bad_role_or_source_code_refuses_on_read(tmp_path):
    good = row(source_code="claude_jsonl.last_usage:exact")
    for key in ("role", "source_code"):
        bad = {**good, key: "free text with spaces " + "x" * 80}
        path = tmp_path / f"{key}.jsonl"
        path.write_text(json.dumps(bad) + "\n")
        with pytest.raises(ledger.LedgerError, match="line 1"):
            ledger.read_rows(path)
    ok = tmp_path / "ok.jsonl"
    ok.write_text(json.dumps(good) + "\n")
    assert ledger.read_rows(ok) == [good]


# --- append gaps (DECISION-APPEND-GAPS-1115) ---------------------------------------


def _damage(r, how):
    r = dict(r)
    if how == "exact_with_unknown":
        r["output"] = None  # exact stays true, fresh stays a number
    elif how == "schema":
        r["schema"] = "other.v1"
    elif how == "role":
        r["role"] = "free text with spaces"
    elif how == "missing_key":
        del r["attempt"]
    elif how == "attempt_text":
        r["attempt"] = "1"
    elif how == "attempt_zero":
        r["attempt"] = 0
    elif how == "attempt_unhashable":
        r["attempt"] = [1]
    elif how == "short_sha":
        r["request_sha256"] = "abc"
    elif how == "nan":
        r["at"] = float("nan")
    elif how == "task":
        r["task"] = "not a label!"
    return r


DAMAGE = [
    "exact_with_unknown",
    "schema",
    "role",
    "missing_key",
    "attempt_text",
    "attempt_zero",
    "attempt_unhashable",
    "short_sha",
    "nan",
    "task",
]


@pytest.mark.parametrize("how", DAMAGE)
def test_append_refuses_a_row_that_would_not_read_back_and_writes_nothing(
    tmp_path, how
):
    p = tmp_path / "l.jsonl"
    good = row()
    ledger.append_rows(p, [good])
    before = p.read_bytes()
    bad = _damage(row(role="reviewer_b"), how)
    # a good row in the same call is not written either: all or nothing
    with pytest.raises(ledger.LedgerError, match="row 1 to append"):
        ledger.append_rows(p, [row(role="reviewer_c"), bad])
    assert p.read_bytes() == before
    assert ledger.read_rows(p) == [good]  # the file is still readable


def test_append_refuses_a_bad_first_row_without_creating_the_file(tmp_path):
    p = tmp_path / "sub" / "l.jsonl"
    with pytest.raises(ledger.LedgerError):
        ledger.append_rows(p, [_damage(row(), "schema")])
    assert not p.exists() and not p.parent.exists()


def test_append_after_a_row_that_lost_only_its_newline_starts_on_a_fresh_line(tmp_path):
    p = tmp_path / "l.jsonl"
    first = row(role="reviewer_a")
    p.write_text(json.dumps(first, sort_keys=True))  # a crash before the "\n"
    assert ledger.read_rows(p) == [first]  # a whole row is a row
    second = row(role="reviewer_b")
    assert ledger.append_rows(p, [second]) == 1
    text = p.read_text()
    assert text.endswith("\n") and len(text.splitlines()) == 2
    assert ledger.read_rows(p) == [first, second]
    # and a file that already ends in a newline gets no extra blank line
    ledger.append_rows(p, [row(role="reviewer_c")])
    assert "\n\n" not in p.read_text()


def test_a_cut_short_last_line_is_an_explicit_error_on_read_and_on_append(tmp_path):
    p = tmp_path / "l.jsonl"
    whole = json.dumps(row(), sort_keys=True)
    p.write_text(whole + "\n" + whole[: len(whole) // 2])  # killed mid-write
    with pytest.raises(ledger.LedgerError, match=r"line 2 .*cut short"):
        ledger.read_rows(p)
    before = p.read_bytes()
    with pytest.raises(ledger.LedgerError, match=r"line 2 .*cut short"):
        ledger.append_rows(p, [row(role="reviewer_b")])
    assert p.read_bytes() == before  # nothing was glued on
    # the same damage in the middle of the file is the plain "not a ledger row"
    p.write_text(whole[: len(whole) // 2] + "\n" + whole + "\n")
    with pytest.raises(ledger.LedgerError, match=r"line 1 of .*is not a ledger row$"):
        ledger.read_rows(p)
