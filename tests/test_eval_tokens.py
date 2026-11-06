"""External-agent token usage from session transcripts (levi/eval/tokens.py).

All transcripts here are built by the tests; nothing reads a real session.
"""

import glob
import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from levi.automatic import external_report
from levi.eval import tokens

T0 = "2026-11-06T09:00:00Z"


def epoch(text):
    return datetime.fromisoformat(text).timestamp()


def row(
    mid,
    output,
    *,
    at=T0,
    input_tokens=3,
    cache_read=100,
    cache_write=10,
    kind="assistant",
    uuid=None,
    model="claude-x",
    usage=None,
):
    """One transcript line: an assistant message's usage record."""
    message = {
        "model": model,
        "usage": usage
        if usage is not None
        else {
            "input_tokens": input_tokens,
            "output_tokens": output,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_write,
        },
    }
    if mid is not None:
        message["id"] = mid
    out = {"type": kind, "timestamp": at, "message": message}
    if uuid is not None:
        out["uuid"] = uuid
    return out


def transcript(path, *rows):
    """Write dicts as JSON lines and strings as they are."""
    path.write_text(
        "\n".join(r if isinstance(r, str) else json.dumps(r) for r in rows) + "\n"
    )
    return path


def test_the_names_are_the_ones_of_the_external_report():
    assert tokens.FIELDS == external_report.TOKEN_FIELDS


def test_the_last_usage_record_of_a_message_wins(tmp_path):
    # One message written as three lines whose output count grows: the first
    # line is a fraction of the final one (measured: 16 against 415).
    f = transcript(
        tmp_path / "s.jsonl",
        row("msg_a", 16),
        row("msg_a", 200),
        row("msg_a", 415),
    )
    usage = tokens.usage_from_files([f])
    assert usage.messages == 1
    assert usage.tokens["output"] == 415


def test_lines_of_different_messages_may_interleave(tmp_path):
    f = transcript(
        tmp_path / "s.jsonl",
        row("msg_a", 10),
        row("msg_b", 20),
        row("msg_a", 30),
        row("msg_c", 5),
        row("msg_b", 40),
    )
    usage = tokens.usage_from_files([f])
    assert usage.messages == 3
    assert usage.tokens["output"] == 30 + 40 + 5


def test_last_means_last_written_not_the_largest(tmp_path):
    f = transcript(tmp_path / "s.jsonl", row("msg_a", 415), row("msg_a", 16))
    assert tokens.usage_from_files([f]).tokens["output"] == 16


def test_the_four_fields_are_reported_apart_under_the_report_names(tmp_path):
    f = transcript(
        tmp_path / "s.jsonl",
        row("msg_a", 7, input_tokens=1, cache_read=20, cache_write=300),
        row("msg_b", 8, input_tokens=2, cache_read=40, cache_write=500),
    )
    assert tokens.usage_from_files([f]).tokens == {
        "input_new": 3,
        "output": 15,
        "cache_read": 60,
        "cache_write": 800,
    }


def test_a_message_repeated_in_a_later_file_is_counted_once_and_the_later_copy_wins(
    tmp_path,
):
    first = transcript(tmp_path / "1.jsonl", row("msg_a", 100), row("msg_b", 1))
    second = transcript(tmp_path / "2.jsonl", row("msg_a", 150), row("msg_c", 2))
    usage = tokens.usage_from_files([first, second])
    assert usage.messages == 3
    assert usage.tokens["output"] == 150 + 1 + 2
    assert usage.files == 2


def test_a_line_without_message_id_falls_back_to_its_uuid(tmp_path):
    f = transcript(
        tmp_path / "s.jsonl",
        row(None, 5, uuid="u1"),
        row(None, 6, uuid="u2"),
        row(None, 9, uuid="u2"),
    )
    usage = tokens.usage_from_files([f])
    assert (usage.messages, usage.tokens["output"]) == (2, 5 + 9)


