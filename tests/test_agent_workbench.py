"""Hardware-free Agent contract/transaction tests. Never import a model runtime."""

import sys

import pytest

from levi.agent.capabilities import invoke
from levi.agent.runtime import Workbench
from levi.agent.schema import ModelOutput, ProviderConfig, TaskContext
from levi.agent.security import Principal, endpoint_addresses
from levi.agent.store import Conflict, Store, file_hash, resolve


@pytest.fixture(autouse=True)
def no_model_runtime(monkeypatch):
    import builtins

    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.split(".")[0] in {"torch", "sam3", "tensorflow", "jax", "pydantic_ai"}:
            raise AssertionError(
                "Real model/accelerator import forbidden in Agent tests"
            )
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)


class GroundedFixture:
    def generate(self, config, instruction, summary, evidence, artifacts, budget):
        return ModelOutput.model_validate(
            {
                "summary": "Fixture only",
                "proposals": [
                    {
                        "kind": "segment",
                        "episode_index": summary["episode_index"],
                        "content": "Move gripper",
                        "start": summary["start"],
                        "end": summary["end"],
                        "evidence_ids": [evidence[0]["id"]],
                    }
                ],
            }
        ), {"requests": 1, "tokens": 17}


@pytest.fixture
def bench(client, dataset, monkeypatch):
    monkeypatch.delenv("LEVI_MODEL_API_KEY", raising=False)
    from levi import catalog, service

    entry = catalog.register(str(dataset))
    wb = Workbench(service.STATE, provider=GroundedFixture())
    wb.store.put(
        "providers",
        "fixture",
        ProviderConfig(
            name="fixture",
            base_url="https://example.invalid/v1",
            model="fixture",
            tools=True,
        ).model_dump(),
    )
    context = TaskContext(
        repo_id=entry["id"],
        episodes=[0, 1],
        instruction="Review motion",
        provider="fixture",
    )
    return wb, context


def execute(wb, run, pilot=True):
    from levi.agent.planning import approve, pilot_review

    if not run.get("plan", {}).get("approval"):
        run = approve(wb, run["id"], 1, "fixture-human")
    if not pilot and not run["plan"].get("pilot_review"):
        if not run["completed"]:
            run = execute(wb, run)
        if run["status"] == "waiting_for_review":
            change = wb.store.get("changes", run["changes"])
            run = pilot_review(
                wb,
                run["id"],
                change["revision"],
                True,
                "Fixture pilot verified",
                "fixture-human",
            )
    assert wb.store.claim(run["id"], "test-owner")
    wb.execute(run["id"], "test-owner", pilot=pilot)
    return wb.store.get("runs", run["id"])


def test_full_pilot_review_commit_and_legacy_read(bench, dataset, client):
    wb, context = bench
    source_before = {str(p): file_hash(p) for p in dataset.rglob("*") if p.is_file()}
    run = execute(wb, wb.plan(context))
    assert run["status"] == "waiting_for_review", run
    assert run["completed"] == [0]
    change = wb.store.get("changes", run["changes"])
    human = Principal("tester", human=True)
    assert invoke(wb, human, "changes.validate", {"changeset_id": change["id"]})[
        "unprocessed"
    ] == [1]
    invoke(wb, human, "changes.approve", {"changeset_id": change["id"], "revision": 0})
    receipt = invoke(
        wb,
        human,
        "changes.commit",
        {"changeset_id": change["id"], "revision": 0},
        "once",
    )
    assert wb.commit(change["id"], "once", 0) == receipt
    assert wb.store.head(run["dataset_key"]) == receipt["revision"]
    got = client.get(
        "/annotations/api/episodes/0/atoms", params={"repo_id": context.repo_id}
    )
    assert got.status_code == 200, got.text
    assert len(got.json()["atoms"]) == 1
    assert got.headers["x-levi-annotation-revision"] == receipt["revision"]
    payload = {
        "repo_id": context.repo_id,
        "episode_index": 0,
        "atoms": got.json()["atoms"],
    }
    assert (
        client.post("/annotations/api/episodes/0/atoms", json=payload).status_code
        == 428
    )
    assert (
        client.post(
            "/annotations/api/episodes/0/atoms",
            json=payload,
            headers={"x-levi-annotation-revision": "stale"},
        ).status_code
        == 409
    )
    saved = client.post(
        "/annotations/api/episodes/0/atoms",
        json=payload,
        headers={"x-levi-annotation-revision": receipt["revision"]},
    )
    assert saved.status_code == 200, saved.text
    assert {
        str(p): file_hash(p) for p in dataset.rglob("*") if p.is_file()
    } == source_before


def test_resume_does_not_repeat_completed_shard(bench):
    wb, context = bench
    run = execute(wb, wb.plan(context))
    first = wb.store.get("shards", f"{run['id']}:0")
    run = execute(wb, run, pilot=False)
    assert run["completed"] == [0, 1]
    assert run["requests"] == 2
    assert wb.store.get("shards", f"{run['id']}:0") == first
    events = wb.store.events(run["id"])
    assert all(a["seq"] < b["seq"] for a, b in __import__("itertools").pairwise(events))
    assert wb.store.events(run["id"], events[-1]["seq"]) == []


