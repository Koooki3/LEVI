"""Training manifests: operations, provenance, refusals, CLI/API and the
standalone trainer-side reader (integrations/training_manifest)."""

import hashlib
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


def test_a_review_that_needs_the_place_condition_is_never_a_certain_success(repo):
    """Under the terminal-aware spec the review record cannot apply the last
    placement (the live service does, in its own verdict), so its success is
    undecided in a training manifest; the default rule's success is not."""
    import json

    from levi.agent.anchored import AnchoredSpec, outcome
    from levi.live import generic

    def basis(name):
        spec = AnchoredSpec.model_validate(generic.anchored_spec("put it away", name))
        events = [
            {"frame_index": 9, "valid": True, "verdict": "supported", "answer": {}}
        ]
        result, found = outcome(spec, events, closes=[3])
        assert result == "success"
        return json.loads(json.dumps(found))

    name = _name(repo)
    _anchored(
        name,
        {0: "success", 1: "success"},
        bases={
            0: basis("generic-release.v2.json"),
            1: basis("generic-release.v1.json"),
        },
    )
    f = _frames(tm.build(repo, "all_rollouts"))
    flags = {
        ep: bool(f[f.episode_index == ep].anchored_undecided.iloc[0]) for ep in (0, 1)
    }
    assert flags == {0: True, 1: False}
    verified = _frames(tm.build(repo, "verified_success"))
    kept = set(verified[verified.include].episode_index)
    assert 1 in kept and 0 not in kept
    assert set(verified[verified.episode_index == 0].exclude_reason) == {
        "anchored_undecided"
    }


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
    for content, start, to, review, sid, *rest in spans:
        levi = {"subtask_id": sid or content, "outcome": "success", "attempt": 1}
        if review:
            levi["review"] = review
        if rest:
            levi["origin"] = rest[0]
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
    assert (
        e0.prompt_subtask_source.iloc[0]
        == "segment 0.000-0.500 review=human origin=human"
    )
    assert (
        e0.prompt_subtask_source.iloc[-1]
        == "segment 0.500-end review=edited origin=human"
    )
    assert f[f.episode_index == 1].prompt_subtask_source.isna().all()
    # No time segment: the second prompt falls back to the task alone.
    e1 = f[f.episode_index == 1]
    assert (e1.prompt_subtask == e1.prompt_task).all()
    assert not e1.prompt_has_subtask.any()
    assert set(e1.prompt_subtask_skip) == {"no_segment"}
    meta = result["prompt"]
    assert meta["template_version"] == "v1-en"
    assert meta["template"] == "{task}; current subtask: {subtask}"
    assert meta["frames"] == 30 and meta["frames_with_subtask"] == 12
    assert meta["subtask_skipped"] == {"no_segment": 18}
    assert meta["subtask_max_chars"] == 80


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


def test_special_labels_with_punctuation_or_case_are_still_special():
    from levi import manifest_prompts as mp

    for label in (
        "Unknown.",
        " background! ",
        "OTHER",
        "other,",
        "...Other?",
        "unknown\n",
    ):
        segment = {"text": label, "id": label, "review": None}
        out = mp.frame_prompts("t", segment, template="{task}; {subtask}", max_chars=80)
        assert (out[2], out[3]) == (False, "special"), label
    # An id that is special wins over free text, whatever the text says.
    out = mp.frame_prompts(
        "t", {"text": "Pick it up", "id": "Other.", "review": None},
        template="{task}; {subtask}", max_chars=80,
    )  # fmt: skip
    assert out[3] == "special"


