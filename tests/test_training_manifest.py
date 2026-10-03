"""Training manifests: operations, provenance, refusals, CLI/API and the
standalone trainer-side reader (integrations/training_manifest)."""

import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest

from levi import catalog, paths
from levi import training_manifest as tm

PROJECT = Path(__file__).resolve().parents[1]
TASKS = ["stack the plates", "pick the screws"]
# episode -> (task, robot flag, frames)
EPISODES = {
    0: (0, True, 12),
    1: (0, False, 10),
    2: (1, True, 8),
}


def _dataset(root: Path) -> Path:
    (root / "meta").mkdir(parents=True)
    (root / "data/chunk-000").mkdir(parents=True)
    info = {
        "codebase_version": "v2.1",
        "robot_type": "generic_arm",
        "fps": 10,
        "total_episodes": len(EPISODES),
        "total_frames": sum(n for *_, n in EPISODES.values()),
        "total_tasks": 2,
        "chunks_size": 1000,
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
        "features": {
            "action": {"dtype": "float32", "shape": [2], "names": ["x", "gripper"]},
            "observation.state": {
                "dtype": "float32",
                "shape": [2],
                "names": ["x", "gripper"],
            },
            "timestamp": {"dtype": "float32", "shape": [1], "names": None},
            "episode_index": {"dtype": "int64", "shape": [1], "names": None},
            "frame_index": {"dtype": "int64", "shape": [1], "names": None},
            "index": {"dtype": "int64", "shape": [1], "names": None},
            "task_index": {"dtype": "int64", "shape": [1], "names": None},
        },
    }
    (root / "meta/info.json").write_text(json.dumps(info))
    (root / "meta/tasks.jsonl").write_text(
        "".join(
            json.dumps({"task_index": i, "task": t}) + "\n" for i, t in enumerate(TASKS)
        )
    )
    rows, start = [], 0
    for ep, (task, flag, n) in EPISODES.items():
        rows.append(
            {
                "episode_index": ep,
                "tasks": [TASKS[task]],
                "length": n,
                "is_success": flag,
                "rollout_source_demo": f"/rollouts/demo_{ep:04d}",
            }
        )
        t = np.arange(n, dtype=float) / 10
        pd.DataFrame(
            {
                "action": [[float(x), 1.0] for x in t],
                "observation.state": [[float(x), 1.0] for x in t],
                "timestamp": t,
                "episode_index": [ep] * n,
                "frame_index": range(n),
                "index": range(start, start + n),
                "task_index": [task] * n,
            }
        ).to_parquet(root / f"data/chunk-000/episode_{ep:06d}.parquet")
        start += n
    (root / "meta/episodes.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows)
    )
    return root


@pytest.fixture
def repo(client, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "EXPORTS", tmp_path / "exports")
    root = _dataset(tmp_path / "rollouts")
    response = client.post("/api/levi/catalog", json={"path": str(root)})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _name(repo):
    return repo.split("/", 1)[1]


def _anchored(
    name, outcomes, run_id="anchored-20260928-0001", bases=None, status="stable"
):
    from levi.agent.anchored import builtin
    from levi.agent.store import Store

    store = Store(catalog.STATE)
    spec = builtin()["plates-release"].model_dump(by_alias=True)
    spec["status"] = status
    store.put(
        "runs",
        run_id,
        {
            "id": run_id,
            "status": "succeeded",
            "created_at": 1.0,
            "dataset_key": name,
            "context": {"workflow": {"kind": "anchored", "anchored": spec}},
            "provider_config": {"kind": "openai-local", "name": "q", "model": "m"},
        },
    )
    for ep, outcome in outcomes.items():
        store.put(
            "anchored",
            f"{run_id}:{ep}",
            {
                "episode_index": ep,
                "outcome": outcome,
                "basis": (bases or {}).get(
                    ep, {"undecided_labels": ["white"] if ep == 1 else []}
                ),
                "events": [],
            },
        )
    return run_id


