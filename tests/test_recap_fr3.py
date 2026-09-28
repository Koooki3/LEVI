"""FR3 RECAP value r1 integration: the ``fr3_recap`` preset, shared base
models, RLinf's unified threshold over several datasets, the RLinf-format
``is_success`` outcome and the training static-pose filter."""

import importlib.util
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest
from test_recap_value import BODY, _training_run, finish, run, store_env  # noqa: F401
from test_views import register_raw

from levi import catalog
from levi.recap import advantage, base_models, checkpoints, jobs, static_filter


# ---------------------------------------------------------------- preset


def test_fr3_preset_fills_the_training_run_fields(store_env, tmp_path, capsys):
    from levi.recap.cli import main

    step_dir = _training_run(tmp_path)
    code = main(
        [
            "import",
            str(step_dir),
            "--name",
            "fr3",
            "--preset",
            "fr3_recap",
            "--return-min",
            "-488",
            "--return-max",
            "0",
            "--provenance",
            "returns_tag=fr3_r1_fail300",
        ]
    )
    assert code == 0
    manifest = json.loads(capsys.readouterr().out)["manifest"]
    assert manifest["env_type"] == "fr3_recap" and manifest["model_type"] == "pi05"
    assert manifest["views"] == {
        "base_0_rgb": "observation.images.view1",
        "left_wrist_0_rgb": "observation.images.hand",
        "right_wrist_0_rgb": None,
    }
    assert manifest["critic_expert_variant"] == "gemma_1m"
    assert (manifest["action_dim"], manifest["action_horizon"]) == (7, 10)
    assert (manifest["num_bins"], manifest["v_min"], manifest["v_max"]) == (201, -1, 0)
    assert (manifest["return_min"], manifest["return_max"]) == (-488, 0)
    assert manifest["lookahead"] == 10 and manifest["positive_quantile"] == 0.3
    assert manifest["static_filter"]["xyz_threshold_m"] == 0.005
    assert manifest["provenance"] == {"returns_tag": "fr3_r1_fail300"}
    # The preset's input transform is checked: other views are refused.
    main(["set", "fr3", "--views", "left_wrist=none"])
    reason = json.loads(capsys.readouterr().out)["reason"]
    assert "differ from the fr3_recap input transform" in reason
    # So is a variant that disagrees with the weights' shapes.
    folder, manifest_obj = checkpoints.load("fr3")
    checkpoints.write_manifest(
        folder,
        manifest_obj.model_copy(
            update={"inspection": {"inferred_variant": "gemma_100m"}}
        ),
    )
    assert "weights have the shape of gemma_100m" in checkpoints.listing()[0]["reason"]
    assert main(["import", str(step_dir), "--name", "x", "--preset", "nope"]) == 1
    assert "Unknown preset" in capsys.readouterr().err


def test_worker_padding_mask_follows_model_type():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations/recap_value"))
    try:
        from levi_recap_worker.rlinf_provider import padding_mask
    finally:
        sys.path.pop(0)
    # RLinf LiberoInputs / FrankaEEInputs: a padded slot is masked except for
    # pi0-FAST.
    assert padding_mask({"model_type": "pi05"}) is False
    assert padding_mask({"model_type": "pi0"}) is False
    assert padding_mask({}) is False
    assert padding_mask({"model_type": "pi0_fast"}) is True
    with pytest.raises(ValueError):
        padding_mask({"model_type": "pi1"})


# ---------------------------------------------------------------- base models


def _model_folder(root: Path, files: dict[str, bytes]) -> Path:
    root.mkdir(parents=True)
    for name, data in files.items():
        (root / name).write_bytes(data)
    return root


