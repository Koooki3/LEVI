"""Real campaign/catalog/configuration reads share bounded request snapshots."""

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_result_index import (
    change_session,
    change_state,
    configured,
    legacy_client,
    linked_fixture,
    lookup,
)

from levi.live import api, result_index


def client_for(cfg, monkeypatch):
    monkeypatch.setattr(api, "_result_config", lambda: cfg)
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def deny_path_reads(monkeypatch, root):
    old_text, old_bytes = Path.read_text, Path.read_bytes

    def text(path, *a, **kw):
        assert not path.is_relative_to(root), "unbudgeted read_text"
        return old_text(path, *a, **kw)

    def binary(path, *a, **kw):
        assert not path.is_relative_to(root), "unbudgeted read_bytes"
        return old_bytes(path, *a, **kw)

    monkeypatch.setattr(Path, "read_text", text)
    monkeypatch.setattr(Path, "read_bytes", binary)


def real_campaign(tmp_path, monkeypatch):
    from levi.automatic.campaign import api as campaign_api
    from levi.automatic.campaign import controller, spec
    from levi.automatic.campaign.journal import CampaignJournal

    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "automatic/campaign"))
    from campaign_fixtures import FakePlanner, write_job
    from test_campaign_journal import launch, to_env_confirm, who

    cfg, args = configured(tmp_path)
    path = write_job(tmp_path / "job", campaign_id="c1", trials=2, segment_trials=2)
    plan = spec.plan_campaign(
        path, job_root=tmp_path / "jobs", planner=FakePlanner()
    ).to_json()
    plan["campaign"]["arms"]["A"]["group"] = "model-a"
    child = next(c for c in plan["children"] if c["arm"] == "A")
    child["run_id"] = "run-a"
    plan["campaign_sha256"] = spec.campaign_sha256(plan)
    change_session(
        args, campaign_id="c1", policy=plan["campaign"]["arms"]["A"]["policy_forward"]
    )
    home = tmp_path / "aeri"
    folder = home / "campaigns/c1"
    journal = CampaignJournal.create(folder, plan=plan, authority=who())
    to_env_confirm(journal)
    launch(journal)
    journal.move("SEGMENT_SEALED", "run_completed", authority=who(), segment=1)
    journal.move("SEGMENT_PREPARE", "segment_started", authority=who(), segment=2)
    to_env_confirm(journal, segment=2)
    launch(journal, segment=2)
    journal.move("SEGMENT_SEALED", "run_completed", authority=who(), segment=2)
    journal.move("ANALYZING", "campaign_complete", authority=who())
    journal.close()
    (folder / "ctl").mkdir(exist_ok=True)
    (folder / "ctl" / controller.HOST_FILE).write_text(
        json.dumps({"host": "guided", "rollout_root": str(args["rollout_root"])})
    )
    report = folder / "report/operator_label"
    (report / "data").mkdir(parents=True)
    (report / "manifest.json").write_text(
        json.dumps(
            {
                "schema": "levi.aeri.campaign_report_manifest.v1",
                "campaign_id": "c1",
                "campaign_state": "ANALYZING",
                "progress_only": False,
                "campaign_sha256": plan["campaign_sha256"],
                "settings_sha256": plan["settings_sha256"],
                "runs": [{"run_id": "run-a"}],
            }
        )
    )
    (report / "data/analysis.json").write_text(
        json.dumps({"header": {"coverage": {}}, "gated": True})
    )
    monkeypatch.setattr(campaign_api, "aeri_home", lambda: home)
    return cfg, args, folder, report


def test_real_campaign_preserves_hash_chain_and_original_report_gate(
    tmp_path, monkeypatch
):
    cfg, _args, folder, _report = real_campaign(tmp_path, monkeypatch)
    deny_path_reads(monkeypatch, folder)
    item = lookup(cfg)
    assert item["result_visibility"] == "visible"
    assert item["statistics_visibility"] == "withheld"
    assert item["analysis"] is None
    assert item["reports"][0]["basis"] == "operator_label"
    assert item["reports"][0]["api_url"].endswith("/c1/report")


