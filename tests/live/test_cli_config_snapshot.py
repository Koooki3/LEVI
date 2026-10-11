"""Private config and remembered pointers share the source snapshot budget."""

import json

import pytest
from test_cli_source_contract import argv, fixture

from levi.live import cli, result_index
from levi.live.config import render

REAL_RESOLVE = cli.resolve_config


def configured(tmp_path, monkeypatch):
    cfg, args = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "resolve_config", REAL_RESOLVE)
    for name in (cli.ENV_WORKSPACE, cli.ENV_CONFIG, cli.ENV_HOME):
        monkeypatch.delenv(name, raising=False)
    path = cfg.workspace / "live.toml"
    path.write_text(render(cfg))
    return cfg, args, path


@pytest.mark.parametrize("command", ["report", "analysis"])
def test_real_cli_uses_bounded_config_and_same_pinned_sources(
    tmp_path, monkeypatch, capsys, command
):
    cfg, _, path = configured(tmp_path, monkeypatch)
    assert cli.main(argv(command) + ["--workspace", str(cfg.workspace)]) == 0
    assert json.loads(capsys.readouterr().out)["schema"]
    assert path.is_file()


@pytest.mark.parametrize("command", ["report", "analysis"])
def test_config_rebound_after_render_is_refused_without_output(
    tmp_path, monkeypatch, capsys, command
):
    cfg, _, path = configured(tmp_path, monkeypatch)
    module = cli.statsfmt if command == "report" else cli.analysis
    original = module.to_json

    def changed(payload):
        text = original(payload)
        path.write_text(render(cfg) + "\n# changed generation\n")
        return text

    monkeypatch.setattr(module, "to_json", changed)
    out = tmp_path / "absent/result.json"
    assert cli.main(argv(command, out) + ["--workspace", str(cfg.workspace)]) == 2
    assert capsys.readouterr().out == ""
    assert not out.parent.exists()


@pytest.mark.parametrize("command", ["report", "analysis"])
def test_configuration_byte_limit_refused_before_any_source_parse(
    tmp_path, monkeypatch, capsys, command
):
    cfg, _, _ = configured(tmp_path, monkeypatch)
    monkeypatch.setattr(result_index, "MAX_CONFIG_BYTES", 1)
    monkeypatch.setattr(
        result_index,
        "legacy_sources",
        lambda *a, **kw: pytest.fail("oversized config passed source gate"),
    )
    out = tmp_path / "absent/result.json"
    assert cli.main(argv(command, out) + ["--workspace", str(cfg.workspace)]) == 2
    assert capsys.readouterr().out == ""
    assert not out.parent.exists()


@pytest.mark.parametrize("command", ["report", "analysis"])
def test_remembered_workspace_pointer_changed_before_output_is_refused(
    tmp_path, monkeypatch, capsys, command
):
    cfg, _, _ = configured(tmp_path, monkeypatch)
    cfg.home.mkdir()
    pointer = cfg.home / "started.json"
    pointer.write_text(json.dumps({"workspace": str(cfg.workspace)}))
    (cfg.live_dir / "workspace.json").write_text("{}")
    module = cli.statsfmt if command == "report" else cli.analysis
    original = module.to_json

    def changed(payload):
        text = original(payload)
        pointer.write_text(json.dumps({"workspace": str(tmp_path / "other-workspace")}))
        return text

    monkeypatch.setattr(module, "to_json", changed)
    out = tmp_path / "absent/result.json"
    assert cli.main(argv(command, out) + ["--home", str(cfg.home)]) == 2
    assert capsys.readouterr().out == ""
    assert not out.parent.exists()


def test_optional_missing_config_creation_is_a_generation_change(
    tmp_path, monkeypatch, capsys
):
    cfg, _, path = configured(tmp_path, monkeypatch)
    path.unlink()
    original = cli.statsfmt.to_json

    def changed(payload):
        text = original(payload)
        path.write_text(render(cfg))
        return text

    monkeypatch.setattr(cli.statsfmt, "to_json", changed)
    assert (
        cli.main(
            argv("report")
            + ["--workspace", str(cfg.workspace), "--root", cfg.watch.roots[0]]
        )
        == 2
    )
    assert capsys.readouterr().out == ""


def test_config_parse_error_does_not_print_input_values(tmp_path, monkeypatch, capsys):
    _cfg, _, path = configured(tmp_path, monkeypatch)
    path.write_text('secret_example = "private-canary"\n')
    assert cli.main(argv("report") + ["--config", str(path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "private-canary" not in captured.err and "secret_example" not in captured.err


@pytest.mark.parametrize("command", ["report", "analysis"])
def test_config_and_statistics_share_one_total_byte_budget(
    tmp_path, monkeypatch, capsys, command
):
    cfg, _, path = configured(tmp_path, monkeypatch)
    state = cfg.live_dir / "datasets/dataset-a.json"
    monkeypatch.setattr(
        result_index, "MAX_REQUEST_BYTES", path.stat().st_size + state.stat().st_size
    )
    assert cli.main(argv(command) + ["--workspace", str(cfg.workspace)]) == 2
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("command", ["report", "analysis"])
def test_toml_nodes_enter_shared_budget_before_statistics(
    tmp_path, monkeypatch, capsys, command
):
    cfg, _, _ = configured(tmp_path, monkeypatch)
    monkeypatch.setattr(result_index, "MAX_JSON_VALUES", 0)
    monkeypatch.setattr(
        result_index, "legacy_sources", lambda *a, **kw: pytest.fail("unbudgeted TOML")
    )
    assert cli.main(argv(command) + ["--workspace", str(cfg.workspace)]) == 2
    assert capsys.readouterr().out == ""


def test_config_ancestor_symlink_after_render_refuses_output(
    tmp_path, monkeypatch, capsys
):
    cfg, _, _ = configured(tmp_path, monkeypatch)
    original = cli.statsfmt.to_json

    def changed(payload):
        text = original(payload)
        saved = tmp_path / "saved-home"
        cfg.workspace.rename(saved)
        cfg.workspace.symlink_to(saved, target_is_directory=True)
        return text

    monkeypatch.setattr(cli.statsfmt, "to_json", changed)
    out = tmp_path / "absent/result.json"
    assert cli.main(argv("report", out) + ["--workspace", str(cfg.workspace)]) == 2
    assert not out.parent.exists()
    assert capsys.readouterr().out == ""