def test_base_model_import_verifies_and_labels(store_env, tmp_path, monkeypatch):
    config, tok = b'{"model_type": "gemma3_text"}\n', b"tokenizer bytes"
    monkeypatch.setitem(
        base_models.OFFICIAL,
        "example/model",
        {
            "revision": "0" * 40,
            "files": {
                "config.json": ("git", base_models.git_blob(_tmp(tmp_path, config))),
                "tokenizer.model": ("sha256", base_models.sha256(_tmp(tmp_path, tok))),
            },
        },
    )
    good = _model_folder(
        tmp_path / "good", {"config.json": config, "tokenizer.model": tok, "model.safetensors": b"w"}
    )
    record = base_models.import_base(store_env, good, "m", official="example/model")
    assert record["verified"] and not record["dev_only"]
    assert not record["weights_included"]  # weights only with weights=True
    placed = store_env / "_base/m"
    assert sorted(p.name for p in placed.iterdir()) == [
        "config.json",
        "levi_base.json",
        "tokenizer.model",
    ]
    assert good.joinpath("config.json").read_bytes() == config  # source untouched
    # A different file is refused unless labelled as development-only.
    bad = _model_folder(tmp_path / "bad", {"config.json": b"{}", "tokenizer.model": tok})
    with pytest.raises(ValueError, match="differs from example/model"):
        base_models.import_base(store_env, bad, "b", official="example/model")
    dev = base_models.import_base(
        store_env, bad, "b", official="example/model", label="DEV mirror"
    )
    assert dev["dev_only"] and not dev["verified"]
    # A package hash list (sha256sum format, folder-prefixed paths) verifies too.
    sums = tmp_path / "pretrained_models.sha256"
    sums.write_text(
        f"{base_models.sha256(bad / 'config.json')}  bad/config.json\n"
        f"{base_models.sha256(bad / 'tokenizer.model')}  bad/tokenizer.model\n"
    )
    listed = base_models.import_base(store_env, bad, "c", sha256_file=sums)
    assert listed["verified"] and not listed["dev_only"]
    sums.write_text(f"{'0' * 64}  bad/config.json\n")
    with pytest.raises(ValueError, match="differs from"):
        base_models.import_base(store_env, bad, "d", sha256_file=sums)
    # The store listing never shows _base as a checkpoint.
    assert all(r["name"] != "_base" for r in checkpoints.listing())
    assert {b["name"] for b in base_models.listing(store_env)} == {"m", "b", "c"}
    # A checkpoint pointing at a dev folder says so.
    step_dir = _training_run(tmp_path)
    checkpoints.import_checkpoint(
        step_dir,
        "k",
        inspect=False,
        preset="fr3_recap",
        fields={"base_models": {"siglip": "../_base/m", "gemma3": "../_base/b", "tokenizer": "../_base/m"}},
    )
    row = next(r for r in checkpoints.listing() if r["name"] == "k")
    assert row["dev_only_base_models"] == ["gemma3"]


def _tmp(tmp_path: Path, data: bytes) -> Path:
    path = tmp_path / f"blob-{abs(hash(data))}"
    path.write_bytes(data)
    return path


def test_official_hashes_cover_the_files_rlinf_reads():
    for repo in ("google/gemma-3-270m", "google/siglip2-so400m-patch14-224"):
        files = base_models.OFFICIAL[repo]["files"]
        assert "config.json" in files and "tokenizer.model" in files
        for kind, digest in files.values():
            assert kind in ("git", "sha256")
            assert len(digest) == (40 if kind == "git" else 64)


# ---------------------------------------------------------------- static filter


def _write_demo(demo: Path, xyz, grip, quat=None):
    demo.mkdir(parents=True)
    n = len(xyz)
    ids = np.arange(1, n + 1)
    t = 1.7e9 + ids / 10
    quat = np.tile([0.0, 0.0, 0.0, 1.0], (n, 1)) if quat is None else np.asarray(quat)
    pd.DataFrame(
        {
            "timestamp_sec": t,
            "frame_index": ids,
            "success_flag": 0,
            "source_stamp_sec": t,
            "px": np.asarray(xyz)[:, 0],
            "py": np.asarray(xyz)[:, 1],
            "pz": np.asarray(xyz)[:, 2],
            "qx": quat[:, 0],
            "qy": quat[:, 1],
            "qz": quat[:, 2],
            "qw": quat[:, 3],
        }
    ).to_csv(demo / "end_effector_pose.csv", index=False)
    pd.DataFrame(
        {
            "timestamp_sec": t,
            "frame_index": ids,
            "success_flag": 0,
            "source_stamp_sec": t,
            "finger_left": 0.04,
            "finger_right": 0.04,
            "gripper_width": 0.08,
            "last_gripper_command": grip,
        }
    ).to_csv(demo / "gripper_state.csv", index=False)
    pd.DataFrame(
        {"timestamp_sec": t, "frame_index": ids, "success_flag": 0,
         "wrist_video": "wrist_camera.mp4", "side_video": "side_camera.mp4"}
    ).to_csv(demo / "frames.csv", index=False)


PARAMS = {
    "xyz_threshold_m": 0.005,
    "euler_threshold_rad": 0.01,
    "gripper_epsilon": 1e-6,
    "gripper_protect_margin": 2,
    "min_frames": 2,
}