def test_the_window_is_inclusive_and_counts_what_it_leaves_out(tmp_path):
    f = transcript(
        tmp_path / "s.jsonl",
        row("early", 1, at="2026-11-06T08:59:59Z"),
        row("start", 2, at="2026-11-06T09:00:00Z"),
        row("end", 4, at="2026-11-06T10:00:00Z"),
        row("late", 8, at="2026-11-06T10:00:01Z"),
    )
    usage = tokens.usage_from_files(
        [f], since="2026-11-06T09:00:00Z", until=epoch("2026-11-06T10:00:00Z")
    )
    assert usage.tokens["output"] == 2 + 4
    assert (usage.messages, usage.outside_window) == (2, 2)


def test_a_message_is_placed_by_its_first_line_and_keeps_its_final_usage(tmp_path):
    # The first line is inside the window, the last one after its end: the
    # message is one API response and is counted whole, not cut at the edge.
    f = transcript(
        tmp_path / "s.jsonl",
        row("msg_a", 16, at="2026-11-06T09:59:59Z"),
        row("msg_a", 415, at="2026-11-06T10:00:03Z"),
        row("msg_b", 50, at="2026-11-06T10:00:02Z"),
    )
    usage = tokens.usage_from_files([f], until="2026-11-06T10:00:00Z")
    assert usage.tokens["output"] == 415
    assert usage.outside_window == 1


def test_a_message_without_a_valid_time_is_reported_when_a_window_is_set(tmp_path):
    f = transcript(
        tmp_path / "s.jsonl",
        row("msg_a", 5, at=None),
        row("msg_b", 6, at="2026-11-06 09:00:00"),  # no zone: not trusted
        row("msg_c", 7),
    )
    windowed = tokens.usage_from_files([f], since="2026-11-06T08:00:00Z")
    assert windowed.tokens["output"] == 7
    assert windowed.unplaced == 2
    assert not windowed.exact
    # Without a window the time does not matter and nothing is set aside.
    assert tokens.usage_from_files([f]).tokens["output"] == 5 + 6 + 7


def test_bad_lines_are_counted_and_reported_not_swallowed(tmp_path):
    good = row("msg_a", 11)
    f = transcript(
        tmp_path / "s.jsonl",
        good,
        '{"type": "assistant", "message": {"id": "msg_b", "usage": {"output_tok',  # cut
        '["usage"]',  # JSON, but not a record
        'not json at all, but it says "usage"',
        row(None, 3),  # assistant usage with neither id nor uuid
        json.dumps({"type": "assistant", "message": {"id": "m", "usage": "lots"}}),
        json.dumps({"type": "user", "message": {"usage": {"output_tokens": 99}}}),
        json.dumps({"type": "summary", "text": "no accounting here"}),
        "",
    )
    usage = tokens.usage_from_files([f])
    assert usage.tokens["output"] == 11
    assert usage.bad_lines == 4
    assert usage.no_id == 1
    assert usage.other_lines == 2  # the user line and the summary
    assert usage.lines == 8  # the blank line is not a line
    assert not usage.exact
    assert "LOWER BOUND" in usage.source


