"""CLI uses the API's canonical source gate; all files are private synthetic."""

import json

import pytest
from test_result_index import change_session, change_state, configured

from levi.live import analysis, cli, report, result_index, sessions, stats, statsview
from levi.live.config import Config


def fixture(tmp_path, monkeypatch):
    old, args = configured(tmp_path)
    change_state(args, lambda state: state["demos"].pop("demo_0001"))
    path = old.live_dir / analysis.FILE
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in lines if row["session"] == "run-a")
    )
    cfg = Config()
    cfg.service.workspace = str(old.workspace)
    cfg.service.home = str(tmp_path / "home")
    cfg.watch.roots = old.watch.roots
    stats.record(
        cfg.live_dir,
        {
            "schema": stats.SCHEMA,
            "dataset": "dataset-a",
            "demo": "demo_0000",
            "session": "run-a",
            "at": 100.0,
            "result": {"state": "done"},
        },
    )
    monkeypatch.setattr(cli, "resolve_config", lambda args, **_kwargs: cfg)
    return cfg, args


def argv(command, out=None, fmt="json"):
    args = [command, "--format", fmt]
    if command == "analysis":
        args += ["--no-sync"]
    else:
        args += ["--dataset", "dataset-a", "--session", "run-a"]
    if out:
        args += ["--out", str(out)]
    return args


@pytest.mark.parametrize("command", ["report", "analysis"])
@pytest.mark.parametrize(
    "change",
    [
        {"blinded": True},
        {"operator_blind": True},
        {"aeri": {"run_id": "run-a"}},
        {"run_id": "old-same-name"},
    ],
)
def test_cli_refuses_unknown_and_withheld_before_output(
    tmp_path, monkeypatch, capsys, command, change
):
    _, args = fixture(tmp_path, monkeypatch)
    change_session(args, **change)
    out = tmp_path / "absent" / "result.json"
    assert cli.main(argv(command, out)) == 2
    assert not out.parent.exists()
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "command,fmt",
    [
        ("report", "json"),
        ("report", "csv"),
        ("report", "md"),
        ("analysis", "json"),
        ("analysis", "md"),
    ],
)
def test_source_generation_change_after_render_refuses_output(
    tmp_path, monkeypatch, capsys, command, fmt
):
    _, args = fixture(tmp_path, monkeypatch)
    module = cli.statsfmt if command == "report" else cli.analysis
    function = {"json": "to_json", "md": "to_markdown", "csv": "to_csv"}[fmt]
    original = getattr(module, function)

    def changed(payload, *a, **kw):
        text = original(payload, *a, **kw)
        change_session(args, session_id="changed-generation")
        return text

    monkeypatch.setattr(module, function, changed)
    out = tmp_path / "absent" / "result.json"
    assert cli.main(argv(command, out, fmt)) == 2
    assert not out.parent.exists()
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("command", ["report", "analysis"])
@pytest.mark.parametrize(
    "budget", ["MAX_REQUEST_BYTES", "MAX_JSON_VALUES", "MAX_RECORD_LINES", "MAX_STATES"]
)
def test_shared_byte_budget_refuses_output(
    tmp_path, monkeypatch, capsys, command, budget
):
    fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(result_index, budget, 0)
    out = tmp_path / "absent" / "result.json"
    assert cli.main(argv(command, out)) == 2
    assert not out.parent.exists()
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("command", ["report", "analysis"])
def test_valid_cli_uses_pinned_sources_without_legacy_reloads(
    tmp_path, monkeypatch, capsys, command
):
    cfg, _ = fixture(tmp_path, monkeypatch)

    def forbidden(*a, **kw):
        pytest.fail("unpinned source read")

    monkeypatch.setattr(sessions, "read_sessions", forbidden)
    monkeypatch.setattr(report, "previous_of", forbidden)
    monkeypatch.setattr(report, "policy_of", forbidden)
    monkeypatch.setattr(statsview, "records", forbidden)
    monkeypatch.setattr(cli.mirror, "list_states", forbidden)
    assert cli.main(argv(command)) == 0
    got = json.loads(capsys.readouterr().out)
    assert (
        got["episodes"]["total"] == 1
        if command == "report"
        else got["all"]["episodes"] == 1
    )
    assert not (cfg.live_dir / "reports").exists()


def test_analysis_sync_refused_before_unpinned_writer(tmp_path, monkeypatch, capsys):
    fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        analysis, "sync", lambda *a, **kw: pytest.fail("sync before source gate")
    )
    assert cli.main(["analysis", "--sync"]) == 2
    assert capsys.readouterr().out == ""


def test_cutoff_does_not_authorize_outside_window_rows(tmp_path, monkeypatch, capsys):
    cfg, _ = fixture(tmp_path, monkeypatch)
    stats.record(
        cfg.live_dir,
        {
            "schema": stats.SCHEMA,
            "dataset": "dataset-a",
            "demo": "demo_9999",
            "session": "unknown-run",
            "at": 200.0,
        },
    )
    assert cli.main(argv("report") + ["--before", "200"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["episodes"]["total"] == got["signature"]["records"] == 1


@pytest.mark.parametrize("command", ["report", "analysis"])
@pytest.mark.parametrize("where", ["meta", "eval", "aeri"])
def test_capture_marker_cannot_be_masked_by_public_session(
    tmp_path, monkeypatch, capsys, command, where
):
    _, args = fixture(tmp_path, monkeypatch)
    change_session(args, analysis_visibility="public")
    path = args["rollout_root"] / "model-a/task-a/demo_0000/metadata.json"
    value = json.loads(path.read_text())
    if where == "meta":
        value["operator_blind"] = True
    elif where == "eval":
        value["eval"]["blinded"] = True
    else:
        value["eval"]["aeri"] = {"campaign_id": "private-campaign"}
    path.write_text(json.dumps(value))
    assert cli.main(argv(command)) == 2
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("command", ["report", "analysis"])
def test_failed_source_gate_preserves_existing_target(
    tmp_path, monkeypatch, capsys, command
):
    _, args = fixture(tmp_path, monkeypatch)
    change_session(args, run_id="rebound-run")
    out = tmp_path / "existing.json"
    out.write_text("previous output")
    assert cli.main(argv(command, out)) == 2
    assert out.read_text() == "previous output"
    assert not out.with_name(out.name + ".partial").exists()
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("command", ["report", "analysis"])
def test_unconfigured_root_is_not_trusted_from_state_name(
    tmp_path, monkeypatch, capsys, command
):
    _, args = fixture(tmp_path, monkeypatch)
    change_state(
        args,
        lambda state: state.update(source=str(tmp_path / "foreign/model-a/task-a")),
    )
    assert cli.main(argv(command)) == 2
    assert capsys.readouterr().out == ""


def test_default_analysis_sync_refused_without_writes(tmp_path, monkeypatch, capsys):
    cfg, _ = fixture(tmp_path, monkeypatch)
    before = (cfg.live_dir / analysis.FILE).read_bytes()
    assert cli.main(["analysis"]) == 2
    assert "--no-sync" in capsys.readouterr().err
    assert before == (cfg.live_dir / analysis.FILE).read_bytes()
    assert not (cfg.live_dir / "reports").exists()


def test_verified_report_omits_unpinned_external_metadata(
    tmp_path, monkeypatch, capsys
):
    fixture(tmp_path, monkeypatch)
    assert cli.main(argv("report")) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["metadata_omitted"] == ["previous_report", "policy"]
    assert got["previous"] is None and "policy" not in got["settings"]
