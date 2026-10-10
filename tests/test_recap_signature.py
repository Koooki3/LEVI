"""Which parameters decide a RECAP result's numbers (the merge signature),
and the bookkeeping of a merged result.

Review D1 (B1): a subset computed with other camera views, expert variant or
prompt length was silently merged into a result, because the signature named
a hand-picked list of fields. The signature now covers every effective
setting, and every field must be classified explicitly: a new manifest,
result or worker field fails ``test_every_field_is_classified`` until it is
declared as affecting the values or not.
"""

import ast
import json
from pathlib import Path

import pytest
from test_recap_fr3 import PARAMS, raw_capture, store_env  # noqa: F401 (fixtures)
from test_recap_store import (  # noqa: F401 (helpers and fixtures)
    _episode,
    _meta,
    _publish,
    models_layout,
    recap_models,
)
from test_recap_value import BODY, run

from levi.recap import checkpoints, jobs, signature, static_filter, store

PROJECT = Path(__file__).resolve().parents[1]
WORKER = PROJECT / "integrations/recap_value/levi_recap_worker"


# ---------------------------------------------------------------- classification


def _provenance_keys(path: Path) -> set[str]:
    """Keys of the provenance dicts a worker provider returns."""
    tree = ast.parse(path.read_text())
    keys: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "provenance" for t in node.targets
            )
            and isinstance(node.value, ast.Dict)
        ):
            keys |= {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "provenance"
            and isinstance(node.slice, ast.Constant)
        ):
            keys.add(node.slice.value)
        if isinstance(node, ast.Dict):  # {"provenance": {**provenance, "x": …}}
            for k, v in zip(node.keys, node.values):
                if (
                    isinstance(k, ast.Constant)
                    and k.value == "provenance"
                    and isinstance(v, ast.Dict)
                ):
                    keys |= {i.value for i in v.keys if isinstance(i, ast.Constant)}
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple):
            for item in node.value.elts:
                if isinstance(item, ast.Dict):
                    keys |= {k.value for k in item.keys if isinstance(k, ast.Constant)}
    return keys


def test_every_field_is_classified(recap_models):  # noqa: F811
    """Add a field anywhere below and this fails until it is classified."""
    manifest_fields = {
        (f.alias or name) for name, f in checkpoints.Manifest.model_fields.items()
    }
    for kind, affecting, ignored, fields in (
        (
            "manifest",
            signature.MANIFEST_AFFECTING,
            signature.MANIFEST_IGNORED,
            manifest_fields,
        ),
        (
            "static filter params",
            signature.STATIC_FILTER_PARAMS,
            set(),
            set(checkpoints.StaticFilter.model_fields),
        ),
    ):
        assert not affecting & ignored, kind
        assert fields == affecting | ignored, (
            f"unclassified {kind} fields: {sorted(fields - affecting - ignored)}; "
            f"stale entries: {sorted((affecting | ignored) - fields)}"
        )
    assert not signature.RESULT_AFFECTING & signature.RESULT_IGNORED
    assert not signature.WORKER_AFFECTING & signature.WORKER_IGNORED
    # Every key a worker provider reports.
    reported = set()
    for source in ("rlinf_provider.py", "fake.py", "runner.py"):
        reported |= _provenance_keys(WORKER / source)
    assert {"rlinf_commit", "torch", "decoder", "load_seconds"} <= reported
    assert "elapsed_seconds" in reported
    unclassified = reported - signature.WORKER_AFFECTING - signature.WORKER_IGNORED
    assert not unclassified, f"unclassified worker provenance keys: {unclassified}"
    # Every key of real records: a full result, a merged one, a revision.
    client, entry = recap_models
    repo = entry["id"]
    checkpoints.update(
        "fake-a", {"unified_threshold": 0.01, "return_min": -64.0, "return_max": 0.0}
    )
    run(client, repo)
    run(client, repo, episodes=[1])
    merged = store.revision(entry["name"])
    assert merged["merged"]
    on_disk = json.loads(
        (
            store.root(entry["name"])
            / "models/fake-a/v"
            / merged["version"]
            / "result.json"
        ).read_text()
    )
    keys = set(on_disk) | set(merged)
    keys |= set(store.revision(entry["name"], "fake-a"))
    classified = signature.RESULT_AFFECTING | signature.RESULT_IGNORED
    assert keys <= classified, f"unclassified result keys: {sorted(keys - classified)}"
    filter_keys = set(on_disk["static_filter"])
    known = signature.STATIC_FILTER_AFFECTING | signature.STATIC_FILTER_IGNORED
    assert filter_keys <= known, sorted(filter_keys - known)


