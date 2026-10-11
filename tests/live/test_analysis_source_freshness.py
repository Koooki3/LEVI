"""Shared canonical freshness gate, using only private synthetic sources."""

import json

import pytest
from fastapi import HTTPException
from test_cli_source_contract import argv, fixture
from test_result_index import change_state, lookup

from levi.live import cli, result_index


@pytest.mark.parametrize(
    "change",
    [
        {"episode_index": 7},
        {"temporal": {"committed_at": 2}},
        {"operator_label": {"outcome": "failure"}},
        {"verdict": {"outcome": "failure"}},
        {"online": {"status": "done", "verdict": {"outcome": "failure"}}},
    ],
    ids=["episode_index", "committed_at", "operator", "agent", "online"],
)
def test_stale_identity_rejected_by_session_and_legacy_cli(
    tmp_path, monkeypatch, capsys, change
):
    cfg, args = fixture(tmp_path, monkeypatch)
    change_state(args, lambda s: s["demos"]["demo_0000"].update(change))
    item = lookup(cfg)
    assert item["status"] == "stale"
    assert item["analysis"] is None
    with pytest.raises(HTTPException) as error:
        result_index.legacy_sources(cfg, result_index._Snapshot(), kind="analysis")
    assert error.value.status_code == 409
    assert error.value.detail["reason"] == "statistics_source_stale"
    out = tmp_path / "output" / "analysis.json"
    assert cli.main(argv("analysis", out)) == 2
    assert capsys.readouterr().out == ""
    assert not out.parent.exists()


def test_current_identity_stays_available(tmp_path, monkeypatch, capsys):
    cfg, _ = fixture(tmp_path, monkeypatch)
    assert lookup(cfg)["status"] == "reported"
    assert cli.main(argv("analysis")) == 0
    assert json.loads(capsys.readouterr().out)["all"]["episodes"] == 1
