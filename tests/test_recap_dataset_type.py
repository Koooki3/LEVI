"""RECAP dataset type: ``auto`` resolution, the dataset setting and the
value-only result a dataset without outcomes gets (never all-positive labels).
"""

import json
from pathlib import Path

import numpy as np
import pytest
from test_recap_value import BODY, run
from test_training_manifest import EPISODES, _frames, repo  # noqa: F401 (fixture)

from levi import training_manifest as tm
from levi.recap import checkpoints, compare, jobs, store


def _dataset(tmp_path, rows, *, recap_meta=None):
    root = tmp_path / "ds"
    (root / "meta").mkdir(parents=True, exist_ok=True)
    if recap_meta is not None:
        (root / "meta/levi_recap.json").write_text(json.dumps(recap_meta))
    return jobs.Dataset("local/ds", "ds", root, {"fps": 10}, rows, {})


def _kind(ds, requested="auto"):
    found = jobs.resolve_dataset_type(ds, requested)
    assert found["reason"]  # always recorded with the result
    return found["dataset_type"], found["source"]


def test_auto_resolution_order(client, tmp_path):
    labelled = _dataset(tmp_path, {0: {"levi_outcome": "failure"}, 1: {}})
    assert _kind(labelled) == ("rollout", "outcomes")
    # An RLinf-format is_success and a human label count as outcomes too.
    assert _kind(_dataset(tmp_path, {0: {"is_success": False}})) == (
        "rollout",
        "outcomes",
    )
    human = _dataset(tmp_path, {0: {}})
    human.human[0] = "success"
    assert _kind(human)[0] == "rollout"
    # Nothing to decide from: the old default (rollout), never "every episode
    # a success"; values only must be asked for.
    bare = _dataset(tmp_path, {0: {}, 1: {"levi_outcome": "unknown"}})
    assert _kind(bare) == ("rollout", "fallback")
    assert "no dataset setting" in jobs.resolve_dataset_type(bare)["reason"]
    # A LEVI recap_value export says what it is.
    exported = _dataset(tmp_path, {0: {}}, recap_meta={"dataset_type": "sft"})
    assert _kind(exported) == ("sft", "manifest")
    # The person's dataset setting wins over the metadata ...
    store.write_json(store.root("ds") / "dataset.json", {"dataset_type": "rollout"})
    assert _kind(exported) == ("rollout", "user")
    store.write_json(store.root("ds") / "dataset.json", {"dataset_type": "value_only"})
    assert _kind(exported) == ("value_only", "user")
    # ... and an explicit request over everything.
    assert _kind(exported, "sft") == ("sft", "request")
    assert _kind(exported, "value_only") == ("value_only", "request")
    # A broken or unknown setting is ignored, not trusted.
    store.write_json(store.root("ds") / "dataset.json", {"dataset_type": "bogus"})
    assert _kind(exported) == ("sft", "manifest")
    with pytest.raises(jobs.RecapError) as refused:
        jobs.resolve_dataset_type(exported, "bogus")
    assert refused.value.status == 400


@pytest.fixture
def unlabelled(client, monkeypatch, tmp_path):
    from test_formats import capture_fixture
    from test_views import register_raw

    monkeypatch.setenv("LEVI_RECAP_VALUE_CHECKPOINT_DIR", str(tmp_path / "ckpt"))
    monkeypatch.setenv("LEVI_RECAP_VALUE_WORKER_PYTHON", str(tmp_path / "no-python"))
    checkpoints.import_checkpoint(
        None, "fake-a", provider="fake", fields={"failure_reward": -30.0}
    )
    entry = register_raw(client, capture_fixture(tmp_path / "plates"))
    rows = Path(entry["view"]) / "meta/episodes.jsonl"
    lines = [json.loads(line) for line in rows.read_text().splitlines()]
    for line in lines:
        line.pop("levi_outcome", None)
        line.pop("is_success", None)
    rows.write_text("".join(json.dumps(r) + "\n" for r in lines))
    yield client, entry
    assert not jobs.wait_idle(60)


def test_default_is_auto_and_falls_back_to_rollout(unlabelled):
    """Decision I2 (a): the API and CLI default is auto; with nothing to
    decide from it falls back to the old default, which refuses as before."""
    client, entry = unlabelled
    repo_id = entry["id"]
    body = {k: v for k, v in BODY.items() if k != "dataset_type"}
    old = client.post("/annotations/api/recap/run", json={**body, "repo_id": repo_id})
    assert old.status_code == 400 and "label outcomes first" in old.json()["detail"]
    assert "fell back to rollout" in old.json()["detail"]
    status = client.get("/annotations/api/recap/status", params={"repo_id": repo_id})
    found = status.json()["dataset_type"]
    assert (found["setting"], found["dataset_type"], found["source"]) == (
        "auto",
        "rollout",
        "fallback",
    )


