"""Synthetic staged exports only: no private sessions, services or queues."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from levi.automatic import external_report as R
from levi.automatic import local_log_import as L

ANSWER = (
    b'{"answers":[{"index":1,"value":true,"camera":"side","reason":"synthetic"}]}\n'
)
REQUEST = "request-synthetic"
FIXTURE = Path(__file__).parent / "fixtures_local_import/local_agent_export_v1.json"


def example():
    return json.loads(FIXTURE.read_bytes())


def encode(value):
    return json.dumps(value).encode()


def stage(tmp_path, value=None, answer=ANSWER):
    directory = tmp_path / "redacted-export"
    directory.mkdir()
    (directory / "agent-export.json").write_bytes(
        encode(example() if value is None else value)
    )
    (directory / "answer.json").write_bytes(answer)
    return directory


def test_synthetic_fixture_consumed_by_existing_report_boundary():
    result = L.parse_export(FIXTURE.read_bytes(), ANSWER, REQUEST)
    assert result["tokens"] == {
        "input_new": 0,
        "output": 3,
        "cache_read": None,
        "cache_write": 0,
    }
    assert result["elapsed_ms"] == 12
    evidence = R.evidence(("candidate", encode(result)), REQUEST, ANSWER.decode(), 20)
    verified = R.verify_evidence(evidence, REQUEST, ANSWER.decode())
    assert verified["binding_verified"] is True
    assert verified["report_source"] == "external_self_reported"
    assert "usage" not in result and "round_id" not in result


def test_cli_prints_only_report_and_does_not_write(tmp_path, capsys):
    directory = stage(tmp_path)
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    assert L.main(["--export-dir", str(directory), "--request-id", REQUEST]) == 0
    out = capsys.readouterr()
    assert out.err == ""
    assert json.loads(out.out) == L.parse_export(encode(example()), ANSWER, REQUEST)
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before


@pytest.mark.parametrize("source", ["codex", "claude", "other", "synthetic"])
def test_source_is_declaration_only(source):
    value = example()
    value["source"] = source
    assert (
        L.parse_export(encode(value), ANSWER, REQUEST)["agent_id"] == "synthetic-agent"
    )


@pytest.mark.parametrize("tokens", [{}, {"output": None}, {"output": 0}, {"output": 7}])
def test_unknown_is_not_zero(tokens):
    value = example()
    value["report"]["tokens"] = tokens
    result = L.parse_export(encode(value), ANSWER, REQUEST)
    assert result["tokens"]["input_new"] is None
    assert result["tokens"]["output"] == tokens.get("output")


@pytest.mark.parametrize(
    "tokens",
    [
        None,
        [],
        {"output": True},
        {"output": -1},
        {"output": 1.5},
        {"output": "2"},
        {"total": 2},
        {"output": 2**53},
    ],
)
def test_invalid_tokens_are_not_successful_empty_reports(tokens):
    value = example()
    value["report"]["tokens"] = tokens
    with pytest.raises(L.ImportRefused, match="invalid_report"):
        L.parse_export(encode(value), ANSWER, REQUEST)


@pytest.mark.parametrize(
    "change",
    [
        {"schema": "native-codex-session"},
        {"source": "auto"},
        {"source": {}},
        {"redacted": False},
        {"redacted": 1},
        {"transcript": "private-text"},
    ],
)
def test_export_contract_rejects_unknown_unredacted_native_formats(change):
    value = example()
    value.update(change)
    with pytest.raises(L.ImportRefused, match="invalid_export"):
        L.parse_export(encode(value), ANSWER, REQUEST)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"a":1,"a":2}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":1e400}',
        b"{}\n{}",
        b"\xff",
        b"[" * 18 + b"0" + b"]" * 18,
    ],
)
@pytest.mark.parametrize("input_name", ["export", "answer"])
def test_strict_json_boundary(raw, input_name):
    args = (raw, ANSWER) if input_name == "export" else (encode(example()), raw)
    with pytest.raises(L.ImportRefused, match="invalid_json"):
        L.parse_export(*args, REQUEST)


@pytest.mark.parametrize("answer", [b"[]", b'{"choices":[]}', b'{"usage":{}}'])
def test_completion_envelopes_are_not_raw_answers(answer):
    with pytest.raises(L.ImportRefused, match="raw_answer_required"):
        L.parse_export(encode(example()), answer, REQUEST)


# `request` is a reserved pytest fixture name and may not be a parameter name.
@pytest.mark.parametrize(
    "answer,request_id", [(ANSWER, "request-other"), (ANSWER.rstrip(), REQUEST)]
)
def test_exact_id_and_original_bytes_are_required(answer, request_id):
    with pytest.raises(L.ImportRefused, match="report_mismatch"):
        L.parse_export(encode(example()), answer, request_id)


@pytest.mark.parametrize(
    "field,value",
    [("round_id", "round1"), ("elapsed_ms", True), ("agent_notes", "x" * 4097)],
)
def test_invalid_report_fields(field, value):
    data = example()
    data["report"][field] = value
    with pytest.raises(L.ImportRefused, match="invalid_report"):
        L.parse_export(encode(data), ANSWER, REQUEST)


@pytest.mark.parametrize("target", ["export", "answer"])
def test_size_limits(target):
    args = (
        (b" " * (L.MAX_EXPORT_BYTES + 1), ANSWER)
        if target == "export"
        else (encode(example()), b" " * (L.MAX_ANSWER_BYTES + 1))
    )
    with pytest.raises(L.ImportRefused, match="input_too_large"):
        L.parse_export(*args, REQUEST)


@pytest.mark.parametrize("name", ["agent-export.json", "answer.json"])
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory"])
def test_special_file_refused_without_reading(tmp_path, name, kind):
    directory = stage(tmp_path)
    target = directory / name
    target.unlink()
    other = tmp_path / "synthetic-only.json"
    other.write_text("not read")
    if kind == "symlink":
        target.symlink_to(other)
    elif kind == "hardlink":
        os.link(other, target)
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        target.mkdir()
    with pytest.raises(L.ImportRefused):
        L.import_directory(directory, REQUEST)


def test_intermediate_symlink_and_hidden_paths_refused(tmp_path):
    directory = stage(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(L.ImportRefused):
        L.import_directory(alias / directory.name, REQUEST)
    with pytest.raises(L.ImportRefused, match="export_directory_required"):
        L.import_directory(tmp_path / ".private" / "export", REQUEST)
    with pytest.raises(L.ImportRefused, match="export_directory_required"):
        L.import_directory(Path("relative"), REQUEST)


def test_failure_stdout_empty_and_no_content_or_path_disclosure(tmp_path, capsys):
    value = example()
    value["report"]["agent_notes"] = "SECRET-SYNTHETIC"
    directory = stage(tmp_path, value)
    assert L.main(["--export-dir", str(directory), "--request-id", "wrong"]) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == "import refused: report_mismatch\n"
    assert "SECRET-SYNTHETIC" not in out.err and str(directory) not in out.err


def test_answer_json_hash_is_not_normalized():
    spaced = b'{ "answers": [] }\r\n'
    value = example()
    value["report"]["answer_sha256"] = hashlib.sha256(spaced).hexdigest()
    assert (
        L.parse_export(encode(value), spaced, REQUEST)["answer_sha256"]
        == hashlib.sha256(spaced).hexdigest()
    )


@pytest.mark.parametrize("delta,accepted", [(0, True), (-1, False)])
def test_serialized_report_budget_includes_newline(monkeypatch, delta, accepted):
    normalized = R.normalize_report(example()["report"], REQUEST, ANSWER.decode())
    limit = len(L.serialize_report(normalized)) + delta
    monkeypatch.setattr(R, "MAX_REPORT_BYTES", limit)
    if accepted:
        result = L.parse_export(encode(example()), ANSWER, REQUEST)
        assert len(L.serialize_report(result)) == limit
        assert L.serialize_report(result).endswith(b"\n")
    else:
        with pytest.raises(L.ImportRefused, match="invalid_report"):
            L.parse_export(encode(example()), ANSWER, REQUEST)


def test_missing_fields_expansion_is_bounded(monkeypatch):
    data = example()
    data["report"] = {
        k: data["report"][k] for k in ("schema", "request_id", "answer_sha256")
    }
    monkeypatch.setattr(R, "MAX_REPORT_BYTES", len(encode(data["report"])) + 1)
    with pytest.raises(L.ImportRefused, match="invalid_report"):
        L.parse_export(encode(data), ANSWER, REQUEST)


def test_argument_errors_do_not_echo_parameters(capsys):
    with pytest.raises(SystemExit) as error:
        L.main(["--unknown-SECRET-SYNTHETIC"])
    assert error.value.code == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == "import refused: invalid_arguments\n"


def test_invalid_filesystem_string_fails_without_traceback(capsys):
    assert L.main(["--export-dir", "/synthetic\0private", "--request-id", REQUEST]) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == "import refused: unreadable_export\n"


@pytest.mark.parametrize("hidden", [".claude", ".codex", ".ssh", ".x"])
def test_hidden_directories_other_than_state_are_refused(tmp_path, hidden):
    directory = tmp_path / hidden / "export"
    directory.mkdir(parents=True)
    with pytest.raises(L.ImportRefused, match="export_directory_required"):
        L._directory(directory)


def test_levi_state_directory_is_allowed(tmp_path):
    directory = tmp_path / ".state" / "export"
    directory.mkdir(parents=True)
    os.close(L._directory(directory))  # no ImportRefused; it returns an fd