def _recap(repo, threshold=0.0):
    """A published RECAP revision: frame f of each episode is positive when
    f is even; the last frame of episode 0 is unlabelled (static-filtered)."""
    from levi.recap import jobs, store

    ds = jobs.dataset(repo)
    episodes = {}
    for ep, (*_, n) in EPISODES.items():
        frames = np.arange(n - 1 if ep == 0 else n)
        adv = np.where(frames % 2 == 0, 0.5, -0.5)
        episodes[ep] = {
            "frame_index": frames,
            "timestamp": frames / 10,
            "value": -frames / 100,
            "value_next": np.zeros(len(frames)),
            "reward_sum": np.zeros(len(frames)),
            "reward_sum_raw": np.zeros(len(frames)),
            "return": np.zeros(len(frames)),
            "advantage": adv,
            "num_valid_rewards": np.ones(len(frames), dtype=np.int64),
            "positive": adv >= threshold,
        }
    return store.publish(
        ds.name,
        episodes,
        {
            "checkpoint": {"name": "fake-a", "sha256": "0" * 64},
            "provider": "fake",
            "threshold": threshold,
            "threshold_source": "request",
            "lookahead": 10,
            "dataset_type": "rollout",
            "fingerprint": ds.fingerprint(),
            "outcomes": {str(e): ds.outcome(e) for e in EPISODES},
            "static_filter": {"applied": True},
        },
        dataset_name=ds.name,
    )


def _subtasks(name):
    from levi.agent.store import resolve

    folder = resolve(catalog.STATE, name, "annotations")
    folder.mkdir(parents=True, exist_ok=True)
    atom = lambda sid, start, to, outcome: {
        "role": "assistant",
        "content": sid,
        "style": "subtask",
        "timestamp": start,
        "to": to,
        "levi": {"subtask_id": sid, "outcome": outcome, "attempt": 1},
    }
    (folder / "episode_000000.json").write_text(
        json.dumps(
            {
                "episode_index": 0,
                "atoms": [
                    atom("grasp", 0.0, 0.5, "success"),
                    atom("place", 0.5, None, "failure"),
                ],
            }
        )
    )


def _frames(result):
    return pq.read_table(Path(result["output_dir"]) / tm.FRAMES).to_pandas()


def test_a_contested_waiver_leaves_an_anchored_success_undecided(repo):
    """A success that rests on a start-check waiver its own events contest
    (or on an undecided veto) is flagged; a plain success is not."""
    name = _name(repo)
    plain = {"undecided_labels": [], "waived_labels": [], "contested_waivers": []}
    _anchored(
        name,
        {0: "success", 1: "success", 2: "success"},
        bases={
            0: plain | {"waived_labels": ["white"], "contested_waivers": ["white"]},
            1: plain | {"waived_labels": ["white"]},
            2: plain | {"undecided_vetoes": [{"veto": "x", "frame_index": 3}]},
        },
    )
    f = _frames(tm.build(repo, "all_rollouts"))
    flags = {
        ep: bool(f[f.episode_index == ep].anchored_undecided.iloc[0])
        for ep in (0, 1, 2)
    }
    assert flags == {0: True, 1: False, 2: True}

    # verified_success leaves undecided anchored successes out unless asked.
    verified = _frames(tm.build(repo, "verified_success"))
    assert set(verified[verified.include].episode_index) == {1}
    assert set(verified[verified.episode_index == 0].exclude_reason) == {
        "anchored_undecided"
    }
    kept = _frames(tm.build(repo, "verified_success", params={"undecided": "include"}))
    assert set(kept[kept.include].episode_index) == {0, 1, 2}


