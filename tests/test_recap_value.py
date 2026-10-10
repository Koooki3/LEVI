"""RECAP value model in LEVI: checkpoints, RLinf's advantage, storage, routes.

The routes run the ``fake`` provider through the real worker module with
LEVI's own Python, so the whole job lifecycle is exercised without Torch.
"""

import ast
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import pytest
from test_views import register_raw

from levi import catalog
from levi.recap import advantage, checkpoints, jobs, store
from levi.recap.advantage import episode_advantages, episode_rewards

PROJECT = Path(__file__).resolve().parents[1]
VENDORED = PROJECT / "integrations/recap_value/levi_recap_worker/rlinf"


# ---------------------------------------------------------------- formula


def test_hand_computed_advantage_with_episode_end():
    values = np.array([-0.5, -0.4, -0.3, -0.2, -0.1])
    returns, rewards = episode_rewards(5, True, 1.0, -300.0)
    assert returns.tolist() == [-4, -3, -2, -1, 0]
    out = episode_advantages(
        values, returns, rewards, lookahead=2, gamma=1.0, ret_min=-10.0, ret_max=0.0
    )
    # normalize(x) = (x + 10) / 10 - 1 = x / 10.
    # t=0: R = (G0 - G2)/10 = -0.2; A = -0.2 + V2 - V0.
    # t=3: t+N = 5 >= n: R = G3/10 = -0.1, V_next = 0; A = -0.1 - V3.
    # t=4: R = 0, V_next = 0; A = -V4.
    expected = [
        -0.2 + (-0.3) - (-0.5),
        -0.2 + (-0.2) - (-0.4),
        -0.2 + (-0.1) - (-0.3),
        -0.1 + 0.0 - (-0.2),
        0.0 + 0.0 - (-0.1),
    ]
    assert np.allclose(out["advantage"], expected)
    assert out["value_next"].tolist() == [-0.3, -0.2, -0.1, 0.0, 0.0]
    assert out["num_valid_rewards"].tolist() == [2, 2, 2, 2, 1]
    assert np.allclose(out["reward_sum_raw"], [-2, -2, -2, -1, 0])


def test_failure_terminal_and_discounted_rewards():
    returns, rewards = episode_rewards(4, False, 1.0, -300.0)
    assert returns.tolist() == [-303, -302, -301, -300]
    out = episode_advantages(
        np.zeros(4), returns, rewards, lookahead=3, gamma=1.0, ret_min=-303, ret_max=0
    )
    # t=0: R_raw = G0 - G3 = -3; t=1: pad, R_raw = G1 = -302.
    assert out["reward_sum_raw"].tolist() == [-3, -302, -301, -300]
    assert np.allclose(out["reward_sum"][0], -3 / 303 + 303 / 303 - 1)
    # gamma < 1 sums the discounted rewards and discounts V_next by gamma^N.
    returns, rewards = episode_rewards(4, True, 0.9, -300.0)
    values = np.array([-0.4, -0.3, -0.2, -0.1])
    out = episode_advantages(
        values, returns, rewards, lookahead=2, gamma=0.9, ret_min=-3.0, ret_max=0.0
    )
    raw0 = -1 + 0.9 * -1
    assert np.isclose(out["reward_sum_raw"][0], raw0)
    assert np.isclose(out["advantage"][0], (raw0 + 3) / 3 - 1 + 0.81 * -0.2 + 0.4)
    # Last frame: one valid reward (0 on success), V_next 0.
    assert np.isclose(out["advantage"][3], (0 + 3) / 3 - 1 + 0.1)
    # An empty return range normalises to -0.5, as in RLinf.
    flat = episode_advantages(
        np.zeros(2), [0, 0], [0, 0], lookahead=1, gamma=1.0, ret_min=0, ret_max=0
    )
    assert flat["reward_sum"].tolist() == [-0.5, -0.5]


