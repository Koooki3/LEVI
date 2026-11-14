"""Leak gate before an escalation request leaves, and the confirm-once claim (U8, U12)."""

import base64
import hashlib
import json

import pytest

from levi.decision import confirm, leakcheck
from levi.decision.contracts import EscalationRequest
from levi.decision.queue import to_wire
from levi.events import causal_eval

FRAG = "ep_20260101_frag7"
FRAG_SHA = hashlib.sha256(b"frame-bytes-of-a-confirm-episode").hexdigest()
DENY = leakcheck.Denylist.build([FRAG], [FRAG_SHA])


def req(**kw):
    base = {
        "request_id": "r9f3a1c2b7d4e",
        "question_kind": "boundary",
        "spec_id": "spec-boundary-v1",
        "messages": ({"role": "user", "content": "Is the gripper closed?"},),
        "response_schema": {"type": "object"},
    }
    base.update(kw)
    return EscalationRequest(**base)


def test_clean_request_passes():
    leakcheck.check(to_wire(req()), DENY)


def test_fragment_id_in_any_teacher_visible_place_is_refused():
    for kw in (
        {"request_id": f"x{FRAG}"},  # an id derived from the fragment
        {
            "messages": ({"role": "user", "content": f"see {FRAG.upper()}"},)
        },  # case-insensitive
        {"signals": {"note": FRAG}},
        {"spec_id": FRAG},
        {"signals": {FRAG: 1}},  # a key
    ):
        with pytest.raises(leakcheck.LeakFound) as e:
            leakcheck.check(to_wire(req(**kw)), DENY)
        assert e.value.exit_code == leakcheck.LEAK_EXIT_CODE
        assert FRAG not in str(e.value) and FRAG.upper() not in str(e.value)


def test_sha256_in_text_and_content_hash_of_a_data_url_are_refused():
    body = b"frame-bytes-of-a-confirm-episode"
    url = "data:image/png;base64," + base64.b64encode(body).decode()
    for content in (f"hash {FRAG_SHA.upper()}", FRAG_SHA, url):
        with pytest.raises(leakcheck.LeakFound):
            leakcheck.check(
                to_wire(req(messages=({"role": "user", "content": content},))), DENY
            )
    other = "data:image/png;base64," + base64.b64encode(b"another image").decode()
    leakcheck.check(to_wire(req(messages=({"role": "user", "content": other},))), DENY)


def test_a_deny_list_that_would_match_everything_is_refused():
    with pytest.raises(ValueError):
        leakcheck.Denylist.build([""])
    with pytest.raises(ValueError):
        leakcheck.Denylist.build(["ab"])
    with pytest.raises(ValueError):
        leakcheck.Denylist.build([], ["abc"])


def test_exit_codes_are_distinct():
    from levi.decision.calibration import ConfirmDataError

    codes = {
        leakcheck.LEAK_EXIT_CODE,
        confirm.CONFIRM_REUSED_EXIT_CODE,
        ConfirmDataError.exit_code,
    }
    assert len(codes) == 3


def test_cli_returns_the_leak_code(tmp_path):
    deny = tmp_path / "deny.json"
    deny.write_text(json.dumps({"ids": [FRAG], "sha256": [FRAG_SHA]}))
    clean, dirty = tmp_path / "c.json", tmp_path / "d.json"
    clean.write_text(json.dumps(to_wire(req())))
    dirty.write_text(json.dumps(to_wire(req(signals={"x": FRAG}))))
    run = lambda r: leakcheck.main(["--request", str(r), "--denylist", str(deny)])
    assert run(clean) == 0
    assert run(dirty) == leakcheck.LEAK_EXIT_CODE
    assert run(tmp_path / "missing.json") == 2


def entry(*ids, shas=None):
    return {
        "schema": causal_eval.LEDGER_SCHEMA,
        "at": "2026-11-12T00:00:00+00:00",
        "episodes": [
            {
                "id": i,
                "source": "s",
                "sha256": {"video": (shas or {}).get(i, "0" * 63 + str(n))},
            }
            for n, i in enumerate(ids)
        ],
    }


def test_denylist_comes_from_the_confirm_ledger_rows():
    d = leakcheck.Denylist.from_ledger_rows(
        [entry("epi_aaaa", shas={"epi_aaaa": FRAG_SHA})]
    )
    assert "epi_aaaa" in d.ids and FRAG_SHA in d.sha256


# U8: confirm is opened once
def test_claim_is_pure_and_refuses_a_second_confirm_of_the_same_episode():
    rows = confirm.claim([], entry("epi_aaaa", "epi_bbbb"))
    assert len(rows) == 1
    with pytest.raises(confirm.ConfirmReused) as e:
        confirm.claim(rows, entry("epi_cccc", "epi_bbbb"))
    assert e.value.exit_code == confirm.CONFIRM_REUSED_EXIT_CODE
    # the same capture under another id is the same episode
    with pytest.raises(confirm.ConfirmReused):
        confirm.claim(rows, entry("epi_dddd", shas={"epi_dddd": "0" * 63 + "0"}))
    assert len(confirm.claim(rows, entry("epi_eeee", shas={"epi_eeee": "1" * 64}))) == 2