def test_operations_decide_include_and_weight(repo):
    name = _name(repo)
    run_id = _anchored(name, {0: "failure", 1: "failure"})
    _recap(repo)
    _subtasks(name)

    everything = tm.build(repo, "all_rollouts")
    f = _frames(everything)
    assert len(f) == 30 and f.include.all() and (f.weight == 1).all()
    assert everything["counts"]["episodes_included"] == 3
    # Evidence columns ride along whatever the operation.
    e0 = f[f.episode_index == 0]
    assert list(e0.subtask_id[:5]) == ["grasp"] * 5 and e0.subtask_id.iloc[5] == "place"
    assert e0.subtask_outcome.iloc[-1] == "failure"
    assert e0.recap_positive.iloc[-1] is None and e0.recap_positive.iloc[0]
    assert (
        e0.robot_flag.iloc[0] == "success" and e0.anchored_outcome.iloc[0] == "failure"
    )
    # Anchored review outranks the robot flag; episode 2 has no anchored verdict.
    assert e0.episode_success_source.iloc[0] == "anchored"
    assert f[f.episode_index == 2].episode_success_source.iloc[0] == "robot_flag"
    assert f[f.episode_index == 1].anchored_undecided.iloc[0]

    flag = _frames(tm.build(repo, "robot_flag_success"))
    assert set(flag[flag.include].episode_index) == {0, 2}
    assert set(flag[~flag.include].exclude_reason) == {"robot_flag_failure"}

    verified = tm.build(repo, "verified_success")
    v = _frames(verified)
    # Episode 0: the robot said success, the anchored review failure.
    assert set(v[v.include].episode_index) == {2}
    assert verified["anchored"]["run_id"] == run_id
    assert verified["counts"]["included_by_verdict_source"] == {"robot_flag": 1}
    strict = _frames(tm.build(repo, "verified_success", {"fallback": "exclude"}))
    assert not strict.include.any()
    assert set(strict[strict.episode_index == 2].exclude_reason) == {"unverified"}
    # The anchored spec is declared valid for plates only: screws keep the flag,
    # plates episodes use the anchored verdict.
    scoped = tm.build(repo, "verified_success", anchored_tasks=["pick the screws"])
    s = _frames(scoped)
    assert set(s[s.include].episode_index) == {0, 2}

    mask = _frames(tm.build(repo, "advantage_positive_mask"))
    assert (mask.include == (mask.recap_positive == True)).all()
    assert "advantage_unlabelled" in set(mask.exclude_reason)
    weighted = _frames(
        tm.build(
            repo,
            "advantage_weighted",
            {"negative_weight": 0.25, "unlabelled_weight": 0.5},
        )
    )
    expected = np.where(
        weighted.recap_positive.isna(),
        0.5,
        np.where(weighted.recap_positive == True, 1.0, 0.25),
    )
    assert np.allclose(weighted.weight, expected) and weighted.include.all()

    # A human label outranks both.
    from levi.agent.store import resolve
    from levi.annotations import outcomes

    outcomes.write_label(resolve(catalog.STATE, name, "annotations"), 0, "success")
    human = _frames(tm.build(repo, "verified_success"))
    assert set(human[human.include].episode_index) == {0, 2}
    assert human[human.episode_index == 0].episode_success_source.iloc[0] == "human"


def test_verified_success_from_human_labels_alone(repo):
    """A task without anchored-review rules is verified by people only."""
    from levi.agent.store import resolve
    from levi.annotations import outcomes

    name = _name(repo)
    labels = resolve(catalog.STATE, name, "annotations")
    outcomes.write_label(labels, 0, "failure")
    outcomes.write_label(labels, 2, "success")
    frames = _frames(tm.build(repo, "verified_success", params={"fallback": "exclude"}))
    assert set(frames[frames.include].episode_index) == {2}
    assert set(frames[frames.include].episode_success_source) == {"human"}


def test_manifest_records_provenance(repo):
    name = _name(repo)
    _anchored(name, {0: "success"})
    _recap(repo)
    result = tm.build(repo, "advantage_weighted", tasks=["stack the plates"])
    manifest = json.loads((Path(result["output_dir"]) / tm.MANIFEST).read_text())
    assert manifest["schema"] == tm.SCHEMA
    assert manifest["operation"] == {
        "name": "advantage_weighted",
        "summary": tm.OPERATIONS["advantage_weighted"].summary,
        "params": {
            "positive_weight": 1.0,
            "negative_weight": 0.2,
            "unlabelled_weight": 0.0,
        },
    }
    ds = manifest["dataset"]
    assert ds["name"] == name and ds["namespace"] is None and ds["fps"] == 10
    assert set(ds["fingerprint"]["meta_sha256"]) == {
        "info.json",
        "episodes.jsonl",
        "tasks.jsonl",
    }
    assert ds["fingerprint"]["data_files"] == 3
    assert manifest["annotation"]["revision"] and manifest["annotation"]["digest"]
    assert manifest["anchored"]["spec"]["id"] == "plates-release"
    assert manifest["anchored"]["provider"]["model"] == "m"
    recap = manifest["recap"]
    assert recap["checkpoint"] == "fake-a" and recap["threshold"] == 0.0
    assert recap["stale_reasons"] == [] and recap["static_filter"]
    # Out-of-scope episodes stay listed, excluded, so the file covers the dataset.
    frames = _frames(result)
    assert set(frames[frames.task == "pick the screws"].exclude_reason) == {
        "out_of_scope"
    }
    assert manifest["counts"]["episodes_in_scope"] == 2
    assert manifest["episodes"][0]["rollout_source_demo"] == "/rollouts/demo_0000"
    assert "levi_commit" in manifest
    _, table = tm.load(result["output_dir"])
    assert table.num_rows == 30
    (Path(result["output_dir"]) / tm.FRAMES).write_bytes(b"changed")
    with pytest.raises(tm.ManifestError, match="does not match"):
        tm.load(result["output_dir"])