def test_scope_and_human_approval_cannot_be_escalated(bench):
    wb, context = bench
    run = execute(wb, wb.plan(context))
    external = Principal("external", datasets=(context.repo_id,))
    with pytest.raises(PermissionError):
        invoke(
            wb,
            external,
            "changes.approve",
            {"changeset_id": run["changes"], "revision": 0},
        )
    with pytest.raises(PermissionError):
        invoke(wb, Principal("other"), "runs.get", {"run_id": run["id"]})
    tools = invoke(wb, external, "capabilities.list", {})["tools"]
    assert not next(t for t in tools if t["name"] == "changes.commit")["available"]


def test_source_content_change_detected_even_with_preserved_stat(bench, dataset):
    import os

    wb, context = bench
    run = execute(wb, wb.plan(context))
    path = dataset / "meta/tasks.jsonl"
    before = path.stat()
    path.write_text(path.read_text().replace("gripper", "GRIPPER"))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(ValueError, match="Source changed"):
        wb.validate(wb.store.get("changes", run["changes"]))


def test_human_save_after_plan_blocks_agent(bench, client):
    wb, context = bench
    run = execute(wb, wb.plan(context))
    saved = client.post(
        "/annotations/api/episodes/0/atoms",
        json={
            "repo_id": context.repo_id,
            "episode_index": 0,
            "atoms": [
                {
                    "role": "user",
                    "style": "subtask",
                    "content": "Human correction",
                    "timestamp": 0,
                }
            ],
        },
    )
    assert saved.status_code == 200
    with pytest.raises(Conflict):
        wb.validate(wb.store.get("changes", run["changes"]))


def test_bundle_failure_never_exposes_partial_files(tmp_path, monkeypatch):
    store = Store(tmp_path)
    base, version, folder = store.prepare("dataset")
    (folder / "annotations").mkdir()
    (folder / "annotations/episode_000000.json").write_text("{}")

    def fail(*args):
        raise OSError("injected database write failure")

    monkeypatch.setattr(store, "save", fail)
    with pytest.raises(OSError):
        store.publish(
            "dataset",
            base,
            version,
            "request",
            "digest",
            {"ok": True},
            change={"id": "c"},
        )
    assert store.head("dataset") == "legacy"
    assert store.receipt("request", "digest") is None
    assert (
        resolve(tmp_path, "dataset", "annotations") == tmp_path / "annotations/dataset"
    )


def test_budget_and_cancel_are_durable(bench):
    wb, context = bench
    context.budget.max_calls = 1
    run = execute(wb, wb.plan(context), pilot=False)
    assert run["status"] == "blocked"
    assert run["completed"] == [0]
    assert run["requests"] == 1
    wb.control(run["id"], "cancel")
    assert Store(wb.store.state).get("runs", run["id"])["status"] == "cancelled"
    partial = invoke(
        wb, Principal("human", human=True), "runs.finish", {"run_id": run["id"]}
    )
    assert partial["status"] == "cancelled" and partial["changes"]
    assert partial["requests"] == 1


def test_recovery_respects_live_lease(tmp_path):
    store = Store(tmp_path)
    store.put("runs", "live", {"id": "live", "status": "running"})
    store.put("runs", "dead", {"id": "dead", "status": "running"})
    store.claim("live", "other")
    store.recover()
    assert store.get("runs", "live")["status"] == "running"
    assert store.get("runs", "dead")["status"] == "interrupted"


def test_egress_rejects_metadata_private_and_redirect_credentials(monkeypatch):
    import socket

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(2, 1, 6, "", ("169.254.169.254", 443))],
    )
    with pytest.raises(ValueError):
        endpoint_addresses("https://host.invalid/v1", True)
    with pytest.raises(ValueError):
        endpoint_addresses("https://user:secret@example.com/v1")
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **kw: [(2, 1, 6, "", ("127.0.0.1", 8000))]
    )
    with pytest.raises(ValueError):
        endpoint_addresses("http://localhost:8000/v1")
    assert endpoint_addresses("http://localhost:8000/v1", True) == {"127.0.0.1"}


def test_no_invalid_intervals_or_unreviewed_outcome(bench):
    from levi.agent.schema import Proposal

    with pytest.raises(ValueError):
        Proposal(
            episode_index=0,
            kind="segment",
            content="x",
            start=1,
            end=1,
            evidence_ids=["e"],
        )
    with pytest.raises(ValueError):
        Proposal(
            episode_index=0,
            kind="event",
            content="x",
            start=1,
            end=2,
            evidence_ids=["e"],
        )
    wb, context = bench
    run = execute(wb, wb.plan(context))
    with pytest.raises(Conflict):
        wb.commit(run["changes"], "not-approved", 0)


def test_legacy_routes_cannot_bypass_external_scope(client, monkeypatch):
    monkeypatch.setenv("LEVI_AGENT_TOKEN", "test-scoped-token")
    monkeypatch.setenv("LEVI_UI_TOKEN", "test-ui-token")
    auth = {"Authorization": "Bearer test-scoped-token"}
    assert (
        client.post(
            "/annotations/api/episodes/0/atoms", headers=auth, json={}
        ).status_code
        == 403
    )
    assert client.get("/api/levi/catalog").status_code == 401
    assert (
        client.get("/api/levi/agent/v1/capabilities", headers=auth).status_code == 200
    )


def test_dependency_free_import():
    assert not any(name in sys.modules for name in ["torch", "sam3"])


