"""Analysis authorization uses the engine's window before latest selection."""

import json

import pytest
from test_cli_source_contract import argv, fixture
from test_result_index import change_session

from levi.live import analysis, cli, result_index


def append_unknown(cfg, *, now=0, demo="demo_9999", labelled_at=None):
    record = analysis.build_record(
        "dataset-a",
        demo,
        {"run_id": "unknown-run", "episode_index": 99},
        state={"group": "model-a", "task_folder": "task-a"},
        now=now,
    )
    if labelled_at is not None:
        record["labelled_at"] = labelled_at
    analysis.append(cfg.live_dir, record)


@pytest.mark.parametrize("demo", ["demo_9999", "demo_0000"])
def test_old_unknown_record_outside_since_does_not_poison_selection(
    tmp_path, monkeypatch, capsys, demo
):
    cfg, _ = fixture(tmp_path, monkeypatch)
    append_unknown(cfg, demo=demo)
    expected = analysis.build(cfg, since="1")
    assert expected["all"]["episodes"] == 1
    assert cli.main(argv("analysis") + ["--since", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["all"] == expected["all"]
    snapshot = result_index._Snapshot()
    sources = result_index.legacy_sources(cfg, snapshot, kind="analysis", since="1")
    assert sources["records"][0]["session"] == "run-a"
    assert snapshot.valid() and not snapshot.truncated


@pytest.mark.parametrize("now,labelled_at", [(1, None), (0, 1), (1, 0)])
def test_selected_unknown_still_refused_at_inclusive_since(
    tmp_path, monkeypatch, capsys, now, labelled_at
):
    cfg, _ = fixture(tmp_path, monkeypatch)
    append_unknown(cfg, now=now, labelled_at=labelled_at)
    assert cli.main(argv("analysis") + ["--since", "1"]) == 2
    assert capsys.readouterr().out == ""


def test_since_does_not_remove_global_blind_state_gate(tmp_path, monkeypatch, capsys):
    _, args = fixture(tmp_path, monkeypatch)
    change_session(args, blinded=True)
    assert cli.main(argv("analysis") + ["--since", "2"]) == 2
    assert capsys.readouterr().out == ""


def test_outside_window_records_still_enter_snapshot_budget(
    tmp_path, monkeypatch, capsys
):
    cfg, _ = fixture(tmp_path, monkeypatch)
    append_unknown(cfg)
    monkeypatch.setattr(result_index, "MAX_RECORD_LINES", 1)
    assert cli.main(argv("analysis") + ["--since", "1"]) == 2
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("now,status", [(0, 200), (1, 409)])
def test_api_uses_same_window_and_rejects_selected_unknown(
    tmp_path, monkeypatch, now, status
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from levi.live import api

    cfg, _ = fixture(tmp_path, monkeypatch)
    append_unknown(cfg, now=now)
    monkeypatch.setattr(api, "_result_config", lambda: cfg)
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        response = client.get("/api/levi/live/analysis", params={"since": "1"})
    assert response.status_code == status
    if status == 200:
        assert response.json()["all"]["episodes"] == 1
    else:
        assert response.json()["detail"]["reason"] == "statistics_source_unknown"