def test_threshold_and_labels_match_rlinf_helpers():
    spec = importlib.util.spec_from_file_location(
        "rlinf_adv", VENDORED / "advantage.py"
    )
    rlinf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rlinf)
    scores = np.random.default_rng(3).normal(size=1000)
    for fraction in (0.1, 0.3, 0.5):
        ours = advantage.quantile_threshold(scores, fraction)
        assert ours == rlinf.quantile_threshold(scores, fraction)
        assert np.array_equal(
            advantage.label(scores, ours),
            rlinf.apply_boolean_label(scores, ours, inclusive=True),
        )
    assert advantage.label(scores, 99.0, sft=True).all()


def _rlinf_phase_two():
    """RLinf's own phase-2 loop, lifted from compute_advantages.py (the
    vendored copy, or ``RLINF_SOURCE``'s when set)."""
    source = os.environ.get("RLINF_SOURCE")
    path = (
        Path(source)
        / "examples/offline_rl/advantage_labeling/recap/process/compute_advantages.py"
        if source
        else PROJECT / "integrations/recap_value/vendor/rlinf/compute_advantages.py"
    )
    tree = ast.parse(path.read_text())
    func = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "compute_advantages_for_dataset"
    )
    loop = next(
        n
        for n in func.body
        if isinstance(n, ast.For)
        and isinstance(n.target, ast.Name)
        and n.target.id == "i"
    )
    normalize = next(
        n for n in func.body if isinstance(n, ast.FunctionDef) and n.name == "normalize"
    )
    module = ast.Module(body=[normalize, loop], type_ignores=[])
    return compile(ast.fix_missing_locations(module), str(path), "exec")


class _Stats:
    def update(self, _x):
        pass


@pytest.mark.parametrize("gamma", [1.0, 0.95])
def test_advantages_equal_rlinf_loop_on_the_same_values(gamma):
    code = _rlinf_phase_two()
    lengths = [7, 3, 12, 1]
    outcomes = [True, False, True, False]
    rng = np.random.default_rng(7)
    values = [rng.uniform(-1, 0, size=n) for n in lengths]
    parts = [episode_rewards(n, s, gamma, -30.0) for n, s in zip(lengths, outcomes)]
    ret_min = float(min(p[0].min() for p in parts))
    ret_max = float(max(p[0].max() for p in parts))
    ends = np.cumsum(lengths).tolist()
    results = {
        k: []
        for k in (
            "episode_index",
            "frame_index",
            "advantage",
            "return",
            "value_current",
            "value_next",
            "reward_sum",
            "reward_sum_raw",
            "num_valid_rewards",
        )
    }
    total = sum(lengths)
    namespace = {
        "np": np,
        "ret_min": ret_min,
        "ret_max": ret_max,
        "ret_range": ret_max - ret_min,
        "shard_size": total,
        "shard_start": 0,
        "extended_size": total,
        "meta_ep_idx": np.concatenate([np.full(n, e) for e, n in enumerate(lengths)]),
        "meta_frame_idx": np.concatenate([np.arange(n) for n in lengths]),
        "meta_return": np.concatenate([p[0] for p in parts]).astype(np.float64),
        "meta_reward": np.concatenate([p[1] for p in parts]).astype(np.float64),
        "v_values": np.concatenate(values),
        "ep_ends": dict(enumerate(ends)),
        "action_horizon": 4,
        "gamma": gamma,
        "gamma_powers": np.array([gamma**i for i in range(4)]),
        "discount_next_value": True,
        "v_curr_stats": _Stats(),
        "v_next_stats": _Stats(),
        "reward_sum_raw_stats": _Stats(),
        "results": results,
        "flush_every_samples": 10**9,
        "flush_results_to_disk": lambda: None,
    }
    exec(code, namespace)  # noqa: S102 -- RLinf's own loop, from its source
    ours = [
        episode_advantages(
            v, p[0], p[1], lookahead=4, gamma=gamma, ret_min=ret_min, ret_max=ret_max
        )
        for v, p in zip(values, parts)
    ]
    for key, theirs in (
        ("advantage", "advantage"),
        ("value_next", "value_next"),
        ("reward_sum", "reward_sum"),
        ("reward_sum_raw", "reward_sum_raw"),
        ("num_valid_rewards", "num_valid_rewards"),
    ):
        mine = np.concatenate([o[key] for o in ours])
        assert np.array_equal(mine, np.asarray(results[theirs])), key