def test_named_paths_and_cache_account_isolation(bench, tmp_path):
    from levi.naming import hub_cache_directory

    wb, context = bench
    run = wb.plan(context)
    directory = wb.store.run_dir(run["id"])
    assert directory.parts[-4:] == ("datasets", run["dataset_key"], "runs", run["id"])
    # dataset (the parent) + task kind + timestamp, and never a digest.
    kind, _, stamp = run["id"].partition("-")
    assert kind == run["context"]["workflow"]["kind"]
    assert stamp.split("-")[0].replace("T", "").isdigit()
    first = hub_cache_directory(
        tmp_path, "owner/robot", "main", "account-fingerprint-a"
    )
    assert first == tmp_path / "owner__robot/account-0001/revision-0001"
    assert (
        hub_cache_directory(tmp_path, "owner/robot", "main", "account-fingerprint-a")
        == first
    )
    assert (
        hub_cache_directory(tmp_path, "owner/robot", "main", "account-fingerprint-b")
        != first
    )
    assert (
        hub_cache_directory(
            tmp_path, "owner/robot", "commit-digest", "account-fingerprint-a"
        )
        == tmp_path / "owner__robot/account-0001/revision-0002"
    )


def test_review_decisions_survive_remaining_execution(bench):
    wb, ctx = bench
    run = execute(wb, wb.plan(ctx))
    change = wb.store.get("changes", run["changes"])
    human = Principal("reviewer", human=True)
    proposals = change["proposals"]
    proposals[0]["content"] = "Human corrected instruction"
    changed = invoke(
        wb,
        human,
        "changes.edit",
        {"changeset_id": change["id"], "revision": 0, "proposals": proposals},
    )
    invoke(
        wb,
        human,
        "changes.review",
        {
            "changeset_id": change["id"],
            "revision": changed["revision"],
            "indices": [0],
            "decision": "rejected",
        },
    )
    run = execute(wb, run, pilot=False)
    revised = wb.store.get("changes", run["changes"])
    assert revised["proposals"][0]["content"] == "Human corrected instruction"
    assert revised["decisions"] == {"0": "rejected"}
    assert len(revised["proposals"]) == 2
    invoke(
        wb,
        human,
        "changes.approve",
        {"changeset_id": revised["id"], "revision": revised["revision"]},
    )
    wb.commit(revised["id"], "reviewed", revised["revision"])
    root = resolve(wb.store.state, run["dataset_key"], "annotations")
    assert not (root / "episode_000000.json").exists()
    assert (root / "episode_000001.json").exists()


def test_external_workflow_needs_no_model_and_undo_is_reviewed(bench):
    wb, ctx = bench
    ctx.provider = "external"
    external = Principal("client", datasets=(ctx.repo_id,))
    run = invoke(wb, external, "runs.plan", ctx.model_dump())
    invoke(
        wb,
        Principal("human", human=True),
        "plans.approve",
        {"run_id": run["id"], "revision": 1},
    )
    invoke(wb, external, "runs.prepare", {"run_id": run["id"]})
    refs = invoke(wb, external, "media.sample", {"run_id": run["id"]})["items"]
    change = invoke(
        wb,
        external,
        "annotations.propose_segments",
        {
            "run_id": run["id"],
            "inspected_episodes": [0],
            "proposals": [
                {
                    "episode_index": 0,
                    "kind": "segment",
                    "content": "External grounded draft",
                    "start": 0,
                    "end": 1,
                    "evidence_ids": [refs[0]["id"]],
                }
            ],
        },
    )
    human = Principal("reviewer", human=True)
    invoke(wb, human, "changes.approve", {"changeset_id": change["id"], "revision": 0})
    wb.commit(change["id"], "external-review", 0)
    inverse = invoke(wb, human, "changes.undo", {"changeset_id": change["id"]})
    assert inverse["status"] == "draft"
    with pytest.raises(Conflict):
        wb.commit(inverse["id"], "undo", 0)
    invoke(wb, human, "changes.approve", {"changeset_id": inverse["id"], "revision": 0})
    wb.commit(inverse["id"], "undo", 0)
    assert not (
        resolve(wb.store.state, run["dataset_key"], "annotations")
        / "episode_000000.json"
    ).exists()


def test_session_key_is_not_persisted_and_disconnects(bench, client):
    from levi.agent.credentials import get

    wb, _ = bench
    prefix = "/api/levi/agent/v1/providers/fixture"
    assert (
        client.post(
            prefix + "/session", json={"key": "fixture-secret-only"}
        ).status_code
        == 200
    )
    config = ProviderConfig.model_validate(wb.store.get("providers", "fixture"))
    assert get(config) == "fixture-secret-only"
    assert (
        b"fixture-secret-only" not in (wb.store.root / "workbench.sqlite3").read_bytes()
    )
    assert "fixture-secret-only" not in client.get("/api/levi/agent/v1/providers").text
    assert client.post(prefix + "/disconnect").status_code == 200
    assert get(config) is None
    assert not wb.store.get("providers", "fixture")["enabled"]