def test_default_follows_the_dataset_setting(unlabelled, capsys):
    from levi.recap.cli import main

    client, entry = unlabelled
    repo_id = entry["id"]
    client.post(
        "/annotations/api/recap/settings",
        json={"repo_id": repo_id, "dataset_type": "sft"},
    )
    # No dataset_type in the request: the setting decides (it used to be
    # ignored under the rollout default).
    body = {k: v for k, v in BODY.items() if k != "dataset_type"}
    started = client.post(
        "/annotations/api/recap/run", json={**body, "repo_id": repo_id}
    )
    assert started.status_code == 202, started.text
    from test_recap_value import finish

    assert finish(client, repo_id, started.json()["id"])["status"] == "succeeded"
    record = store.revision(entry["name"])
    assert record["dataset_type"] == "sft" and record["dataset_type_source"] == "user"
    assert record["dataset_type_reason"]
    # The CLI default is auto too.
    client.post(
        "/annotations/api/recap/settings",
        json={"repo_id": repo_id, "dataset_type": "value_only"},
    )
    assert main(["run", repo_id, "--checkpoint", "fake-a"]) == 0
    capsys.readouterr()
    assert store.revision(entry["name"])["dataset_type"] == "value_only"


def test_default_on_labelled_data_is_the_old_rollout(client, monkeypatch, tmp_path):
    from test_formats import capture_fixture
    from test_views import register_raw

    monkeypatch.setenv("LEVI_RECAP_VALUE_CHECKPOINT_DIR", str(tmp_path / "ckpt"))
    monkeypatch.setenv("LEVI_RECAP_VALUE_WORKER_PYTHON", str(tmp_path / "no-python"))
    checkpoints.import_checkpoint(None, "fake-a", provider="fake")
    entry = register_raw(client, capture_fixture(tmp_path / "plates"))
    body = {k: v for k, v in BODY.items() if k != "dataset_type"}
    started = client.post(
        "/annotations/api/recap/run", json={**body, "repo_id": entry["id"]}
    )
    from test_recap_value import finish

    assert finish(client, entry["id"], started.json()["id"])["status"] == "succeeded"
    record = store.revision(entry["name"])
    assert record["dataset_type"] == "rollout"
    assert record["dataset_type_source"] == "outcomes"
    assert record["request"]["dataset_type_requested"] == "auto"
    assert record["outcomes"] == {"0": "success", "1": "failure"}
    assert not jobs.wait_idle(60)


def test_values_only_never_positive_labels(unlabelled):
    client, entry = unlabelled
    repo_id = entry["id"]
    job = run(client, repo_id, dataset_type="value_only")
    assert job["status"] == "succeeded", job
    current = client.get(
        "/annotations/api/recap/status", params={"repo_id": repo_id}
    ).json()["current"]
    assert current["dataset_type"] == "value_only" and current["labels"] is False
    assert current["threshold"] is None and current["positive_fraction"] is None
    assert current["stale"] is False
    ep = client.get(
        "/annotations/api/recap/episodes/0", params={"repo_id": repo_id}
    ).json()
    assert ep["labels"] is False and ep["threshold"] is None
    assert len(ep["value"]) == 30 and all(-1 <= v <= 0 for v in ep["value"])
    # Decision I3: empty, never a row of nulls that today's viewer would draw
    # as "negative advantage" everywhere.
    assert ep["positive"] == [] and ep["advantage"] == []
    summary = client.get(
        "/annotations/api/recap/summary", params={"repo_id": repo_id}
    ).json()
    assert summary["threshold"] is None
    for key, row in summary["episodes"].items():
        assert row["positive_fraction"] is None and row["mean_advantage"] is None
        assert row["mean_value"] < 0
        # The value range per episode (the viewer's dataset-wide axis).
        values = client.get(
            f"/annotations/api/recap/episodes/{key}", params={"repo_id": repo_id}
        ).json()["value"]
        assert row["min_value"] == pytest.approx(min(values))
        assert row["max_value"] == pytest.approx(max(values))
        assert row["min_value"] <= row["mean_value"] <= row["max_value"]
    record = store.revision(entry["name"])
    assert record["request"]["dataset_type_requested"] == "value_only"
    assert record["dataset_type_source"] == "request"
    assert record["outcomes"] == {} and record["return_min"] is None
    # RLinf's advantages table carries no label column a trainer could read
    # as False.
    import pyarrow.parquet as pq

    table = pq.read_table(store.advantages_path(entry["name"]))
    assert "advantage" not in table.column_names
    assert "value_current" in table.column_names
    # An agent's digest has values but no runs of labels.
    digest = jobs.episode_digest(repo_id, 0)
    assert digest["labels"] is False and digest["runs"] == []
    assert digest["positive_fraction"] is None and digest["value_samples"]
    # The unified threshold needs advantages.
    with pytest.raises(jobs.RecapError, match="values only"):
        jobs.unified_threshold([repo_id])


