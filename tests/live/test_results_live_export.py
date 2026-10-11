import hashlib
import json

import pytest

from levi.evaluation_results import CatalogError, entries, export_live
from levi.live import analysis


def sources(tmp_path):
    rollout = tmp_path / "rollouts/models"
    source = rollout / "model-a/task-a"
    source.mkdir(parents=True)
    session_file = rollout / ".eval_sessions/model-a__task-a.json"
    session_file.parent.mkdir()
    session = {
        "schema": "levi.eval.session.v1",
        "state": "finished",
        "group": "model-a",
        "task_folder": "task-a",
        "run_id": "run-a",
        "session_id": "session-a",
    }
    session_file.write_text(json.dumps(session))
    live = tmp_path / "live-home/live"
    (live / "datasets").mkdir(parents=True)
    state = {
        "group": "model-a",
        "task_folder": "task-a",
        "source": str(source),
        "demos": {
            "demo_0000": {"run_id": "run-a", "state": "done"},
            "demo_0001": {"run_id": "other-run", "state": "done"},
        },
    }
    (live / "datasets/dataset-a.json").write_text(json.dumps(state))
    for demo, run, outcome in [
        ("demo_0000", "run-a", "success"),
        ("demo_0001", "other-run", "failure"),
    ]:
        record = analysis.build_record(
            "dataset-a",
            demo,
            {"run_id": run, "episode_index": 0, "operator_label": {"outcome": outcome}},
            state=state,
            now=1,
        )
        analysis.append(live, record)
    return {
        "root": tmp_path / "catalog",
        "live_dir": live,
        "rollout_root": rollout,
        "session_file": session_file,
    }


def test_live_export_uses_real_analysis_and_explicit_session(tmp_path):
    args = sources(tmp_path)
    before = (args["live_dir"] / "analysis.jsonl").read_bytes()
    row = export_live(**args)
    assert row == export_live(**args)
    assert entries(args["root"]) == [row]
    directory = row["report"]["directory"]
    from pathlib import Path

    report = Path(directory)
    summary = json.loads((report / "data/analysis.json").read_text())
    assert summary["all"]["episodes"] == 1
    assert summary["all"]["success"]["n"] == 1
    assert summary["all"]["success"]["of"] == 1
    assert before == (args["live_dir"] / "analysis.jsonl").read_bytes()
    assert (report / "figures/success.svg").is_file()
    for name, digest in row["report"]["files"].items():
        assert hashlib.sha256((report / name).read_bytes()).hexdigest() == digest
    assert len(list(report.parent.glob("*/manifest.json"))) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"state": "running"},
        {"blinded": True},
        {"operator_blind": True},
        {"campaign_id": "c001"},
        {"group": "wrong-model"},
        {"run_id": "unknown-run"},
    ],
)
def test_active_blind_unknown_or_wrong_session_refused(tmp_path, change):
    args = sources(tmp_path)
    file = args["session_file"]
    value = json.loads(file.read_text())
    value.update(change)
    file.write_text(json.dumps(value))
    with pytest.raises(CatalogError):
        export_live(**args)
    assert not args["root"].exists()


def test_episode_source_session_changed_refused(tmp_path):
    args = sources(tmp_path)
    state = args["live_dir"] / "datasets/dataset-a.json"
    value = json.loads(state.read_text())
    value["demos"]["demo_0000"]["run_id"] = "other"
    state.write_text(json.dumps(value))
    with pytest.raises(CatalogError):
        export_live(**args)


def test_deleted_episode_not_in_statistics(tmp_path):
    args = sources(tmp_path)
    state = args["live_dir"] / "datasets/dataset-a.json"
    value = json.loads(state.read_text())
    value["demos"]["demo_0000"]["deleted"] = True
    state.write_text(json.dumps(value))
    with pytest.raises(CatalogError, match="No committed"):
        export_live(**args)


@pytest.mark.parametrize(
    "marker", [{"aeri": {"run_id": "run-a"}}, {"episode_role": "forward"}]
)
def test_aeri_unknown_visibility_is_not_permission_to_reveal(tmp_path, marker):
    args = sources(tmp_path)
    file = args["session_file"]
    value = json.loads(file.read_text())
    value.update(marker)
    file.write_text(json.dumps(value))
    with pytest.raises(CatalogError, match="visibility"):
        export_live(**args)


def test_source_change_during_summary_refuses_publication(tmp_path, monkeypatch):
    args = sources(tmp_path)
    original = analysis.summarize

    def changing(*a, **kw):
        result = original(*a, **kw)
        state = args["live_dir"] / "datasets/dataset-a.json"
        value = json.loads(state.read_text())
        value["demos"]["demo_0000"]["deleted"] = True
        state.write_text(json.dumps(value))
        return result

    monkeypatch.setattr(analysis, "summarize", changing)
    with pytest.raises(CatalogError, match="changed"):
        export_live(**args)
    assert not args["root"].exists()


def test_real_cli_and_configured_terminal_session_sync(tmp_path, capsys):
    from types import SimpleNamespace

    from levi.evaluation_results import main, sync_live_catalog

    args = sources(tmp_path)
    assert (
        main(
            [
                "live-export",
                "--root",
                str(args["root"]),
                "--live-dir",
                str(args["live_dir"]),
                "--rollout-root",
                str(args["rollout_root"]),
                "--session-file",
                str(args["session_file"]),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["session"] == "run-a"
    cfg = SimpleNamespace(
        workspace=args["live_dir"].parent,
        live_dir=args["live_dir"],
        watch=SimpleNamespace(roots=[str(args["rollout_root"])]),
    )
    assert sync_live_catalog(cfg, root=args["root"]) == {"written": 1, "skipped": []}


def test_two_label_bases_remain_in_session_index(tmp_path):
    from pathlib import Path

    args = sources(tmp_path)
    row = export_live(**args)
    target = Path(row["report"]["directory"]).parents[1]
    second = dict(row, basis="operator")
    (target / "reports/operator").mkdir()
    (target / "reports/operator/reference.json").write_text(json.dumps(second))
    assert {r["basis"] for r in entries(args["root"])} == {
        "operator",
        "operator_and_automatic_separate",
    }


def test_same_identity_different_rollout_root_is_rejected(tmp_path):
    args = sources(tmp_path)
    export_live(**args)
    other = sources(tmp_path / "other")
    other["root"] = args["root"]
    with pytest.raises(CatalogError, match="different rollout"):
        export_live(**other)


def test_catalog_failure_does_not_undo_committed_analysis(tmp_path, monkeypatch):
    import levi.evaluation_results as catalog

    args = sources(tmp_path)
    before = (args["live_dir"] / "analysis.jsonl").read_bytes()
    called = []
    monkeypatch.setattr(analysis, "sync", lambda *args: called.append("analysis"))

    def broken(config):
        called.append("catalog")
        raise OSError("disk failure")

    monkeypatch.setattr(catalog, "sync_live_catalog", broken)
    analysis.sync_quiet(object())
    assert called == ["analysis", "catalog"]
    assert (args["live_dir"] / "analysis.jsonl").read_bytes() == before