def test_new_annotation_kind_uses_registry_without_runtime_change(monkeypatch):
    from levi.agent.formats import ANNOTATIONS, AnnotationKind
    from levi.agent.schema import Proposal

    monkeypatch.setitem(
        ANNOTATIONS,
        "instruction",
        AnnotationKind("instruction", "language", "language_persistent", False),
    )
    suggestion = Proposal(
        episode_index=0,
        kind="instruction",
        start=0,
        end=1,
        content="registered",
        evidence_ids=["e"],
    )
    assert ANNOTATIONS[suggestion.kind].paths(suggestion.model_dump()) == [
        "annotations/episode_000000.json"
    ]


def test_snapshot_plan_checks_content_before_copy(bench, dataset):
    wb, ctx = bench
    run = wb.plan(ctx)
    path = dataset / "meta/tasks.jsonl"
    path.write_text(path.read_text().replace("gripper", "GRIPPER"))
    run = execute(wb, run)
    assert run["status"] == "blocked"
    assert not run["completed"]
    assert "content changed" in run["reason"]


def test_nested_validation_cannot_commit_outer_transaction(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(RuntimeError), store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        store.put("test", "outer", {"visible": False})
        Store(tmp_path).get("test", "outer")
        raise RuntimeError("failure after nested read")
    with pytest.raises(KeyError):
        Store(tmp_path).get("test", "outer")


def test_sam3_staging_review_and_composite_publication(bench, dataset):
    import json

    import cv2
    import numpy as np

    from levi.agent.objects import ObjectRequest, plan
    from levi.annotations.sam3_protocol import fake_annotations
    from levi.annotations.schema import Sam3Plan
    from levi.annotations.sidecar import SidecarStore

    wb, ctx = bench
    camera = "observation.images.front"
    info_path = dataset / "meta/info.json"
    info = json.loads(info_path.read_text())
    info["features"][camera] = {"dtype": "video", "shape": [48, 64, 3]}
    info_path.write_text(json.dumps(info))
    directory = dataset / f"videos/chunk-000/{camera}"
    directory.mkdir(parents=True)
    for ep in range(2):
        writer = cv2.VideoWriter(
            str(directory / f"episode_{ep:06d}.mp4"),
            cv2.VideoWriter_fourcc(*"mp4v"),
            10,
            (64, 48),
        )
        assert writer.isOpened()
        for _ in range(20):
            writer.write(np.zeros((48, 64, 3), dtype=np.uint8))
        writer.release()
    config = wb.store.get("providers", "fixture")
    config["vision"] = True
    wb.store.put("providers", "fixture", config)
    ctx = ctx.model_copy(
        update={"cameras": [camera], "allow_media_egress": True, "episodes": [0]}
    )
    before = {str(p): file_hash(p) for p in dataset.rglob("*") if p.is_file()}
    run = execute(wb, wb.plan(ctx))
    assert run["status"] == "waiting_for_review", run
    job_info = plan(
        wb, ObjectRequest(run_id=run["id"], prompts=["fixture cup"], max_frames=1)
    )
    job = wb.store.get("object_jobs", job_info["id"])
    rows = fake_annotations(Sam3Plan.model_validate(job["plan"]))
    draft = SidecarStore(wb.store.run_dir(run["id"]) / "objects" / job["id"] / "draft")
    draft.publish(rows, model={"provider": "test fixture, never SAM3 inference"})
    wb.store.mutate(
        "object_jobs",
        job["id"],
        lambda j: j.update(status="waiting_for_review", count=len(rows)),
    )
    wb.store.mutate(
        "changes", run["changes"], lambda c: c.update(object_jobs=[job["id"]])
    )
    human = Principal("reviewer", human=True)
    assert (
        invoke(wb, human, "changes.validate", {"changeset_id": run["changes"]})[
            "pending_objects"
        ]
        > 0
    )
    with pytest.raises(ValueError, match="Review all"):
        invoke(
            wb,
            human,
            "changes.approve",
            {"changeset_id": run["changes"], "revision": 0},
        )
    inspected = invoke(wb, human, "objects.inspect", {"job_id": job["id"]})
    assert inspected["evidence"]["frame_index"] == 0
    assert inspected["evidence"]["camera_key"] == camera
    edit = {
        "episode_index": 0,
        "camera_key": camera,
        "operation": "accept",
        "base_revision": inspected["revision"],
    }
    invoke(wb, human, "objects.edit", {"job_id": job["id"], "edit": edit})
    with pytest.raises(Conflict):
        invoke(wb, human, "objects.edit", {"job_id": job["id"], "edit": edit})
    change = wb.store.get("changes", run["changes"])
    invoke(
        wb,
        human,
        "changes.approve",
        {"changeset_id": change["id"], "revision": change["revision"]},
    )
    wb.commit(change["id"], "object-composite", change["revision"])
    published = SidecarStore(
        resolve(wb.store.state, run["dataset_key"], "object_annotations")
    )
    assert len(published.read_annotations()) == len(rows)
    assert all(r["status"] == "accepted" for r in published.read_annotations())
    preview = invoke(
        wb,
        human,
        "objects.frame",
        {
            "job_id": job["id"],
            "episode": 0,
            "camera": camera,
            "timestamp": 0,
            "window_seconds": 1,
        },
    )
    assert preview["rows"][0]["mask_rle"] == rows[0].mask_rle
    assert preview["ledger"][0]["frame"] == 0
    from levi.agent.exporting import export

    result = export(wb, wb.store.get("runs", run["id"]))
    assert result["validation"]["object_rows"] == len(rows)
    assert {str(p): file_hash(p) for p in dataset.rglob("*") if p.is_file()} == before


def test_staged_objects_do_not_probe_or_import_model(bench, monkeypatch, tmp_path):
    from backend import app
    from levi.agent.objects import readiness

    checkpoint = tmp_path / "sam3.pt"
    checkpoint.write_bytes(b"not a model")
    worker = tmp_path / "python"
    worker.write_text("not executable")
    monkeypatch.setattr(app, "_sam3_checkpoint_path", lambda: checkpoint)
    monkeypatch.setenv("LEVI_SAM3_WORKER_PYTHON", str(worker))
    monkeypatch.setenv("LEVI_SAM3_ENABLED", "1")
    result = readiness()
    assert result["checkpoint_ready"]
    assert result["cuda_probe_performed"] is False
    assert "torch" not in sys.modules


def test_profile_switch_never_sends_new_key_to_old_endpoint(bench, client):
    from levi.agent.credentials import get

    wb, ctx = bench
    old = ProviderConfig.model_validate(wb.store.get("providers", "fixture"))
    assert (
        client.post(
            "/api/levi/agent/v1/providers/fixture/session",
            json={"key": "old-fixture-key"},
        ).status_code
        == 200
    )
    run = wb.plan(ctx)
    new = old.model_copy(update={"base_url": "https://new-provider.invalid/v1"})
    wb.store.put("providers", "fixture", new.model_dump())
    assert get(new) is None
    assert (
        client.post(
            "/api/levi/agent/v1/providers/fixture/session",
            json={"key": "new-fixture-key"},
        ).status_code
        == 200
    )
    assert get(old) is None
    assert get(new) == "new-fixture-key"
    result = execute(wb, run)
    assert result["status"] == "blocked"
    assert result["requests"] == 0
    assert "configuration changed" in result["reason"]


def test_human_can_disconnect_external_agent_without_exposing_token(
    bench, client, monkeypatch
):
    _wb, ctx = bench
    monkeypatch.setenv("LEVI_AGENT_TOKEN", "fixture-external-token")
    monkeypatch.setenv("LEVI_AGENT_DATASETS", ctx.repo_id)
    root = "/api/levi/agent/v1"
    response = client.get(root + "/connections")
    assert response.json()["external"]["configured"]
    assert "fixture-external-token" not in response.text
    headers = {"Authorization": "Bearer fixture-external-token"}
    assert client.get(root + "/capabilities", headers=headers).status_code == 200
    assert (
        client.post(root + "/connections/external", json={"enabled": False}).status_code
        == 200
    )
    assert client.get(root + "/capabilities", headers=headers).status_code == 401
    assert (
        client.post(root + "/connections/external", json={"enabled": True}).status_code
        == 200
    )
    assert client.get(root + "/capabilities", headers=headers).status_code == 200


def test_execution_plan_gates_cannot_be_bypassed(bench):
    from levi.agent.planning import approve

    wb, ctx = bench
    run = wb.plan(ctx)
    with pytest.raises(Conflict, match="Approve"):
        wb.launch(run["id"])
    with pytest.raises(PermissionError):
        invoke(
            wb,
            Principal("external", datasets=(ctx.repo_id,)),
            "plans.approve",
            {"run_id": run["id"], "revision": 1},
        )
    with pytest.raises(Conflict):
        approve(wb, run["id"], 2, "human")
    approve(wb, run["id"], 1, "human")
    with pytest.raises(Conflict, match="pilot"):
        wb.launch(run["id"], pilot=False)
    wb.store.mutate(
        "runs",
        run["id"],
        lambda r: r["context"].update(instruction="Changed after approval"),
    )
    with pytest.raises(Conflict, match="changed"):
        wb.launch(run["id"])


def test_clarification_and_semantic_contracts(bench):
    from levi.agent.observations import quality
    from levi.agent.planning import clarify
    from levi.agent.schema import Proposal

    _wb, ctx = bench
    ctx = TaskContext.model_validate(
        {**ctx.model_dump(), "workflow": {"kind": "temporal"}}
    )
    fields = {q["field"] for q in clarify(ctx.model_dump())["questions"]}
    assert fields == {"cameras", "definitions"}
    p = Proposal(
        episode_index=0,
        kind="segment",
        content="Unknown movement",
        start=0,
        end=1,
        subtask_id="unknown",
        outcome="unknown",
        attempt=2,
        evidence_ids=["f"],
        uncertainty="Occluded",
    )
    report = quality(ctx, [p], [{"id": "f", "timestamp": 0}], {"start": 0, "end": 2})
    assert report["uncovered_intervals"] == [[1, 2]]
    assert report["human_review_required"]
    p.outcome = "success"
    with pytest.raises(ValueError, match="evidence"):
        quality(ctx, [p], [], {"start": 0, "end": 2})


def test_cache_reuses_identical_evidence_and_counts_usage(bench, monkeypatch):
    import time

    from levi.agent.schema import ProviderConfig

    wb, ctx = bench
    from levi.agent import runtime

    original = runtime.digest
    seen = []

    def track(value):
        if isinstance(value, dict) and "evidence" in value and "config" in value:
            seen.append(value)
        return original(value)

    monkeypatch.setattr(runtime, "digest", track)
    run = execute(wb, wb.plan(ctx))
    saved = wb.store.get("evidence", f"{run['id']}:0")
    before = run["requests"]
    output, usage = wb.model_step(
        run["id"],
        ProviderConfig.model_validate(run["provider_config"]),
        ctx,
        saved["summary"],
        saved["items"],
        "coarse",
        time.monotonic(),
    )
    assert output.proposals
    result = wb.store.get("runs", run["id"])
    assert seen[0] == seen[1], {
        k: (seen[0][k], seen[1][k]) for k in seen[0] if seen[0][k] != seen[1][k]
    }
    assert result["requests"] == before and result["cache_hits"] == 1
    assert usage["usage_kind"] == "reported"


def test_dense_policy_stops_instead_of_reducing_coverage(bench):
    from levi.agent.observations import frame_scope

    wb, ctx = bench
    run = execute(wb, wb.plan(ctx))
    ctx = TaskContext.model_validate(
        {
            **ctx.model_dump(),
            "workflow": {
                "kind": "temporal",
                "coarse_step_seconds": 0.05,
                "max_evidence_frames": 3,
            },
        }
    )
    with pytest.raises(ValueError, match="coverage"):
        frame_scope(ctx, wb.store.run_dir(run["id"]) / "input", 0)


def test_video_pts_mapping_is_not_frame_divided_by_fps():
    from levi.agent.video_evidence import locate

    assert locate([0.4, 0.43, 0.57, 0.61], 0.56, 0.02) == (2, 0.57)
    with pytest.raises(ValueError, match="mismatch"):
        locate([0, 0.8], 0.4, 0.1)


def test_evaluation_retries_events_and_equal_work_guard():
    from levi.agent.evaluation import efficiency, temporal

    reference = [
        {
            "episode_index": 0,
            "kind": "event",
            "subtask_id": "pick",
            "attempt": 2,
            "outcome": "failure",
            "layer": "activity",
            "start": 0.5,
        }
    ]
    assert temporal(reference, [{**reference[0], "start": 0.55}])["f1"] == 1
    assert temporal(reference, [{**reference[0], "outcome": "success"}])["f1"] == 0
    baseline = dict.fromkeys(
        (
            "dataset_digest",
            "policy_digest",
            "evidence_digest",
            "validation_digest",
            "output_digest",
        ),
        "same",
    )
    baseline.update(tokens=100, model_calls=2)
    optimized = {**baseline, "tokens": 50, "model_calls": 1}
    assert efficiency(baseline, optimized)["tokens"]["saved"] == 50
    with pytest.raises(ValueError):
        efficiency(baseline, {**optimized, "evidence_digest": "less coverage"})


def test_rejected_pilot_blocks_bulk_execution(bench):
    from levi.agent.planning import pilot_review

    wb, ctx = bench
    run = execute(wb, wb.plan(ctx))
    pilot_review(wb, run["id"], 0, False, "Wrong retry interpretation", "human")
    with pytest.raises(Conflict, match="pilot"):
        wb.launch(run["id"], pilot=False)


def test_exact_evidence_recall_is_paged_and_scoped(bench):
    wb, ctx = bench
    run = execute(wb, wb.plan(ctx))
    human = Principal("human", human=True)
    page = invoke(
        wb, human, "evidence.read", {"run_id": run["id"], "episode": 0, "limit": 1}
    )
    assert len(page["items"]) == 1 and page["total"] == 3 and page["next_offset"] == 1
    with pytest.raises(ValueError):
        invoke(wb, human, "evidence.read", {"run_id": run["id"], "episode": 99})


def test_harness_temporal_pilot_edit_export_roundtrip(bench, dataset):
    import json

    import cv2
    import numpy as np

    from levi.agent.exporting import export
    from levi.agent.planning import pilot_review
    from levi.agent.schema import Proposal

    wb, ctx = bench
    camera = "observation.images.front"
    info_path = dataset / "meta/info.json"
    info = json.loads(info_path.read_text())
    info["features"][camera] = {"dtype": "video", "shape": [48, 64, 3]}
    info_path.write_text(json.dumps(info))
    directory = dataset / f"videos/chunk-000/{camera}"
    directory.mkdir(parents=True)
    for ep in range(2):
        writer = cv2.VideoWriter(
            str(directory / f"episode_{ep:06d}.mp4"),
            cv2.VideoWriter_fourcc(*"mp4v"),
            10,
            (64, 48),
        )
        for frame in range(20):
            writer.write(np.full((48, 64, 3), frame * 10, dtype=np.uint8))
        writer.release()
    config = wb.store.get("providers", "fixture")
    config["vision"] = True
    wb.store.put("providers", "fixture", config)
    flow = {
        "kind": "temporal",
        "coarse_step_seconds": 0.5,
        "definitions": [
            {
                "id": "move",
                "label": "Move",
                "definition": "Move gripper",
                "starts_when": "Motion starts",
                "ends_when": "Motion ends",
                "success_when": "Target visibly reached",
            }
        ],
    }
    ctx = TaskContext.model_validate(
        {
            **ctx.model_dump(),
            "episodes": [0],
            "cameras": [camera],
            "allow_media_egress": True,
            "workflow": flow,
        }
    )

    class TemporalFixture:
        def generate(self, config, instruction, summary, evidence, artifacts, budget):
            return ModelOutput(
                summary="Synthetic motion",
                proposals=[
                    Proposal(
                        episode_index=0,
                        kind="segment",
                        content="Move",
                        start=0,
                        end=1,
                        evidence_ids=[r["id"] for r in evidence],
                        subtask_id="move",
                        attempt=1,
                        outcome="unknown",
                        uncertainty="Synthetic fixture has no semantic success ground truth",
                    )
                ],
            ), {"requests": 1, "tokens": 25}

    wb.provider = TemporalFixture()
    before = {str(p): file_hash(p) for p in dataset.rglob("*") if p.is_file()}
    run = execute(wb, wb.plan(ctx))
    assert run["status"] == "waiting_for_review", run
    assert run["requests"] == 2
    evidence = wb.store.get("evidence", f"{run['id']}:0")["items"]
    assert all(r["decoded_video_timestamp"] is not None for r in evidence)
    human = Principal("reviewer", human=True)
    change = wb.store.get("changes", run["changes"])
    change["proposals"][0]["content"] = "Human verified synthetic segment"
    edited = invoke(
        wb,
        human,
        "changes.edit",
        {"changeset_id": change["id"], "revision": 0, "proposals": change["proposals"]},
    )
    pilot_review(
        wb,
        run["id"],
        edited["revision"],
        True,
        "Synthetic contract checked",
        "reviewer",
    )
    invoke(
        wb,
        human,
        "changes.approve",
        {"changeset_id": change["id"], "revision": edited["revision"]},
    )
    wb.commit(change["id"], "temporal-fixture", edited["revision"])
    result = export(wb, wb.store.get("runs", run["id"]))
    assert result["validation"]["ok"]
    from pathlib import Path

    history = json.loads(
        (Path(result["output_dir"]) / "meta/levi_agent_provenance.json").read_text()
    )
    assert history[-1]["proposals"][0]["attempt"] == 1
    assert history[-1]["proposals"][0]["outcome"] == "unknown"
    assert {str(p): file_hash(p) for p in dataset.rglob("*") if p.is_file()} == before


def test_budget_revision_retains_completed_work_and_requires_approval(bench):
    from levi.agent.planning import approve, rebudget
    from levi.agent.schema import Budget

    wb, ctx = bench
    run = execute(wb, wb.plan(ctx))
    updated = rebudget(wb, run["id"], 1, Budget(max_calls=16, max_tokens=32000))
    assert updated["completed"] == [0] and updated["plan"]["approval"] is None
    assert updated["changes"] == run["changes"]
    with pytest.raises(Conflict):
        wb.launch(run["id"])
    approved = approve(wb, run["id"], 2, "human")
    assert approved["plan"]["approval"]["digest"] == approved["plan"]["digest"]


def test_a_run_may_have_no_token_limit(bench):
    from levi.agent.planning import rebudget
    from levi.agent.schema import Budget

    wb, ctx = bench
    run = execute(
        wb, wb.plan(ctx.model_copy(update={"budget": Budget(max_tokens=None)}))
    )
    assert run["context"]["budget"]["max_tokens"] is None
    assert run["completed"] == [0], "requests still run and settle"
    # And a limited run can be lifted to none.
    updated = rebudget(wb, run["id"], 1, Budget(max_calls=16, max_tokens=None))
    assert updated["context"]["budget"]["max_tokens"] is None


def test_external_run_may_remove_token_and_task_time_caps(bench):
    from levi.agent.planning import rebudget
    from levi.agent.schema import Budget

    wb, ctx = bench
    with pytest.raises(ValueError, match="external Agent"):
        TaskContext.model_validate(
            ctx.model_dump() | {"budget": Budget(max_seconds=None).model_dump()}
        )
    external = TaskContext.model_validate(ctx.model_dump() | {"provider": "external"})
    run = wb.plan(external)
    revised = rebudget(
        wb,
        run["id"],
        1,
        Budget(max_calls=1000, max_tokens=None, max_seconds=None),
    )
    assert revised["plan"]["revision"] == 2
    assert revised["plan"]["approval"] is None
    assert revised["context"]["budget"]["max_tokens"] is None
    assert revised["context"]["budget"]["max_seconds"] is None


def test_changed_pilot_must_be_reviewed_again(bench):
    from levi.agent.planning import pilot_review

    wb, ctx = bench
    run = execute(wb, wb.plan(ctx))
    pilot_review(wb, run["id"], 0, True, "Reviewed", "human")
    change = wb.store.get("changes", run["changes"])
    change["proposals"][0]["content"] = "Human correction"
    invoke(
        wb,
        Principal("human", human=True),
        "changes.edit",
        {"changeset_id": change["id"], "revision": 0, "proposals": change["proposals"]},
    )
    with pytest.raises(Conflict, match="Pilot annotations changed"):
        wb.launch(run["id"], pilot=False)


def test_object_only_plan_has_no_language_model_budget_or_egress(bench):
    from levi.agent.planning import clarify

    _wb, ctx = bench
    ctx = TaskContext.model_validate(
        {
            **ctx.model_dump(),
            "provider": "local-tools",
            "cameras": ["observation.images.front"],
            "workflow": {"kind": "objects", "object_concepts": ["cup"]},
        }
    )
    assert clarify(ctx.model_dump())["ready"]
    # Dataset/media existence is still validated before a real plan can be created.
    assert not ctx.allow_media_egress


def test_dataset_adapter_must_declare_temporal_window_support(
    bench, monkeypatch, tmp_path
):
    from levi.agent.formats import DATASETS
    from levi.agent.observations import observe

    _, ctx = bench

    class Reader:
        def sample(self, *args):
            return {"fixture": "registered reader"}, []

    monkeypatch.setitem(DATASETS, "test-reader", Reader())
    ctx = TaskContext.model_validate(
        {**ctx.model_dump(), "dataset_adapter": "test-reader"}
    )
    assert observe(ctx, tmp_path, 0, tmp_path)[0]["fixture"] == "registered reader"
    ctx.workflow["kind"] = "temporal"
    with pytest.raises(ValueError, match="temporal-window"):
        observe(ctx, tmp_path, 0, tmp_path)


def test_second_run_rebases_onto_the_first_instead_of_being_redone(bench):
    """Two runs on one dataset: the loser of the race keeps its work."""
    from levi.agent.planning import pilot_review
    from levi.agent.store import Conflict

    wb, context = bench
    human = Principal("tester", human=True)

    def staged(ctx):
        run = execute(wb, wb.plan(ctx))
        change = wb.store.get("changes", run["changes"])
        pilot_review(wb, run["id"], change["revision"], True, "ok", "tester")
        return wb.store.get("changes", run["changes"])

    first = staged(context)
    second = staged(context.model_copy(update={"episodes": [1]}))
    assert second["base_revision"] == first["base_revision"]

    invoke(
        wb,
        human,
        "changes.approve",
        {"changeset_id": first["id"], "revision": first["revision"]},
    )
    invoke(
        wb,
        human,
        "changes.commit",
        {"changeset_id": first["id"], "revision": first["revision"]},
        "first",
    )
    head = wb.store.head(wb.store.get("runs", first["run_id"])["dataset_key"])

    second = wb.store.get("changes", second["id"])
    with pytest.raises(Conflict):
        invoke(
            wb,
            human,
            "changes.approve",
            {"changeset_id": second["id"], "revision": second["revision"]},
        )

    moved = invoke(
        wb,
        human,
        "changes.rebase",
        {"changeset_id": second["id"], "revision": second["revision"]},
    )
    assert moved["rebased"] and moved["base_revision"] == head
    assert moved["previous_base"] == first["base_revision"]

    second = wb.store.get("changes", second["id"])
    invoke(
        wb,
        human,
        "changes.approve",
        {"changeset_id": second["id"], "revision": second["revision"]},
    )
    receipt = invoke(
        wb,
        human,
        "changes.commit",
        {"changeset_id": second["id"], "revision": second["revision"]},
        "second",
    )
    # Both runs' work is published, and the second records where it came from.
    assert receipt["revision"] != head
    assert (
        wb.store.get("changes", second["id"])["provenance"]["rebased_from"]
        == first["base_revision"]
    )


def test_rebase_refuses_when_the_frozen_source_moved(bench, dataset):
    from levi.agent.planning import pilot_review
    from levi.agent.store import Conflict

    wb, context = bench
    run = execute(wb, wb.plan(context))
    change = wb.store.get("changes", run["changes"])
    pilot_review(wb, run["id"], change["revision"], True, "ok", "tester")
    wb.store.mutate("changes", change["id"], lambda c: c.update(base_revision="stale"))
    # The dataset itself changed under the run: rebasing would silently attach
    # suggestions to frames nobody read.
    parquet = dataset / "data/chunk-000/episode_000000.parquet"
    parquet.write_bytes(parquet.read_bytes() + b"\0")
    with pytest.raises((Conflict, ValueError)):
        wb.rebase(change["id"], wb.store.get("changes", change["id"])["revision"])


def test_a_hub_credential_does_not_lock_the_ui_out_of_local_datasets(
    client, monkeypatch
):
    """Signing in to Hugging Face must not break LEVI's own file service.

    A signed-in browser attaches its Hub bearer token to dataset requests. LEVI
    used to read any bearer token as a failed Agent credential and answer 401,
    so every local dataset stopped loading the moment someone signed in.
    """
    monkeypatch.setenv("LEVI_AGENT_TOKEN", "test-scoped-token")
    monkeypatch.setenv("LEVI_UI_TOKEN", "test-ui-token")
    ui = {"x-levi-ui-token": "test-ui-token"}
    hub = {"Authorization": "Bearer hf_a_hugging_face_token", **ui}

    assert client.get("/api/levi/catalog", headers=ui).status_code == 200
    assert client.get("/api/levi/catalog", headers=hub).status_code == 200

    # A token nobody recognises is still not a way past the UI credential.
    assert (
        client.get(
            "/api/levi/catalog", headers={"Authorization": "Bearer hf_a_token"}
        ).status_code
        == 401
    )
    # A real Agent credential is still confined to the Agent API...
    scoped = {"Authorization": "Bearer test-scoped-token"}
    assert client.get("/api/levi/catalog", headers=scoped).status_code == 403
    assert (
        client.get("/api/levi/agent/v1/capabilities", headers=scoped).status_code == 200
    )
    # ...and an invalid one is still rejected there.
    assert (
        client.get(
            "/api/levi/agent/v1/capabilities",
            headers={"Authorization": "Bearer not-the-token"},
        ).status_code
        == 401
    )