def test_dataset_setting_route_and_cli(unlabelled, capsys):
    from levi.recap.cli import main

    client, entry = unlabelled
    repo_id = entry["id"]
    got = client.get("/annotations/api/recap/settings", params={"repo_id": repo_id})
    assert (got.json()["setting"], got.json()["source"]) == ("auto", "fallback")
    bad = client.post(
        "/annotations/api/recap/settings",
        json={"repo_id": repo_id, "dataset_type": "bogus"},
    )
    assert bad.status_code == 422
    saved = client.post(
        "/annotations/api/recap/settings",
        json={"repo_id": repo_id, "dataset_type": "sft"},
    ).json()
    assert (saved["setting"], saved["dataset_type"], saved["source"]) == (
        "sft",
        "sft",
        "user",
    )
    on_disk = json.loads((store.root(entry["name"]) / "dataset.json").read_text())
    assert on_disk["dataset_type"] == "sft" and on_disk["source"] == "user"
    job = run(client, repo_id, dataset_type="auto")
    ep = client.get(
        "/annotations/api/recap/episodes/1", params={"repo_id": repo_id}
    ).json()
    assert job["status"] == "succeeded" and all(ep["positive"]) and ep["labels"]
    assert store.revision(entry["name"])["dataset_type_source"] == "user"
    # The CLI shows and clears the setting.
    assert main(["settings", repo_id]) == 0
    assert json.loads(capsys.readouterr().out)["setting"] == "sft"
    assert main(["settings", repo_id, "--dataset-type", "auto"]) == 0
    assert json.loads(capsys.readouterr().out)["source"] == "fallback"
    assert not (store.root(entry["name"]) / "dataset.json").exists()
    assert (
        main(
            [
                "run",
                repo_id,
                "--checkpoint",
                "fake-a",
                "--sft",
                "--dataset-type",
                "auto",
            ]
        )
        == 1
    )
    assert "contradicts" in capsys.readouterr().err
    assert (
        main(["run", repo_id, "--checkpoint", "fake-a", "--dataset-type", "value_only"])
        == 0
    )
    capsys.readouterr()
    assert store.revision(entry["name"])["dataset_type"] == "value_only"


def _publish(name, *, value_only, threshold=0.0):
    episodes = {}
    for ep, (*_, n) in EPISODES.items():
        frames = np.arange(n)
        adv = np.where(frames % 2 == 0, 0.5, -0.5)
        episodes[ep] = {
            "frame_index": frames,
            "timestamp": frames / 10,
            "value": -frames / 100,
            "value_next": np.zeros(n),
            "reward_sum": np.zeros(n),
            "reward_sum_raw": np.zeros(n),
            "return": np.zeros(n),
            "advantage": np.full(n, np.nan) if value_only else adv,
            "num_valid_rewards": np.ones(n, dtype=np.int64),
            "positive": None if value_only else adv >= threshold,
        }
    return store.publish(
        name,
        episodes,
        {
            "checkpoint": {"name": "fake-a", "sha256": "0" * 64},
            "provider": "fake",
            "threshold": None if value_only else threshold,
            "threshold_source": None if value_only else "manual",
            "lookahead": 10,
            "dataset_type": "value_only" if value_only else "rollout",
            "outcomes": {},
            "return_min": None if value_only else -10.0,
            "return_max": None if value_only else 0.0,
        },
        dataset_name=name,
    )


def test_value_only_results_are_refused_by_label_exports(repo):  # noqa: F811
    name = repo.split("/", 1)[1]
    _publish(name, value_only=True)
    for operation in ("advantage_positive_mask", "advantage_weighted"):
        with pytest.raises(tm.ManifestError, match="values only"):
            tm.build(repo, operation)
    # An operation that does not need labels carries the values and leaves
    # the labels null (never False, never True).
    frames = _frames(tm.build(repo, "all_rollouts"))
    assert frames.recap_positive.isna().all()
    assert frames.recap_advantage.isna().all()
    assert frames.recap_value.notna().all()


def test_comparison_with_a_value_only_side_compares_values_only(client, monkeypatch):
    ds = jobs.Dataset(
        "local/pair",
        "pair",
        Path("/nonexistent"),
        {"fps": 10},
        {ep: {"length": n} for ep, (*_, n) in EPISODES.items()},
        {},
    )
    monkeypatch.setattr(jobs, "dataset", lambda _repo: ds)
    monkeypatch.setattr(ds, "fingerprint", dict)
    a = _publish("pair", value_only=False)["revision_id"]
    b = _publish("pair", value_only=True)["revision_id"]
    result = compare.compare_payload(ds.repo_id, a, b)
    assert result["labels"] is None and result["advantage"] is None
    assert result["value"]["mean_abs_diff"] == 0.0
    assert "labels_unavailable" in result["notes"]
    assert result["per_episode"][0]["label_agreement"] is None
    assert result["b"]["dataset_type"] == "value_only"
    response = client.get(
        "/annotations/api/recap/compare",
        params={"repo_id": ds.repo_id, "a": a, "b": b},
    )
    assert response.status_code == 200, response.text