# ---------------------------------------------------------------- checkpoints


@pytest.fixture
def store_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LEVI_RECAP_VALUE_CHECKPOINT_DIR", str(tmp_path / "ckpt"))
    monkeypatch.setenv("LEVI_RECAP_VALUE_WORKER_PYTHON", str(tmp_path / "no-python"))
    return tmp_path / "ckpt"


def _training_run(tmp_path):
    actor = tmp_path / "run/checkpoints/global_step_3000/actor/model_state_dict"
    actor.mkdir(parents=True)
    (actor / "full_weights.pt").write_bytes(b"not really torch" * 1000)
    return actor.parents[1]


def test_cli_import_copies_hashes_and_reports_readiness(store_env, tmp_path, capsys):
    from levi.recap.cli import main

    step_dir = _training_run(tmp_path)
    source = step_dir / "actor/model_state_dict/full_weights.pt"
    before = source.read_bytes()
    code = main(
        [
            "import",
            str(step_dir),
            "--name",
            "fr3-step3000",
            "--return-min",
            "-700",
            "--views",
            "base=observation.images.view1,left_wrist=observation.images.hand",
        ]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    manifest = out["manifest"]
    assert manifest["schema"] == checkpoints.SCHEMA
    assert manifest["step"] == 3000 and manifest["return_min"] == -700
    assert manifest["sha256"] == checkpoints.sha256(source)
    assert manifest["views"]["left_wrist_0_rgb"] == "observation.images.hand"
    assert manifest["inspection"]["error"].startswith("worker environment not found")
    assert source.read_bytes() == before  # the source is only read
    copied = store_env / "fr3-step3000/full_weights.pt"
    assert copied.read_bytes() == before
    assert not out["ready"]
    assert "critic_expert_variant is not set" in out["reason"]
    assert "base_models.siglip is not set" in out["reason"]
    # A name is imported once.
    assert main(["import", str(source), "--name", "fr3-step3000"]) == 1
    assert "already exists" in capsys.readouterr().err
    # Fields are confirmed later with `set`; base models inside the folder.
    for sub in ("siglip", "gemma3"):
        (store_env / "fr3-step3000" / sub).mkdir()
        (store_env / "fr3-step3000" / sub / "config.json").write_text("{}")
    (store_env / "fr3-step3000/tok").mkdir()
    (store_env / "fr3-step3000/tok/tokenizer.json").write_text("{}")
    assert (
        main(
            [
                "set",
                "fr3-step3000",
                "--variant",
                "gemma_100m",
                "--siglip",
                "siglip",
                "--gemma3",
                "gemma3",
                "--tokenizer",
                "tok",
            ]
        )
        == 0
    )
    edited = json.loads(capsys.readouterr().out)
    assert edited["ready"], edited["reason"]
    assert edited["manifest"]["variant_source"] == "given"
    assert main(["checkpoints", "--verify"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["verify"]["ok"]
    # A changed file is noticed by size (cheap) and by hash (--verify).
    copied.unlink()
    copied.write_bytes(b"x")
    assert "changed size" in checkpoints.listing()[0]["reason"]


def test_manifest_validation(store_env, tmp_path, capsys):
    from levi.recap.cli import main

    source = _training_run(tmp_path) / "actor"
    assert main(["import", str(source), "--name", "a", "--variant", "gemma_9b"]) == 1
    assert "critic_expert_variant" in capsys.readouterr().err
    assert main(["import", str(source), "--name", "b", "--views", "base=hand"]) == 1
    assert "observation.images" in capsys.readouterr().err
    assert main(["import", str(source), "--name", "../x"]) == 1
    capsys.readouterr()
    outside = "/" + "tmp" + "/elsewhere"
    assert main(["import", str(source), "--name", "c", "--siglip", outside]) == 1
    assert "workspace" in capsys.readouterr().err
    assert not (store_env / "c").exists() and not list(store_env.glob(".c.*"))
    # A broken manifest is listed as not ready, never hidden.
    (store_env / "broken").mkdir(parents=True)
    (store_env / "broken/manifest.json").write_text('{"schema": "other"}')
    row = next(r for r in checkpoints.listing() if r["name"] == "broken")
    assert not row["ready"] and "Invalid manifest" in row["reason"]
    assert main(["import", "--fake", "--name", "demo"]) == 0
    assert json.loads(capsys.readouterr().out)["ready"]


# ---------------------------------------------------------------- routes


@pytest.fixture
def recap(client, store_env):
    checkpoints.import_checkpoint(
        None, "fake-a", provider="fake", fields={"failure_reward": -30.0}
    )
    yield client
    assert not jobs.wait_idle(60), "a RECAP watch thread outlived its test"


def finish(client, repo, job_id, limit=400):
    for _ in range(limit):
        job = client.get(
            f"/annotations/api/recap/jobs/{job_id}", params={"repo_id": repo}
        ).json()
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.05)
    raise AssertionError("RECAP job did not finish")


BODY = {
    "checkpoint": "fake-a",
    "episodes": None,
    "lookahead": 10,
    "positive_quantile": 0.3,
    "threshold": None,
}


def run(client, repo, **overrides):
    body = {**BODY, "repo_id": repo, **overrides}
    response = client.post(
        "/annotations/api/recap/run", params={"repo_id": repo}, json=body
    )
    assert response.status_code == 202, response.text
    return finish(client, repo, response.json()["id"])


@pytest.fixture
def capture(recap, tmp_path):
    from test_formats import capture_fixture

    entry = register_raw(recap, capture_fixture(tmp_path / "plates"))
    return entry


def test_fake_run_publishes_labels_and_serves_them(recap, capture):
    client, repo = recap, capture["id"]
    empty = client.get("/annotations/api/recap/status", params={"repo_id": repo})
    assert empty.status_code == 200
    status = empty.json()
    assert status["current"] is None and status["job"] is None
    assert status["worker"]["ready"] is False
    assert [c["name"] for c in status["checkpoints"]] == ["fake-a"]
    assert (
        status["checkpoints"][0]["ready"]
        and status["checkpoints"][0]["provider"] == "fake"
    )
    missing = client.get("/annotations/api/recap/episodes/0", params={"repo_id": repo})
    assert missing.status_code == 404
    assert missing.json() == {"detail": "No advantage labels for this dataset yet"}
    assert (
        client.get(
            "/annotations/api/recap/summary", params={"repo_id": repo}
        ).status_code
        == 404
    )
    # The viewer asks with optional=true: "no labels yet" is a 200 null.
    for path in ("/annotations/api/recap/summary", "/annotations/api/recap/episodes/0"):
        quiet = client.get(path, params={"repo_id": repo, "optional": "true"})
        assert quiet.status_code == 200 and quiet.json() is None
    unknown = client.get(
        "/annotations/api/recap/summary",
        params={"repo_id": "local/no-such-dataset", "optional": "true"},
    )
    assert unknown.status_code != 200

    job = run(client, repo)
    assert job["status"] == "succeeded", job
    assert set(job) == {
        "id",
        "repo_id",
        "checkpoint",
        "status",
        "progress",
        "error",
        "revision_id",
        "created_at",
    }
    assert job["repo_id"] == repo and job["checkpoint"] == "fake-a"
    status = client.get(
        "/annotations/api/recap/status", params={"repo_id": repo}
    ).json()
    current = status["current"]
    assert current["revision_id"] == job["revision_id"]
    assert current["episodes"] == 2 and current["frames"] == 30 + 34
    assert current["threshold_source"] == "dataset_quantile"
    assert current["lookahead"] == 10 and current["positive_quantile"] == 0.3
    assert current["stale"] is False and current["provider"] == "fake"
    assert abs(current["positive_fraction"] - 0.3) < 0.05
    assert status["job"]["id"] == job["id"]

    ep = client.get(
        "/annotations/api/recap/episodes/0", params={"repo_id": repo}
    ).json()
    assert ep["episode_index"] == 0 and ep["fps"] == 10.0
    assert ep["frame_index"] == list(range(30))
    assert len(ep["value"]) == len(ep["advantage"]) == len(ep["positive"]) == 30
    assert all(-1 <= v <= 0 for v in ep["value"])
    assert ep["positive"] == [a >= ep["threshold"] for a in ep["advantage"]]
    assert np.allclose(ep["timestamp"], np.arange(30) / 10)
    summary = client.get(
        "/annotations/api/recap/summary", params={"repo_id": repo}
    ).json()
    assert summary["revision_id"] == job["revision_id"]
    assert set(summary["episodes"]) == {"0", "1"}
    assert summary["episodes"]["1"]["frames"] == 34
    # The success episode is valued above the failure.
    assert (
        summary["episodes"]["0"]["mean_value"] > summary["episodes"]["1"]["mean_value"]
    )
    assert (
        client.get(
            "/annotations/api/recap/episodes/9", params={"repo_id": repo}
        ).status_code
        == 404
    )
    assert (
        client.get(
            "/annotations/api/recap/episodes/9",
            params={"repo_id": repo, "optional": "true"},
        ).json()
        is None
    )

    # Storage: per-episode parquet, RLinf's advantages table, provenance.
    folder = store.root(capture["name"]) / "revisions" / job["revision_id"]
    table = pq.read_table(folder / "episode-000000.parquet")
    assert table.schema.names == [f.name for f in store.EPISODE_SCHEMA]
    rlinf = pq.read_table(folder / "advantages.parquet")
    assert rlinf.schema.names == [
        "episode_index",
        "frame_index",
        "advantage_continuous",
        "return",
        "value_current",
        "value_next",
        "reward_sum",
        "reward_sum_raw",
        "num_valid_rewards",
        "dataset_name",
        "advantage",
    ]
    assert rlinf.num_rows == 64
    record = json.loads((folder / "revision.json").read_text())
    assert record["checkpoint"]["name"] == "fake-a"
    assert record["outcomes"] == {"0": "success", "1": "failure"}
    assert record["return_range_source"] == "dataset"
    assert record["fingerprint"]["source_fingerprint"]
    # The labels recompute from the stored values with RLinf's formula.
    values = table.column("value").to_numpy().astype(np.float64)
    returns, rewards = episode_rewards(30, True, 1.0, -30.0)
    again = episode_advantages(
        values,
        returns,
        rewards,
        lookahead=10,
        gamma=1.0,
        ret_min=record["return_min"],
        ret_max=record["return_max"],
    )
    assert np.allclose(
        again["advantage"], table.column("advantage").to_numpy(), atol=1e-6
    )
    # Nothing is written into the dataset or the capture.
    assert not (Path(capture["view"]) / "meta/advantages.parquet").exists()


def test_threshold_priority_and_stale(recap, capture):
    client, repo = recap, capture["id"]
    manual = run(client, repo, threshold=-0.05)
    ep = client.get(
        "/annotations/api/recap/episodes/1", params={"repo_id": repo}
    ).json()
    assert ep["threshold"] == -0.05 and ep["revision_id"] == manual["revision_id"]
    status = client.get(
        "/annotations/api/recap/status", params={"repo_id": repo}
    ).json()
    assert status["current"]["threshold_source"] == "manual"
    checkpoints.update("fake-a", {"unified_threshold": 0.01, "return_min": -64.0})
    run(client, repo)
    current = client.get(
        "/annotations/api/recap/status", params={"repo_id": repo}
    ).json()["current"]
    assert current["threshold_source"] == "checkpoint" and current["threshold"] == 0.01
    record = store.revision(capture["name"])
    assert record["return_min"] == -64.0 and record["return_max"] == 0.0
    assert record["return_range_source"] == "dataset"  # return_max still from data
    # A changed outcome label makes the labels stale ...
    client.post(
        "/annotations/api/episodes/1/outcome",
        json={"repo_id": repo, "episode_index": 1, "outcome": "success"},
    )
    stale = client.get("/annotations/api/recap/status", params={"repo_id": repo}).json()
    assert stale["current"]["stale"] is True
    client.post(
        "/annotations/api/episodes/1/outcome",
        json={"repo_id": repo, "episode_index": 1, "outcome": "failure"},
    )
    fresh = client.get("/annotations/api/recap/status", params={"repo_id": repo}).json()
    assert fresh["current"]["stale"] is False
    # ... and so does a changed capture.
    meta = Path(capture["view"]) / "meta/levi_view.json"
    view = json.loads(meta.read_text())
    meta.write_text(json.dumps({**view, "source_fingerprint": "changed"}))
    stale = client.get("/annotations/api/recap/status", params={"repo_id": repo}).json()
    assert stale["current"]["stale"] is True
    meta.write_text(json.dumps(view))


def test_results_are_separate_per_namespace(recap, capture):
    client, repo = recap, capture["id"]
    ns = catalog.create_namespace(capture["name"], "exp-a")
    ns_job = run(client, ns["id"], threshold=-0.2)
    assert ns_job["status"] == "succeeded" and ns_job["repo_id"] == ns["id"]
    base = client.get("/annotations/api/recap/episodes/0", params={"repo_id": repo})
    assert base.status_code == 404
    assert (
        client.get("/annotations/api/recap/status", params={"repo_id": repo}).json()[
            "current"
        ]
        is None
    )
    base_job = run(client, repo)
    assert base_job["revision_id"], base_job
    a = client.get(
        "/annotations/api/recap/episodes/0", params={"repo_id": ns["id"]}
    ).json()
    b = client.get("/annotations/api/recap/episodes/0", params={"repo_id": repo}).json()
    assert a["threshold"] == -0.2 and b["threshold"] != -0.2
    assert store.root(ns["name"]) != store.root(capture["name"])
    # A job is found through its own dataset only (ids are per dataset: the
    # base may have a job of the same minute, which is its own).
    wrong = client.get(
        f"/annotations/api/recap/jobs/{ns_job['id']}", params={"repo_id": repo}
    )
    assert wrong.status_code == 404 or wrong.json()["repo_id"] == repo
    # Namespace outcome labels are the namespace's own.
    client.post(
        "/annotations/api/episodes/0/outcome",
        json={"repo_id": ns["id"], "episode_index": 0, "outcome": "failure"},
    )
    assert client.get(
        "/annotations/api/recap/status", params={"repo_id": ns["id"]}
    ).json()["current"]["stale"]
    assert not client.get(
        "/annotations/api/recap/status", params={"repo_id": repo}
    ).json()["current"]["stale"]


def test_cancel_conflict_and_failure(recap, capture, monkeypatch):
    client, repo = recap, capture["id"]
    monkeypatch.setenv("LEVI_RECAP_VALUE_FAKE_DELAY_SECONDS", "30")
    started = client.post("/annotations/api/recap/run", json={**BODY, "repo_id": repo})
    assert started.status_code == 202, started.text
    job = started.json()
    assert job["status"] == "running" and job["progress"]["total"] == 64
    again = client.post("/annotations/api/recap/run", json={**BODY, "repo_id": repo})
    assert again.status_code == 409 and "already running" in again.json()["detail"]
    status = client.get(
        "/annotations/api/recap/status", params={"repo_id": repo}
    ).json()
    assert status["job"]["status"] == "running"
    cancelled = client.post(
        f"/annotations/api/recap/jobs/{job['id']}/cancel", params={"repo_id": repo}
    )
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert finish(client, repo, job["id"])["status"] == "cancelled"
    assert (
        client.get("/annotations/api/recap/status", params={"repo_id": repo}).json()[
            "current"
        ]
        is None
    )
    monkeypatch.delenv("LEVI_RECAP_VALUE_FAKE_DELAY_SECONDS")

    # A worker that dies without a result fails the job with its log.
    monkeypatch.setattr(
        jobs,
        "worker_command",
        lambda provider: [sys.executable, "-c", "print('boom'); raise SystemExit(3)"],
    )
    failed = run(client, repo)
    assert failed["status"] == "failed"
    assert "exited without a result (code 3)" in failed["error"]
    # A worker that reports an error fails with that error.
    monkeypatch.setattr(
        jobs,
        "worker_command",
        lambda provider: [
            sys.executable,
            "-c",
            (
                "import json,sys; p=sys.argv[sys.argv.index('--output')+1]; "
                "open(p,'w').write(json.dumps({'status':'failed','error':'no GPU'}))"
            ),
        ],
    )
    reported = run(client, repo)
    assert reported["status"] == "failed" and "no GPU" in reported["error"]
    assert (
        client.get("/annotations/api/recap/status", params={"repo_id": repo}).json()[
            "current"
        ]
        is None
    )


def test_cancel_wins_over_the_watcher_collecting_the_stopped_worker(
    recap, capture, monkeypatch
):
    """Regression: the watch thread collects as soon as the stopped worker
    exits; cancel used to record "cancelled" only afterwards, so a cancelled
    job was reported as failed ("exited without a result") about half the
    time. The watcher's collect is forced to win the race here."""
    client, repo = recap, capture["id"]
    monkeypatch.setenv("LEVI_RECAP_VALUE_FAKE_DELAY_SECONDS", "30")
    started = client.post("/annotations/api/recap/run", json={**BODY, "repo_id": repo})
    assert started.status_code == 202, started.text
    job_id = started.json()["id"]
    real_stop = jobs._stop

    def stop_then_collect(process, grace=10.0):
        real_stop(process, grace)
        jobs.collect(jobs.find(job_id, capture["name"]))

    monkeypatch.setattr(jobs, "_stop", stop_then_collect)
    cancelled = client.post(
        f"/annotations/api/recap/jobs/{job_id}/cancel", params={"repo_id": repo}
    )
    assert cancelled.json()["status"] == "cancelled"
    assert finish(client, repo, job_id)["status"] == "cancelled"


def test_refusals(recap, capture, monkeypatch, tmp_path):
    client, repo = recap, capture["id"]

    def post(**b):
        return client.post(
            "/annotations/api/recap/run", json={**BODY, "repo_id": repo, **b}
        )

    unknown = post(checkpoint="nope")
    assert (
        unknown.status_code == 400
        and "Unknown RECAP value checkpoint" in unknown.json()["detail"]
    )
    assert post(episodes=[7]).status_code == 400
    assert post(lookahead=0).status_code == 422
    # An rlinf checkpoint that is not confirmed is refused with its reasons.
    actor = _training_run(tmp_path) / "actor"
    checkpoints.import_checkpoint(actor, "real", inspect=False)
    refused = post(checkpoint="real")
    assert refused.status_code == 400
    assert "critic_expert_variant is not set" in refused.json()["detail"]
    # Episodes without an outcome are left out and reported; asked for, refused.
    rows = Path(capture["view"]) / "meta/episodes.jsonl"
    original = rows.read_text()
    lines = [json.loads(line) for line in original.splitlines()]
    lines[1].pop("levi_outcome", None)
    rows.write_text("\n".join(json.dumps(r) for r in lines) + "\n")
    try:
        assert post(episodes=[1]).status_code == 400
        job = run(client, repo)
        summary = client.get(
            "/annotations/api/recap/summary", params={"repo_id": repo}
        ).json()
        assert set(summary["episodes"]) == {"0"}
        assert summary["skipped_episodes"] == {"1": "no success/failure label"}
        sft = run(client, repo, dataset_type="sft")
        ep = client.get(
            "/annotations/api/recap/episodes/1", params={"repo_id": repo}
        ).json()
        assert sft["revision_id"] != job["revision_id"] and all(ep["positive"])
    finally:
        rows.write_text(original)
    assert (
        client.get(
            "/annotations/api/recap/jobs/20200101-0000", params={"repo_id": repo}
        ).status_code
        == 404
    )
    assert (
        client.get(
            "/annotations/api/recap/jobs/bad", params={"repo_id": repo}
        ).status_code
        == 400
    )
    assert (
        client.get(
            "/annotations/api/recap/status", params={"repo_id": "local/none"}
        ).status_code
        == 400
    )


def test_rlinf_refused_under_cpu_only_and_without_gpu_headroom(
    recap, capture, monkeypatch, tmp_path
):
    client, repo = recap, capture["id"]
    folder = checkpoints.store_dir() / "ready"
    checkpoints.import_checkpoint(
        _training_run(tmp_path) / "actor",
        "ready",
        inspect=False,
        fields={
            "critic_expert_variant": "gemma_1m",
            "views": {"base_0_rgb": "observation.images.view1"},
            "base_models": {"siglip": "s", "gemma3": "g", "tokenizer": "t"},
        },
    )
    for sub, name in (
        ("s", "config.json"),
        ("g", "config.json"),
        ("t", "tokenizer.json"),
    ):
        (folder / sub).mkdir()
        (folder / sub / name).write_text("{}")
    python = tmp_path / "python"
    python.write_text("")
    monkeypatch.setenv("LEVI_RECAP_VALUE_WORKER_PYTHON", str(python))
    monkeypatch.setenv("LEVI_CPU_ONLY", "1")
    body = {**BODY, "repo_id": repo, "checkpoint": "ready"}
    refused = client.post("/annotations/api/recap/run", json=body)
    assert refused.status_code == 400 and "LEVI_CPU_ONLY" in refused.json()["detail"]
    monkeypatch.delenv("LEVI_CPU_ONLY")
    from levi.agent import objects

    monkeypatch.setattr(
        objects,
        "gpu_headroom",
        lambda: {"known": True, "free_mib": 100, "total_mib": 32000},
    )
    low = client.post("/annotations/api/recap/run", json=body)
    assert low.status_code == 400 and "MiB of GPU memory" in low.json()["detail"]
    # A camera the dataset lacks is named before anything starts.
    checkpoints.update(
        "ready", {"views": {"left_wrist_0_rgb": "observation.images.top"}}
    )
    wrong = client.post("/annotations/api/recap/run", json=body)
    assert (
        wrong.status_code == 400 and "observation.images.top" in wrong.json()["detail"]
    )
    # The fake provider stays available under LEVI_CPU_ONLY.
    monkeypatch.setenv("LEVI_CPU_ONLY", "1")
    assert run(client, repo)["status"] == "succeeded"


def test_agent_reads_labels_without_side_effects(recap, capture):
    from levi import service
    from levi.agent.capabilities import SPECS, invoke
    from levi.agent.runtime import Workbench
    from levi.agent.security import Principal

    client, repo = recap, capture["id"]
    assert SPECS["recap.status"][1] == SPECS["recap.get"][1] == "read"
    run(client, repo)
    wb = Workbench(service.STATE)
    agent = Principal("conn", datasets=(repo,))
    status = invoke(wb, agent, "recap.status", {"repo_id": repo})
    assert status["current"]["frames"] == 64
    summary = invoke(wb, agent, "recap.get", {"repo_id": repo})
    assert set(summary["episodes"]) == {"0", "1"}
    digest = invoke(wb, agent, "recap.get", {"repo_id": repo, "episode": 1})
    assert sum(r["to_frame"] - r["from_frame"] + 1 for r in digest["runs"]) == 34
    assert len(digest["value_samples"]) == 4
    stranger = Principal("other", datasets=("local/elsewhere",))
    with pytest.raises(PermissionError):
        invoke(wb, stranger, "recap.status", {"repo_id": repo})


def test_cli_run_and_show(recap, capture, capsys):
    from levi.recap.cli import main

    assert (
        main(["run", capture["id"], "--checkpoint", "fake-a", "--episodes", "0"]) == 0
    )
    job = json.loads(capsys.readouterr().out)
    assert job["status"] == "succeeded"
    assert main(["show", capture["id"]]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert (
        shown["status"]["episodes"] == 1
        and shown["summary"]["episodes"]["0"]["frames"] == 30
    )
    assert main(["show", capture["id"], "--episode", "0"]) == 0
    assert len(json.loads(capsys.readouterr().out)["value"]) == 30