def test_namespace_keeps_its_own_evidence(client, repo):
    name = _name(repo)
    created = client.post(
        f"/api/levi/catalog/{name}/namespaces", json={"namespace": "e2-a3"}
    )
    assert created.status_code == 200, created.text
    ns = (
        created.json()["id"]
        if "id" in created.json()
        else "local/" + created.json()["name"]
    )
    _anchored(name, {0: "failure"})  # on the base, not the namespace
    with pytest.raises(tm.ManifestError, match="needs an anchored review"):
        tm.build(ns, "verified_success")
    result = tm.build(ns, "robot_flag_success")
    assert result["dataset"]["base"] == name
    assert result["dataset"]["namespace"] == "e2-a3"
    assert Path(result["output_dir"]).parent == tm.root(f"{name}--e2-a3")


def test_refusals(repo):
    name = _name(repo)
    with pytest.raises(tm.ManifestError, match="Unknown operation"):
        tm.build(repo, "all_the_things")
    with pytest.raises(tm.ManifestError, match="takes no parameter"):
        tm.build(repo, "all_rollouts", {"weight": 2})
    with pytest.raises(tm.ManifestError, match="must be one of"):
        tm.build(repo, "advantage_positive_mask", {"unlabelled": "maybe"})
    with pytest.raises(tm.ManifestError, match="finite weight"):
        tm.build(repo, "advantage_weighted", {"negative_weight": -1})
    with pytest.raises(tm.ManifestError, match="needs RECAP"):
        tm.build(repo, "advantage_weighted")
    with pytest.raises(tm.ManifestError, match="needs an anchored"):
        tm.build(repo, "verified_success")
    with pytest.raises(tm.ManifestError, match="No episode has task"):
        tm.build(repo, "all_rollouts", tasks=["fold laundry"])
    with pytest.raises(tm.ManifestError, match="not in the dataset"):
        tm.build(repo, "all_rollouts", episodes=[7])
    with pytest.raises(tm.ManifestError, match="No anchored review run"):
        tm.build(repo, "all_rollouts", anchored_run="anchored-nope")
    _recap(repo)
    # The robot's flag changed after the labels were computed: stale.
    meta = catalog.local_root(repo) / "meta/episodes.jsonl"
    rows = [json.loads(line) for line in meta.read_text().splitlines()]
    rows[1]["is_success"] = True
    meta.write_text("".join(json.dumps(r) + "\n" for r in rows))
    with pytest.raises(tm.ManifestError, match="stale"):
        tm.build(repo, "advantage_positive_mask")
    stale = tm.build(repo, "advantage_positive_mask", allow_stale=True)
    assert stale["recap"]["stale_reasons"]
    out = catalog.local_root(repo).parent / "taken"
    out.mkdir()
    (out / "x").write_text("x")
    with pytest.raises(tm.ManifestError, match="not empty"):
        tm.build(repo, "all_rollouts", output=out)
    assert name


def test_compare_measures_the_input_difference(repo):
    a = tm.build(repo, "all_rollouts")["output_dir"]
    b = tm.build(repo, "robot_flag_success")["output_dir"]
    diff = tm.compare(a, b)
    assert diff["episodes_only_in_a"] == [1] and diff["episodes_only_in_b"] == []
    assert diff["frames_included_only_in_a"] == 10
    # all: 30 frames at 1/30; flag: 20 frames at 1/20 -> TV = 10/30.
    assert diff["input_difference"] == pytest.approx(1 / 3, abs=1e-6)
    assert tm.compare(a, a)["input_difference"] == 0