def test_unknown_fields_count_as_affecting():
    """A field nobody classified yet still blocks a merge (safe default)."""
    a = _meta()
    b = _meta()
    b["checkpoint"]["manifest"] = {**b["checkpoint"]["manifest"], "brand_new": 1}
    assert "manifest.brand_new" in signature.differences(a, b)
    c = {**_meta(), "brand_new_run_field": 2}
    assert "brand_new_run_field" in signature.differences(a, c)
    d = {**_meta(), "worker": {"brand_new_version": "x"}}
    assert "worker.brand_new_version" in signature.differences(a, d)
    # Display fields never do.
    e = {**_meta(), "job_id": "x", "levi_commit": "y", "lookahead": 10.0}
    e["checkpoint"] = {
        **e["checkpoint"],
        "manifest": {**a["checkpoint"]["manifest"], "notes": "n"},
    }
    assert signature.differences(a, e) == []


# ---------------------------------------------------------------- store level


@pytest.mark.parametrize(
    "change",
    [
        {"views": {"base_0_rgb": "observation.images.wrist"}},
        {"critic_expert_variant": "gemma_1m"},
        {"max_token_len": 48},
        {"model_type": "pi0_fast"},
        {"env_type": "fr3_recap"},
        {"base_models": {"siglip": "../_base/other"}},
        {"action_horizon": 10},
    ],
)
def test_subset_with_other_model_settings_is_refused(client, change):
    """The review's reproduction: other views (or variant, prompt length,
    model type, base models) must not be merged into the stored result."""
    base = {"views": {"base_0_rgb": "observation.images.front"}, "max_token_len": 200}
    full = _meta()
    full["checkpoint"]["manifest"] = {**full["checkpoint"]["manifest"], **base}
    store.publish_model(
        "ds", {e: _episode(6) for e in range(4)}, full, dataset_name="ds"
    )
    sub = _meta()
    sub["checkpoint"]["manifest"] = {**full["checkpoint"]["manifest"], **change}
    with pytest.raises(store.MergeRefused, match="manifest"):
        store.publish_model(
            "ds", {0: _episode(6, 0.1)}, sub, dataset_name="ds", subset=True
        )
    record = store.revision("ds", "r1")
    assert record["merged"] is False and record["episode_indices"] == [0, 1, 2, 3]
    assert record["checkpoint"]["manifest"]["views"] == base["views"]


def test_subset_from_another_worker_version_is_refused(client):
    _publish(
        {0: (6, 0.0), 1: (6, 0.0)}, worker={"rlinf_commit": "aaa", "load_seconds": 3}
    )
    # Timing and memory are not inputs: the subset merges.
    merged = _publish(
        {0: (6, 0.1)}, subset=True, worker={"rlinf_commit": "aaa", "load_seconds": 9}
    )
    assert merged["merged"]
    with pytest.raises(store.MergeRefused, match="worker.rlinf_commit"):
        _publish({0: (6, 0.2)}, subset=True, worker={"rlinf_commit": "bbb"})


def test_merged_record_keeps_per_computation_provenance(client):
    """Review I1: a merged record describes all its episodes, not the last
    subset alone."""
    first = _publish(
        {0: (6, 0.0), 1: (6, 0.0), 2: (6, 0.0)},
        job_id="20261010-0001",
        request={"episodes": None},
    )
    merged = _publish(
        {1: (6, 0.1)}, subset=True, job_id="20261010-0002", request={"episodes": [1]}
    )
    assert merged["last_request"] == {"episodes": [1]} and "request" not in merged
    assert merged["job_id"] == "20261010-0002"
    sources = {(m["job_id"], tuple(m["episodes"])) for m in merged["merged_from"]}
    assert sources == {("20261010-0001", (0, 2)), ("20261010-0002", (1,))}
    versions = {m["job_id"]: m["version"] for m in merged["merged_from"]}
    assert versions["20261010-0001"] == first["version"]
    rows = store.summary("ds", "r1")["episodes"]
    assert (
        rows["0"]["job_id"] == "20261010-0001"
        and rows["1"]["job_id"] == "20261010-0002"
    )


