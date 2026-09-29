"""Training pool: which policy produced a rollout, and how it was run."""

import json
from pathlib import Path

import pandas as pd
import pytest

from levi.pool import export, index, policy, recipe, scanner
from levi.pool.recipe import Recipe

COUNTER = 0


def demo(path: Path, meta: dict | None = None, task="stack plates") -> Path:
    """A raw capture without videos; every call records other data."""
    global COUNTER
    COUNTER += 1
    path.mkdir(parents=True)
    t = [COUNTER * 1000 + i / 10 for i in range(8)]
    pd.DataFrame({"timestamp_sec": t, "frame_index": range(8)}).to_csv(
        path / "frames.csv", index=False
    )
    pd.DataFrame({"timestamp_sec": t, "success_flag": 1, "px": t}).to_csv(
        path / "end_effector_pose.csv", index=False
    )
    stamp = f"2026-09-{1 + COUNTER % 27:02d}T10:{COUNTER % 60:02d}:00"
    body = {
        "task_description": task,
        "created_at": stamp,
        "stopped_at": stamp[:-2] + "59",
        "frame_count": 8,
        "success_flag_final": 1,
    }
    body.update(meta or {})
    (path / "metadata.json").write_text(json.dumps(body))
    return path


def rollout(path: Path, **kw) -> Path:
    policy_block = kw.pop("policy", None)
    meta = {"data_source": "policy_rollout", **kw}
    if policy_block is not None:
        meta["policy"] = policy_block
    return demo(path, meta)


def lerobot(root: Path, episodes: list[dict]) -> Path:
    (root / "meta").mkdir(parents=True)
    (root / "meta/info.json").write_text(
        json.dumps(
            {
                "codebase_version": "v2.1",
                "fps": 10,
                "chunks_size": 1000,
                "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
                "features": {},
            }
        )
    )
    (root / "meta/tasks.jsonl").write_text(
        json.dumps({"task_index": 0, "task": "stack plates"}) + "\n"
    )
    rows = [
        {"episode_index": i, "tasks": ["stack plates"], "length": 8 + i, **row}
        for i, row in enumerate(episodes)
    ]
    (root / "meta/episodes.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows)
    )
    return root


CHECKPOINT = "checkpoints/pi05_a_step100"


@pytest.fixture(scope="module")
def pool_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("policy") / "data"
    online = root / "online"
    task = "stack_plates"
    # Direct deployments: two models that share one checkpoint name.
    for i in range(2):
        rollout(
            online / f"models/pi05_a_state/{task}/demo_{i:04d}",
            control_mode="policy_rollout",
            policy={"config": "pi05_a_state", "checkpoint_dir": CHECKPOINT},
        )
    rollout(
        online / f"models/pi05_a/{task}/demo_0000",
        control_mode="policy_rollout",
        policy={"config": "pi05_a", "checkpoint_dir": CHECKPOINT},
    )
    # Online RL and a student.
    for i in range(3):
        rollout(
            online / f"online_rl/dsrl/pi05_a_state/{task}/demo_{i:04d}",
            control_mode="dsrl_online_rl",
            method="dsrl",
            policy={
                "config": "pi05_a_state",
                "checkpoint_dir": CHECKPOINT,
                "method": "dsrl",
                "phase": "eval_ckpt",
            },
        )
    rollout(
        online / f"models/student_x/{task}/demo_0000",
        control_mode="student_policy_rollout",
        method="student",
        policy={
            "config": "pi05_a_state",
            "checkpoint_dir": CHECKPOINT,
            "method": "student",
            "phase": "student",
            "student": "/banks/student_x",
        },
    )
    # Older rollouts: no policy block. The folder says how; or nothing does.
    rollout(online / f"online_rl/rlt/pi05_a_state/{task}/demo_0000")
    rollout(online / f"models/pi05_old/{task}/demo_0000")
    rollout(root / f"misc/{task}/demo_0000")
    # A human teleoperation capture: no policy at all.
    demo(root / f"human/{task}/demo_0000", {"control_mode": "pygame"})
    # A LeRobot rollout set: linked to a raw capture, to an unindexed one
    # (named only by path), by method only, and by nothing.
    lerobot(
        root / "recap",
        [
            {
                "data_source": "policy_rollout",
                "rollout_source_method": "dsrl",
                "rollout_source_demo": str(
                    online / f"online_rl/dsrl/pi05_a_state/{task}/demo_0000"
                ),
            },
            {
                "data_source": "policy_rollout",
                "rollout_source_method": "sfe",
                "rollout_source_demo": "/gone/online_rl/sfe/pi05_b/t/demo_0009",
            },
            {"data_source": "policy_rollout", "rollout_source_method": "models"},
            {"data_source": "policy_rollout"},
        ],
    )
    return root