@pytest.mark.parametrize("budget", ["bytes", "nodes", "records"])
def test_real_campaign_deep_reads_respect_all_request_budgets(
    tmp_path, monkeypatch, budget
):
    cfg, _args, folder, _report = real_campaign(tmp_path, monkeypatch)
    deny_path_reads(monkeypatch, folder)
    if budget == "bytes":
        monkeypatch.setattr(result_index, "MAX_REQUEST_BYTES", 2048)
    elif budget == "nodes":
        monkeypatch.setattr(result_index, "MAX_JSON_VALUES", 200)
    else:
        monkeypatch.setattr(result_index, "MAX_RECORD_LINES", 3)
    out = result_index.build(cfg)
    assert out["complete"] is False
    assert {"request_bytes", "json_values", "record_lines"} & set(out["truncated"])
    assert all(
        i["result_visibility"] == i["statistics_visibility"] == "unknown"
        for i in out["items"]
    )
    assert all(
        i["analysis"] is None and not i["reports"] and not i["runs"]
        for i in out["items"]
    )


@pytest.mark.parametrize(
    "file", ["plan.json", "journal.jsonl", "report/operator_label/data/analysis.json"]
)
def test_campaign_source_generation_change_closes_all_links(
    tmp_path, monkeypatch, file
):
    from levi.automatic.campaign import api as campaign_api

    cfg, _args, folder, _report = real_campaign(tmp_path, monkeypatch)
    old = campaign_api.get_report

    def changed(*a, **kw):
        value = old(*a, **kw)
        path = folder / file
        path.write_bytes(path.read_bytes() + b" ")
        return value

    monkeypatch.setattr(campaign_api, "get_report", changed)
    item = lookup(cfg)
    assert item["status"] == "stale"
    assert item["statistics_visibility"] == item["result_visibility"] == "unknown"
    assert not item["reports"]


def foreign_catalog(tmp_path, monkeypatch, padding=None):
    from levi import catalog, links

    old_link = api._linked_id
    cfg, args = linked_fixture(tmp_path, monkeypatch, True)
    monkeypatch.setattr(api, "_linked_id", old_link)
    foreign = tmp_path / "foreign-live"
    path = foreign / links.CATALOG
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"ignored": {"base": True, "padding": padding or []}}))
    monkeypatch.setattr(links, "workspaces", lambda **kw: [cfg.workspace, foreign])
    monkeypatch.setattr(catalog, "STATE", tmp_path / "product/workbench")
    return cfg, args, foreign, path


def test_real_foreign_catalog_obeys_read_budget_before_allocation(
    tmp_path, monkeypatch
):
    cfg, _args, foreign, _path = foreign_catalog(tmp_path, monkeypatch, ["x"] * 2000)
    monkeypatch.setattr(result_index, "LIMIT", 1024)
    monkeypatch.setattr(result_index, "MAX_REQUEST_BYTES", 4096)
    deny_path_reads(monkeypatch, foreign)
    out = result_index.build(cfg, linker=api._result_linker)
    assert out["complete"] is False and "file_bytes" in out["truncated"]
    assert all(i["analysis"] is None and not i["viewers"] for i in out["items"])


def test_real_foreign_catalog_nodes_and_source_generation_are_pinned(
    tmp_path, monkeypatch
):
    from levi import links

    cfg, _args, _foreign, path = foreign_catalog(tmp_path, monkeypatch)
    old = links.product_id

    def changed(*a, **kw):
        value = old(*a, **kw)
        path.write_text(json.dumps({"changed": True}))
        return value

    monkeypatch.setattr(links, "product_id", changed)
    item = api.result_session(
        root=cfg.watch.roots[0], group="model-a", task="task-a", session="run-a"
    )
    assert item["status"] == "stale" and item["viewers"] == []


def test_validation_rejects_larger_rebound_before_reading_new_bytes(
    tmp_path, monkeypatch
):
    import os

    cfg, args = configured(tmp_path)
    old = result_index.analysis.summarize
    source = args["rollout_root"] / "model-a/task-a/demo_0000/metadata.json"
    previous_size = source.stat().st_size

    def changed(*a, **kw):
        value = old(*a, **kw)
        source.write_bytes(b"x" * 100_000)
        return value

    real_read = os.read
    reads = []

    def read(fd, n):
        if os.fstat(fd).st_size == 100_000:
            reads.append(n)
        return real_read(fd, n)

    monkeypatch.setattr(result_index.analysis, "summarize", changed)
    monkeypatch.setattr(os, "read", read)
    assert lookup(cfg)["status"] == "stale"
    assert not reads and previous_size < 100_000