def test_invalid_utf8_bytes_are_a_bad_line_not_a_crash(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_bytes(json.dumps(row("msg_a", 4)).encode() + b'\n{"usage": "\xff\xfe"}\n')
    usage = tokens.usage_from_files([f])
    assert (usage.tokens["output"], usage.bad_lines) == (4, 1)


def test_a_field_that_is_missing_or_invalid_is_unknown_not_zero(tmp_path):
    no_write = {
        "input_tokens": 1,
        "output_tokens": 2,
        "cache_read_input_tokens": 3,
    }
    f = transcript(
        tmp_path / "s.jsonl",
        row("msg_a", 0, usage=no_write),
        row("msg_b", 0, usage={**no_write, "cache_creation_input_tokens": None}),
    )
    usage = tokens.usage_from_files([f])
    # No message carried cache_write: unknown. The others are known.
    assert usage.tokens == {
        "input_new": 2,
        "output": 4,
        "cache_read": 6,
        "cache_write": None,
    }
    assert usage.missing_fields["cache_write"] == 2
    assert not usage.exact


@pytest.mark.parametrize("bad", ["12", -1, 1.5, True, 2**53])
def test_an_invalid_count_is_set_aside_and_the_total_is_a_lower_bound(tmp_path, bad):
    f = transcript(
        tmp_path / "s.jsonl",
        row("msg_a", 10),
        row("msg_b", 0, usage={"input_tokens": 1, "output_tokens": bad}),
    )
    usage = tokens.usage_from_files([f])
    assert usage.tokens["output"] == 10
    assert usage.missing_fields["output"] == 1
    assert not usage.exact


def test_a_true_zero_stays_zero_and_a_clean_transcript_is_exact(tmp_path):
    f = transcript(
        tmp_path / "s.jsonl",
        row("msg_a", 0, input_tokens=0, cache_read=0, cache_write=0),
    )
    usage = tokens.usage_from_files([f])
    assert usage.tokens == dict.fromkeys(tokens.FIELDS, 0)
    assert usage.exact
    assert usage.models == ("claude-x",)
    assert "LOWER BOUND" not in usage.source


def test_an_empty_transcript_is_unknown_not_zero(tmp_path):
    f = tmp_path / "empty.jsonl"
    f.write_text("")
    usage = tokens.usage_from_files([f])
    assert usage.tokens == dict.fromkeys(tokens.FIELDS)
    assert usage.messages == 0
    assert not usage.exact
    assert "tokens unknown" in usage.source


def test_nothing_in_the_window_is_unknown_not_zero(tmp_path):
    f = transcript(tmp_path / "s.jsonl", row("msg_a", 5))
    usage = tokens.usage_from_files([f], since="2027-01-01T00:00:00Z")
    assert usage.tokens == dict.fromkeys(tokens.FIELDS)
    assert usage.outside_window == 1


def test_no_files_a_directory_or_a_missing_file_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="explicitly"):
        tokens.usage_from_files([])
    with pytest.raises(OSError):
        tokens.usage_from_files([tmp_path])
    with pytest.raises(FileNotFoundError):
        tokens.usage_from_files([tmp_path / "nope.jsonl"])
    f = transcript(tmp_path / "s.jsonl", row("msg_a", 1))
    with pytest.raises(ValueError, match="since is after until"):
        tokens.usage_from_files([f], since=2, until=1)


def test_only_the_named_files_are_read_no_listing_no_home(tmp_path, monkeypatch):
    f = transcript(tmp_path / "s.jsonl", row("msg_a", 3))
    sibling = transcript(tmp_path / "other.jsonl", row("msg_z", 999))

    def forbidden(*args, **kwargs):
        raise AssertionError("the token reader must not search for sessions")

    for owner, name in (
        (os, "listdir"),
        (os, "scandir"),
        (os, "walk"),
        (glob, "glob"),
        (glob, "iglob"),
        (Path, "glob"),
        (Path, "rglob"),
        (Path, "iterdir"),
        (Path, "home"),
        (os.path, "expanduser"),
    ):
        monkeypatch.setattr(owner, name, forbidden)
    usage = tokens.usage_from_files([f])
    assert usage.tokens["output"] == 3  # the sibling file was not touched
    assert sibling.exists()


def test_a_window_bound_without_a_zone_or_not_finite_is_refused():
    assert tokens.parse_time("2026-11-06T09:00:00+00:00") == epoch(T0)
    assert tokens.parse_time(T0) == epoch(T0)
    assert tokens.parse_time("1.5") == 1.5
    for bad in ("2026-11-06T09:00:00", "yesterday", float("nan"), "inf"):
        with pytest.raises(ValueError):
            tokens.parse_time(bad)
    with pytest.raises(TypeError):
        tokens.parse_time(None)


def test_command_line_prints_the_usage_and_refuses_bad_input(tmp_path, capsys):
    f = transcript(tmp_path / "s.jsonl", row("msg_a", 16), row("msg_a", 415))
    assert tokens.main([str(f), "--since", T0]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["tokens"]["output"] == 415
    assert printed["rule"] == tokens.RULE
    assert tokens.main([str(tmp_path / "missing.jsonl")]) == 2
    assert tokens.main([str(f), "--since", "2026-11-06T09:00:00"]) == 2
    assert "error:" in capsys.readouterr().err