@pytest.fixture
def scanned(pool_root, tmp_path, monkeypatch):
    monkeypatch.setenv("LEVI_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("LEVI_POOL_ROOTS", str(pool_root))
    monkeypatch.setenv("LEVI_POOL_HELDOUT", "none")
    monkeypatch.setenv("LEVI_EXPORT_ROOTS", str(tmp_path))
    monkeypatch.setattr(export, "_levi_commit", lambda: "test")
    return scanner.scan()


def by_key(**filters):
    rows = index.episodes(limit=1000, show_copies=True, **filters)["episodes"]
    return {r["episode"]: r for r in rows}


def fields(row):
    return {
        k: row[k]
        for k in ("policy_model", "policy_checkpoint", "policy_method", "policy_phase")
    }


# ------------------------------------------------------------------ units


def test_method_normalization():
    n = policy.normalize_method
    assert n("dsrl") == n("DSRL_online_rl") == "dsrl"
    assert n("policy_rollout") == n("models") == "direct"
    assert n("student_policy_rollout") == "student"
    assert n("something") == "other" and n(None) is None
    assert n("pygame", strict=False) is None
    assert policy.folder_policy("/a/online_rl/sfe/pi05/t/demo_0001") == {
        "method": "sfe",
        "model": "pi05",
    }
    assert policy.folder_policy("/a/models/pi05/t/demo_0001") == {
        "method": "direct",
        "model": "pi05",
    }
    assert policy.folder_policy("/a/data/t/demo_0001") == {}
    assert policy.short_checkpoint("pi05_fr3_all_step49999") == "step49999"


def test_priority_policy_method_then_method_then_control_mode():
    both = policy.from_metadata(
        {"policy": {"method": "rlt"}, "method": "sfe", "control_mode": "dsrl_online_rl"}
    )
    assert both["policy_method"] == "rlt"
    top = policy.from_metadata({"method": "sfe", "control_mode": "dsrl_online_rl"})
    assert top["policy_method"] == "sfe"
    mode = policy.from_metadata({"control_mode": "dsrl_online_rl"})
    assert mode["policy_method"] == "dsrl"
    # A method the pool does not know does not hide a known control mode.
    odd = policy.from_metadata({"method": "xyz", "control_mode": "rlt_online_rl"})
    assert odd["policy_method"] == "rlt"
    assert policy.from_metadata({"method": "xyz"})["policy_method"] == "other"


# ------------------------------------------------------------------ index


def test_schema_and_columns(scanned):
    assert scanned["schema"] == "levi.pool.index.v2"
    frame = index.frame()
    assert {
        "policy",
        "policy_model",
        "policy_checkpoint",
        "policy_method",
        "policy_phase",
        "policy_label",
    } <= set(frame.columns)


def test_old_index_asks_for_a_rescan(scanned):
    frame = index.frame().drop(columns=["policy_method"])
    frame.to_parquet(scanner.index_path(), index=False)
    with pytest.raises(ValueError, match="older LEVI"):
        index.frame()


def test_direct_dsrl_and_student_rows(scanned):
    # models/pi05_a_state/... and models/pi05_a/... share one checkpoint name
    # and episode names: they differ by model.
    a_state = by_key(policy_models=["pi05_a_state"], policy_methods=["direct"])
    assert set(a_state) == {"stack_plates/demo_0000", "stack_plates/demo_0001"}
    row = a_state["stack_plates/demo_0000"]
    assert fields(row) == {
        "policy_model": "pi05_a_state",
        "policy_checkpoint": "pi05_a_step100",
        "policy_method": "direct",
        "policy_phase": None,
    }
    plain = by_key(policy_models=["pi05_a"])
    assert plain["stack_plates/demo_0000"]["policy_checkpoint"] == "pi05_a_step100"
    assert row["policy"] == "pi05_a_step100"  # the old column: the checkpoint
    assert row["policy_label"] == "pi05_a_state · step100 · direct deployment"
    dsrl = by_key(policy_methods=["dsrl"], formats=["robot_capture"])
    assert len(dsrl) == 3
    row = dsrl["stack_plates/demo_0000"]
    assert fields(row) == {
        "policy_model": "pi05_a_state",
        "policy_checkpoint": "pi05_a_step100",
        "policy_method": "dsrl",
        "policy_phase": "eval_ckpt",
    }
    assert row["policy_label"] == "pi05_a_state · step100 · DSRL online RL"
    student = by_key(policy_methods=["student"])
    assert len(student) == 1
    only = next(iter(student.values()))
    assert only["policy_phase"] == "student"
    assert only["policy_label"].endswith("student policy")


def test_rollouts_without_a_policy_block(scanned):
    rows = index.episodes(limit=1000, show_copies=True)["episodes"]
    rlt = next(
        r
        for r in rows
        if r["key"].endswith("online_rl/rlt/pi05_a_state/stack_plates/demo_0000")
    )
    # The folder layout says how it was run and which model.
    assert rlt["policy_method"] == "rlt" and rlt["policy_model"] == "pi05_a_state"
    assert rlt["policy_checkpoint"] is None
    old = next(r for r in rows if "models/pi05_old" in r["key"])
    assert old["policy_method"] == "direct" and old["policy_model"] == "pi05_old"
    lost = next(r for r in rows if "/misc/" in r["key"])
    assert lost["policy_method"] == "unknown" and lost["policy_model"] is None
    assert lost["policy_label"] == "unknown method"
    human = next(r for r in rows if "/human/" in r["key"])
    assert human["category"] == "human"
    assert human["policy_method"] is None and human["policy_label"] is None


def test_lerobot_rollouts_inherit_from_the_linked_capture(scanned):
    recap = {
        r["episode"]: r
        for r in index.episodes(limit=100, show_copies=True)["episodes"]
        if r["format"] == "lerobot"
    }
    assert len(recap) == 4
    linked = recap["0"]
    assert linked["derived_from"] and linked["category"] == "rollout"
    assert fields(linked) == {
        "policy_model": "pi05_a_state",
        "policy_checkpoint": "pi05_a_step100",
        "policy_method": "dsrl",
        "policy_phase": "eval_ckpt",
    }
    # Not indexed: the source path and rollout_source_method are what is left.
    unindexed = recap["1"]
    assert unindexed["derived_from"] is None
    assert unindexed["policy_method"] == "sfe" and unindexed["policy_model"] == "pi05_b"
    assert recap["2"]["policy_method"] == "direct"
    assert recap["3"]["policy_method"] == "unknown"


def test_facet_counts(scanned):
    facets = index.facets(categories=["rollout"])
    # Canonical episodes only: the linked LeRobot episode is a copy of its capture.
    assert facets["policy_methods"] == {
        "direct": 5,  # 3 models/ rollouts + pi05_old + lerobot #2
        "dsrl": 3,
        "rlt": 1,
        "student": 1,
        "sfe": 1,
        "unknown": 2,
    }
    assert facets["policy_models"]["pi05_a_state"] == 2 + 3 + 1 + 1
    assert facets["policy_models"]["pi05_a"] == 1
    assert facets["policy_checkpoints"] == {"pi05_a_step100": 7}
    # The old field: the checkpoint, else the model.
    assert facets["policies"]["pi05_a_step100"] == 7
    assert facets["policies"]["pi05_old"] == 1
    # Human data has none, so a category without rollouts shows no policy.
    human = index.facets(categories=["human"])
    assert human["policy_methods"] == {} and human["policy_models"] == {}


def test_tasks_carry_the_methods(scanned):
    task = index.tasks(categories=["rollout"], policy_methods=["dsrl"])
    assert task[0]["policy_methods"] == {"dsrl": 3}


# ------------------------------------------------------------------ recipes


def test_recipe_filters_and_the_old_policies_field(scanned):
    def count(**kw):
        return recipe.preview(Recipe(name="r", categories=["rollout"], **kw))[
            "episodes"
        ]

    assert count(policy_methods=["dsrl"]) == 3
    assert count(policy_methods=["dsrl", "student"]) == 4
    assert count(policy_models=["pi05_a"]) == 1
    assert count(policy_checkpoints=["pi05_a_step100"]) == 7
    assert count(policy_models=["pi05_a_state"], policy_methods=["direct"]) == 2
    # A saved recipe from before the split still means the checkpoint.
    assert count(policies=["pi05_a_step100"]) == 7
    assert count(policies=["nothing"]) == 0
    preview = recipe.preview(
        Recipe(name="r", categories=["rollout"], policy_methods=["dsrl"])
    )
    assert preview["policy_methods"] == {"dsrl": 3}
    assert preview["policy_models"] == {"pi05_a_state": 3}
    with pytest.raises(ValueError):
        Recipe(name="r", policy_methods=["magic"])
    old = Recipe.model_validate({"name": "old", "policies": ["pi05_a_step100"]})
    assert old.policy_methods == [] and old.policies == ["pi05_a_step100"]


def test_export_plan_records_the_policy_per_episode(scanned, tmp_path):
    rec = Recipe(name="dsrl", categories=["rollout"], policy_methods=["dsrl"])
    options = export.ExportOptions(
        format="raw_capture", name="dsrl-v1", output_dir=str(tmp_path / "exports")
    )
    planned = export.plan(rec, options)
    assert len(planned["episodes"]) == 3
    for ep in planned["episodes"]:
        assert ep["policy_method"] == "dsrl"
        assert ep["policy_model"] == "pi05_a_state"
        assert ep["policy_checkpoint"] == "pi05_a_step100"
        assert ep["policy_phase"] == "eval_ckpt"
        assert ep["policy_label"] == "pi05_a_state · step100 · DSRL online RL"
        assert ep["policy"] == "pi05_a_step100"
    assert planned["recipe"]["policy_methods"] == ["dsrl"]


def test_cli_flags(scanned, capsys):
    from levi.pool import cli

    cli.main(
        [
            "episodes",
            "--category",
            "rollout",
            "--policy-method",
            "student",
            "--policy-model",
            "pi05_a_state",
            "--policy-checkpoint",
            "pi05_a_step100",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["total"] == 1
    cli.main(
        [
            "recipe",
            "save",
            "cli-r",
            "--policy-method",
            "rlt",
            "--policy-model",
            "pi05_a_state",
            "--policy-checkpoint",
            "pi05_a_step100",
        ]
    )
    saved = recipe.load("cli-r")
    assert saved.policy_methods == ["rlt"] and saved.policy_models == ["pi05_a_state"]


def test_api_facets_and_filters(scanned, client):
    facets = client.get(
        "/api/levi/pool/facets", params={"category": ["rollout"]}
    ).json()
    assert facets["policy_methods"]["dsrl"] == 3
    assert facets["policy_models"]["pi05_a"] == 1
    assert facets["policy_checkpoints"] == {"pi05_a_step100": 7}
    page = client.get(
        "/api/levi/pool/episodes",
        params={
            "category": ["rollout"],
            "policy_method": ["dsrl", "student"],
            "policy_model": ["pi05_a_state"],
            "policy_checkpoint": ["pi05_a_step100"],
        },
    ).json()
    assert page["total"] == 4
    assert {e["policy_method"] for e in page["episodes"]} == {"dsrl", "student"}
    old = client.get(
        "/api/levi/pool/episodes", params={"policy": ["pi05_a_step100"]}
    ).json()
    assert old["total"] == 7
    tasks = client.get(
        "/api/levi/pool/tasks",
        params={"category": ["rollout"], "policy_method": ["rlt"]},
    ).json()["tasks"]
    assert tasks[0]["policy_methods"] == {"rlt": 1}