def test_old_analysis_mixed_unknown_header_is_refused_at_real_http_route(
    tmp_path, monkeypatch
):
    client, _cfg, args = legacy_client(tmp_path, monkeypatch)
    change_state(
        args,
        lambda s: s["demos"].update(
            demo_0002={"state": "mirrored", "run_id": "unknown"}
        ),
    )
    response = client.get("/api/levi/live/analysis")
    assert response.status_code == 409 and "success" not in response.text


def test_readonly_loader_is_never_a_public_http_parameter():
    from levi.automatic.campaign import api as campaign_api

    app = FastAPI()
    app.include_router(api.router)
    app.include_router(campaign_api.router)
    for path, methods in app.openapi()["paths"].items():
        if "get" in methods and ("/results" in path or path.endswith("/report")):
            names = {p["name"] for p in methods["get"].get("parameters", [])}
            assert not names & {
                "loader",
                "_loader",
                "sources",
                "_campaign",
                "_snapshot",
            }


@pytest.mark.parametrize(
    "aggregate",
    [{"discarded": 1}, {"incomplete": {"count": 1}}, {"incomplete": {"fr3_fault": 1}}],
)
def test_unbound_legacy_aggregate_cannot_bypass_real_analysis_gate(
    tmp_path, monkeypatch, aggregate
):
    client, _cfg, args = legacy_client(tmp_path, monkeypatch)
    change_state(args, lambda s: s.update(aggregate))
    response = client.get("/api/levi/live/analysis")
    assert response.status_code == 409 and "success" not in response.text
    assert response.json()["detail"]["reason"] == "statistics_aggregate_source_unknown"


def test_pending_unknown_other_dataset_does_not_supply_filtered_session_end(
    tmp_path, monkeypatch
):
    client, cfg, _args = legacy_client(tmp_path, monkeypatch)
    unknown = json.loads((cfg.live_dir / "datasets/dataset-a.json").read_text())
    unknown.update(source=str(tmp_path / "foreign-root/model-a/task-a"))
    (cfg.live_dir / "datasets/unknown.json").write_text(json.dumps(unknown))
    response = client.get("/api/levi/live/stats?dataset=dataset-a&session=run-a")
    assert response.status_code == 200
    assert response.json()["datasets"] == ["dataset-a"]


@pytest.mark.parametrize("budget", ["bytes", "nodes"])
def test_actual_readonly_toml_configuration_shares_request_budget(
    tmp_path, monkeypatch, budget
):
    from levi.live import config as live_config
    from levi.live import locate

    cfg, _args = configured(tmp_path)
    value = live_config.Config()
    value.watch.roots = cfg.watch.roots
    value.service.workspace = str(cfg.workspace)
    (cfg.live_dir / "effective.toml").write_text(live_config.render(value))
    monkeypatch.setattr(
        locate, "find", lambda own, **kw: locate.Found(cfg.workspace, "env")
    )
    monkeypatch.setattr(locate, "is_live", lambda path: True)
    app = FastAPI()
    app.include_router(api.router)
    if budget == "bytes":
        monkeypatch.setattr(result_index, "MAX_REQUEST_BYTES", 32)
    else:
        monkeypatch.setattr(result_index, "MAX_JSON_VALUES", 3)
    body = TestClient(app).get("/api/levi/live/results").json()
    assert body["complete"] is False and body["items"] == []
    assert ("request_bytes" if budget == "bytes" else "json_values") in body[
        "truncated"
    ]


def test_real_campaign_invalid_plan_hash_does_not_authorize_report(
    tmp_path, monkeypatch
):
    cfg, _args, folder, _report = real_campaign(tmp_path, monkeypatch)
    path = folder / "plan.json"
    plan = json.loads(path.read_text())
    plan["campaign_sha256"] = "0" * 64
    path.write_text(json.dumps(plan))
    item = lookup(cfg)
    assert item["result_visibility"] != "visible" and not item["reports"]


def test_json_node_budget_is_checked_before_decoder_allocation(monkeypatch):
    snapshot = result_index._Snapshot()
    monkeypatch.setattr(result_index, "MAX_JSON_VALUES", 3)

    def forbidden(*a, **kw):
        raise AssertionError("Decoder allocated an over-budget value tree")

    monkeypatch.setattr(result_index.json, "loads", forbidden)
    with pytest.raises(ValueError):
        snapshot.decode(b"[0,0,0,0]")
    assert snapshot.truncated == {"json_values"}