def test_cli_and_api(repo, client, capsys):
    assert tm.main(["operations"]) == 0
    names = [o["name"] for o in json.loads(capsys.readouterr().out)]
    assert names == list(tm.OPERATIONS)
    assert tm.main(["manifest", _name(repo), "--operation", "robot_flag_success",
                    "--episodes", "0-1"]) == 0  # fmt: skip
    printed = json.loads(capsys.readouterr().out)
    assert printed["counts"]["episodes_included"] == 1
    assert tm.main(["manifest", repo, "--operation", "verified_success"]) == 1
    assert "needs an anchored review" in capsys.readouterr().out
    assert tm.main(["list", repo]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 1

    ops = client.get("/api/levi/manifest/operations").json()["operations"]
    assert {o["name"] for o in ops} == set(tm.OPERATIONS)
    made = client.post(
        "/api/levi/manifest",
        json={
            "repo_id": repo,
            "operation": "all_rollouts",
            "tasks": ["pick the screws"],
        },
    )
    assert made.status_code == 200, made.text
    body = made.json()
    assert body["counts"]["episodes_included"] == 1 and "episodes" not in body
    assert Path(body["output_dir"], tm.FRAMES).is_file()
    bad = client.post("/api/levi/manifest", json={"repo_id": repo, "operation": "x"})
    assert bad.status_code == 400 and "Unknown operation" in bad.json()["detail"]
    listed = client.get("/api/levi/manifest", params={"repo_id": repo}).json()
    assert len(listed["manifests"]) == 2


# ---------------------------------------------------------------- reader


def _reader():
    path = PROJECT / "integrations/training_manifest/levi_manifest_reader.py"
    spec = importlib.util.spec_from_file_location("levi_manifest_reader", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reader_masks_weights_and_audits(repo):
    reader = _reader()
    _recap(repo)
    result = tm.build(repo, "advantage_positive_mask")
    root = catalog.local_root(repo)
    m = reader.ManifestFrames.open(result["output_dir"], root)
    assert m.episodes() == [0, 1, 2]
    # Frame 0 positive, 1 negative, 2 positive; past the end is padding.
    assert m.action_mask(1, 0, 3).tolist() == [True, False, True]
    assert m.action_mask(1, 8, 4).tolist() == [True, False, False, False]
    keys = [(0, 0), (0, 1), (5, 0)]
    assert m.sampling_weights(keys).tolist() == [1.0, 0.0, 0.0]
    rng = np.random.default_rng(0)
    prompts = [m.prompt("stack", 0, 0, rng) for _ in range(2000)]
    share = sum(p.endswith("Advantage: positive") for p in prompts) / 2000
    assert 0.87 < share < 0.93
    assert m.prompt("stack", 0, 1, rng) == "stack"

    # A loader drawing by the manifest's weights never draws an excluded frame.
    weights = m.weight * m.include
    rows = rng.choice(len(weights), size=5000, p=weights / weights.sum())
    drawn = list(zip(m.episode[rows], m.frame[rows], strict=True))
    report = reader.audit(m, drawn)
    assert report["excluded_drawn"] == 0
    for ep, share in report["expected"].items():
        assert abs(report["observed"][ep] - share) < 0.03
    assert reader.audit(m, [(0, 1)])["excluded_drawn"] == 1

    # The trainer's dataset must be the one the manifest was written for.
    (root / "meta/tasks.jsonl").write_text('{"task_index": 0, "task": "other"}\n')
    with pytest.raises(ValueError, match="differs"):
        reader.ManifestFrames.open(result["output_dir"], root)


def test_a_candidate_anchored_review_is_not_used_unless_asked_for(repo):
    """The live service's generic release review is a candidate spec nobody
    has validated: a manifest must not take its verdicts as verified by
    default."""
    name = _name(repo)
    run_id = _anchored(name, {0: "failure", 1: "failure"}, status="candidate")
    default = tm.build(repo, "all_rollouts")
    assert default["anchored"] == {"skipped_candidate_runs": [run_id]}
    f = _frames(default)
    assert set(f.episode_success_source) == {"robot_flag"}
    assert f[f.episode_index == 0].anchored_outcome.isna().all()
    # An operation that needs verification says why it cannot proceed.
    with pytest.raises(tm.ManifestError, match="candidate"):
        tm.build(repo, "verified_success")
    # Asking for it, by flag or by naming the run, uses it and says what it is.
    for kwargs in ({"allow_candidate_anchored": True}, {"anchored_run": run_id}):
        used = tm.build(repo, "verified_success", **kwargs)
        assert used["anchored"]["run_id"] == run_id
        assert used["anchored"]["spec_status"] == "candidate"
        assert (
            _frames(used).query("episode_index == 0").episode_success_source.iloc[0]
            == "anchored"
        )


def test_subtask_segments_written_by_the_live_service_are_marked_in_the_manifest(repo):
    from levi.agent.store import resolve

    name = _name(repo)
    folder = resolve(catalog.STATE, name, "annotations")
    folder.mkdir(parents=True, exist_ok=True)

    def atom(sid, start, to, review):
        levi = {"subtask_id": sid, "outcome": "success", "attempt": 1}
        if review:
            levi["review"] = review
        return {
            "role": "assistant",
            "content": sid,
            "style": "subtask",
            "timestamp": start,
            "to": to,
            "levi": levi,
        }

    atoms = [atom("grasp", 0.0, 0.5, "auto"), atom("place", 0.5, None, None)]
    (folder / "episode_000000.json").write_text(
        json.dumps({"episode_index": 0, "atoms": atoms})
    )
    result = tm.build(repo, "all_rollouts")
    f = _frames(result)
    e0 = f[f.episode_index == 0]
    assert (
        list(e0.subtask_review[:5]) == ["auto"] * 5
        and e0.subtask_review.iloc[5] is None
    )
    assert e0.subtask_review.iloc[-1] is None  # a person's segment is not marked
    note = result["annotation"]
    assert note["subtask_frames"] == 12 and note["subtask_frames_auto"] == 5
    assert note["subtask_auto_share"] == pytest.approx(5 / 12, abs=1e-4)
    assert f[f.episode_index == 1].subtask_review.isna().all()


# ------------------------------------------------- per-frame prompt columns


def _segments(name, ep, spans):
    """Subtask segments for one episode: (content, start, to, review, subtask_id)."""
    from levi.agent.store import resolve

    folder = resolve(catalog.STATE, name, "annotations")
    folder.mkdir(parents=True, exist_ok=True)
    atoms = []
    for content, start, to, review, sid in spans:
        levi = {"subtask_id": sid or content, "outcome": "success", "attempt": 1}
        if review:
            levi["review"] = review
        atoms.append(
            {
                "role": "assistant",
                "content": content,
                "style": "subtask",
                "timestamp": start,
                "to": to,
                "levi": levi,
            }
        )
    (folder / f"episode_{ep:06d}.json").write_text(
        json.dumps({"episode_index": ep, "atoms": atoms})
    )


def test_prompt_columns_hold_the_task_and_the_task_with_a_reviewed_subtask(repo):
    name = _name(repo)
    _segments(
        name,
        0,
        [
            ("Pick up the red plate.", 0.0, 0.5, None, "grasp"),
            ("move to the stack", 0.5, None, "edited", "transfer"),
        ],
    )
    result = tm.build(repo, "all_rollouts")
    f = _frames(result)
    e0 = f[f.episode_index == 0]
    assert (e0.prompt_task == "stack the plates").all()
    assert e0.prompt_subtask.iloc[0] == (
        "stack the plates; current subtask: Pick up the red plate"
    )
    assert e0.prompt_subtask.iloc[-1] == (
        "stack the plates; current subtask: move to the stack"
    )
    assert e0.prompt_has_subtask.all() and e0.prompt_subtask_skip.isna().all()
    assert e0.prompt_subtask_source.iloc[0] == "segment 0.000-0.500 review=human"
    assert e0.prompt_subtask_source.iloc[-1] == "segment 0.500-end review=edited"
    assert f[f.episode_index == 1].prompt_subtask_source.isna().all()
    # No time segment: the second prompt falls back to the task alone.
    e1 = f[f.episode_index == 1]
    assert (e1.prompt_subtask == e1.prompt_task).all()
    assert not e1.prompt_has_subtask.any()
    assert set(e1.prompt_subtask_skip) == {"no_segment"}
    meta = result["prompt"]
    assert meta["template_version"] == "v1"
    assert meta["template"] == "{task}; current subtask: {subtask}"
    assert meta["frames"] == 30 and meta["frames_with_subtask"] == 12
    assert meta["subtask_skipped"] == {"no_segment": 18}
    assert meta["subtask_max_chars"] == 120


def test_only_reviewed_segments_become_subtask_prompts(repo):
    """The hard rule: a segment the live service wrote and nobody reviewed
    (``auto``), and any review state the manifest does not know, never reaches
    a prompt; the manifest counts the frames refused."""
    name = _name(repo)
    _segments(
        name,
        0,
        [
            ("grasp", 0.0, 0.3, "auto", None),
            ("lift", 0.3, 0.6, "pending-review", None),
            ("place", 0.6, None, None, None),
        ],
    )
    result = tm.build(repo, "all_rollouts")
    f = _frames(result)
    e0 = f[f.episode_index == 0]
    assert list(e0.prompt_has_subtask[:3]) == [False] * 3  # frames 0-2: auto
    assert set(e0.prompt_subtask_skip[:3]) == {"unreviewed"}
    assert set(e0.prompt_subtask_skip[3:6]) == {"review_unrecognised"}
    assert list(e0.prompt_has_subtask[6:]) == [True] * 6
    assert (e0.prompt_subtask[:6] == e0.prompt_task[:6]).all()
    assert "grasp" not in " ".join(e0.prompt_subtask) and "lift" not in " ".join(
        e0.prompt_subtask
    )
    skipped = result["prompt"]["subtask_skipped"]
    assert skipped["unreviewed"] == 3 and skipped["review_unrecognised"] == 3
    assert result["prompt"]["frames_rejected_unreviewed"] == 6
    # An operation that leaves the frames out still counts only included ones here.
    kept = tm.build(repo, "robot_flag_success", episodes=[1, 2])
    assert kept["prompt"]["included_frames"]["rejected_unreviewed"] == 0


def test_other_unknown_background_and_empty_text_give_no_subtask(repo):
    name = _name(repo)
    _segments(
        name,
        0,
        [
            ("something", 0.0, 0.2, None, "other"),
            ("Unknown", 0.2, 0.4, None, None),
            ("anything", 0.4, 0.6, None, "background"),
            ("  .  ", 0.6, 0.8, None, "grasp"),
            ("   ", 0.8, None, None, "place"),
        ],
    )
    f = _frames(tm.build(repo, "all_rollouts"))
    e0 = f[f.episode_index == 0]
    assert not e0.prompt_has_subtask.any()
    assert (e0.prompt_subtask == e0.prompt_task).all()
    assert list(e0.prompt_subtask_skip[:6]) == ["special"] * 6
    assert list(e0.prompt_subtask_skip[6:]) == ["empty_text"] * 6


def test_subtask_text_is_cleaned_and_long_text_is_cut_and_counted(repo):
    from levi import manifest_prompts as mp

    assert mp.clean_subtask("  Pick up\n the   plate;  ", 120) == (
        "Pick up the plate",
        False,
    )
    assert mp.clean_subtask("...place it, ", 120) == ("place it", False)
    assert mp.clean_subtask("放到盘子上。", 120) == ("放到盘子上", False)
    assert mp.clean_subtask("Keep Case", 120) == ("Keep Case", False)  # no re-casing
    text, cut = mp.clean_subtask("a" * 50 + " " + "b" * 50, 60)
    assert cut and len(text) <= 60 and text.startswith("a" * 50)
    assert mp.clean_subtask("x" * 10, 10) == ("x" * 10, False)

    name = _name(repo)
    _segments(name, 0, [("word " * 40, 0.0, None, None, "grasp")])
    result = tm.build(repo, "all_rollouts", subtask_max_chars=30)
    f = _frames(result)
    e0 = f[f.episode_index == 0]
    assert e0.prompt_has_subtask.all()
    suffix = e0.prompt_subtask.iloc[0].split("current subtask: ", 1)[1]
    assert len(suffix) <= 30
    assert result["prompt"]["truncated_frames"] == 12
    assert result["prompt"]["subtask_max_chars"] == 30
    with pytest.raises(tm.ManifestError, match="subtask_max_chars"):
        tm.build(repo, "all_rollouts", subtask_max_chars=0)
    with pytest.raises(tm.ManifestError, match="prompt template"):
        tm.build(repo, "all_rollouts", prompt_template="nope")


def test_a_task_with_several_texts_keeps_the_first_and_is_flagged(repo):
    root = catalog.local_root(repo)
    path = root / "meta/episodes.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["tasks"] = ["stack the plates", "put plates on top of each other"]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    result = tm.build(repo, "all_rollouts")
    f = _frames(result)
    assert (f[f.episode_index == 0].prompt_task == "stack the plates").all()
    assert set(f[f.episode_index == 0].episode_task_count) == {2}
    assert set(f[f.episode_index == 1].episode_task_count) == {1}
    assert result["prompt"]["episodes_with_several_tasks"] == 1
    assert {e["episode_index"]: e["task_count"] for e in result["episodes"]} == {
        0: 2,
        1: 1,
        2: 1,
    }


def test_prompt_columns_are_documented_in_the_file_and_reachable_from_the_cli(
    repo, capsys
):
    result = tm.build(repo, "all_rollouts")
    schema = pq.read_schema(Path(result["output_dir"]) / tm.FRAMES)
    for column in ("prompt_task", "prompt_subtask"):
        note = schema.field(column).metadata
        assert note[b"levi.prompt_template"] == b"v1"
        assert b"levi.description" in note
    assert tm.main(["manifest", repo, "--operation", "all_rollouts",
                    "--subtask-max-chars", "40", "--prompt-template", "v1",
                    "--json"]) == 0  # fmt: skip
    assert json.loads(capsys.readouterr().out)["prompt"]["subtask_max_chars"] == 40


def _rewrite_frames(directory, drop=()):
    """Rewrite a manifest as an older LEVI wrote it: without some columns."""
    import hashlib

    directory = Path(directory)
    table = pq.read_table(directory / tm.FRAMES)
    table = table.drop_columns([c for c in drop if c in table.column_names])
    pq.write_table(table, directory / tm.FRAMES)
    meta = json.loads((directory / tm.MANIFEST).read_text())
    meta["files"][tm.FRAMES]["sha256"] = hashlib.sha256(
        (directory / tm.FRAMES).read_bytes()
    ).hexdigest()
    (directory / tm.MANIFEST).write_text(json.dumps(meta))


PROMPT_COLUMNS = (
    "prompt_task",
    "prompt_subtask",
    "prompt_has_subtask",
    "prompt_subtask_skip",
    "prompt_subtask_source",
    "episode_task_count",
)


def test_reader_modes_mix_deterministically_and_keep_the_recap_suffix_last(repo):
    reader = _reader()
    name = _name(repo)
    _recap(repo)
    _segments(
        name, 0, [("grasp", 0.0, 0.5, None, None), ("place", 0.5, None, "auto", None)]
    )
    result = tm.build(repo, "advantage_positive_mask")
    m = reader.ManifestFrames.open(result["output_dir"], catalog.local_root(repo))
    rng = np.random.default_rng(0)
    task = "stack the plates"
    sub = "stack the plates; current subtask: grasp"
    # Frame 0 of episode 0: positive advantage, reviewed segment.
    assert m.prompt(task, 0, 0, rng, p_conditioned=0.0) == task
    assert m.prompt(task, 0, 0, rng, p_conditioned=0.0, mode="subtask") == sub
    assert m.prompt(task, 0, 0, rng, p_conditioned=1.0, mode="subtask") == (
        sub + "\nAdvantage: positive"
    )
    # Frame 6 is in the auto segment: no subtask, so the task alone.
    assert m.prompt(task, 0, 6, rng, p_conditioned=0.0, mode="subtask") == task
    # mix: the same frame gets the same version every time; the share follows
    # mix_ratio; another seed may choose differently.
    first = [
        m.prompt(task, 0, 0, rng, p_conditioned=0.0, mode="mix", seed=3)
        for _ in range(20)
    ]
    assert len(set(first)) == 1
    for ratio, expected in ((0.0, task), (1.0, sub)):
        assert (
            m.prompt(task, 0, 0, rng, p_conditioned=0.0, mode="mix", mix_ratio=ratio)
            == expected
        )
    picks = [
        m.prompt(task, 0, 0, rng, p_conditioned=0.0, mode="mix", seed=s) == sub
        for s in range(2000)
    ]
    assert 0.45 < sum(picks) / 2000 < 0.55
    # The suffix stays last whichever version is drawn.
    for seed in range(20):
        text = m.prompt(task, 0, 0, rng, p_conditioned=1.0, mode="mix", seed=seed)
        assert text in (task + "\nAdvantage: positive", sub + "\nAdvantage: positive")
    with pytest.raises(ValueError, match="mode"):
        m.prompt(task, 0, 0, rng, mode="both")
    with pytest.raises(ValueError, match="mix_ratio"):
        m.prompt(task, 0, 0, rng, mode="mix", mix_ratio=1.5)


def test_reader_leaves_a_manifest_without_prompt_columns_exactly_as_before(repo):
    reader = _reader()
    _recap(repo)
    result = tm.build(repo, "advantage_positive_mask")
    new = reader.ManifestFrames.open(result["output_dir"], catalog.local_root(repo))
    _rewrite_frames(result["output_dir"], drop=PROMPT_COLUMNS)
    old = reader.ManifestFrames.open(result["output_dir"], catalog.local_root(repo))
    assert not old.has_prompt_columns and new.has_prompt_columns
    a, b = np.random.default_rng(7), np.random.default_rng(7)
    for frame in range(10):
        assert old.prompt("stack", 0, frame, a) == new.prompt("stack", 0, frame, b)
    # Asking an old manifest for subtask prompts is an error, not a silent task-only run.
    for mode in ("subtask", "mix"):
        with pytest.raises(ValueError, match="prompt columns"):
            old.prompt("stack", 0, 0, a, mode=mode)
    assert old.episodes() == new.episodes()