def test_a_named_episode_skipped_now_is_not_carried_silently(client):
    """Review S3: an episode the subset run skipped leaves the result with
    its reason instead of keeping its old values."""
    _publish({0: (6, 0.0), 1: (6, 0.0)})
    merged = _publish(
        {0: (6, 0.1)},
        subset=True,
        skipped_episodes={"1": "static filter: no source_demo"},
    )
    assert merged["episode_indices"] == [0]
    assert merged["skipped_episodes"] == {"1": "static filter: no source_demo"}
    assert store.read_episode("ds", 1, "r1") is None


# ---------------------------------------------------------------- routes


def test_checkpoint_edited_after_the_result_refuses_a_subset(recap_models):  # noqa: F811
    client, entry = recap_models
    repo, name = entry["id"], entry["name"]
    checkpoints.update(
        "fake-a", {"unified_threshold": 0.01, "return_min": -64.0, "return_max": 0.0}
    )
    run(client, repo)
    for change in (
        {"views": {"base_0_rgb": "observation.images.view1"}},
        {"max_token_len": 48},
        {"critic_expert_variant": "gemma_1m"},
    ):
        before = checkpoints.load("fake-a")[1].public()
        checkpoints.update("fake-a", change)
        refused = client.post(
            "/annotations/api/recap/run",
            json={**BODY, "repo_id": repo, "episodes": [1]},
        )
        assert refused.status_code == 409, (change, refused.text)
        assert "manifest." + next(iter(change)) in refused.json()["detail"]
        checkpoints.update("fake-a", {k: before[k] for k in change})
    assert not store.revision(name)["merged"]


def test_precheck_and_publication_use_the_same_signature(recap_models, monkeypatch):  # noqa: F811
    """Review S2: a stored checkpoint threshold vs the same number given by
    hand differed only at publication, after the worker had run."""
    client, entry = recap_models
    repo, name = entry["id"], entry["name"]
    checkpoints.update(
        "fake-a", {"unified_threshold": 0.01, "return_min": -64.0, "return_max": 0.0}
    )
    run(client, repo)
    same_number = client.post(
        "/annotations/api/recap/run",
        json={**BODY, "repo_id": repo, "episodes": [1], "threshold": 0.01},
    )
    assert same_number.status_code == 409
    assert "threshold_source" in same_number.json()["detail"]
    # Should the precheck ever pass a run publication refuses, the job fails
    # and the stored result stays whole and readable.
    before = store.revision(name)
    monkeypatch.setattr(jobs, "_refuse_unmergeable", lambda *a, **k: None)
    failed = run(client, repo, episodes=[1], lookahead=5)
    assert failed["status"] == "failed" and "lookahead" in failed["error"]
    after = store.revision(name)
    assert after["version"] == before["version"]
    assert store.read_episode(name, 0, "fake-a") is not None


def test_merged_static_filter_counts_cover_every_episode(raw_capture, monkeypatch):  # noqa: F811
    """Review I1: the header's "static filter k/N" after a merge counts all
    episodes, not the last subset."""
    monkeypatch.setenv("LEVI_RECAP_STORE_LAYOUT", "models")
    client, entry = raw_capture
    repo, name = entry["id"], entry["name"]
    checkpoints.update(
        "fake-f", {"unified_threshold": 0.01, "return_min": -64.0, "return_max": 0.0}
    )
    assert run(client, repo, checkpoint="fake-f")["status"] == "succeeded"
    full = store.revision(name)["static_filter"]
    assert full["applied"] and full["frames"] == 30 + 34
    merged = run(client, repo, checkpoint="fake-f", episodes=[1])
    assert merged["status"] == "succeeded", merged["error"]
    record = store.revision(name)
    assert record["merged"]
    info = record["static_filter"]
    assert (
        info["frames"] == full["frames"] and info["kept_frames"] == full["kept_frames"]
    )
    assert info["dropped_fraction"] == pytest.approx(full["dropped_fraction"])
    status = client.get(
        "/annotations/api/recap/status", params={"repo_id": repo}
    ).json()
    assert status["current"]["static_filter"]["frames"] == 30 + 34
    source = Path(entry["path"])
    demos = sorted(p for p in source.rglob("demo_*") if p.is_dir())
    kept = sum(len(static_filter.kept_positions(d, PARAMS)["keep"]) for d in demos)
    assert info["kept_frames"] == kept