def test_text_with_reserved_markers_or_invisible_characters_is_not_used():
    from levi import manifest_prompts as mp

    template = "{task}; {subtask}"

    def run(text):
        return mp.frame_prompts(
            "t", {"text": text, "id": "grasp", "review": None},
            template=template, max_chars=80,
        )  # fmt: skip

    for text in (
        "grasp. Advantage: positive",
        "ADVANTAGE : positive",
        "go; Task: other",
        "State: 1 2 3",
        "then Action:",
        "pick up the state:",
    ):
        out = run(text)
        assert (out[2], out[3]) == (False, "reserved_word"), text
    # Words alone, without the colon, are ordinary text.
    assert run("take action on the state of the task")[2] is True
    # Zero-width, control and format characters go; the rest stays.
    out = run("gra\u200bsp\x07 the\u2060 plate\x00")
    assert out[1] == "t; grasp the plate" and out[2] is True
    assert mp.clean_subtask("a\u200fb\x1b[0m", 80)[0] == "ab[0m"


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
    longest = max(len(x) for x in e0.prompt_subtask)
    assert result["prompt"]["max_prompt_chars"] == longest
    assert result["prompt"]["max_prompt_utf8_bytes"] == max(
        len(x.encode()) for x in e0.prompt_subtask
    )
    assert longest <= len("stack the plates; current subtask: ") + 30
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
        assert note[b"levi.prompt_template"] == b"v1-en"
        assert b"levi.description" in note
    assert tm.main(["manifest", repo, "--operation", "all_rollouts",
                    "--subtask-max-chars", "40", "--prompt-template", "v1-en",
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
    "prompt_subtask_origin",
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


def test_the_origin_of_a_subtask_is_recorded_and_counted(repo):
    """``review`` empty also covers agent segments a script approved with a
    person's identity: the manifest says where each subtask came from."""
    name = _name(repo)
    agent = {"kind": "agent", "run_id": "temporal-20260923T1301", "changeset": "c1"}
    _segments(
        name,
        0,
        [
            ("grasp", 0.0, 0.3, None, None, agent),
            ("lift", 0.3, 0.6, "edited", None, agent),
            ("place", 0.6, None, None, None),
        ],
    )
    result = tm.build(repo, "all_rollouts")
    f = _frames(result)
    e0 = f[f.episode_index == 0]
    assert e0.prompt_subtask_origin.iloc[0] == "agent_run"
    assert e0.prompt_subtask_origin.iloc[3] == "edited"
    assert e0.prompt_subtask_origin.iloc[-1] == "human"
    assert e0.prompt_subtask_source.iloc[0] == (
        "segment 0.000-0.300 review=human origin=agent run=temporal-20260923T1301"
    )
    assert f[f.episode_index == 1].prompt_subtask_origin.isna().all()
    meta = result["prompt"]
    assert meta["frames_with_subtask_by_origin"] == {
        "human": 6,
        "agent_run": 3,
        "edited": 3,
    }
    assert meta["frames_with_subtask_from_agent_runs"] == 3
    assert meta["frames_with_subtask_by_agent_run"] == {"temporal-20260923T1301": 3}
    assert (
        sum(meta["frames_with_subtask_by_origin"].values())
        == (meta["frames_with_subtask"])
    )


def test_the_reader_can_keep_only_some_origins(repo):
    reader = _reader()
    name = _name(repo)
    agent = {"kind": "agent", "run_id": "r1"}
    _segments(
        name,
        0,
        [("grasp", 0.0, 0.5, None, None, agent), ("place", 0.5, None, None, None)],
    )
    result = tm.build(repo, "all_rollouts")
    m = reader.ManifestFrames.open(result["output_dir"], catalog.local_root(repo))
    rng = np.random.default_rng(0)
    task = "stack the plates"
    sub = lambda text: f"{task}; current subtask: {text}"
    # Default: every origin is allowed.
    assert m.prompt(task, 0, 0, rng, mode="subtask") == sub("grasp")
    assert m.prompt(task, 0, 8, rng, mode="subtask") == sub("place")
    only = {"subtask_origins": ("human", "edited")}
    assert m.prompt(task, 0, 0, rng, mode="subtask", **only) == task
    assert m.prompt(task, 0, 8, rng, mode="subtask", **only) == sub("place")
    assert m.prompt(task, 0, 0, rng, mode="mix", mix_ratio=1.0, **only) == task
    with pytest.raises(ValueError, match="subtask_origins"):
        m.prompt(task, 0, 0, rng, mode="subtask", subtask_origins=("robot",))
    # A manifest without the prompt columns still refuses the subtask modes.
    _rewrite_frames(result["output_dir"], drop=PROMPT_COLUMNS)
    old = reader.ManifestFrames.open(result["output_dir"], catalog.local_root(repo))
    with pytest.raises(ValueError, match="prompt columns"):
        old.prompt(task, 0, 0, rng, mode="subtask", **only)


# ------------------------------------------------------------ tempo step
#
# ``--tempo per_event_uniform`` only multiplies ``weight`` inside subtask
# segments ("events"); it is a weight approximation of VPP2's N-frame draw per
# event, not that method. Its invariants: include and every other column are
# untouched (a trainer's action_mask counts a target only when its frame is
# included, so thinning by include would delete action supervision), each
# event's weights add up to N, a frame in no event keeps its weight, the
# parameters are in the fingerprint, and ``none`` changes nothing.


def _tempo(repo, op="all_rollouts", frames=None, **params):
    if frames is not None:
        params["frames"] = frames
    return tm.build(repo, op, tempo="per_event_uniform", tempo_params=params)


def _assert_sound_weights(f):
    weight = f.weight.to_numpy(dtype=float)
    assert np.isfinite(weight).all() and (weight >= 0).all()
    assert (weight[~f.include.to_numpy()] == 0).all()


def _unit(owner, include=None, weight=None):
    span_of = np.array(owner, dtype=np.int64)
    include = np.ones(len(owner), bool) if include is None else np.array(include, bool)
    start = np.ones(len(owner)) if weight is None else np.array(weight, float)
    return include, np.where(include, start, 0.0).astype(np.float32), span_of


def test_per_event_uniform_gives_every_event_the_same_total_weight():
    # Events of 20, 5 and 8 frames, then three frames in no event; N = 8.
    include, weight, span_of = _unit([0] * 20 + [1] * 5 + [2] * 8 + [-1] * 3)
    before = include.copy()
    found = tm.per_event_uniform(include, weight, span_of, [True] * 3, 8)
    for event, length in enumerate((20, 5, 8)):
        mine = weight[span_of == event]
        assert mine.sum() == pytest.approx(8, rel=1e-5)
        assert mine == pytest.approx(8 / length, rel=1e-5)
    assert (weight[span_of == -1] == 1).all()  # outside every event: as it was
    assert (include == before).all()  # include is never written
    assert found == {
        "events": 3,
        "events_scaled": 3,
        "events_empty": 0,
        "events_upweighted": 1,  # 5 frames < 8
        "events_unchanged": 1,  # 8 frames == 8
        "events_downweighted": 1,  # 20 frames > 8
        "events_partly_excluded": 0,
        "frames_in_events": 33,
        "frames_excluded_in_events": 0,
        "frames_outside_events": 3,
        "min_factor": pytest.approx(0.4),
        "max_factor": pytest.approx(1.6),
    }


def test_degenerate_events_never_produce_nan_or_negative_weights():
    # event 0: all three frames excluded (T = 0); 1: a single frame (T = 1);
    # 2: owns no frame at all; 3: owns two frames but is not an event;
    # frame 6 is in no event.
    include, weight, span_of = _unit(
        [0, 0, 0, 1, 3, 3, -1], include=[0, 0, 0, 1, 1, 1, 1]
    )
    found = tm.per_event_uniform(include, weight, span_of, [True, True, True, False], 8)
    assert weight.tolist() == [0, 0, 0, 8, 1, 1, 1]  # T = 1 gets all of N
    assert np.isfinite(weight).all() and (weight >= 0).all()
    assert (found["events"], found["events_scaled"], found["events_empty"]) == (3, 1, 2)
    assert (found["frames_in_events"], found["frames_outside_events"]) == (1, 3)
    assert found["frames_excluded_in_events"] == 3  # event 0, kept by nobody
    assert found["max_factor"] == found["min_factor"] == 8

    # N == T changes nothing; N = 1 gives every event a total of one.
    include, weight, span_of = _unit([0] * 4 + [1] * 2)
    tm.per_event_uniform(include, weight, span_of, [True, True], 4)
    assert weight.tolist() == [1, 1, 1, 1, 2, 2]  # the 2-frame event is raised to 4
    include, weight, span_of = _unit([0] * 4 + [1] * 2)
    tm.per_event_uniform(include, weight, span_of, [True, True], 1)
    assert weight.tolist() == pytest.approx([0.25] * 4 + [0.5] * 2)

    # No segment at all: nothing happens, and nothing is divided.
    include, weight, span_of = _unit([-1] * 5)
    found = tm.per_event_uniform(include, weight, span_of, [], 8)
    assert weight.tolist() == [1] * 5 and found["events"] == 0
    assert found["min_factor"] is None and found["frames_outside_events"] == 5


def test_the_operations_own_weights_keep_their_ratio_inside_an_event():
    # advantage_weighted gives 1.0 and 0.2: the factor depends on T alone, so
    # the event adds up to N times the mean weight it had, not to N.
    include, weight, span_of = _unit([0] * 5, weight=[1.0, 0.2, 1.0, 0.2, 1.0])
    tm.per_event_uniform(include, weight, span_of, [True], 8)
    assert weight.tolist() == pytest.approx([1.6, 0.32, 1.6, 0.32, 1.6])
    assert weight.sum() == pytest.approx(8 * 3.4 / 5)
    # A weight that is not a finite number is refused, never carried on.
    for bad in (np.inf, np.nan):
        include, weight, span_of = _unit([0, 0, 0], weight=[1.0, bad, 1.0])
        with pytest.raises(tm.ManifestError, match="finite"):
            tm.per_event_uniform(include, weight, span_of, [True], 8)


def test_tempo_none_is_the_default_and_changes_nothing(repo):
    name = _name(repo)
    _anchored(name, {0: "failure", 1: "failure"})
    _recap(repo)
    _segments(
        name, 0, [("grasp", 0.0, 0.5, None, None), ("place", 0.5, None, None, None)]
    )
    for op in ("all_rollouts", "verified_success", "advantage_weighted"):
        plain, none = tm.build(repo, op), tm.build(repo, op, tempo="none")
        a, b = Path(plain["output_dir"]), Path(none["output_dir"])
        assert (a / tm.FRAMES).read_bytes() == (b / tm.FRAMES).read_bytes()
        ma, mb = (json.loads((d / tm.MANIFEST).read_text()) for d in (a, b))
        ma.pop("created_at"), mb.pop("created_at")  # the only thing a clock changes
        assert ma == mb
        # Not even a new key: a `none` manifest is what it was before the step.
        assert "tempo" not in ma
        assert all("tempo_events_scaled" not in e for e in ma["episodes"])
        diff = tm.compare(a, b)
        assert diff["input_difference"] == 0 and diff["tempo_differs"] is False
        assert diff["a"]["tempo"] == diff["b"]["tempo"]
        assert diff["a"]["tempo"]["name"] == "none"
    with pytest.raises(tm.ManifestError, match="Tempo step none takes no parameter"):
        tm.build(repo, "all_rollouts", tempo="none", tempo_params={"frames": 4})


def test_per_event_uniform_changes_weight_only_and_only_inside_events(repo):
    name = _name(repo)
    # Episode 0: 5 + 7 frames, both reviewed. Episode 2: 3 frames of an
    # automatic (unreviewed) segment, then 5 frames of an edited one.
    _segments(
        name, 0, [("grasp", 0.0, 0.5, None, None), ("place", 0.5, None, None, None)]
    )
    _segments(
        name, 2, [("a", 0.0, 0.3, "auto", None), ("b", 0.3, None, "edited", None)]
    )
    base = _frames(tm.build(repo, "all_rollouts"))
    result = _tempo(repo, frames=4)
    f = _frames(result)
    e0, e1, e2 = (f[f.episode_index == e] for e in (0, 1, 2))
    assert e0[e0.subtask_id == "grasp"].weight.tolist() == pytest.approx([0.8] * 5)
    assert e0[e0.subtask_id == "place"].weight.tolist() == pytest.approx([4 / 7] * 7)
    assert e2[e2.subtask_id == "b"].weight.sum() == pytest.approx(4)
    # The automatic segment is not an event, and episode 1 has no segment.
    assert (e2[e2.subtask_id == "a"].weight == 1).all() and (e1.weight == 1).all()
    # Every other column, include and exclude_reason among them, is as before.
    others = [c for c in f.columns if c != "weight"]
    pd.testing.assert_frame_equal(f[others], base[others])
    _assert_sound_weights(f)
    # Said in manifest.json: what ran, with which numbers, and what it found.
    tempo = result["tempo"]
    assert tempo["name"] == "per_event_uniform" and tempo["version"] == 2
    assert tempo["params"] == {"frames": 4, "segments": "reviewed", "count": "segment"}
    assert tempo["events"] == {
        "spans": 4,
        "spans_not_events": 1,
        "events": 3,
        "events_scaled": 3,
        "events_empty": 0,
        "events_upweighted": 0,
        "events_unchanged": 0,
        "events_downweighted": 3,
        "events_partly_excluded": 0,
        "frames_in_events": 17,
        "frames_excluded_in_events": 0,
        "frames_outside_events": 13,
        "episodes_with_events": 2,
        "min_factor": pytest.approx(4 / 7, abs=1e-6),
        "max_factor": 0.8,
    }
    assert result["counts"]["weight_sum"] == pytest.approx(13 + 3 * 4, abs=1e-4)
    assert [e.get("tempo_events_scaled") for e in result["episodes"]] == [2, 0, 1]
    assert "approximation" not in tempo and "not the paper's method" in tempo["summary"]
    # `segments=all` also takes the automatic segment as an event.
    f = _frames(_tempo(repo, frames=4, segments="all"))
    e2 = f[f.episode_index == 2]
    assert e2[e2.subtask_id == "a"].weight.tolist() == pytest.approx([4 / 3] * 3)


def test_the_tempo_step_follows_whichever_operation_ran(repo):
    name = _name(repo)
    _recap(repo)  # frame f is positive when f is even; ep 0's last is unlabelled
    _segments(
        name, 0, [("grasp", 0.0, 0.5, None, None), ("place", 0.5, None, None, None)]
    )
    # grasp (5 frames) keeps frames 0, 2, 4 and place (7 frames) keeps 6, 8, 10;
    # the frames the operation dropped count in T as weight 0.
    none = _frames(tm.build(repo, "advantage_positive_mask"))
    f = _frames(_tempo(repo, "advantage_positive_mask", frames=8))
    assert (f.include == none.include).all()
    assert (f.exclude_reason.fillna("") == none.exclude_reason.fillna("")).all()
    for event, covered in (("grasp", 5), ("place", 7)):
        kept = f[f.include & (f.subtask_id == event)]
        assert len(kept) == 3 and kept.weight.tolist() == pytest.approx(
            [8 / covered] * 3
        )
    assert (f[~f.include].weight == 0).all()
    _assert_sound_weights(f)

    # advantage_weighted: positive 1.0, negative 0.25 -- the ratio survives.
    params = {"negative_weight": 0.25, "unlabelled_weight": 0.5}
    none = _frames(tm.build(repo, "advantage_weighted", params))
    result = tm.build(
        repo,
        "advantage_weighted",
        params,
        tempo="per_event_uniform",
        tempo_params={"frames": 8},
    )
    f = _frames(result)
    grasp, before = f[f.subtask_id == "grasp"], none[none.subtask_id == "grasp"]
    assert grasp.weight.sum() == pytest.approx(8 * before.weight.mean(), rel=1e-5)
    assert (grasp.weight / before.weight).tolist() == pytest.approx([8 / 5] * 5)
    assert (f.include == none.include).all()
    outside = f.subtask_id.isna()
    assert (f.weight[outside] == none.weight[outside]).all()

    # An event the operation emptied is counted and left alone (T = 0).
    _segments(
        name, 1, [("x", 0.0, 0.5, None, None), ("y", 0.5, None, None, None)]
    )  # episode 1: robot flag failure -> out under robot_flag_success
    result = _tempo(repo, "robot_flag_success")
    f = _frames(result)
    assert (f[f.episode_index == 1].weight == 0).all()
    found = result["tempo"]["events"]
    # grasp and place of episode 0 are scaled; x and y of episode 1 are empty.
    assert (found["events"], found["events_scaled"], found["events_empty"]) == (4, 2, 2)
    _assert_sound_weights(f)


def test_overlapping_empty_and_tiny_segments_are_each_accounted_for(repo):
    name = _name(repo)
    # Overlap: b starts last, so frames 5-7 belong to it (as in subtask_id);
    # nothing is scaled twice.
    _segments(name, 0, [("a", 0.0, 0.8, None, None), ("b", 0.5, None, None, None)])
    f = _frames(_tempo(repo, frames=6))
    e0 = f[f.episode_index == 0]
    assert e0[e0.subtask_id == "a"].weight.tolist() == pytest.approx([6 / 5] * 5)
    assert e0[e0.subtask_id == "b"].weight.tolist() == pytest.approx([6 / 7] * 7)
    assert e0.weight.sum() == pytest.approx(12)

    # Two segments from the same start: the second owns everything, the first
    # owns nothing and is an empty event, not a division by zero.
    _segments(name, 0, [("c", 0.0, 0.3, None, None), ("d", 0.0, None, None, None)])
    result = _tempo(repo, frames=6)
    f = _frames(result)
    assert f[f.episode_index == 0].weight.tolist() == pytest.approx([0.5] * 12)
    assert result["tempo"]["events"]["events_empty"] == 1

    # Episode 2 (8 frames): a segment after the episode ends, a one-frame
    # segment (it gets the whole N) and a segment of seven frames.
    _segments(
        name,
        2,
        [("blink", 0.0, 0.1, None, None), ("rest", 0.1, 0.7, None, None),
         ("late", 5.0, None, None, None)],
    )  # fmt: skip
    result = _tempo(repo, frames=8)
    f = _frames(result)
    e2 = f[f.episode_index == 2]
    assert e2[e2.subtask_id == "blink"].weight.tolist() == [8]
    assert e2[e2.subtask_id == "rest"].weight.tolist() == pytest.approx([8 / 6] * 6)
    assert (e2[e2.subtask_id.isna()].weight == 1).all()  # the frame after "rest"
    assert result["tempo"]["events"]["max_factor"] == 8
    assert result["tempo"]["events"]["events_empty"] >= 2  # "c" and "late"
    _assert_sound_weights(f)


def test_without_any_segment_the_tempo_step_changes_no_weight(repo):
    none = _frames(tm.build(repo, "all_rollouts"))
    result = _tempo(repo, frames=4)
    pd.testing.assert_frame_equal(_frames(result), none)
    events = result["tempo"]["events"]
    assert events["events"] == 0 and events["frames_outside_events"] == 30
    assert events["min_factor"] is None and events["max_factor"] is None


def test_an_event_the_operation_thinned_does_not_hand_its_last_frame_the_whole_n():
    # An event of 5 frames of which the operation kept one (advantage_positive_mask
    # leaves one positive frame). T counts the segment, so that frame gets N / 5,
    # not N: the event adds up to N times the share it kept.
    include, weight, span_of = _unit(
        [0] * 5 + [1] * 5, include=[1, 0, 0, 0, 0] + [1] * 5
    )
    found = tm.per_event_uniform(include, weight, span_of, [True, True], 8)
    assert weight.tolist() == pytest.approx([1.6, 0, 0, 0, 0] + [1.6] * 5)
    assert weight[:5].sum() == pytest.approx(8 * 1 / 5)
    assert weight[5:].sum() == pytest.approx(8)  # kept whole: exactly N
    assert found["events_partly_excluded"] == 1
    assert found["frames_excluded_in_events"] == 4
    assert found["events_empty"] == 0 and found["max_factor"] == pytest.approx(1.6)
    # count=included is the first version: the lone frame gets all of N.
    include, weight, span_of = _unit([0] * 5, include=[1, 0, 0, 0, 0])
    found = tm.per_event_uniform(include, weight, span_of, [True], 8, "included")
    assert weight.tolist() == [8, 0, 0, 0, 0] and found["events_partly_excluded"] == 1
    with pytest.raises(tm.ManifestError, match="count must be one of"):
        tm.per_event_uniform(include, weight, span_of, [True], 8, "kept")


def test_the_lone_positive_frame_of_an_event_is_not_weighted_like_the_event(repo):
    name = _name(repo)
    _recap(repo)  # frame f is positive when f is even
    # Episode 1: frames 1-3 are one event and only frame 2 is positive.
    _segments(name, 1, [("x", 0.1, 0.4, None, None)])
    f = _frames(_tempo(repo, "advantage_positive_mask", frames=8))
    kept = f[(f.episode_index == 1) & f.include & (f.subtask_id == "x")]
    assert kept.frame_index.tolist() == [2]
    assert kept.weight.tolist() == pytest.approx([8 / 3])  # not 8
    old = _frames(_tempo(repo, "advantage_positive_mask", frames=8, count="included"))
    lone = old[(old.episode_index == 1) & old.include & (old.subtask_id == "x")]
    assert lone.weight.tolist() == [8]


def test_an_event_total_is_continuous_in_the_operations_cutoff(repo):
    name = _name(repo)
    _recap(repo)
    _segments(
        name, 0, [("grasp", 0.0, 0.5, None, None), ("place", 0.5, None, None, None)]
    )

    def totals(negative, **tempo_params):
        params = {"negative_weight": negative, "unlabelled_weight": 0.5}
        f = _frames(
            tm.build(
                repo,
                "advantage_weighted",
                params,
                tempo="per_event_uniform",
                tempo_params={"frames": 8, **tempo_params},
            )
        )
        return {e: f[f.subtask_id == e].weight.sum() for e in ("grasp", "place")}

    # A negative frame going from weight 0.001 to excluded (zero_weight) must
    # change an event's total by about 0.001 of N, not make it jump to N.
    soft, gone = totals(0.001), totals(0)
    for event in soft:
        assert abs(soft[event] - gone[event]) < 0.01, (event, soft, gone)
    assert gone["grasp"] == pytest.approx(8 * 3 / 5)  # 3 positive of 5 frames
    # count=included, the first version, jumps: the negatives leave T.
    soft, gone = totals(0.001, count="included"), totals(0, count="included")
    assert abs(soft["grasp"] - gone["grasp"]) > 3 and gone["grasp"] == pytest.approx(8)


def test_tempo_parameters_are_in_the_fingerprint_and_in_compare(repo):
    name = _name(repo)
    _segments(
        name, 0, [("grasp", 0.0, 0.5, None, None), ("place", 0.5, None, None, None)]
    )
    _segments(name, 2, [("a", 0.0, 0.3, "auto", None), ("b", 0.3, None, None, None)])
    four, eight = _tempo(repo, frames=4), _tempo(repo, frames=8)
    default, everything = _tempo(repo), _tempo(repo, frames=8, segments="all")
    fp = lambda r: r["tempo"]["fingerprint"]
    assert tm.DEFAULT_EVENT_FRAMES == 8 and fp(default) == fp(eight)
    assert len({fp(four), fp(eight), fp(everything)}) == 3
    assert fp(eight) == tm.tempo_fingerprint(
        "per_event_uniform",
        2,
        {"frames": 8, "segments": "reviewed", "count": "segment"},
    )
    # A changed step with the same parameters is a different input.
    assert tm.tempo_fingerprint("per_event_uniform", 3, eight["tempo"]["params"]) != fp(
        eight
    )
    included = _tempo(repo, frames=8, count="included")
    assert fp(included) not in {fp(four), fp(eight), fp(everything)}
    on_disk = json.loads((Path(four["output_dir"]) / tm.MANIFEST).read_text())["tempo"]
    assert on_disk["params"] == {
        "frames": 4,
        "segments": "reviewed",
        "count": "segment",
    }
    assert on_disk["fingerprint"] == fp(four)

    diff = tm.compare(four["output_dir"], eight["output_dir"])
    assert diff["tempo_differs"] is True and diff["input_difference"] > 0
    assert diff["a"]["tempo"]["params"]["frames"] == 4
    assert diff["b"]["tempo"]["params"]["frames"] == 8
    same = tm.compare(eight["output_dir"], default["output_dir"])
    assert same["tempo_differs"] is False and same["input_difference"] == 0
    plain = tm.build(repo, "all_rollouts")
    diff = tm.compare(plain["output_dir"], eight["output_dir"])
    assert diff["tempo_differs"] and diff["a"]["tempo"]["name"] == "none"
    assert diff["b"]["tempo"]["name"] == "per_event_uniform"
    # `levi export list` shows the step of the manifests that have one.
    listed = {Path(m["output_dir"]): m for m in tm.listing(repo)}
    assert "tempo" not in listed[Path(plain["output_dir"])]
    assert listed[Path(four["output_dir"])]["tempo"]["params"]["frames"] == 4


def test_a_different_tempo_parameter_is_flagged_even_when_the_weights_agree(repo):
    # No segment: the weights are the same whatever N is, yet the two inputs
    # were asked for differently, and compare must say so.
    four, eight = _tempo(repo, frames=4), _tempo(repo, frames=8)
    diff = tm.compare(four["output_dir"], eight["output_dir"])
    assert diff["input_difference"] == 0 and diff["tempo_differs"] is True


def test_the_reader_masks_and_includes_exactly_as_without_the_tempo_step(repo):
    reader = _reader()
    name = _name(repo)
    _recap(repo)
    _segments(
        name, 0, [("grasp", 0.0, 0.5, None, None), ("place", 0.5, None, None, None)]
    )
    root = catalog.local_root(repo)
    plain = tm.build(repo, "advantage_positive_mask")
    tempo = _tempo(repo, "advantage_positive_mask", frames=6)  # 3 kept frames each
    a = reader.ManifestFrames.open(plain["output_dir"], root)
    b = reader.ManifestFrames.open(tempo["output_dir"], root)
    assert a.episodes() == b.episodes()
    assert (a.include == b.include).all()
    for ep, (*_, n) in EPISODES.items():
        for frame in range(n):
            assert (
                a.action_mask(ep, frame, 5).tolist()
                == b.action_mask(ep, frame, 5).tolist()
            )
    keys = [(ep, frame) for ep, (*_, n) in EPISODES.items() for frame in range(n)]
    drawn = b.sampling_weights(keys)
    assert drawn.sum() == pytest.approx(tempo["counts"]["weight_sum"], abs=1e-4)
    assert not np.allclose(a.sampling_weights(keys), drawn)  # weights differ
    assert (drawn[~b.include[[b._row[k] for k in keys]]] == 0).all()


def test_tempo_refusals_cli_and_docs(repo, capsys):
    with pytest.raises(tm.ManifestError, match="Unknown tempo step"):
        tm.build(repo, "all_rollouts", tempo="thin_free_motion")
    for params, message in (
        ({"frames": 0}, "frames must be at least 1"),
        ({"frames": 100001}, "frames must be at most"),
        ({"frames": 2.5}, "frames must be a whole number"),
        ({"frames": True}, "frames must be a whole number"),
        ({"frames": "many"}, "frames must be a whole number"),
        ({"segments": "some"}, "segments must be one of reviewed, all"),
        ({"count": "kept"}, "count must be one of segment, included"),
        (
            {"stride": 3},
            r"takes no parameter stride \(it takes frames, segments, count\)",
        ),
    ):
        with pytest.raises(tm.ManifestError, match=message):
            _tempo(repo, **params)
    assert _tempo(repo, frames=8.0)["tempo"]["params"]["frames"] == 8  # a whole 8.0

    assert tm.main(["tempos"]) == 0
    steps = {s["name"]: s for s in json.loads(capsys.readouterr().out)}
    assert list(steps) == list(tm.TEMPOS) == ["none", "per_event_uniform"]
    frames = steps["per_event_uniform"]["params"]["frames"]
    assert frames["default"] == 8 and frames["minimum"] == 1
    assert steps["per_event_uniform"]["params"]["segments"]["choices"] == [
        "reviewed",
        "all",
    ]
    count = steps["per_event_uniform"]["params"]["count"]
    assert count["default"] == "segment" and count["choices"] == ["segment", "included"]
    assert tm.main(["manifest", repo, "--operation", "all_rollouts"]) == 0
    assert "tempo" not in json.loads(capsys.readouterr().out)
    assert tm.main(["manifest", repo, "--operation", "all_rollouts",
                    "--tempo", "per_event_uniform", "--tempo-param", "frames=4"]) == 0  # fmt: skip
    printed = json.loads(capsys.readouterr().out)
    assert printed["tempo"]["params"] == {
        "frames": 4,
        "segments": "reviewed",
        "count": "segment",
    }
    for extra, text in (
        (["--tempo", "per_event_uniform", "--tempo-param", "frames=0"], "at least 1"),
        (["--tempo", "per_event_uniform", "--tempo-param", "frames=2.5"], "whole"),
        (["--tempo", "per_event_uniform", "--tempo-param", "frames"], "key=value"),
        (["--tempo-param", "frames=4"], "Tempo step none takes no parameter"),
    ):
        assert tm.main(["manifest", repo, "--operation", "all_rollouts", *extra]) == 1
        assert text in capsys.readouterr().out
    with pytest.raises(SystemExit):  # argparse names the choices
        tm.main(["manifest", repo, "--operation", "all_rollouts", "--tempo", "x"])
    assert "per_event_uniform" in capsys.readouterr().err

    # The documentation names every step and every parameter of the code.
    doc = (PROJECT / "docs/TRAINING_MANIFEST.md").read_text()
    for step in tm.TEMPOS.values():
        assert f"`{step.name}`" in doc
        for param in step.params:
            assert f"`{param}`" in doc, param
    for flag in ("--tempo", "--tempo-param", "levi export tempos"):
        assert flag in doc


# --------------------------------------------- `none` is what main wrote
#
# `--tempo none` must leave a manifest exactly as it was before the tempo step
# existed. Two of the checks above (no `tempo` key, equal bytes to a build
# without the argument) compare this code with itself and cannot see a change
# that both paths share. This one compares with the code of main (c57d53ab, the
# commit this branch started from): `tests/fixtures/manifest/none-golden.json`
# holds sha256 digests of what main's `build()` wrote for six builds over the
# small synthetic dataset above, so no output file is committed. It is a
# digest of content (every frames.parquet row and its schema, and manifest.json
# with the fields that differ between two runs of the same code removed), not
# of file bytes: pyarrow writes its own version into a parquet footer.
#
# Why a golden and not a second checkout of main in the test: CI has no main
# beside the branch under test, and a digest written once cannot drift with
# the code it guards. The file was written by main's code, never by this
# branch: copy this test module into `git archive c57d53ab`, replace the final
# assertion of the test below by `print(json.dumps(got))`, run it there, and
# keep `input` and `arms`. Rewrite it only when `none` is changed on purpose,
# and then say so in the commit; never to make this test pass.

GOLDEN = PROJECT / "tests" / "fixtures" / "manifest" / "none-golden.json"
GOLDEN_ARMS = {
    "all_rollouts": ("all_rollouts", {}, {}),
    "robot_flag_success": ("robot_flag_success", {}, {}),
    "verified_success": ("verified_success", {}, {}),
    "verified_success_scoped": (
        "verified_success",
        {},
        {"anchored_tasks": ["pick the screws"], "episodes": [0, 2]},
    ),
    "advantage_positive_mask": ("advantage_positive_mask", {}, {}),
    "advantage_weighted": (
        "advantage_weighted",
        {"negative_weight": 0.25, "unlabelled_weight": 0.5},
        {},
    ),
}
# manifest.json fields that differ between two runs of the same code over the
# same input: a clock, a path, a revision stamp, a hash of bytes a library
# wrote (parquet), and the hash of an anchored spec shipped with LEVI.
GOLDEN_VOLATILE = (
    ("created_at",),
    ("levi_commit",),
    ("annotation", "path"),
    ("dataset", "root"),
    ("dataset", "fingerprint", "sha256"),
    ("dataset", "fingerprint", "dataset_revision"),
    ("files", tm.FRAMES, "sha256"),
    ("recap", "version"),
    ("recap", "result_digest"),
    ("anchored", "spec", "sha256"),
)


def _digest(value):
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(text.encode()).hexdigest()


def _golden_digest(result):
    """sha256 of the frames, of their schema and of the manifest's stable part."""
    directory = Path(result["output_dir"])
    manifest = json.loads((directory / tm.MANIFEST).read_text())
    for *path, last in GOLDEN_VOLATILE:
        node = manifest
        for key in path:
            node = node.get(key) or {}
        node.pop(last, None)
    table = pq.read_table(directory / tm.FRAMES)
    schema = [
        [
            f.name,
            str(f.type),
            {k.decode(): v.decode() for k, v in (f.metadata or {}).items()},
        ]
        for f in table.schema
    ]
    return {
        "frames": _digest(table.to_pylist()),
        "schema": _digest(schema),
        "manifest": _digest(manifest),
    }


def _golden_inputs(repo):
    name = _name(repo)
    _anchored(name, {0: "failure", 1: "failure"})
    _recap(repo)
    _segments(
        name, 0, [("grasp", 0.0, 0.5, None, None), ("place", 0.5, None, None, None)]
    )
    _segments(
        name, 2, [("a", 0.0, 0.3, "auto", None), ("b", 0.3, None, "edited", None)]
    )


def _golden_arms(repo, **extra):
    """The digests of every arm and of the input; ``extra`` is passed on to
    build() (``tempo="none"`` here, nothing on main, which has no such argument)."""
    arms, inputs = {}, None
    for arm, (operation, params, scope) in GOLDEN_ARMS.items():
        result = tm.build(repo, operation, dict(params), **scope, **extra)
        arms[arm] = _golden_digest(result)
        inputs = {
            "meta_sha256": result["dataset"]["fingerprint"]["meta_sha256"],
            "annotation_digest": result["annotation"]["digest"],
        }
    return {"input": inputs, "arms": arms}


def test_tempo_none_writes_what_main_wrote(repo):
    golden = json.loads(GOLDEN.read_text())
    _golden_inputs(repo)
    got = _golden_arms(repo, tempo="none")
    # The input first: a changed helper above is not a changed `none`.
    assert got["input"] == golden["input"], (
        "the synthetic input of this test changed (EPISODES, _dataset, _anchored, "
        "_recap or _segments); regenerate the golden from main as the comment says"
    )
    for arm, digests in golden["arms"].items():
        assert got["arms"][arm] == digests, (
            f"--tempo none no longer writes what main wrote for {arm}: "
            "compare frames, schema and manifest to see which part changed"
        )
    # And the default, the argument left out, is that same thing.
    assert _golden_arms(repo)["arms"] == golden["arms"]