def test_actual_filtered_stats_datasets_are_exact_authorized_scope(
    tmp_path, monkeypatch
):
    client, cfg, _args = legacy_client(tmp_path, monkeypatch)
    from levi.live import stats

    stats.record(
        cfg.live_dir,
        {
            "dataset": "foreign-dataset",
            "demo": "demo_0000",
            "session": "foreign-run",
            "at": 1,
            "result": {"state": "done"},
        },
    )
    for path in ("stats", "stats/export"):
        response = client.get(
            "/api/levi/live/" + path,
            params={"dataset": "dataset-a", "session": "run-a"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["datasets"] == ["dataset-a"]
        assert body["scope"]["dataset"] == "dataset-a"
        assert all(
            r["dataset"] == "dataset-a" and r["session"] == "run-a"
            for r in body["episodes"]["rows"]
        )


@pytest.mark.parametrize(
    "marker",
    [
        {"episode_role": "unknown"},
        {"campaign_id": []},
        {"blinded": None},
        {"analysis_visibility": []},
    ],
)
def test_malformed_independent_marker_cannot_authorize_public_analysis(
    tmp_path, marker
):
    cfg, args = configured(tmp_path)
    change_session(args, analysis_visibility="public")
    path = args["rollout_root"] / "model-a/task-a/demo_0000/metadata.json"
    path.write_text(
        json.dumps(
            {
                "aeri": {"episode_role": "forward"},
                "eval": {"run_id": "run-a", "aeri": marker},
            }
        )
    )
    item = lookup(cfg)
    assert item["result_visibility"] != "visible"
    assert item["statistics_visibility"] != "visible"


def test_active_session_without_its_own_readable_header_is_not_trusted(tmp_path):
    cfg, args = configured(tmp_path)
    change_session(args, state="running")
    change_state(args, lambda s: s.update(demos={}))
    item = lookup(cfg)
    assert item["statistics_visibility"] != "visible"


def test_malformed_canonical_session_aeri_is_withheld(tmp_path):
    cfg, args = configured(tmp_path)
    change_session(args, aeri="unknown", analysis_visibility="public")
    assert lookup(cfg)["result_visibility"] != "visible"


def test_config_byte_budget_precedes_toml_parse_and_source_allocation(
    tmp_path, monkeypatch
):
    from levi.live import locate

    cfg, _args = configured(tmp_path)
    path = cfg.live_dir / "effective.toml"
    path.write_text("#" + "x" * result_index.MAX_CONFIG_BYTES)
    monkeypatch.setattr(
        locate, "find", lambda own, **kw: locate.Found(cfg.workspace, "env")
    )
    monkeypatch.setattr(locate, "is_live", lambda path: True)

    def forbidden(*a, **kw):
        raise AssertionError("TOML decoder allocated an oversized configuration")

    monkeypatch.setattr(api.tomllib, "loads", forbidden)
    app = FastAPI()
    app.include_router(api.router)
    body = TestClient(app).get("/api/levi/live/results").json()
    assert body["complete"] is False and "config_bytes" in body["truncated"]
    assert body["limits"]["config_bytes"] == 65536
    assert body["items"] == []


def test_actual_stats_source_identities_close_between_request_root_rebinding(
    tmp_path, monkeypatch
):
    from test_result_index import write_header

    client, cfg, args = legacy_client(tmp_path, monkeypatch)
    selected = lookup(cfg)["identity"]
    params = {"dataset": "dataset-a", "session": "run-a"}
    initial = client.get("/api/levi/live/stats", params=params).json()
    assert initial["source_identities"] == [
        {
            "root": selected["root"],
            "group": selected["group"],
            "task": selected["task"],
            "run_id": selected["run_id"],
            "session_id": selected["session_id"],
            "datasets": ["dataset-a"],
        }
    ]
    other = tmp_path / "root-b"
    write_header(other / "model-a/task-a", "demo_0000", "run-a")
    session_dir = other / ".eval_sessions"
    session_dir.mkdir()
    (session_dir / "same.json").write_bytes(args["session_file"].read_bytes())
    cfg.watch.roots.append(str(other))
    change_state(
        args, lambda s: s.update(root=str(other), source=str(other / "model-a/task-a"))
    )
    for endpoint in ("stats", "stats/export"):
        response = client.get("/api/levi/live/" + endpoint, params=params)
        assert response.status_code == 200
        actual = response.json()["source_identities"]
        assert len(actual) == 1 and actual[0]["root"] == str(other)
        assert actual[0]["root"] != selected["root"]
        assert actual[0]["datasets"] == ["dataset-a"]
    assert lookup(cfg)["statistics_visibility"] != "visible"