def test_claim_ledger_appends_once_and_agrees_with_causal_eval(tmp_path):
    path = tmp_path / "confirm.jsonl"
    confirm.claim_ledger(path, entry("epi_aaaa"))
    with pytest.raises(confirm.ConfirmReused):
        confirm.claim_ledger(path, entry("epi_aaaa"))
    assert len(path.read_text().splitlines()) == 1
    # same line format and same semantics as the causal ledger, in both directions
    with pytest.raises(SystemExit):
        causal_eval.claim_ledger(path, entry("epi_aaaa"))
    other = tmp_path / "other.jsonl"
    causal_eval.claim_ledger(other, entry("epi_bbbb"))
    with pytest.raises(confirm.ConfirmReused):
        confirm.claim_ledger(other, entry("epi_bbbb"))


def test_a_line_that_is_not_a_confirm_record_refuses(tmp_path):
    path = tmp_path / "confirm.jsonl"
    path.write_text('{"schema": "other"}\n')
    with pytest.raises(confirm.LedgerCorrupt):
        confirm.read_rows(path)
    with pytest.raises(confirm.LedgerCorrupt):
        confirm.claim([{"schema": "other"}], entry("epi_aaaa"))


# --- a refused id never reaches a message (LEAKCHECK-KEY-ECHO-1115) ---------------


def everything_said(wire, deny=DENY):
    """The exception text, its repr, its hits, and the CLI's stderr and stdout."""
    with pytest.raises(leakcheck.LeakFound) as raised:
        leakcheck.check(wire, deny)
    e = raised.value
    return " ".join([str(e), repr(e), repr(e.hits), repr(e.args)])


def test_a_refused_id_used_as_a_key_with_a_second_hit_below_it_is_not_echoed():
    wire = {
        "signals": {
            FRAG: {"note": f"also {FRAG}", "deeper": [{FRAG.upper(): FRAG_SHA}]},
            "plain": "x",
        }
    }
    said = everything_said(wire)
    for secret in (FRAG, FRAG.upper(), FRAG_SHA):
        assert secret.lower() not in said.lower()
    # the path still says where: ordinals of keys, indexes of lists
    hits = leakcheck.scan(wire, DENY)
    assert ("$.#0.<key #0>", "id") in hits
    assert ("$.#0.#0.#0", "id") in hits
    assert ("$.#0.#0.#1[0].#0", "sha256") in hits


def test_a_refused_id_as_a_key_at_every_depth_never_appears_in_a_path():
    wire = {"a": {"b": [{"c": {FRAG: [FRAG]}}]}, FRAG: {FRAG: FRAG}}
    for path, _ in leakcheck.scan(wire, DENY):
        assert FRAG.lower() not in path.lower()
    assert FRAG.lower() not in everything_said(wire).lower()


def test_the_cli_stderr_does_not_echo_a_refused_id_used_as_a_key(tmp_path, capsys):
    deny = tmp_path / "deny.json"
    deny.write_text(json.dumps({"ids": [FRAG], "sha256": [FRAG_SHA]}))
    request = tmp_path / "r.json"
    request.write_text(json.dumps({"signals": {FRAG: {"x": FRAG}}}))
    code = leakcheck.main(["--request", str(request), "--denylist", str(deny)])
    seen = capsys.readouterr()
    assert code == leakcheck.LEAK_EXIT_CODE
    assert FRAG.lower() not in (seen.err + seen.out).lower()
    assert "leakcheck:" in seen.err


def test_a_key_that_is_not_a_string_is_still_reported_by_ordinal():
    wire = {3: "see " + FRAG, "k": "ok"}
    assert leakcheck.scan(wire, DENY) == [("$.#0", "id")]


# --- claim ledger: fsync, fresh line, cut-short line (DECISION-APPEND-GAPS-1115) ----


def test_claim_ledger_fsyncs_the_line_before_it_returns(tmp_path, monkeypatch):
    import os

    path = tmp_path / "confirm.jsonl"
    seen = []
    real = os.fsync

    def spy(fd):
        seen.append(os.fstat(fd).st_size)  # what is on the file when fsync is asked
        return real(fd)

    monkeypatch.setattr(confirm.os, "fsync", spy)
    confirm.claim_ledger(path, entry("epi_aaaa"))
    assert seen and seen[-1] == path.stat().st_size > 0


def test_claim_after_a_row_that_lost_only_its_newline_starts_on_a_fresh_line(tmp_path):
    path = tmp_path / "confirm.jsonl"
    path.write_text(json.dumps(entry("epi_aaaa"), sort_keys=True))
    second = entry("epi_bbbb", shas={"epi_bbbb": "1" * 64})
    confirm.claim_ledger(path, second)
    text = path.read_text()
    assert text.endswith("\n") and len(text.splitlines()) == 2
    assert [e["id"] for r in confirm.read_rows(path) for e in r["episodes"]] == [
        "epi_aaaa",
        "epi_bbbb",
    ]
    with pytest.raises(confirm.ConfirmReused):
        confirm.claim_ledger(path, second)


def test_a_cut_short_last_line_is_an_explicit_error_in_the_claim_ledger(tmp_path):
    path = tmp_path / "confirm.jsonl"
    whole = json.dumps(entry("epi_aaaa"), sort_keys=True)
    path.write_text(whole + "\n" + whole[:20])
    with pytest.raises(confirm.LedgerCorrupt, match=r"line 2 .*cut short"):
        confirm.read_rows(path)
    before = path.read_bytes()
    with pytest.raises(confirm.LedgerCorrupt, match=r"line 2 .*cut short"):
        confirm.claim_ledger(path, entry("epi_bbbb"))
    assert path.read_bytes() == before
