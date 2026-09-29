"""Training pool: scan, classify, deduplicate, hold out, compose, export."""

import hashlib
import json
import shutil
import subprocess

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest

from levi.conversion.options import Options
from levi.pool import export, heldout, index, jobs, recipe, scanner, settings
from levi.pool.recipe import Recipe

# ------------------------------------------------------------------ fixture

VIDEO = 0


def _video(path, frames, fps=10):
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
            f"testsrc2=size=64x48:rate={fps}:duration={frames / fps}",
            "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", str(path),
        ],
        check=True,
    )


def make_demo(demo, n=20, *, rollout=None, task=None, created="2026-08-01T10:00:00"):
    """A raw capture episode (RC files); ``rollout`` = outcome of a policy
    rollout, None = human teleoperation."""
    demo.mkdir(parents=True)
    t = np.arange(n) / 10
    pose = pd.DataFrame(
        {
            "timestamp_sec": t, "frame_index": np.arange(n), "success_flag": 1,
            "source_stamp_sec": t, "px": t * 0.01, "py": 0.0, "pz": 0.0,
            "qx": 0.0, "qy": 0.0, "qz": 0.0, "qw": 1.0,
        }
    )
    pose.to_csv(demo / "end_effector_pose.csv", index=False)
    pd.DataFrame(
        {
            "timestamp_sec": t, "frame_index": np.arange(n), "success_flag": 1,
            "source_stamp_sec": t, "finger_left": 0.01, "finger_right": 0.01,
            "gripper_width": 0.02,
            "last_gripper_command": ["open"] * (n // 2) + ["close"] * (n - n // 2),
        }
    ).to_csv(demo / "gripper_state.csv", index=False)
    pose[["timestamp_sec", "frame_index"]].to_csv(demo / "frames.csv", index=False)
    pd.DataFrame({"timestamp_sec": [t[-1]], "event": ["stop_demo"]}).to_csv(
        demo / "events.csv", index=False
    )
    meta = {
        "task_description": task or demo.parent.name,
        "created_at": created,
        "stopped_at": created,
        "frame_count": n,
        "collection_freq_hz": 10.0,
    }
    if rollout:
        meta.update(
            data_source="policy_rollout",
            control_mode="policy_rollout",
            eval={"outcome": rollout},
            success_flag_final=int(rollout == "success"),
            policy={"checkpoint_dir": "/models/pi05_test_step10"},
        )
    else:
        meta.update(control_mode="pygame", success_flag_final=0)
    (demo / "metadata.json").write_text(json.dumps(meta))
    for camera in Options().cameras:
        _video(demo / f"{camera}.mp4", n)
    return demo


def make_lerobot(root, dims, fps=10, n=20, episodes=2, task="Move the gripper"):
    """A small LeRobot v2.1 dataset with videos (state/action of ``dims``)."""
    (root / "meta").mkdir(parents=True)
    features = {
        "action": {"dtype": "float32", "shape": [dims], "names": [f"a{i}" for i in range(dims)]},
        "observation.state": {"dtype": "float32", "shape": [dims], "names": [f"s{i}" for i in range(dims)]},
        "timestamp": {"dtype": "float32", "shape": [1], "names": None},
        "episode_index": {"dtype": "int64", "shape": [1], "names": None},
        "frame_index": {"dtype": "int64", "shape": [1], "names": None},
        "index": {"dtype": "int64", "shape": [1], "names": None},
        "task_index": {"dtype": "int64", "shape": [1], "names": None},
    }
    for key in ("observation.images.hand", "observation.images.view1"):
        features[key] = {
            "dtype": "video", "shape": [48, 64, 3], "names": ["height", "width", "channel"],
            "info": {"video.fps": fps, "video.codec": "h264", "video.pix_fmt": "yuv420p"},
        }
    info = {
        "codebase_version": "v2.1", "robot_type": "fixture", "fps": fps,
        "total_episodes": episodes, "total_frames": n * episodes, "total_tasks": 1,
        "total_videos": 2 * episodes, "total_chunks": 1, "chunks_size": 1000,
        "splits": {"train": f"0:{episodes}"},
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
        "features": features,
    }
    (root / "meta/info.json").write_text(json.dumps(info))
    (root / "meta/tasks.jsonl").write_text(json.dumps({"task_index": 0, "task": task}) + "\n")
    rows = []
    for ep in range(episodes):
        rows.append({"episode_index": ep, "tasks": [task], "length": n})
        t = np.arange(n, dtype=np.float32)
        pd.DataFrame(
            {
                "observation.state": [[float(x + d) for d in range(dims)] for x in t],
                "action": [[float(x + d + 1) for d in range(dims)] for x in t],
                "timestamp": t / fps,
                "episode_index": [ep] * n,
                "frame_index": range(n),
                "index": range(ep * n, (ep + 1) * n),
                "task_index": [0] * n,
            }
        ).to_parquet(root / f"data/chunk-000/episode_{ep:06d}.parquet")
        for key in ("observation.images.hand", "observation.images.view1"):
            _video(root / f"videos/chunk-000/{key}/episode_{ep:06d}.mp4", n, fps)
    (root / "meta/episodes.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return root


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def pool_root(tmp_path_factory):
    """One pool root with every situation the survey found."""
    root = tmp_path_factory.mktemp("pool") / "wenkai"
    root.mkdir()
    collect = root / "collect/data"
    for i in range(3):
        make_demo(collect / "Pour_water_into_cup" / f"demo_{i:04d}", created=f"2026-08-0{i + 1}T10:00:00")
    make_demo(collect / "pour_water_into_cup/demo_0000", task="pour_water_into_cup")
    for i in range(2):
        make_demo(collect / "stack_the_plates" / f"demo_{i:04d}")
    make_demo(collect / "stack_the_plates/demo_0002 copy")
    make_demo(collect / "stack_the_plates/demo_0003_failure")
    # A filtered subset: byte copies of two demos.
    for name in ("Pour_water_into_cup/demo_0000", "stack_the_plates/demo_0001"):
        shutil.copytree(collect / name, root / "collect/data_filtered" / name)
    rollouts = root / "rollouts/models/pi05_test"
    for i, outcome in enumerate(["success", "failure", "success", "success"]):
        make_demo(rollouts / "stack_the_plates" / f"demo_{i:04d}", rollout=outcome,
                  task="stack the plates", created=f"2026-09-1{i}T10:00:00")
    # A frozen (held-out) rollout and its copy inside an evaluation workspace.
    frozen = rollouts / "stack_the_plates/demo_0001"
    evalws = root / "evalws"
    shutil.copytree(frozen, evalws / "plates_frozen/demo_0000")
    (evalws / "outputs/LEVI/workbench").mkdir(parents=True)
    (evalws / "outputs/LEVI/workbench/datasets.json").write_text("{}")
    # The LEVI workspace with a human outcome label on a rollout demo.
    levi_ws = root / "LEVI/.state"
    view = levi_ws / "outputs/LEVI/workbench/views/plates"
    (view / "meta").mkdir(parents=True)
    (view / "meta/episodes.jsonl").write_text(
        json.dumps({"episode_index": 0, "source_demo": "stack_the_plates/demo_0002"}) + "\n"
    )
    (levi_ws / "outputs/LEVI/workbench/datasets.json").write_text(
        json.dumps({"plates": {"name": "plates", "kind": "raw", "path": str(rollouts), "view": str(view)}})
    )
    label = levi_ws / "outputs/LEVI/workbench/annotations/plates/outcomes"
    label.mkdir(parents=True)
    (label / "episode_000000.json").write_text(
        json.dumps({"episode_index": 0, "outcome": "failure", "source": "human"})
    )
    # LeRobot datasets: one compatible with the raw conversion (7 dims), one not.
    make_lerobot(root / "lerobot/fr3_seven", 7, task="Move the gripper")
    make_lerobot(root / "lerobot/two_dims", 2, task="Wave")
    # Archive, DROID and a record-only format.
    shutil.copytree(collect / "stack_the_plates/demo_0000", root / "_archive/old/stack_the_plates/demo_0000")
    droid = root / "droid/2026-01-01/Sun_Jan__1_00_00_00_2026"
    droid.mkdir(parents=True)
    (droid / "trajectory.h5").write_bytes(b"\x89HDF")
    (droid / "metadata_x.json").write_text(json.dumps({"current_task": "Open the drawer", "trajectory_length": 30, "success": True}))
    (root / "buffers").mkdir()
    np.save(root / "buffers/replay.npy", np.zeros(300_000))
    # Never descended: a virtual environment holding a "dataset".
    make_lerobot(root / "LEVI/.venv/lib/site-packages/data", 2, episodes=1)
    lists = root / "frozen.json"
    lists.write_text(
        json.dumps(
            {
                "version": "fixture-frozen-v1",
                "episodes": [
                    {
                        "path": "rollouts/models/pi05_test/stack_the_plates/demo_0001",
                        "frame_count": 20,
                        "sha256": {c: _sha(frozen / c) for c in ("side_camera.mp4", "wrist_camera.mp4")},
                        "frozen_id": "F000",
                    }
                ],
            }
        )
    )
    return root


@pytest.fixture
def pool(pool_root, tmp_path, monkeypatch):
    """A scanned pool in a fresh workspace, exports allowed under tmp_path."""
    workspace = tmp_path / "ws"
    monkeypatch.setenv("LEVI_WORKSPACE", str(workspace))
    monkeypatch.setenv("LEVI_POOL_ROOTS", str(pool_root))
    monkeypatch.setenv("LEVI_POOL_HELDOUT", str(pool_root / "frozen.json"))
    monkeypatch.setenv("LEVI_EXPORT_ROOTS", str(tmp_path))
    monkeypatch.setattr(export, "_levi_commit", lambda: "test")
    summary = scanner.scan()
    return {"root": pool_root, "workspace": workspace, "summary": summary, "out": tmp_path}


def rows(**filters):
    return index.episodes(limit=10000, **filters)["episodes"]


# ------------------------------------------------------------------ P1


def test_scan_detects_formats_and_classifies(pool):
    summary = pool["summary"]
    assert summary["formats"] == {"robot_capture": 16, "lerobot": 4, "droid_raw": 1}
    by_source = {s["id"]: s for s in index.sources(show_archive=True)}
    assert by_source["collect/data"]["category"] == "human"
    assert by_source["rollouts/models/pi05_test"]["category"] == "rollout"
    assert by_source["droid"]["category"] == "external"
    assert by_source["_archive/old"]["category"] == "archive"
    assert by_source["evalws/plates_frozen"]["category"] == "levi"
    assert by_source["lerobot/fr3_seven"]["category"] == "human"  # no provenance
    assert not by_source["droid"]["exportable"]
    assert by_source["buffers"]["format"] == "npy" and not by_source["buffers"]["exportable"]
    assert "LEVI/.venv/lib/site-packages/data" not in by_source
    every = rows(show_heldout=True, show_copies=True, show_archive=True)
    reasons = {r["category_reason"] for r in every}
    assert {"control_mode=pygame", "data_source=policy_rollout", "path_rule", "format_rule",
            "levi_workspace_copy"} <= reasons
    rollout = next(r for r in every if r["episode"] == "stack_the_plates/demo_0000" and r["category"] == "rollout")
    assert rollout["policy"] == "pi05_test_step10" and rollout["date"] == "2026-09-10"
    assert rollout["robot_flag"] == "success" and rollout["outcome_source"] == "robot_flag"
    # A human label from the LEVI workspace beats the robot's flag.
    labelled = next(r for r in every if r["key"].endswith("pi05_test/stack_the_plates/demo_0002"))
    assert labelled["robot_flag"] == "success" and labelled["human_label"] == "failure"
    assert labelled["outcome"] == "failure" and labelled["outcome_source"] == "human"
    # Archive and held-out episodes are hidden by default.
    assert all(r["category"] != "archive" and not r["heldout"] for r in rows())


def test_nonstandard_folders_and_case_duplicate_tasks(pool):
    every = rows(show_copies=True)
    odd = {r["episode"]: r for r in every if r["nonstandard"]}
    assert set(odd) == {"stack_the_plates/demo_0002 copy", "stack_the_plates/demo_0003_failure"}
    assert all(not r["exportable"] and r["nonstandard_reason"] == "nonstandard_folder_name" for r in odd.values())
    tasks = {t["task"]: t for t in index.tasks(categories=["human"])}
    assert tasks["pour water into cup"]["spellings"] == ["Pour_water_into_cup", "pour_water_into_cup"]
    assert tasks["pour water into cup"]["episodes"] == 4
    preview = recipe.preview(Recipe(name="r", categories=["human"], tasks=["stack the plates"]))
    assert preview["excluded_nonstandard"] == 2 and preview["episodes"] == 2
    included = recipe.preview(Recipe(name="r", categories=["human"], tasks=["stack the plates"], include_nonstandard=True))
    assert included["episodes"] == 4


def test_dedup_byte_copies_keeps_the_original(pool):
    dedup = pool["summary"]["dedup"]
    # data_filtered (2) + evalws copy (1) + archive copy (1) + the same demo in the venv is skipped
    assert dedup["duplicate_episodes"] == 4 and dedup["groups_with_copies"] == 4
    every = rows(show_copies=True, show_heldout=True, show_archive=True)
    copy = next(r for r in every if r["source"] == "collect/data_filtered" and r["episode"] == "stack_the_plates/demo_0001")
    assert not copy["canonical"] and copy["canonical_key"].endswith("collect/data/stack_the_plates/demo_0001")
    original = next(r for r in every if r["key"] == copy["canonical_key"])
    assert original["canonical"] and original["copies"] == 1
    source = next(s for s in index.sources() if s["id"] == "collect/data_filtered")
    assert source["copies"] == 2 and source["copy_of"] == {"collect/data": 2}
    assert all(r["canonical"] for r in rows())


def test_heldout_marks_original_and_copy_and_export_refuses(pool):
    report = pool["summary"]["heldout"]
    assert report["matched_by_path"] == 1 and report["unmatched"] == []
    assert report["heldout_originals"] == 1 and report["heldout_copies"] == 1
    held = rows(show_heldout=True, show_copies=True)
    held = [r for r in held if r["heldout"]]
    assert {r["source"] for r in held} == {"rollouts/models/pi05_test", "evalws/plates_frozen"}
    assert all(r["heldout_id"] == "F000" for r in held)
    # Selecting the evaluation workspace copy directly still excludes it.
    preview = recipe.preview(Recipe(name="r", sources=["evalws/plates_frozen"]))
    assert preview["episodes"] == 0 and preview["excluded_heldout"] == 1
    preview = recipe.preview(Recipe(name="r", categories=["rollout"]))
    assert preview["excluded_heldout"] == 1 and preview["episodes"] == 3
    # The export itself checks again, independent of the index.
    frozen = pool["root"] / "rollouts/models/pi05_test/stack_the_plates/demo_0001"
    for smuggled in (str(frozen), str(pool["root"] / "evalws/plates_frozen/demo_0000")):
        with pytest.raises(PermissionError, match="held-out"):
            export.refuse_heldout(
                [{"key": smuggled, "format": "robot_capture", "frames": 20}],
                [pool["root"]],
                [pool["root"] / "frozen.json"],
            )
    export.refuse_heldout(
        [{"key": str(frozen.parent / "demo_0000"), "format": "robot_capture", "frames": 20}],
        [pool["root"]],
        [pool["root"] / "frozen.json"],
    )
    assert heldout.load([pool["root"] / "frozen.json"])[0]["set"] == "fixture-frozen-v1"


def test_recipe_preview_counts_and_saved_recipes(pool):
    saved = recipe.save(
        Recipe(name="plates-then-water", categories=["human"],
               tasks=["stack the plates", "pour water into cup"], per_task_cap=2, seed=1)
    )
    assert saved["tasks"] == ["stack the plates", "pour water into cup"]
    loaded = recipe.load("plates-then-water")
    preview = recipe.preview(loaded)
    assert [t["task"] for t in preview["tasks"]] == ["stack the plates", "pour water into cup"]
    assert preview["episodes"] == 4 and preview["frames"] == 80
    assert preview["excluded"] == {"nonstandard": 2, "duplicate": 2, "per_task_cap": 2}
    assert preview["excluded_duplicates"] == 2 and preview["excluded_heldout"] == 0
    # Outcome filters: the robot's flag, or a human label first.
    flagged = recipe.preview(Recipe(name="r", categories=["rollout"], outcome="robot_flag_success"))
    verified = recipe.preview(Recipe(name="r", categories=["rollout"], outcome="verified_success"))
    assert flagged["episodes"] == 2 and verified["episodes"] == 1  # demo_0002 relabelled failure
    # RECAP needs an outcome per episode: human demos have none.
    value = recipe.preview(Recipe(name="r", categories=["human", "rollout"]), target="recap_value")
    assert value["excluded"]["no_outcome"] == 6 and value["episodes"] == 3
    dated = recipe.preview(Recipe(name="r", categories=["rollout"], date_from="2026-09-12"))
    assert dated["episodes"] == 2
    assert [r["name"] for r in recipe.listing()] == ["plates-then-water"]
    assert recipe.delete("plates-then-water") and recipe.listing() == []
    with pytest.raises(ValueError):
        Recipe(name="r", tasks=["a", "A"])


# ------------------------------------------------------------------ P2


def _export(pool, rec, **options):
    options = export.ExportOptions(output_dir=str(pool["out"] / "exports"), **options)
    job = jobs.plan_export(rec, options)
    result = jobs.execute(job)
    return job, result, json.loads((pool["out"] / "exports" / options.name / "pool_export.json").read_text())


def test_export_lerobot_from_two_sources_in_task_order(pool):
    rec = Recipe(name="mix", categories=["human", "rollout"],
                 tasks=["stack the plates", "pour water into cup"], per_task_cap=3, seed=0,
                 task_text={"pour water into cup": "Pour water into the cup"})
    job, result, record = _export(pool, rec, format="lerobot_v21", name="mix-v1")
    assert result["ok"] and result["episodes"] == 6
    out = pool["out"] / "exports/mix-v1"
    info = json.loads((out / "meta/info.json").read_text())
    assert info["codebase_version"] == "v2.1" and info["fps"] == 10 and info["total_episodes"] == 6
    assert sorted(k for k, f in info["features"].items() if f["dtype"] == "video") == [
        "observation.images.hand", "observation.images.view1"]
    assert info["features"]["observation.state"]["shape"] == [7]
    tasks = [json.loads(l) for l in (out / "meta/tasks.jsonl").read_text().splitlines()]
    assert [t["task"] for t in tasks] == ["stack the plates", "Pour water into the cup"]
    episodes = [json.loads(l) for l in (out / "meta/episodes.jsonl").read_text().splitlines()]
    assert [e["tasks"][0] for e in episodes] == ["stack the plates"] * 3 + ["Pour water into the cup"] * 3
    assert [e["episode_index"] for e in episodes] == list(range(6))
    # Within a task: sources in recipe/source order, then episode order.
    plates = [e["pool_key"] for e in episodes[:3]]
    assert all("collect/data/" in k for k in plates[:2]) and "rollouts" in plates[2]
    table = pq.read_table(out / "data/chunk-000/episode_000005.parquet")
    assert table.column("task_index").to_pylist()[0] == 1
    assert record["task_order"] == ["stack the plates", "pour water into cup"]
    assert record["levi_commit"] == "test" and record["recipe"]["name"] == "mix"
    assert {e["reason"] for e in record["excluded"]} == {"nonstandard", "duplicate", "per_task_cap", "heldout"}
    assert len(record["episodes"]) == 6 and all(e["fingerprint"] for e in record["episodes"])
    assert not (out.parent / ".mix-v1.partial").exists()
    validation = json.loads((out / "meta/levi_validation.json").read_text())
    assert validation["ok"]
    # Sources untouched, target may not be reused.
    assert not list(pool["root"].rglob("*.partial"))
    with pytest.raises(ValueError, match="already exists"):
        jobs.plan_export(rec, export.ExportOptions(format="lerobot_v21", name="mix-v1", output_dir=str(pool["out"] / "exports")))


def test_export_recap_value_uses_human_label_over_robot_flag(pool):
    rec = Recipe(name="value", categories=["rollout"], tasks=["stack the plates"])
    job, result, record = _export(pool, rec, format="recap_value", name="value-v1")
    assert result["episodes"] == 3 and record["counts"]["excluded"]["heldout"] == 1
    out = pool["out"] / "exports/value-v1"
    labels = (out / "meta/episode_labels.csv").read_text().splitlines()
    episodes = [json.loads(l) for l in (out / "meta/episodes.jsonl").read_text().splitlines()]
    by_key = {e["pool_key"].rsplit("/", 1)[-1]: e for e in episodes}
    assert by_key["demo_0002"]["levi_outcome"] == "failure"
    assert by_key["demo_0002"]["levi_outcome_source"] == "human"
    assert by_key["demo_0000"]["levi_outcome_source"] == "robot_flag"
    assert labels[0] == "episode_index,success" and len(labels) == 4
    recap = json.loads((out / "meta/levi_recap.json").read_text())
    assert recap["outcomes"] == {"success": 2, "failure": 1}
    assert (out / "meta/returns.parquet").is_file()
    table = pq.read_table(out / "data/chunk-000/episode_000000.parquet")
    assert "next.reward" in table.column_names and "is_success" in table.column_names
    # Human demos have no outcome: refused from the value export.
    with pytest.raises(ValueError, match="no exportable"):
        jobs.plan_export(Recipe(name="h", categories=["human"], tasks=["pour water into cup"]),
                         export.ExportOptions(format="recap_value", name="x", output_dir=str(pool["out"])))


def test_export_raw_capture_copies_renumbered_task_folders(pool):
    rec = Recipe(name="raw", categories=["human", "rollout"], tasks=["pour water into cup", "stack the plates"])
    job, result, record = _export(pool, rec, format="raw_capture", name="raw-v1", hardlink=True)
    out = pool["out"] / "exports/raw-v1"
    assert sorted(p.name for p in out.iterdir()) == ["pool_export.json", "pour_water_into_cup", "stack_the_plates"]
    assert sorted(p.name for p in (out / "stack_the_plates").iterdir()) == [
        "demo_0000", "demo_0001", "demo_0002", "demo_0003", "demo_0004", "task_description.txt"]
    assert (out / "pour_water_into_cup/task_description.txt").read_text().strip() == "pour water into cup"
    assert (out / "pour_water_into_cup/demo_0003/metadata.json").is_file()
    assert record["format"] == "raw_capture" and len(record["episodes"]) == 9
    assert record["episodes"][0]["path"].startswith("pour_water_into_cup/")
    # A LeRobot-only recipe has nothing to copy.
    with pytest.raises(ValueError, match="no exportable"):
        jobs.plan_export(Recipe(name="l", sources=["lerobot/fr3_seven"]),
                         export.ExportOptions(format="raw_capture", name="x", output_dir=str(pool["out"])))


def test_schema_mismatch_between_sources_is_refused(pool):
    rec = Recipe(name="bad", sources=["collect/data", "lerobot/two_dims"], tasks=["pour water into cup", "wave"])
    options = export.ExportOptions(format="lerobot_v21", name="bad-v1", output_dir=str(pool["out"]))
    job = jobs.plan_export(rec, options)
    with pytest.raises(ValueError, match="observation.state has 2 dims, expected 7"):
        jobs.execute(job)
    assert not (pool["out"] / "bad-v1").exists() and not (pool["out"] / ".bad-v1.partial").exists()
    # Two LeRobot sources with matching schemas merge; a wrong fps is refused.
    good = Recipe(name="ok", sources=["lerobot/fr3_seven", "collect/data"], tasks=["move the gripper", "pour water into cup"], per_task_cap=1)
    job, result, record = _export(pool, good, format="lerobot_v21", name="ok-v1")
    assert result["episodes"] == 2 and record["task_order"] == ["move the gripper", "pour water into cup"]
    with pytest.raises(ValueError, match="fps"):
        jobs.execute(jobs.plan_export(good, export.ExportOptions(format="lerobot_v21", name="ok-v2", fps=5, output_dir=str(pool["out"]))))


def test_path_guards(pool):
    sources = [str(pool["root"] / "collect/data")]
    with pytest.raises(PermissionError, match="outside LEVI_EXPORT_ROOTS"):
        settings.check_export_target("/somewhere/else/x", sources)
    with pytest.raises(PermissionError, match="inside the source dataset"):
        settings.check_export_target(pool["root"] / "collect/data/new", sources)
    with pytest.raises(PermissionError, match="would contain"):
        settings.check_export_target(pool["root"] / "collect", sources)
    with pytest.raises(ValueError, match="already exists"):
        settings.check_export_target(pool["out"] / "ws", sources)
    target = settings.check_export_target(pool["out"] / "fresh", sources)
    assert target == pool["out"] / "fresh"
    # Relative names land under the workspace's export folder.
    assert settings.check_export_target("named", sources) == pool["workspace"] / "exports/pool/named"
    with pytest.raises(PermissionError, match="pool source"):
        settings.guard_write(pool["root"] / "collect/data/anything", sources)
    # An export inside a pool root but outside every dataset is allowed
    # (the plan refuses it only when it lies inside a dataset).
    settings.check_export_target(pool["root"] / "exports/new", sources)
    with pytest.raises(ValueError, match="idle"):
        with pytest.MonkeyPatch.context() as mp:
            mp.delenv("LEVI_POOL_ROOTS")
            settings.require_enabled()


def test_incremental_rescan_reuses_unchanged_episodes(pool):
    again = scanner.scan()
    assert again["reused"] == again["episodes"]
    assert again["categories"] == pool["summary"]["categories"]


def test_api_routes(pool, client):
    assert client.get("/api/levi/pool/status").json()["enabled"]
    sources = client.get("/api/levi/pool/sources").json()["sources"]
    assert {s["id"] for s in sources} >= {"collect/data", "rollouts/models/pi05_test"}
    tasks = client.get("/api/levi/pool/tasks", params={"category": ["rollout"]}).json()["tasks"]
    assert [t["task"] for t in tasks] == ["stack the plates"] and tasks[0]["episodes"] == 3
    page = client.get("/api/levi/pool/episodes", params={"task": "stack the plates", "limit": 2}).json()
    assert page["total"] == 5 and len(page["episodes"]) == 2
    body = {"name": "api", "categories": ["rollout"], "tasks": ["stack the plates"]}
    assert client.put("/api/levi/pool/recipes/api", json=body).status_code == 200
    assert client.put("/api/levi/pool/recipes/other", json=body).status_code == 400
    assert client.get("/api/levi/pool/recipes/api").json()["tasks"] == ["stack the plates"]
    preview = client.post("/api/levi/pool/preview", json={"recipe": body, "format": "recap_value"}).json()
    assert preview["episodes"] == 3 and preview["excluded_heldout"] == 1
    dry = client.post(
        "/api/levi/pool/export",
        json={"recipe_name": "api", "options": {"format": "raw_capture", "name": "api-v1", "output_dir": str(pool["out"])}, "dry_run": True},
    ).json()
    assert dry["status"] == "planned" and dry["planned_episodes"] == 3
    refused = client.post(
        "/api/levi/pool/export",
        json={"recipe_name": "api", "options": {"format": "raw_capture", "name": "x", "output_dir": "/elsewhere"}, "dry_run": True},
    )
    assert refused.status_code == 403
    assert client.delete("/api/levi/pool/recipes/api").status_code == 200
    assert client.get("/api/levi/pool/recipes/api").status_code == 404
    assert client.get("/api/levi/pool/jobs").json()["jobs"][0]["kind"] == "export"
