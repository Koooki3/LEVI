"""Readers of RECAP results in the per-model layout: training manifests
(version pinned once, result digest, --recap-model) and the unified
threshold."""

import hashlib
import json

import numpy as np
import pytest
from test_training_manifest import EPISODES, _frames, repo  # noqa: F401 (fixture)

from levi import training_manifest as tm
from levi.recap import jobs, store


def _publish(name, model="r1", offset=0.0, episodes=None, subset=False):
    ds_episodes = {}
    for ep, (*_, n) in EPISODES.items():
        if episodes is not None and ep not in episodes:
            continue
        frames = np.arange(n)
        adv = np.where(frames % 2 == 0, 0.5, -0.5) + offset
        ds_episodes[ep] = {
            "frame_index": frames,
            "timestamp": frames / 10,
            "value": -frames / 100 - offset,
            "value_next": np.zeros(n),
            "reward_sum": np.zeros(n),
            "reward_sum_raw": np.zeros(n),
            "return": np.zeros(n),
            "advantage": adv,
            "num_valid_rewards": np.ones(n, dtype=np.int64),
            "positive": adv >= 0.0,
        }
    return store.publish(
        name,
        ds_episodes,
        {
            "checkpoint": {"name": model, "sha256": "0" * 64},
            "provider": "fake",
            "threshold": 0.0,
            "threshold_source": "checkpoint",
            "positive_quantile": 0.3,
            "lookahead": 10,
            "gamma": 1.0,
            "return_min": -10.0,
            "return_max": 0.0,
            "dataset_type": "rollout",
            "outcomes": {},
            "fingerprint": jobs.dataset("local/" + name).fingerprint(),
        },
        dataset_name=name,
        subset=subset,
    )


@pytest.fixture
def models(monkeypatch):
    monkeypatch.setenv("LEVI_RECAP_STORE_LAYOUT", "models")


def test_manifest_pins_the_version_and_records_its_digest(repo, models):  # noqa: F811
    name = repo.split("/", 1)[1]
    first = _publish(name)
    manifest = tm.build(repo, "advantage_positive_mask")
    recap = manifest["recap"]
    assert recap["revision_id"] == "r1" and recap["model"] == "r1"
    assert recap["version"] == first["version"] and recap["layout"] == "models"
    path = store.root(name) / "models/r1/v" / first["version"] / "advantages.parquet"
    assert (
        recap["result_digest"]
        == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    )
    frames = _frames(manifest)
    assert (frames.include == (frames.recap_positive == True)).all()
    # A second model; the manifest names the one it wants.
    _publish(name, model="r2", offset=-1.0)
    chosen = tm.build(repo, "advantage_positive_mask", recap_revision="r1")
    assert chosen["recap"]["model"] == "r1"
    assert tm.build(repo, "advantage_positive_mask")["recap"]["model"] == "r2"
    with pytest.raises(tm.ManifestError, match="No RECAP result 'r9'"):
        tm.build(repo, "advantage_positive_mask", recap_revision="r9")


def test_manifest_cli_takes_recap_model(repo, models, capsys):  # noqa: F811
    name = repo.split("/", 1)[1]
    _publish(name, model="r1")
    _publish(name, model="r2")
    assert (
        tm.main(
            [
                "manifest",
                repo,
                "--operation",
                "advantage_weighted",
                "--recap-model",
                "r1",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["recap"]["model"] == "r1"
    # The old option name still works.
    assert (
        tm.main(
            [
                "manifest",
                repo,
                "--operation",
                "advantage_weighted",
                "--recap-revision",
                "r2",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["recap"]["model"] == "r2"


def test_manifest_refuses_a_version_reclaimed_mid_export(
    repo,  # noqa: F811
    models,
    monkeypatch,
):
    name = repo.split("/", 1)[1]
    _publish(name)
    real = store.read_episode

    def reclaimed(dataset, episode, rid=None, version=None):
        return None if episode == 1 else real(dataset, episode, rid, version)

    monkeypatch.setattr(store, "read_episode", reclaimed)
    with pytest.raises(tm.ManifestError, match="recomputed while this manifest"):
        tm.build(repo, "all_rollouts")


def test_unified_threshold_reads_the_model_result(repo, models):  # noqa: F811
    name = repo.split("/", 1)[1]
    first = _publish(name)
    found = jobs.unified_threshold([repo], 0.5)
    assert found["datasets"][0]["revision_id"] == "r1"
    assert found["datasets"][0]["version"] == first["version"]
    assert found["frames"] == sum(n for *_, n in EPISODES.values())
    # After a merge the threshold reads the merged result (all episodes).
    _publish(name, offset=0.1, episodes=[0], subset=True)
    again = jobs.unified_threshold([repo], 0.5)
    assert again["frames"] == found["frames"]
    assert again["datasets"][0]["version"] != first["version"]


def test_cli_show_names_a_model(repo, models, monkeypatch, tmp_path, capsys):  # noqa: F811
    from levi.recap.cli import main

    monkeypatch.setenv("LEVI_RECAP_VALUE_CHECKPOINT_DIR", str(tmp_path / "ckpt"))
    name = repo.split("/", 1)[1]
    first = _publish(name, model="r1")
    _publish(name, model="r2")
    assert main(["show", repo, "--model", "r1"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["status"]["model"] == "r2"  # the current result
    assert shown["summary"]["revision_id"] == "r1"
    assert shown["summary"]["version"] == first["version"]
    assert main(["show", repo, "--model", "r1", "--episode", "0"]) == 0
    assert json.loads(capsys.readouterr().out)["model"] == "r1"
    assert main(["show", repo, "--model", "nope"]) == 1
    assert "No revision nope" in capsys.readouterr().err
