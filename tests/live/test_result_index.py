"""Result routes select canonical sessions, never approximate group/task scopes."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from test_results_live_export import sources

from levi.live import analysis, api, result_index


def configured(tmp_path):
    args = sources(tmp_path)
    cfg = SimpleNamespace(
        workspace=args["live_dir"].parent,
        live_dir=args["live_dir"],
        watch=SimpleNamespace(roots=[str(args["rollout_root"])]),
    )
    change_state(
        args,
        lambda state: state["demos"]["demo_0000"].update(
            episode_index=0, operator_label={"outcome": "success"}
        ),
    )
    source = args["rollout_root"] / "model-a/task-a"
    write_header(source, "demo_0000", "run-a")
    return cfg, args


def write_header(source, demo, run, **extra):
    directory = source / demo
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "metadata.json").write_text(
        json.dumps({"eval": {"run_id": run, **extra}})
    )


def lookup(cfg, **kw):
    return result_index.session(
        cfg,
        root=cfg.watch.roots[0],
        group="model-a",
        task="task-a",
        session="run-a",
        **kw,
    )


def change_session(args, **kw):
    path = args["session_file"]
    value = json.loads(path.read_text())
    value.update(kw)
    path.write_text(json.dumps(value))


def change_state(args, fn):
    path = args["live_dir"] / "datasets/dataset-a.json"
    value = json.loads(path.read_text())
    fn(value)
    path.write_text(json.dumps(value))


def test_exact_session_reuses_summary_without_writes(tmp_path):
    cfg, _args = configured(tmp_path)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    item = lookup(cfg)
    assert item["analysis"]["all"]["episodes"] == 1
    assert item["analysis"]["all"]["success"]["n"] == 1
    assert item["identity"]["datasets"] == ["dataset-a"]
    assert item["source"]["kind"] == "current_session"
    assert item["source"]["snapshot"]
    assert before == {
        str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    }


@pytest.mark.parametrize(
    "change,status",
    [
        ({"state": "running"}, "active"),
        ({"aeri": {"run_id": "run-a"}}, "blocked"),
        ({"episode_role": "forward"}, "blocked"),
        ({"blinded": True}, "blocked"),
        ({"operator_blind": True}, "blocked"),
    ],
)
def test_active_and_blind_never_leak_records_or_download(tmp_path, change, status):
    cfg, args = configured(tmp_path)
    change_session(args, **change)
    item = lookup(cfg)
    assert item["status"] == status
    assert item["analysis"] is None
    assert item["records"] == item["runs"] == item["reports"] == []
    with pytest.raises(HTTPException) as exc:
        result_index.download(item, format="json", lang="en")
    assert exc.value.status_code == 409


def test_same_names_in_different_roots_do_not_mix(tmp_path):
    cfg, args = configured(tmp_path)
    other = sources(tmp_path / "other")
    cfg.watch.roots.append(str(other["rollout_root"]))
    write_header(other["rollout_root"] / "model-a/task-a", "demo_0000", "run-a")
    other_state = json.loads(
        (other["live_dir"] / "datasets/dataset-a.json").read_text()
    )
    other_state["demos"]["demo_0000"].update(
        episode_index=0, operator_label={"outcome": "failure"}
    )
    (args["live_dir"] / "datasets/dataset-b.json").write_text(json.dumps(other_state))
    analysis.append(
        args["live_dir"],
        analysis.build_record(
            "dataset-b",
            "demo_0000",
            {
                "run_id": "run-a",
                "episode_index": 0,
                "operator_label": {"outcome": "failure"},
            },
            state=other_state,
            now=1,
        ),
    )
    assert lookup(cfg)["analysis"]["all"]["episodes"] == 1
    selected = result_index.session(
        cfg,
        root=str(other["rollout_root"]),
        group="model-a",
        task="task-a",
        session="run-a",
    )
    assert selected["identity"]["datasets"] == ["dataset-b"]
    assert selected["analysis"]["all"]["success"]["n"] == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("root", "/tmp/unconfigured"),
        ("group", "../model-a"),
        ("task", "../task-a"),
        ("session", "../../run-a"),
    ],
)
def test_client_paths_cannot_select_arbitrary_sources(tmp_path, field, value):
    cfg, _ = configured(tmp_path)
    params = {
        "root": cfg.watch.roots[0],
        "group": "model-a",
        "task": "task-a",
        "session": "run-a",
    }
    params[field] = value
    with pytest.raises(HTTPException):
        result_index.session(cfg, **params)


def test_bookmark_session_and_run_must_both_match(tmp_path):
    cfg, _ = configured(tmp_path)
    item = lookup(cfg, session_id="replaced", run_id="run-a")
    assert item["status"] == "rebound"
    assert item["analysis"] is None


def test_episode_rebound_is_not_counted_as_old_analysis(tmp_path):
    cfg, args = configured(tmp_path)
    change_state(args, lambda s: s["demos"]["demo_0000"].update(run_id="replacement"))
    item = lookup(cfg)
    assert item["status"] == "stale"
    assert item["analysis"] is None


def test_pending_and_partial_use_exact_session_coverage(tmp_path):
    cfg, args = configured(tmp_path)
    change_state(
        args,
        lambda s: s["demos"].update(demo_0002={"run_id": "run-a", "state": "mirrored"}),
    )
    write_header(args["rollout_root"] / "model-a/task-a", "demo_0002", "run-a")
    item = lookup(cfg)
    assert item["status"] == "partial"
    assert item["analysis"]["cells"][0]["missing"]["unlabelled"] == 1
    (args["live_dir"] / "analysis.jsonl").unlink()
    assert lookup(cfg)["status"] == "waiting_annotation"


def test_source_removed_and_archived_are_explicit(tmp_path):
    cfg, args = configured(tmp_path)
    (args["rollout_root"] / "model-a/task-a/demo_0000/metadata.json").unlink()
    (args["rollout_root"] / "model-a/task-a/demo_0000").rmdir()
    (args["rollout_root"] / "model-a/task-a").rmdir()
    assert lookup(cfg)["status"] == "missing_source"
    (args["rollout_root"] / "model-a/task-a").mkdir()
    change_state(args, lambda s: s.update(archived=True))
    item = lookup(cfg)
    assert item["status"] == "archived"
    assert item["analysis"] is None


def test_real_annotation_run_links_and_reverse_lookup(tmp_path):
    cfg, args = configured(tmp_path)
    change_state(
        args,
        lambda s: s["demos"]["demo_0000"].update(
            episode_index=0,
            temporal={"run_id": "temporal-a", "changeset": "change-a"},
            verdict={"run_id": "verdict-a"},
        ),
    )
    state = json.loads((args["live_dir"] / "datasets/dataset-a.json").read_text())
    analysis.append(
        args["live_dir"],
        analysis.build_record(
            "dataset-a", "demo_0000", state["demos"]["demo_0000"], state=state, now=2
        ),
    )
    item = lookup(cfg)
    assert {r["run_id"] for r in item["runs"]} == {"temporal-a", "verdict-a"}
    assert (
        result_index.build(cfg, run_id="temporal-a")["items"][0]["identity"]["run_id"]
        == "run-a"
    )
    assert result_index.build(cfg, run_id="run-a")["items"] == []


def test_source_change_during_summary_discards_response(tmp_path, monkeypatch):
    cfg, args = configured(tmp_path)
    original = analysis.summarize

    def changing(*a, **kw):
        result = original(*a, **kw)
        change_state(args, lambda s: s["demos"]["demo_0000"].update(deleted=True))
        return result

    monkeypatch.setattr(analysis, "summarize", changing)
    item = lookup(cfg)
    assert item["status"] == "stale"
    assert item["analysis"] is None


def test_api_uses_same_identity_and_rejects_download_formats(tmp_path, monkeypatch):
    cfg, _ = configured(tmp_path)
    monkeypatch.setattr(api, "_result_config", lambda: cfg)
    args = {
        "root": cfg.watch.roots[0],
        "group": "model-a",
        "task": "task-a",
        "session": "run-a",
    }
    assert api.result_session(**args)["analysis"]["all"]["episodes"] == 1
    response = api.result_session_download(**args, format="json", lang="en")
    data = json.loads(response.body)
    assert data["source"]["kind"] == "current_session"
    assert data["analysis"]["all"]["episodes"] == 1
    with pytest.raises(HTTPException):
        api.result_session_download(**args, format="../../secret", lang="en")


@pytest.mark.parametrize(
    "extra",
    [
        {"aeri": {}},
        {"aeri": {"episode_role": "forward"}},
        {"episode_role": "forward"},
        {"verdict_by": "aeri"},
    ],
)
def test_legacy_session_cannot_hide_real_aeri_header(tmp_path, extra):
    cfg, args = configured(tmp_path)
    write_header(args["rollout_root"] / "model-a/task-a", "demo_0000", "run-a", **extra)
    item = lookup(cfg)
    assert item["status"] == "blocked"
    assert item["result_visibility"] == "withheld"
    assert item["analysis"] is None
    assert item["records"] == item["runs"] == item["reports"] == []


def test_missing_or_symlinked_header_cannot_authorize_analysis(tmp_path):
    cfg, args = configured(tmp_path)
    path = args["rollout_root"] / "model-a/task-a/demo_0000/metadata.json"
    path.unlink()
    assert lookup(cfg)["result_visibility"] == "withheld"
    external = tmp_path / "external.json"
    external.write_text(json.dumps({"eval": {"run_id": "run-a"}}))
    path.symlink_to(external)
    assert lookup(cfg)["analysis"] is None


def test_explicit_public_aeri_header_remains_scoped_and_blind_header_overrides(
    tmp_path,
):
    cfg, args = configured(tmp_path)
    change_session(args, aeri={}, analysis_visibility="public")
    write_header(
        args["rollout_root"] / "model-a/task-a",
        "demo_0000",
        "run-a",
        aeri={"episode_role": "forward"},
    )
    assert lookup(cfg)["result_visibility"] == "visible"
    write_header(
        args["rollout_root"] / "model-a/task-a",
        "demo_0000",
        "run-a",
        aeri={"blinded": True},
    )
    assert lookup(cfg)["result_visibility"] == "withheld"


def test_published_versions_are_not_current_downloads_and_stale_is_explicit(tmp_path):
    from levi.evaluation_results import export_live

    cfg, args = configured(tmp_path)
    args["root"] = cfg.workspace / "outputs/LEVI/evaluation-results"
    row = export_live(**args)
    item = lookup(cfg)
    assert item["reports"][0]["status"] == "current"
    assert item["reports"][0]["version"] == Path(row["report"]["directory"]).name
    assert item["reports"][0]["download_url"] is None
    change_session(args, updated_at=12345)
    item = lookup(cfg)
    assert item["result_visibility"] == "visible"
    assert item["reports"][0]["status"] == "stale"
    data = json.loads(result_index.download(item, format="json"))
    assert data["source"]["kind"] == "current_session"
    assert data["source"]["snapshot"] != item["reports"][0]["version"]


def test_catalog_external_report_paths_are_not_followed(tmp_path, monkeypatch):
    from levi import evaluation_results as catalog

    cfg, args = configured(tmp_path)
    external = tmp_path / "external-report"
    external.mkdir()
    fake = {
        "model": "model-a",
        "task": "task-a",
        "session": "run-a",
        "progress_only": False,
        "rollout": {
            "root": str(args["rollout_root"]),
            "directory": str(args["rollout_root"] / "model-a/task-a"),
        },
        "report": {"directory": str(external)},
        "basis": "operator",
    }
    monkeypatch.setattr(catalog, "entries", lambda root: [fake])
    read = result_index._Snapshot.read

    def guarded(self, path, **kw):
        assert not Path(path).is_relative_to(external)
        return read(self, path, **kw)

    monkeypatch.setattr(result_index._Snapshot, "read", guarded)
    assert lookup(cfg)["reports"] == []


def campaign_fixture(tmp_path, monkeypatch, state="REPORTED"):
    from levi.automatic.campaign import api as campaign_api

    cfg, args = configured(tmp_path)
    policy = {"config": "frozen-config", "checkpoint_dir": "/configured/checkpoint-a"}
    change_session(args, campaign_id="c001", policy=policy)
    plan = {
        "campaign_sha256": "a" * 64,
        "settings_sha256": "b" * 64,
        "campaign": {
            "primary": {"label_basis": "operator_label"},
            "arms": {
                "A": {"group": "model-a", "policy_forward": policy},
                "B": {
                    "group": "model-b",
                    "policy_forward": {
                        "config": "frozen-config-b",
                        "checkpoint_dir": "/configured/checkpoint-b",
                    },
                },
            },
        },
        "children": [{"run_id": "run-a", "arm": "A"}],
    }
    folder = tmp_path / "campaign"
    report = folder / "report/operator_label"
    (report / "data").mkdir(parents=True)
    manifest = {
        "schema": "levi.aeri.campaign_report_manifest.v1",
        "campaign_id": "c001",
        "campaign_state": "REPORTED",
        "progress_only": False,
        "campaign_sha256": "a" * 64,
        "settings_sha256": "b" * 64,
        "runs": [{"run_id": "run-a"}],
    }
    (report / "manifest.json").write_text(json.dumps(manifest))
    (report / "data/analysis.json").write_text(
        json.dumps({"campaign_statistic": "gated-only"})
    )
    c = SimpleNamespace(
        state=state,
        events=[],
        folder=folder,
        plan=plan,
        host_record={"rollout_root": str(args["rollout_root"])},
    )
    monkeypatch.setattr(campaign_api, "_locate", lambda identity, **_kw: c)
    return cfg, args, c, report


def test_campaign_reuses_original_gate_and_never_live_aggregates(tmp_path, monkeypatch):
    cfg, _args, _c, _report = campaign_fixture(tmp_path, monkeypatch)
    item = lookup(cfg)
    assert item["status"] == "reported"
    assert item["result_visibility"] == "visible"
    assert item["analysis"] is None
    assert item["records"] == item["runs"] == []
    assert item["comparison"]["status"] == "unknown"
    assert "coverage" in item["comparison"]["missing"]
    assert item["reports"][0]["api_url"] == "/api/levi/automatic/campaigns/c001/report"
    with pytest.raises(HTTPException) as exc:
        result_index.download(item)
    assert exc.value.status_code == 409


@pytest.mark.parametrize(
    "case", ["blind", "source", "policy", "run", "hash", "group", "progress"]
)
def test_campaign_unknown_mappings_and_blind_never_reveal(tmp_path, monkeypatch, case):
    cfg, args, c, report = campaign_fixture(tmp_path, monkeypatch)
    if case == "blind":
        c.state = "ARM_RUNNING"
    elif case == "source":
        c.host_record["rollout_root"] = str(tmp_path / "another-root")
    elif case == "policy":
        change_session(
            args,
            policy={"config": "wrong", "checkpoint_dir": "/configured/checkpoint-a"},
        )
    elif case == "run":
        c.plan["children"] = []
    elif case == "group":
        del c.plan["campaign"]["arms"]["A"]["group"]
    elif case == "hash":
        c.plan["settings_sha256"] = "c" * 64
    elif case == "progress":
        path = report / "manifest.json"
        value = json.loads(path.read_text())
        value["progress_only"] = True
        path.write_text(json.dumps(value))
    item = lookup(cfg)
    assert item["result_visibility"] == "withheld"
    assert item["analysis"] is None
    assert item["records"] == item["runs"] == item["reports"] == []


def test_labels_changed_after_analysis_are_stale(tmp_path):
    cfg, args = configured(tmp_path)
    change_state(
        args,
        lambda s: s["demos"]["demo_0000"].update(operator_label={"outcome": "failure"}),
    )
    item = lookup(cfg)
    assert item["status"] == "stale"
    assert item["result_visibility"] == "unknown"
    assert item["analysis"] is None


def test_config_lookup_does_not_remember_embedded_workspaces(tmp_path, monkeypatch):
    from levi.live import config as live_config
    from levi.live import locate
    from levi.pool import exclusions

    cfg, _args = configured(tmp_path)
    path = cfg.workspace / "live/effective.toml"
    path.write_text(live_config.render(live_config.Config()))
    monkeypatch.setattr(
        locate, "find", lambda own, **_kw: locate.Found(cfg.workspace, "env")
    )
    monkeypatch.setattr(locate, "is_live", lambda path: True)

    def writing(*a, **kw):
        raise AssertionError("Result GET attempted training-pool write")

    monkeypatch.setattr(exclusions, "remember", writing)
    assert api._result_config().workspace == cfg.workspace


def test_campaign_condition_differences_never_rank_policy_names(tmp_path, monkeypatch):
    cfg, _args, c, report = campaign_fixture(tmp_path, monkeypatch)
    c.plan["reset_mode"] = "operator_reset"
    manifest = json.loads((report / "manifest.json").read_text())
    manifest["runs"] = [
        {"run_id": "run-a", "reset_mode": "operator_reset", "scene_check": "verified"},
        {"run_id": "run-b", "reset_mode": "autonomous", "scene_check": "verified"},
    ]
    (report / "manifest.json").write_text(json.dumps(manifest))
    item = lookup(cfg)
    assert item["comparison"]["status"] == "incompatible"
    assert item["comparison"]["differences"] == ["reset_mode"]
    assert item["comparison"]["conditions"]["label_basis"] == "operator_label"


def test_campaign_report_change_discards_metadata_links(tmp_path, monkeypatch):
    from levi.automatic.campaign import api as campaign_api

    cfg, _args, _c, report = campaign_fixture(tmp_path, monkeypatch)
    original = campaign_api.get_report

    def changing(*a, **kw):
        value = original(*a, **kw)
        (report / "data/analysis.json").write_text(json.dumps({"changed": True}))
        return value

    monkeypatch.setattr(campaign_api, "get_report", changing)
    item = lookup(cfg)
    assert item["status"] == "stale"
    assert item["reports"] == []
    assert item["result_visibility"] == "unknown"


def test_two_same_task_sessions_are_selected_by_exact_run(tmp_path):
    cfg, args = configured(tmp_path)
    other = json.loads(args["session_file"].read_text())
    other.update(run_id="other-run", session_id="session-b")
    (args["session_file"].parent / "model-a__task-a__second.json").write_text(
        json.dumps(other)
    )
    change_state(
        args,
        lambda s: s["demos"]["demo_0001"].update(
            episode_index=0, operator_label={"outcome": "failure"}
        ),
    )
    write_header(args["rollout_root"] / "model-a/task-a", "demo_0001", "other-run")
    first = lookup(cfg)
    second = result_index.session(
        cfg,
        root=cfg.watch.roots[0],
        group="model-a",
        task="task-a",
        session="other-run",
    )
    assert first["analysis"]["all"]["success"]["n"] == 1
    assert second["analysis"]["all"]["success"]["n"] == 0
    assert len(result_index.build(cfg)["items"]) == 2


def test_duplicate_complete_identity_is_unknown(tmp_path):
    cfg, args = configured(tmp_path)
    (args["session_file"].parent / "duplicate.json").write_bytes(
        args["session_file"].read_bytes()
    )
    assert lookup(cfg)["status"] == "unknown"
    assert lookup(cfg)["analysis"] is None


def test_history_remains_discoverable_without_reopening_old_outcomes(
    tmp_path, monkeypatch
):
    from levi.evaluation_results import export_live

    cfg, args = configured(tmp_path)
    args["root"] = cfg.workspace / "outputs/LEVI/evaluation-results"
    row = export_live(**args)
    report = Path(row["report"]["directory"])
    change_session(args, run_id="replacement", session_id="new-session")
    original = result_index._Snapshot.read

    def guarded(self, path, **kw):
        assert not Path(path).is_relative_to(report)
        return original(self, path, **kw)

    monkeypatch.setattr(result_index._Snapshot, "read", guarded)
    items = result_index.build(cfg)["items"]
    history = next(r for r in items if r["identity"]["run_id"] == "run-a")
    assert history["status"] == "stale"
    assert history["result_visibility"] == "unknown"
    assert history["source"]["kind"] == "catalog_reference"
    assert history["source"]["catalog_versions"][0]["version"] == report.name
    assert history["analysis"] is None
    assert (
        history["records"]
        == history["runs"]
        == history["reports"]
        == history["viewers"]
        == []
    )


def test_replaced_source_inode_same_bytes_is_stale(tmp_path, monkeypatch):
    cfg, args = configured(tmp_path)
    path = args["rollout_root"] / "model-a/task-a/demo_0000/metadata.json"
    original = analysis.summarize

    def changing(*a, **kw):
        value = original(*a, **kw)
        copy = path.with_suffix(".replacement")
        copy.write_bytes(path.read_bytes())
        copy.replace(path)
        return value

    monkeypatch.setattr(analysis, "summarize", changing)
    assert lookup(cfg)["status"] == "stale"


def test_source_signature_changed_and_episode_mapping_changed_are_stale(tmp_path):
    from levi.live import mirror

    cfg, args = configured(tmp_path)
    source = args["rollout_root"] / "model-a/task-a/demo_0000"
    change_state(
        args,
        lambda s: s["demos"]["demo_0000"].update(sig=mirror.source_signature(source)),
    )
    assert lookup(cfg)["analysis"] is not None
    write_header(source.parent, "demo_0000", "run-a", extra_note="changed")
    assert lookup(cfg)["status"] == "stale"
    change_state(
        args, lambda s: s["demos"]["demo_0000"].update(sig=None, episode_index=42)
    )
    assert lookup(cfg)["status"] == "stale"


def linked_fixture(tmp_path, monkeypatch, embedded):
    import sqlite3

    from levi.live import backfill

    cfg, args = configured(tmp_path)
    cfg.service = SimpleNamespace(host="127.0.0.1", ui_port=7880)
    capture = cfg.workspace / "captures/dataset-a"
    capture.mkdir(parents=True)
    view = cfg.workspace / "outputs/LEVI/workbench/views/native-a"
    (view / "meta").mkdir(parents=True)
    (view / "meta/info.json").write_text(json.dumps({"fps": 10}))
    catalog = cfg.workspace / "outputs/LEVI/workbench/datasets.json"
    catalog.write_text(
        json.dumps(
            {
                "native-a": {
                    "id": "local/native-a",
                    "name": "native-a",
                    "kind": "raw",
                    "path": str(capture),
                    "view": str(view),
                    "view_status": "ready",
                }
            }
        )
    )
    change_state(args, lambda s: s.update(capture=str(capture)))
    change_state(
        args,
        lambda s: s["demos"]["demo_0000"].update(
            temporal={"run_id": "temporal-a", "changeset": "change-a"}
        ),
    )
    path = cfg.workspace / backfill.STORE
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE records(kind TEXT,id TEXT,body TEXT)")
        db.execute(
            "INSERT INTO records VALUES('runs','temporal-a',?)",
            (
                json.dumps(
                    {"id": "temporal-a", "context": {"repo_id": "local/native-a"}}
                ),
            ),
        )
    monkeypatch.setattr(api, "_result_config", lambda: cfg)
    monkeypatch.setattr(api, "_embedded", lambda path: embedded)
    monkeypatch.setattr(
        api,
        "_linked_id",
        lambda workspace, name, **_kw: "local/live.native-a" if embedded else None,
    )
    return cfg, args


@pytest.mark.parametrize("embedded", [False, True])
def test_native_and_product_alias_viewers_never_fabricate_run_workspace(
    tmp_path, monkeypatch, embedded
):
    cfg, _args = linked_fixture(tmp_path, monkeypatch, embedded)
    params = {
        "root": cfg.watch.roots[0],
        "group": "model-a",
        "task": "task-a",
        "session": "run-a",
    }
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    item = api.result_session(**params)
    assert item["result_visibility"] == "visible"
    viewer = item["viewers"][0]
    repo = "local/live.native-a" if embedded else "local/native-a"
    assert viewer["repo_id"] == repo
    assert viewer["native_repo_id"] == "local/native-a"
    assert viewer["viewer_href"] == "/" + repo + "/episode_0?live_session=run-a"
    run = item["runs"][0]
    assert run["repo_id"] == "local/native-a"
    assert (
        run["annotation_href"] is None
        if embedded
        else run["annotation_href"] == "/local/native-a?agent_run=temporal-a"
    )
    assert before == {
        str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    }
    assert (
        len(
            api.results_view(dataset=repo.removeprefix("local/"), run_id="temporal-a")[
                "items"
            ]
        )
        == 1
    )
    assert (
        api.results_view(dataset="live.dataset-a", run_id="temporal-a")["items"] == []
    )


def test_live_ui_run_link_requires_alive_ui_and_actual_store_binding(
    tmp_path, monkeypatch
):
    cfg, _args = linked_fixture(tmp_path, monkeypatch, True)
    monkeypatch.setattr(api, "_alive", lambda value, now: True)
    (cfg.live_dir / "status.json").write_text(
        json.dumps({"frontend": {"ui": True, "state": "ok"}})
    )
    params = {
        "root": cfg.watch.roots[0],
        "group": "model-a",
        "task": "task-a",
        "session": "run-a",
    }
    item = api.result_session(**params)
    assert (
        item["runs"][0]["annotation_href"]
        == "http://127.0.0.1:7880/local/native-a?agent_run=temporal-a"
    )
    from levi.live import backfill

    (cfg.workspace / backfill.STORE).unlink()
    assert api.result_session(**params)["runs"][0]["annotation_href"] is None


def test_catalog_source_mismatch_never_produces_viewer_or_alias_mapping(
    tmp_path, monkeypatch
):
    cfg, _args = linked_fixture(tmp_path, monkeypatch, True)
    catalog = cfg.workspace / "outputs/LEVI/workbench/datasets.json"
    value = json.loads(catalog.read_text())
    value["native-a"]["path"] = str(tmp_path / "wrong-capture")
    catalog.write_text(json.dumps(value))
    item = api.result_session(
        root=cfg.watch.roots[0], group="model-a", task="task-a", session="run-a"
    )
    assert item["viewers"][0]["viewer_href"] is None
    assert item["runs"][0]["annotation_href"] is None
    assert api.results_view(dataset="live.native-a", run_id="temporal-a")["items"] == []


def test_campaign_versions_unverified_and_differences_stay_explicit(
    tmp_path, monkeypatch
):
    cfg, _args, _c, report = campaign_fixture(tmp_path, monkeypatch)
    path = report / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["arms"] = [
        {"id": "A", "versions": "env-a", "not_verified": ["checkpoint_hash"]},
        {"id": "B", "versions": "env-b", "not_verified": []},
    ]
    path.write_text(json.dumps(manifest))
    item = lookup(cfg)
    assert item["comparison"]["status"] == "incompatible"
    assert "environment_versions" in item["comparison"]["differences"]
    assert "unverified_arms" in item["comparison"]["missing"]


def test_directory_request_reads_and_parses_each_source_once(tmp_path, monkeypatch):
    cfg, args = configured(tmp_path)
    template_session = json.loads(args["session_file"].read_text())
    template_state = json.loads((cfg.live_dir / "datasets/dataset-a.json").read_text())
    template_record = json.loads(
        (cfg.live_dir / analysis.FILE).read_text().splitlines()[0]
    )
    records = []
    # One hundred independent identities, each with one real source header.
    args["session_file"].unlink()
    (cfg.live_dir / "datasets/dataset-a.json").unlink()
    for index in range(100):
        task = f"task-{index:03d}"
        run = f"run-{index:03d}"
        dataset = f"dataset-{index:03d}"
        source = args["rollout_root"] / "model-a" / task
        write_header(source, "demo_0000", run)
        raw = {
            **template_session,
            "task_folder": task,
            "run_id": run,
            "session_id": run,
        }
        (args["session_file"].parent / f"{index:03d}.json").write_text(json.dumps(raw))
        state = {
            **template_state,
            "task_folder": task,
            "source": str(source),
            "demos": {
                "demo_0000": {**template_state["demos"]["demo_0000"], "run_id": run}
            },
        }
        (cfg.live_dir / "datasets" / f"{dataset}.json").write_text(json.dumps(state))
        records.append(
            {**template_record, "dataset": dataset, "task": task, "session": run}
        )
    (cfg.live_dir / analysis.FILE).write_text(
        "\n".join(json.dumps(r) for r in records) + "\n"
    )
    opened = []
    validated = []
    parsed = []
    real_open = result_index._open_below
    real_read = result_index._read_below
    real_load = json.loads

    def count_open(root, parts):
        opened.append(tuple(parts))
        return real_open(root, parts)

    def count_read(root, parts, limit):
        validated.append(tuple(parts))
        return real_read(root, parts, limit)

    def count_load(value, *a, **kw):
        parsed.append(1)
        return real_load(value, *a, **kw)

    monkeypatch.setattr(result_index, "_open_below", count_open)
    monkeypatch.setattr(result_index, "_read_below", count_read)
    monkeypatch.setattr(result_index.json, "loads", count_load)
    output = result_index.build(cfg)
    assert output["complete"] and output["truncated"] == []
    assert len(output["items"]) == 100
    assert all(item["analysis"]["all"]["episodes"] == 1 for item in output["items"])
    assert len(opened) == len(set(opened)) == 301
    assert len(validated) == 301
    assert len(parsed) == 400


def test_directory_limit_is_explicit_and_hides_incomplete_scope(tmp_path, monkeypatch):
    cfg, args = configured(tmp_path)
    (args["session_file"].parent / "duplicate.json").write_bytes(
        args["session_file"].read_bytes()
    )
    monkeypatch.setattr(result_index, "MAX_SESSIONS", 1)
    output = result_index.build(cfg)
    assert output["complete"] is False
    assert output["truncated"] == ["sessions"]
    assert output["limits"]["sessions_per_root"] == 1
    assert output["items"][0]["reason"] == "index_scan_truncated"
    assert output["items"][0]["analysis"] is None


def test_alive_ui_without_catalog_binding_preserves_summary_without_fake_link(
    tmp_path, monkeypatch
):
    cfg, _args = linked_fixture(tmp_path, monkeypatch, True)
    monkeypatch.setattr(api, "_alive", lambda value, now: True)
    (cfg.live_dir / "status.json").write_text(
        json.dumps({"frontend": {"ui": True, "state": "ok"}})
    )
    (cfg.workspace / "outputs/LEVI/workbench/datasets.json").write_text("{}")
    item = api.result_session(
        root=cfg.watch.roots[0], group="model-a", task="task-a", session="run-a"
    )
    assert item["result_visibility"] == "visible"
    assert item["viewers"][0]["annotation_base"] is None
    assert item["runs"][0]["annotation_href"] is None


def test_http_contract_and_unavailable_downloads(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    cfg, args = configured(tmp_path)
    monkeypatch.setattr(api, "_result_config", lambda: cfg)
    application = FastAPI()
    application.include_router(api.router)
    client = TestClient(application)
    params = {
        "root": cfg.watch.roots[0],
        "group": "model-a",
        "task": "task-a",
        "session": "run-a",
    }
    response = client.get("/api/levi/live/results/session", params=params)
    assert response.status_code == 200
    assert response.json()["result_visibility"] == "visible"
    response = client.get(
        "/api/levi/live/results/session/download", params={**params, "format": "json"}
    )
    assert (
        response.status_code == 200 and response.headers["cache-control"] == "no-store"
    )
    assert response.json()["identity"]["run_id"] == "run-a"
    change_session(args, state="running")
    response = client.get("/api/levi/live/results/session", params=params)
    assert response.json()["status"] == "active"
    assert (
        response.json()["records"]
        == response.json()["runs"]
        == response.json()["reports"]
        == []
    )
    assert (
        client.get("/api/levi/live/results/session/download", params=params).status_code
        == 409
    )
    assert (
        client.get(
            "/api/levi/live/results/session", params={**params, "root": "/tmp/outside"}
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/api/levi/live/results/session", params={**params, "task": "../task"}
        ).status_code
        == 400
    )


@pytest.mark.parametrize(
    "kind", ["request_bytes", "json_values", "record_lines", "file_bytes"]
)
def test_request_budget_is_explicit_and_fail_closed(tmp_path, monkeypatch, kind):
    cfg, _args = configured(tmp_path)
    opened = []
    real_read = result_index.os.read

    def limited(fd, count):
        opened.append(count)
        return real_read(fd, count)

    monkeypatch.setattr(result_index.os, "read", limited)
    if kind == "request_bytes":
        monkeypatch.setattr(result_index, "MAX_REQUEST_BYTES", 8)
    elif kind == "json_values":
        monkeypatch.setattr(result_index, "MAX_JSON_VALUES", 2)
    elif kind == "file_bytes":
        monkeypatch.setattr(result_index, "LIMIT", 8)
    else:
        monkeypatch.setattr(result_index, "MAX_RECORD_LINES", 0)
    output = result_index.build(cfg)
    assert output["complete"] is False
    assert kind in output["truncated"]
    for item in output["items"]:
        assert item["result_visibility"] == "unknown"
        assert item["analysis"] is None
        assert (
            item["records"] == item["runs"] == item["reports"] == item["viewers"] == []
        )
    if kind == "request_bytes":
        assert opened == []  # Size checks refuse files before allocating their bytes.
        assert output["limits"]["request_bytes"] == 8
    if kind == "record_lines":
        assert lookup(cfg)["reason"] == "index_scan_truncated"


def test_request_budget_applies_to_total_cached_files_not_only_each_file(
    tmp_path, monkeypatch
):
    cfg, args = configured(tmp_path)
    session_bytes = args["session_file"].stat().st_size
    state_bytes = (cfg.live_dir / "datasets/dataset-a.json").stat().st_size
    monkeypatch.setattr(result_index, "MAX_REQUEST_BYTES", session_bytes + state_bytes)
    snapshot = result_index._Snapshot()
    item = result_index.session(
        cfg,
        root=cfg.watch.roots[0],
        group="model-a",
        task="task-a",
        session="run-a",
        _snapshot=snapshot,
    )
    assert snapshot.bytes == session_bytes + state_bytes
    assert snapshot.truncated == {"request_bytes"}
    assert item["analysis"] is None and item["result_visibility"] == "unknown"


def test_run_body_respects_remaining_request_budget(tmp_path, monkeypatch):
    cfg, _args = linked_fixture(tmp_path, monkeypatch, False)
    snapshot = result_index._Snapshot()
    monkeypatch.setattr(result_index, "MAX_REQUEST_BYTES", 8)
    assert api._result_run_repo(cfg, "temporal-a", "local/native-a", snapshot) is None
    assert snapshot.truncated == {"run_store_bytes"}
    assert snapshot.bytes == 0


@pytest.mark.parametrize("where", ["meta", "eval"])
@pytest.mark.parametrize(
    "restricted",
    [
        {"blinded": True},
        {"operator_blind": True},
        {"campaign_id": "hidden"},
        None,
        "unknown",
    ],
)
def test_independent_aeri_markers_cannot_mask_each_other(tmp_path, where, restricted):
    cfg, args = configured(tmp_path)
    change_session(args, analysis_visibility="public")
    path = args["rollout_root"] / "model-a/task-a/demo_0000/metadata.json"
    top = {"episode_role": "forward"}
    nested = restricted
    if where == "meta":
        top, nested = nested, top
    path.write_text(
        json.dumps({"aeri": top, "eval": {"run_id": "run-a", "aeri": nested}})
    )
    item = lookup(cfg)
    assert item["result_visibility"] != "visible"
    assert item["records"] == item["runs"] == item["reports"] == []


def test_multiple_canonical_dataset_keys_preserve_result_and_deduplicate_capture(
    tmp_path, monkeypatch
):
    cfg, _args = linked_fixture(tmp_path, monkeypatch, False)
    (cfg.live_dir / "datasets/dataset-b.json").write_bytes(
        (cfg.live_dir / "datasets/dataset-a.json").read_bytes()
    )
    item = api.result_session(
        root=cfg.watch.roots[0], group="model-a", task="task-a", session="run-a"
    )
    assert item["result_visibility"] == "visible"
    assert item["identity"]["datasets"] == ["dataset-a", "dataset-b"]
    assert len({v["viewer_href"] for v in item["viewers"]}) == 1


def legacy_client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    cfg, args = configured(tmp_path)
    # One exact normal session, without the intentionally unmatched second-run row.
    path = cfg.live_dir / analysis.FILE
    path.write_text(path.read_text().splitlines()[0] + "\n")
    change_state(args, lambda s: s["demos"].pop("demo_0001", None))
    from levi.live import stats

    stats.record(
        cfg.live_dir,
        {
            "dataset": "dataset-a",
            "demo": "demo_0000",
            "session": "run-a",
            "at": 1,
            "result": {"state": "done", "verdict": {"outcome": "success"}},
        },
    )
    monkeypatch.setattr(api, "_result_config", lambda: cfg)
    application = FastAPI()
    application.include_router(api.router)
    return TestClient(application), cfg, args


@pytest.mark.parametrize("running", [False, True])
def test_real_legacy_handlers_and_active_privacy_field_remain_available(
    tmp_path, monkeypatch, running
):
    client, cfg, args = legacy_client(tmp_path, monkeypatch)
    if running:
        change_session(args, state="running")
    for endpoint, params in [
        ("stats", {"dataset": "dataset-a", "session": "run-a"}),
        ("stats/export", {"dataset": "dataset-a", "session": "run-a"}),
        ("analysis", {"group": "model-a", "task": "task-a"}),
    ]:
        response = client.get("/api/levi/live/" + endpoint, params=params)
        assert response.status_code == 200, response.text
        assert "success" in response.text
    item = lookup(cfg)
    assert item["statistics_visibility"] == "visible"
    assert item["identity"]["datasets"] == ["dataset-a"]
    assert item["identity"]["run_id"] == "run-a"
    if running:
        assert item["analysis"] is None and item["result_visibility"] == "withheld"


@pytest.mark.parametrize(
    "case", ["active_blind", "unknown_aeri", "campaign", "missing_header", "rebound"]
)
def test_real_old_handlers_close_restricted_or_unknown_sources(
    tmp_path, monkeypatch, case
):
    client, cfg, args = legacy_client(tmp_path, monkeypatch)
    if case == "active_blind":
        change_session(args, state="running", campaign_id="blind-campaign")
        write_header(
            args["rollout_root"] / "model-a/task-a",
            "demo_0000",
            "run-a",
            aeri={"blinded": True},
        )
    elif case == "unknown_aeri":
        write_header(
            args["rollout_root"] / "model-a/task-a", "demo_0000", "run-a", aeri={}
        )
    elif case == "campaign":
        change_session(args, campaign_id="c001")
    elif case == "missing_header":
        (args["rollout_root"] / "model-a/task-a/demo_0000/metadata.json").unlink()
    else:
        change_state(
            args, lambda s: s["demos"]["demo_0000"].update(run_id="replacement")
        )
    for endpoint, params in [
        ("stats", {"dataset": "dataset-a", "session": "run-a"}),
        ("stats/export", {"dataset": "dataset-a", "session": "run-a"}),
        ("analysis", {"group": "model-a", "task": "task-a"}),
    ]:
        response = client.get("/api/levi/live/" + endpoint, params=params)
        assert response.status_code == 409, response.text
        assert "success" not in response.text
    assert lookup(cfg)["statistics_visibility"] != "visible"


def test_old_global_mixed_unknown_scope_is_refused_without_hiding_exact_legacy(
    tmp_path, monkeypatch
):
    client, cfg, _args = legacy_client(tmp_path, monkeypatch)
    from levi.live import stats

    stats.record(
        cfg.live_dir,
        {
            "dataset": "unknown-dataset",
            "demo": "demo_0000",
            "session": "unknown-run",
            "at": 2,
            "result": {"state": "done", "verdict": {"outcome": "success"}},
        },
    )
    assert client.get("/api/levi/live/stats").status_code == 409
    assert client.get("/api/levi/live/stats/export").status_code == 409
    assert (
        client.get(
            "/api/levi/live/stats", params={"dataset": "dataset-a", "session": "run-a"}
        ).status_code
        == 200
    )


def test_old_handlers_discard_results_if_source_changes_during_summary(
    tmp_path, monkeypatch
):
    client, _cfg, args = legacy_client(tmp_path, monkeypatch)
    from levi.live import stats

    original = stats.summarize

    def changed(*a, **kw):
        result = original(*a, **kw)
        write_header(
            args["rollout_root"] / "model-a/task-a", "demo_0000", "replacement"
        )
        return result

    monkeypatch.setattr(stats, "summarize", changed)
    response = client.get(
        "/api/levi/live/stats", params={"dataset": "dataset-a", "session": "run-a"}
    )
    assert response.status_code == 409 and "success" not in response.text