def test_static_filter_rule(tmp_path):
    # Steps in metres between consecutive rows (row 0 is the start).
    steps = [0, 0.001, 0.0049, 0.005, 0.02, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    xyz = np.zeros((len(steps), 3))
    xyz[:, 0] = np.cumsum(steps)
    grip = ["open"] * 9 + ["close"] * 4  # flip between rows 8 and 9
    _write_demo(tmp_path / "d", xyz, grip)
    out = static_filter.kept_positions(tmp_path / "d", PARAMS)
    # Pairwise (not against the last kept row): 1, 2 static (< 5 mm) → drop;
    # 3 moves exactly 5 mm (not < 5 mm) → keep; 4 moves → keep; 5, 6 static
    # → drop; 7..11 are within 2 rows of the flip at row 9 → kept; 12 static
    # and outside the margin → drop. Row 0 is never a "later" frame.
    assert out["keep"] == [0, 3, 4, 7, 8, 9, 10, 11]
    assert out["frames"] == 13 and out["pairs"] == 12
    # Rotation: an Euler step above 0.01 rad keeps the frame.
    angle = 0.02
    quat = [[0, 0, 0, 1], [np.sin(angle / 2), 0, 0, np.cos(angle / 2)], [np.sin(angle / 2), 0, 0, np.cos(angle / 2)]]
    _write_demo(tmp_path / "r", np.zeros((3, 3)), ["open"] * 3, quat)
    assert static_filter.kept_positions(tmp_path / "r", PARAMS)["keep"] == [0, 1]
    # Too few aligned states: the training script skips the demo.
    _write_demo(tmp_path / "s", np.zeros((1, 3)), ["open"])
    assert static_filter.kept_positions(tmp_path / "s", PARAMS)["skipped"]


def test_static_filter_matches_the_training_script(tmp_path):
    """Set LEVI_TEST_RECAP_FILTER_SCRIPT to filter_static_pose_frames.py to
    compare the transcription with the original on random demos."""
    script = os.environ.get("LEVI_TEST_RECAP_FILTER_SCRIPT")
    if not script or not Path(script).is_file():
        pytest.skip("LEVI_TEST_RECAP_FILTER_SCRIPT is not set")
    pytest.importorskip("cv2")
    spec = importlib.util.spec_from_file_location("fr3_filter_script", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules["fr3_filter_script"] = module
    spec.loader.exec_module(module)
    rng = np.random.default_rng(0)
    for k in range(20):
        n = int(rng.integers(20, 120))
        xyz = np.cumsum(rng.choice([0, 0.001, 0.004, 0.006, 0.02], size=(n, 3)) / 1.7, axis=0)
        grip = np.where(np.cumsum(rng.random(n) < 0.05) % 2 == 0, "open", "close")
        demo = tmp_path / f"d{k}"
        _write_demo(demo, xyz, list(grip))
        drop, _, _ = module.choose_frames_to_drop(
            module.load_aligned_states(demo), 0.005, 0.01, 1e-6
        )
        _, _, positions, _ = module.build_keep_plan(demo, drop)
        assert static_filter.kept_positions(demo, PARAMS)["keep"] == sorted(positions)


def _fake_filtered(store_env, fields=None):
    checkpoints.import_checkpoint(
        None,
        "fake-f",
        provider="fake",
        fields={
            "failure_reward": -30.0,
            "static_filter": PARAMS,
            "return_min": -60.0,
            "return_max": 0.0,
            **(fields or {}),
        },
    )


@pytest.fixture
def raw_capture(client, store_env, tmp_path):
    from test_formats import capture_fixture

    _fake_filtered(store_env)
    entry = register_raw(client, capture_fixture(tmp_path / "plates"))
    yield client, entry
    assert not jobs.wait_idle(60)


def test_static_filtered_run_labels_only_kept_frames(raw_capture):
    client, entry = raw_capture
    repo = entry["id"]
    job = run(client, repo, checkpoint="fake-f")
    assert job["status"] == "succeeded", job
    name = entry["name"]
    record = jobs.store.revision(name)
    info = record["static_filter"]
    assert info["applied"] and info["rule"] == static_filter.RULE
    source = Path(entry["path"])
    demos = sorted(p for p in source.rglob("demo_*") if p.is_dir())
    expected = [static_filter.kept_positions(d, PARAMS)["keep"] for d in demos]
    assert info["kept_frames"] == sum(map(len, expected)) < info["frames"] == 30 + 34
    for ep, keep in enumerate(expected):
        payload = client.get(
            f"/annotations/api/recap/episodes/{ep}", params={"repo_id": repo}
        ).json()
        assert payload["frame_index"] == keep  # the dropped frames are absent
        assert payload["static_filter"] is True
        assert payload["episode_frames"] == 30 + 4 * ep
        # Returns and advantages run over the kept sequence.
        table = pq.read_table(
            jobs.store.root(name) / "revisions" / record["revision_id"] / f"episode-{ep:06d}.parquet"
        ).to_pandas()
        success = record["outcomes"][str(ep)] == "success"
        returns, rewards = advantage.episode_rewards(len(keep), success, 1.0, -30.0)
        want = advantage.episode_advantages(
            table["value"].to_numpy(np.float64), returns, rewards,
            lookahead=10, gamma=1.0, ret_min=-60.0, ret_max=0.0,
        )
        assert np.allclose(table["advantage"], want["advantage"], atol=1e-6)
    status = client.get("/annotations/api/recap/status", params={"repo_id": repo}).json()
    assert status["current"]["static_filter"]["kept_frames"] == info["kept_frames"]
    # Off: every frame.
    job = run(client, repo, checkpoint="fake-f", static_filter="off")
    assert job["status"] == "succeeded"
    assert jobs.store.revision(name)["frames"] == 30 + 34
    assert not jobs.store.revision(name)["static_filter"]["applied"]


def test_static_filter_needs_a_raw_capture(client, store_env, tmp_path):
    from test_formats import lerobot_fixture

    _fake_filtered(store_env)
    root = lerobot_fixture(catalog.STATE.parent / "ds")
    entry = catalog.register(str(root))
    repo = entry["id"]
    # "auto" leaves a LeRobot dataset as it is (taken as already filtered)…
    job = run(client, repo, checkpoint="fake-f")
    assert job["status"] == "succeeded", job
    info = jobs.store.revision(entry["name"])["static_filter"]
    assert not info["applied"] and "not a raw robot-capture view" in info["reason"]
    # …and "on" refuses it.
    body = {**BODY, "repo_id": repo, "checkpoint": "fake-f", "static_filter": "on"}
    response = client.post("/annotations/api/recap/run", params={"repo_id": repo}, json=body)
    assert response.status_code == 400 and "static filter" in response.json()["detail"]
    assert not jobs.wait_idle(60)


# ---------------------------------------------------------------- threshold, outcomes


def test_unified_threshold_over_datasets_and_is_success_outcomes(client, store_env, tmp_path):
    from test_formats import lerobot_fixture

    checkpoints.import_checkpoint(
        None, "fake-a", provider="fake",
        fields={"failure_reward": -30.0, "return_min": -70.0, "return_max": 0.0},
    )
    entries = []
    for name in ("one", "two"):
        root = lerobot_fixture(catalog.STATE.parent / name)
        # An RLinf-format RECAP dataset: the outcome is episodes.jsonl is_success.
        rows = [json.loads(line) for line in (root / "meta/episodes.jsonl").read_text().splitlines()]
        for row in rows:
            row.pop("levi_outcome", None)
            row["is_success"] = row["episode_index"] == 0
        (root / "meta/episodes.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        entries.append(catalog.register(str(root)))
    for entry, dataset_type in zip(entries, ("rollout", "sft")):
        job = run(client, entry["id"], checkpoint="fake-a", dataset_type=dataset_type)
        assert job["status"] == "succeeded", job
    first = jobs.store.revision(entries[0]["name"])
    assert first["outcomes"] == {"0": "success", "1": "failure"}
    found = jobs.unified_threshold([e["id"] for e in entries], 0.3)
    scores = np.concatenate(
        [
            pq.read_table(
                jobs.store.root(e["name"]) / "revisions" / jobs.store.current_id(e["name"]) / "advantages.parquet"
            ).column("advantage_continuous").to_numpy()
            for e in entries
        ]
    )
    assert found["threshold"] == float(np.percentile(scores, 70.0))
    assert found["frames"] == len(scores)
    assert sum(d["positive_at_threshold"] for d in found["datasets"]) == found["positive_frames"]
    from levi.recap.cli import main

    assert main(["threshold", *[e["id"] for e in entries], "--set", "fake-a",
                 "--provenance-text", "recomputed in a test"]) == 0
    _folder, manifest = checkpoints.load("fake-a")
    assert manifest.unified_threshold == found["threshold"]
    assert manifest.provenance["unified_threshold"] == "recomputed in a test"
    # A later run uses it and records where it came from.
    job = run(client, entries[0]["id"], checkpoint="fake-a")
    record = jobs.store.revision(entries[0]["name"])
    assert record["threshold_source"] == "checkpoint"
    assert record["threshold_provenance"] == "recomputed in a test"
    assert not jobs.wait_idle(60)
