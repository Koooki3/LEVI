"""External token monitor (levi/eval/token_monitor.py); synthetic transcripts only."""

import json

import pytest

from levi.eval import token_monitor as tm


def test_selftest_all_rules():
    names = tm.selftest()
    assert len(names) >= 20


def test_cli_selftest(capsys):
    assert tm.main(["--selftest"]) == 0
    assert "selftest ok" in capsys.readouterr().out


def _file(tmp_path, name, lines):
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n")
    return path


def test_cli_json_and_markdown(tmp_path, capsys):
    f = _file(tmp_path, "s.jsonl", [tm._line("a", 5), tm._line("a", 9)])
    assert tm.main(["--task", f"job={f}", "--estimate", "job=10"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["schema"] == tm.SCHEMA
    assert data["tasks"]["job"]["tokens"]["output"] == 9
    assert data["tasks"]["job"]["fresh"] == 1 + 9 + 100  # 110 > 20 -> alert
    assert data["total"]["alerts"] == ["job"]
    out = tmp_path / "o.md"
    assert (
        tm.main(["--task", f"job={f}", "--format", "markdown", "--out", str(out)]) == 0
    )
    assert "no estimate" in out.read_text()


def test_cli_errors(tmp_path, capsys):
    f = _file(tmp_path, "s.jsonl", [tm._line("a", 5)])
    assert tm.main([]) == 2
    assert tm.main(["--task", f"bad label={f}"]) == 2
    assert tm.main(["--task", f"x={f}", "--estimate", "y=5"]) == 2
    assert tm.main(["--task", f"x={f}", "--estimate", "x=0"]) == 2
    assert tm.main(["--task", f"x={tmp_path / 'missing.jsonl'}"]) == 2


def test_consistency_guard_matches_tokens(tmp_path):
    from levi.eval import tokens

    f = _file(
        tmp_path, "s.jsonl", [tm._line("a", 5), tm._line("a", 9), tm._line("b", 2)]
    )
    rep = tm.monitor({"t": [f]})
    assert rep["tasks"]["t"]["tokens"] == tokens.usage_from_files([f]).tokens


def test_alert_boundary_is_strictly_greater():
    assert tm._alert(20, 10)[0] is False
    assert tm._alert(21, 10)[0] is True
    assert tm._alert(5, None)[1] == "no_estimate"
    assert tm._alert(None, 5)[1] == "fresh_unknown"
    with pytest.raises(ValueError):
        tm.monitor({"t": []}, estimates={"u": 1})


def test_guard_raises_when_views_disagree(tmp_path, monkeypatch):
    from levi.eval import tokens

    f = _file(tmp_path, "s.jsonl", [tm._line("a", 5)])
    real = tokens.usage_from_files

    def lying(paths, **kw):
        u = real(paths, **kw)
        return type(u)(**{**u.__dict__, "tokens": {**u.tokens, "output": 999}})

    monkeypatch.setattr(tokens, "usage_from_files", lying)
    with pytest.raises(RuntimeError):
        tm.monitor({"t": [f]})
