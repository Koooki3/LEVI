"""Training pool: scan, classify, deduplicate, hold out, compose, export."""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

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
    """A short H.264 clip; every call draws different pixels (and bytes)."""
    global VIDEO
    VIDEO += 1
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=64x48:rate={fps}:duration={frames / fps}",
            "-vf",
            f"hue=h={VIDEO * 13 % 360}",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )


def make_demo(
    demo, n=20, *, rollout=None, task=None, created=None, filtered_from=None, fps=10
):
    """A raw capture episode (RC files); ``rollout`` = outcome of a policy
    rollout, None = human teleoperation."""
    demo.mkdir(parents=True)
    t = np.arange(n) / 10 + VIDEO * 100  # each recording its own clock
    created = created or f"2026-08-01T{10 + VIDEO // 60:02d}:{VIDEO % 60:02d}:00"
    pose = pd.DataFrame(
        {
            "timestamp_sec": t,
            "frame_index": np.arange(n),
            "success_flag": 1,
            "source_stamp_sec": t,
            "px": t * 0.01,
            "py": 0.0,
            "pz": 0.0,
            "qx": 0.0,
            "qy": 0.0,
            "qz": 0.0,
            "qw": 1.0,
        }
    )
    pose.to_csv(demo / "end_effector_pose.csv", index=False)
    pd.DataFrame(
        {
            "timestamp_sec": t,
            "frame_index": np.arange(n),
            "success_flag": 1,
            "source_stamp_sec": t,
            "finger_left": 0.01,
            "finger_right": 0.01,
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
        "collection_freq_hz": float(fps),
    }
    if filtered_from:
        meta["filtered_from_frame_count"] = filtered_from
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
        _video(demo / f"{camera}.mp4", n, fps)
    return demo


def _state_names(dims):
    """The pipeline's names for a 7-dim pose + gripper state."""
    return (
        ["x", "y", "z", "rx", "ry", "rz", "gripper"]
        if dims == 7
        else [f"s{i}" for i in range(dims)]
    )


def make_lerobot(root, dims, fps=10, n=20, episodes=2, task="Move the gripper"):
    """A small LeRobot v2.1 dataset with videos (state/action of ``dims``)."""
    (root / "meta").mkdir(parents=True)
    (root / "data/chunk-000").mkdir(parents=True)
    for key in ("observation.images.hand", "observation.images.view1"):
        (root / f"videos/chunk-000/{key}").mkdir(parents=True)
    features = {
        "action": {
            "dtype": "float32",
            "shape": [dims],
            "names": [f"a{i}" for i in range(dims)],
        },
        "observation.state": {
            "dtype": "float32",
            "shape": [dims],
            "names": _state_names(dims),
        },
        "timestamp": {"dtype": "float32", "shape": [1], "names": None},
        "episode_index": {"dtype": "int64", "shape": [1], "names": None},
        "frame_index": {"dtype": "int64", "shape": [1], "names": None},
        "index": {"dtype": "int64", "shape": [1], "names": None},
        "task_index": {"dtype": "int64", "shape": [1], "names": None},
    }
    for key in ("observation.images.hand", "observation.images.view1"):
        features[key] = {
            "dtype": "video",
            "shape": [48, 64, 3],
            "names": ["height", "width", "channel"],
            "info": {
                "video.fps": fps,
                "video.codec": "h264",
                "video.pix_fmt": "yuv420p",
            },
        }
    info = {
        "codebase_version": "v2.1",
        "robot_type": "fixture",
        "fps": fps,
        "total_episodes": episodes,
        "total_frames": n * episodes,
        "total_tasks": 1,
        "total_videos": 2 * episodes,
        "total_chunks": 1,
        "chunks_size": 1000,
        "splits": {"train": f"0:{episodes}"},
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
        "features": features,
    }
    (root / "meta/info.json").write_text(json.dumps(info))
    (root / "meta/tasks.jsonl").write_text(
        json.dumps({"task_index": 0, "task": task}) + "\n"
    )
    rows = []
    for ep in range(episodes):
        rows.append({"episode_index": ep, "tasks": [task], "length": n})
        t = np.arange(n, dtype=np.float32) + ep * 0.5
        pd.DataFrame(
            {
                "observation.state": [[float(x + d) for d in range(dims)] for x in t],
                "action": [[float(x + d + 1) for d in range(dims)] for x in t],
                "timestamp": np.arange(n, dtype=np.float32) / fps,
                "episode_index": [ep] * n,
                "frame_index": range(n),
                "index": range(ep * n, (ep + 1) * n),
                "task_index": [0] * n,
            }
        ).to_parquet(root / f"data/chunk-000/episode_{ep:06d}.parquet")
        for key in ("observation.images.hand", "observation.images.view1"):
            _video(root / f"videos/chunk-000/{key}/episode_{ep:06d}.mp4", n, fps)
    (root / "meta/episodes.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows)
    )
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
        make_demo(
            collect / "Pour_water_into_cup" / f"demo_{i:04d}",
            created=f"2026-08-0{i + 1}T10:00:00",
        )
    make_demo(collect / "pour_water_into_cup/demo_0000", task="pour_water_into_cup")
    for i in range(2):
        make_demo(collect / "stack_the_plates" / f"demo_{i:04d}")
    make_demo(collect / "stack_the_plates/demo_0002 copy")
    make_demo(collect / "stack_the_plates/demo_0003_failure")
    # A filtered subset: byte copies of two demos.
    for name in ("Pour_water_into_cup/demo_0000", "stack_the_plates/demo_0001"):
        shutil.copytree(collect / name, root / "collect/data_filtered" / name)
    # A filtered variant (static frames trimmed) of one recording.
    make_demo(
        root / "collect/data_trimmed/Pour_water_into_cup/demo_0000",
        n=15,
        created="2026-08-02T10:00:00",
        filtered_from=20,
    )
    rollouts = root / "rollouts/models/pi05_test"
    for i, outcome in enumerate(["success", "failure", "success", "success"]):
        make_demo(
            rollouts / "stack_the_plates" / f"demo_{i:04d}",
            rollout=outcome,
            task="stack the plates",
            created=f"2026-09-1{i}T10:00:00",
        )
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
        json.dumps({"episode_index": 0, "source_demo": "stack_the_plates/demo_0002"})
        + "\n"
    )
    (levi_ws / "outputs/LEVI/workbench/datasets.json").write_text(
        json.dumps(
            {
                "plates": {
                    "name": "plates",
                    "kind": "raw",
                    "path": str(rollouts),
                    "view": str(view),
                }
            }
        )
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
    shutil.copytree(
        collect / "stack_the_plates/demo_0000",
        root / "_archive/old/stack_the_plates/demo_0000",
    )
    droid = root / "droid/demo_0000"
    droid.mkdir(parents=True)
    (droid / "trajectory.h5").write_bytes(b"\x89HDF")
    (droid / "metadata_x.json").write_text(
        json.dumps(
            {
                "current_task": "Open the drawer",
                "trajectory_length": 30,
                "success": True,
            }
        )
    )
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
                        "sha256": {
                            c: _sha(frozen / c)
                            for c in ("side_camera.mp4", "wrist_camera.mp4")
                        },
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
    return {
        "root": pool_root,
        "workspace": workspace,
        "summary": summary,
        "out": tmp_path,
    }


def rows(**filters):
    return index.episodes(limit=10000, **filters)["episodes"]


# ------------------------------------------------------------------ P1


def test_scan_detects_formats_and_classifies(pool):
    summary = pool["summary"]
    assert summary["formats"] == {"robot_capture": 17, "lerobot": 4, "droid_raw": 1}
    by_source = {s["id"]: s for s in index.sources(show_archive=True)}
    assert by_source["collect/data"]["category"] == "human"
    assert by_source["rollouts/models/pi05_test"]["category"] == "rollout"
    assert by_source["droid"]["category"] == "external"
    assert by_source["_archive/old"]["category"] == "archive"
    assert by_source["evalws/plates_frozen"]["category"] == "levi"
    assert by_source["lerobot/fr3_seven"]["category"] == "human"  # no provenance
    assert not by_source["droid"]["exportable"]
    assert (
        by_source["buffers"]["format"] == "npy"
        and not by_source["buffers"]["exportable"]
    )
    assert "LEVI/.venv/lib/site-packages/data" not in by_source
    every = rows(show_heldout=True, show_copies=True, show_archive=True)
    reasons = {r["category_reason"] for r in every}
    assert {
        "control_mode=pygame",
        "data_source=policy_rollout",
        "path_rule",
        "format_rule",
        "levi_workspace_copy",
    } <= reasons
    rollout = next(
        r
        for r in every
        if r["episode"] == "stack_the_plates/demo_0000" and r["category"] == "rollout"
    )
    assert rollout["policy"] == "pi05_test_step10" and rollout["date"] == "2026-09-10"
    assert (
        rollout["robot_flag"] == "success" and rollout["outcome_source"] == "robot_flag"
    )
    # A human label from the LEVI workspace beats the robot's flag.
    labelled = next(
        r for r in every if r["key"].endswith("pi05_test/stack_the_plates/demo_0002")
    )
    assert labelled["robot_flag"] == "success" and labelled["human_label"] == "failure"
    assert labelled["outcome"] == "failure" and labelled["outcome_source"] == "human"
    # Archive and held-out episodes are hidden by default.
    assert all(r["category"] != "archive" and not r["heldout"] for r in rows())


def test_nonstandard_folders_and_case_duplicate_tasks(pool):
    every = rows(show_copies=True)
    odd = {r["episode"]: r for r in every if r["nonstandard"]}
    assert set(odd) == {
        "stack_the_plates/demo_0002 copy",
        "stack_the_plates/demo_0003_failure",
    }
    assert all(
        not r["exportable"] and r["nonstandard_reason"] == "nonstandard_folder_name"
        for r in odd.values()
    )
    tasks = {t["task"]: t for t in index.tasks(categories=["human"])}
    assert tasks["pour water into cup"]["spellings"] == [
        "Pour_water_into_cup",
        "pour_water_into_cup",
    ]
    assert tasks["pour water into cup"]["episodes"] == 4
    preview = recipe.preview(
        Recipe(name="r", categories=["human"], tasks=["stack the plates"])
    )
    assert preview["excluded_nonstandard"] == 2 and preview["episodes"] == 2
    included = recipe.preview(
        Recipe(
            name="r",
            categories=["human"],
            tasks=["stack the plates"],
            include_nonstandard=True,
        )
    )
    assert included["episodes"] == 4


def test_dedup_byte_copies_keeps_the_original(pool):
    dedup = pool["summary"]["dedup"]
    # data_filtered (2) + evalws copy (1) + archive copy (1) + trimmed variant (1);
    # the dataset inside the virtual environment is never read.
    assert dedup["duplicate_episodes"] == 5 and dedup["groups_with_copies"] == 5
    assert dedup["filtered_variants"] == 1
    every = rows(show_copies=True, show_heldout=True, show_archive=True)
    copy = next(
        r
        for r in every
        if r["source"] == "collect/data_filtered"
        and r["episode"] == "stack_the_plates/demo_0001"
    )
    assert not copy["canonical"] and copy["canonical_key"].endswith(
        "collect/data/stack_the_plates/demo_0001"
    )
    original = next(r for r in every if r["key"] == copy["canonical_key"])
    assert original["canonical"] and original["copies"] == 1
    source = next(s for s in index.sources() if s["id"] == "collect/data_filtered")
    assert source["copies"] == 2 and source["copy_of"] == {"collect/data": 2}
    assert all(r["canonical"] for r in rows())
    # A filtered variant of one recording belongs to the unfiltered original.
    trimmed = next(r for r in every if r["source"] == "collect/data_trimmed")
    assert trimmed["filtered"] and not trimmed["canonical"]
    assert trimmed["canonical_key"].endswith(
        "collect/data/Pour_water_into_cup/demo_0001"
    )


def test_heldout_marks_original_and_copy_and_export_refuses(pool):
    report = pool["summary"]["heldout"]
    assert report["matched_by_path"] == 1 and report["unmatched"] == []
    assert report["heldout_originals"] == 1 and report["heldout_copies"] == 1
    held = rows(show_heldout=True, show_copies=True)
    held = [r for r in held if r["heldout"]]
    assert {r["source"] for r in held} == {
        "rollouts/models/pi05_test",
        "evalws/plates_frozen",
    }
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
        [
            {
                "key": str(frozen.parent / "demo_0000"),
                "format": "robot_capture",
                "frames": 20,
            }
        ],
        [pool["root"]],
        [pool["root"] / "frozen.json"],
    )
    assert heldout.load([pool["root"] / "frozen.json"])[0]["set"] == "fixture-frozen-v1"
    # A plan edited by hand to carry the copy is refused by the index check too.
    with pytest.raises(PermissionError, match="held-out"):
        export.refuse_heldout_groups(
            [{"key": str(pool["root"] / "evalws/plates_frozen/demo_0000")}]
        )


def test_recipe_preview_counts_and_saved_recipes(pool):
    saved = recipe.save(
        Recipe(
            name="plates-then-water",
            categories=["human"],
            tasks=["stack the plates", "pour water into cup"],
            per_task_cap=2,
            seed=1,
        )
    )
    assert [t["task"] for t in saved["tasks"]] == [
        "stack the plates",
        "pour water into cup",
    ]
    assert {t["strategy"] for t in saved["tasks"]} == {"random"}  # the old form
    loaded = recipe.load("plates-then-water")
    preview = recipe.preview(loaded)
    assert [t["task"] for t in preview["tasks"]] == [
        "stack the plates",
        "pour water into cup",
    ]
    assert preview["episodes"] == 4 and preview["frames"] == 80
    assert preview["excluded"] == {"nonstandard": 2, "duplicate": 3, "per_task_cap": 2}
    assert preview["excluded_duplicates"] == 3 and preview["excluded_heldout"] == 0
    # Outcome filters: the robot's flag, or a human label first.
    flagged = recipe.preview(
        Recipe(name="r", categories=["rollout"], outcome="robot_flag_success")
    )
    verified = recipe.preview(
        Recipe(name="r", categories=["rollout"], outcome="verified_success")
    )
    # demo_0001 is held out; demo_0002's robot flag says success, a person said failure.
    assert flagged["episodes"] == 3 and verified["episodes"] == 2
    # RECAP needs an outcome per episode: human demos have none.
    value = recipe.preview(
        Recipe(name="r", categories=["human", "rollout"]), target="recap_value"
    )
    assert (
        value["excluded"]["no_outcome"] == 10 and value["episodes"] == 3
    )  # 6 raw + 4 LeRobot
    dated = recipe.preview(
        Recipe(name="r", categories=["rollout"], date_from="2026-09-12")
    )
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
    return (
        job,
        result,
        json.loads(
            (pool["out"] / "exports" / options.name / "pool_export.json").read_text()
        ),
    )


def test_export_lerobot_from_two_sources_in_task_order(pool):
    rec = Recipe(
        name="mix",
        categories=["human", "rollout"],
        tasks=["stack the plates", "pour water into cup"],
        per_task_cap=3,
        seed=0,
        task_text={"pour water into cup": "Pour water into the cup"},
    )
    _, result, record = _export(pool, rec, format="lerobot_v21", name="mix-v1")
    assert result["ok"] and result["episodes"] == 6
    out = pool["out"] / "exports/mix-v1"
    info = json.loads((out / "meta/info.json").read_text())
    assert (
        info["codebase_version"] == "v2.1"
        and info["fps"] == 10
        and info["total_episodes"] == 6
    )
    assert sorted(k for k, f in info["features"].items() if f["dtype"] == "video") == [
        "observation.images.hand",
        "observation.images.view1",
    ]
    assert info["features"]["observation.state"]["shape"] == [7]
    tasks = [json.loads(l) for l in (out / "meta/tasks.jsonl").read_text().splitlines()]
    assert [t["task"] for t in tasks] == ["stack the plates", "Pour water into the cup"]
    episodes = [
        json.loads(l) for l in (out / "meta/episodes.jsonl").read_text().splitlines()
    ]
    assert [e["tasks"][0] for e in episodes] == ["stack the plates"] * 3 + [
        "Pour water into the cup"
    ] * 3
    assert [e["episode_index"] for e in episodes] == list(range(6))
    # Within a task: sources in recipe/source order, then episode order.
    plates = [e["pool_source"] for e in episodes[:3]]
    assert plates == sorted(plates)
    table = pq.read_table(out / "data/chunk-000/episode_000005.parquet")
    assert table.column("task_index").to_pylist()[0] == 1
    assert record["task_order"] == ["stack the plates", "pour water into cup"]
    assert record["levi_commit"] == "test" and record["recipe"]["name"] == "mix"
    assert {e["reason"] for e in record["excluded"]} == {
        "nonstandard",
        "duplicate",
        "per_task_cap",
        "heldout",
    }
    assert len(record["episodes"]) == 6 and all(
        e["fingerprint"] for e in record["episodes"]
    )
    assert not (out.parent / ".mix-v1.partial").exists()
    validation = json.loads((out / "meta/levi_validation.json").read_text())
    assert validation["ok"]
    # Sources untouched, target may not be reused.
    assert not list(pool["root"].rglob("*.partial"))
    with pytest.raises(ValueError, match="already exists"):
        jobs.plan_export(
            rec,
            export.ExportOptions(
                format="lerobot_v21",
                name="mix-v1",
                output_dir=str(pool["out"] / "exports"),
            ),
        )


def test_export_recap_value_uses_human_label_over_robot_flag(pool):
    rec = Recipe(name="value", categories=["rollout"], tasks=["stack the plates"])
    _, result, record = _export(pool, rec, format="recap_value", name="value-v1")
    assert result["episodes"] == 3 and record["counts"]["excluded"]["heldout"] == 1
    out = pool["out"] / "exports/value-v1"
    labels = (out / "meta/episode_labels.csv").read_text().splitlines()
    episodes = [
        json.loads(l) for l in (out / "meta/episodes.jsonl").read_text().splitlines()
    ]
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
        jobs.plan_export(
            Recipe(name="h", categories=["human"], tasks=["pour water into cup"]),
            export.ExportOptions(
                format="recap_value", name="x", output_dir=str(pool["out"])
            ),
        )


def test_export_raw_capture_copies_renumbered_task_folders(pool):
    rec = Recipe(
        name="raw",
        categories=["human", "rollout"],
        tasks=["pour water into cup", "stack the plates"],
    )
    _, _, record = _export(
        pool, rec, format="raw_capture", name="raw-v1", hardlink=True
    )
    out = pool["out"] / "exports/raw-v1"
    assert sorted(p.name for p in out.iterdir()) == [
        "pool_export.json",
        "pour_water_into_cup",
        "stack_the_plates",
    ]
    assert sorted(p.name for p in (out / "stack_the_plates").iterdir()) == [
        "demo_0000",
        "demo_0001",
        "demo_0002",
        "demo_0003",
        "demo_0004",
        "task_description.txt",
    ]
    assert (
        out / "pour_water_into_cup/task_description.txt"
    ).read_text().strip() == "pour water into cup"
    assert (out / "pour_water_into_cup/demo_0003/metadata.json").is_file()
    assert record["format"] == "raw_capture" and len(record["episodes"]) == 9
    assert record["episodes"][0]["path"].startswith("pour_water_into_cup/")
    # A LeRobot-only recipe has nothing to copy.
    with pytest.raises(ValueError, match="no exportable"):
        jobs.plan_export(
            Recipe(name="l", sources=["lerobot/fr3_seven"]),
            export.ExportOptions(
                format="raw_capture", name="x", output_dir=str(pool["out"])
            ),
        )


def test_schema_mismatch_between_sources_is_refused(pool):
    rec = Recipe(
        name="bad",
        sources=["collect/data", "lerobot/two_dims"],
        tasks=["pour water into cup", "wave"],
    )
    options = export.ExportOptions(
        format="lerobot_v21", name="bad-v1", output_dir=str(pool["out"])
    )
    job = jobs.plan_export(rec, options)
    with pytest.raises(ValueError, match="observation.state has 2 dims, expected 7"):
        jobs.execute(job)
    assert (
        not (pool["out"] / "bad-v1").exists()
        and not (pool["out"] / ".bad-v1.partial").exists()
    )
    # Two LeRobot sources with matching schemas merge; a wrong fps is refused.
    good = Recipe(
        name="ok",
        sources=["lerobot/fr3_seven", "collect/data"],
        tasks=["move the gripper", "pour water into cup"],
        per_task_cap=1,
    )
    _, result, record = _export(pool, good, format="lerobot_v21", name="ok-v1")
    assert result["episodes"] == 2 and record["task_order"] == [
        "move the gripper",
        "pour water into cup",
    ]
    with pytest.raises(ValueError, match="fps"):
        jobs.execute(
            jobs.plan_export(
                good,
                export.ExportOptions(
                    format="lerobot_v21",
                    name="ok-v2",
                    fps=5,
                    output_dir=str(pool["out"]),
                ),
            )
        )


def test_an_earlier_export_of_the_same_name_is_not_a_source(pool, monkeypatch):
    """The reported bug: 'Export directory X lies inside the source dataset X' when a
    finished export (still indexed as a source, or already deleted) has the same name."""
    monkeypatch.setenv("LEVI_EXPORT_ROOTS", f"{pool['out']},{pool['root']}")
    old = pool["out"] / "again"
    # 1. the earlier export was deleted but the stale index still lists its path
    assert not old.exists()
    assert settings.check_export_target(old, [str(old)]) == old
    # 2. the earlier export is still there: refused as "already exists", not as a source
    old.mkdir()
    (old / "pool_export.json").write_text("{}")
    with pytest.raises(ValueError, match="already exists"):
        settings.check_export_target(old, [str(old)])
    # 3. a real source dataset at the same path still protects it
    real = pool["out"] / "real_source"
    real.mkdir()
    with pytest.raises(PermissionError, match="inside the source dataset"):
        settings.check_export_target(real, [str(real)])
    # 4. a folder inside a finished export (whose folder is a source) is still refused
    with pytest.raises(PermissionError, match="inside the source dataset"):
        settings.check_export_target(old / "nested", [str(old)])


def test_path_guards(pool, monkeypatch):
    monkeypatch.setenv("LEVI_EXPORT_ROOTS", f"{pool['out']},{pool['root']}")
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
    assert (
        settings.check_export_target("named", sources)
        == pool["workspace"] / "exports/pool/named"
    )
    with pytest.raises(PermissionError, match="pool source"):
        settings.guard_write(pool["root"] / "collect/data/anything", sources)
    # An export inside a pool root but outside every dataset is allowed
    # (the plan refuses it only when it lies inside a dataset).
    settings.check_export_target(pool["root"] / "exports/new", sources)
    monkeypatch.delenv("LEVI_POOL_ROOTS")
    with pytest.raises(ValueError, match="idle"):
        settings.require_enabled()


def test_incremental_rescan_reuses_unchanged_episodes(pool):
    again = scanner.scan()
    assert again["reused"] == again["episodes"]
    assert again["categories"] == pool["summary"]["categories"]


def test_an_index_from_older_scan_code_is_read_again(pool, monkeypatch):
    """The scan caches what it derived from an episode's files; when what it
    derives changes (an unattended rollout's robot flag), the cache must not
    keep the old answer."""
    again = scanner.scan()
    assert again["reused"] == again["episodes"] > 0
    monkeypatch.setattr(scanner, "FACTS_VERSION", scanner.FACTS_VERSION + 1)
    fresh = scanner.scan()
    assert fresh["reused"] == 0 and fresh["episodes"] == again["episodes"]


def test_api_routes(pool, client):
    assert client.get("/api/levi/pool/status").json()["enabled"]
    sources = client.get("/api/levi/pool/sources").json()["sources"]
    assert {s["id"] for s in sources} >= {"collect/data", "rollouts/models/pi05_test"}
    tasks = client.get("/api/levi/pool/tasks", params={"category": ["rollout"]}).json()[
        "tasks"
    ]
    assert [t["task"] for t in tasks] == ["stack the plates"] and tasks[0][
        "episodes"
    ] == 3
    page = client.get(
        "/api/levi/pool/episodes", params={"task": "stack the plates", "limit": 2}
    ).json()
    assert (
        page["total"] == 7 and len(page["episodes"]) == 2
    )  # nonstandard listed, flagged
    body = {"name": "api", "categories": ["rollout"], "tasks": ["stack the plates"]}
    assert client.put("/api/levi/pool/recipes/api", json=body).status_code == 200
    assert client.put("/api/levi/pool/recipes/other", json=body).status_code == 400
    assert [
        t["task"] for t in client.get("/api/levi/pool/recipes/api").json()["tasks"]
    ] == ["stack the plates"]
    preview = client.post(
        "/api/levi/pool/preview", json={"recipe": body, "format": "recap_value"}
    ).json()
    assert preview["episodes"] == 3 and preview["excluded_heldout"] == 1
    dry = client.post(
        "/api/levi/pool/export",
        json={
            "recipe_name": "api",
            "options": {
                "format": "raw_capture",
                "name": "api-v1",
                "output_dir": str(pool["out"]),
            },
            "dry_run": True,
        },
    ).json()
    assert dry["status"] == "planned" and dry["planned_episodes"] == 3
    refused = client.post(
        "/api/levi/pool/export",
        json={
            "recipe_name": "api",
            "options": {
                "format": "raw_capture",
                "name": "x",
                "output_dir": "/elsewhere",
            },
            "dry_run": True,
        },
    )
    assert refused.status_code == 403
    assert client.delete("/api/levi/pool/recipes/api").status_code == 200
    assert client.get("/api/levi/pool/recipes/api").status_code == 404
    assert (
        client.get("/api/levi/pool/jobs").json()["jobs"] == []
    )  # dry runs leave no plan


def test_scan_job_runs_as_a_tracked_worker(pool, client):
    job = client.post("/api/levi/pool/scan").json()
    assert job["kind"] == "scan" and job["status"] == "running"
    assert not jobs.wait_idle(120)
    done = client.get(f"/api/levi/pool/jobs/{job['id']}").json()
    assert done["status"] == "done", (
        done.get("error"),
        (jobs.jobs_dir() / f"{job['id']}.log").read_text()[-2000:],
    )
    assert done["result"]["summary"]["episodes"] == pool["summary"]["episodes"]
    assert done["progress"]["stage"] == "done"


# ------------------------------------------------------------------ P3 page


def test_page_routes_facets_outcomes_and_export_summary(pool, client):
    facets = client.get("/api/levi/pool/facets").json()
    assert facets["hidden_heldout"] >= 1 and facets["hidden_copies"] >= 1
    assert facets["categories"]["rollout"] == 3
    shown = client.get("/api/levi/pool/facets", params={"show_heldout": True}).json()
    assert shown["hidden_heldout"] == 0 and shown["episodes"] > facets["episodes"]
    assert facets["outcomes"]["robot_flag_success"] >= 1
    flagged = client.get(
        "/api/levi/pool/episodes", params={"outcome": "robot_flag_success"}
    ).json()["episodes"]
    assert flagged and all(e["robot_flag"] == "success" for e in flagged)
    verified = client.get(
        "/api/levi/pool/episodes", params={"outcome": "verified_success"}
    ).json()["episodes"]
    assert all((e["human_label"] or e["robot_flag"]) == "success" for e in verified)
    page = client.get("/api/levi/pool/episodes").json()["episodes"]
    assert all("viewer" in e and e["viewer"] is None for e in page)
    body = {"name": "sum", "categories": ["rollout"], "tasks": ["stack the plates"]}
    job = client.post(
        "/api/levi/pool/export",
        json={
            "recipe": body,
            "options": {
                "format": "raw_capture",
                "name": "sum-v1",
                "output_dir": str(pool["out"]),
            },
        },
    ).json()
    assert not jobs.wait_idle(120)
    done = client.get(f"/api/levi/pool/jobs/{job['id']}").json()
    assert done["status"] == "done", done.get("error")
    summary = client.get(f"/api/levi/pool/jobs/{job['id']}/summary").json()
    assert (
        summary["counts"]["episodes"] == 3
        and summary["counts"]["excluded"]["heldout"] == 1
    )
    assert client.get("/api/levi/pool/jobs/nope-1/summary").status_code in (400, 404)


# ------------------------------------------------------------------ review


def _snapshot(root):
    """Every file under ``root``: size, mtime and content hash."""
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            st = path.stat()
            out[str(path)] = (st.st_size, st.st_mtime_ns, _sha(path))
        elif path.is_dir():
            out[str(path)] = "dir"
    return out


@pytest.fixture
def small(tmp_path, monkeypatch):
    """A tiny pool root of its own: original rollouts and a LeRobot dataset
    with no link to them; exports may go anywhere under the root."""
    root = tmp_path / "small"
    for i in range(3):
        make_demo(
            root / "orig/models/pi/pick_x" / f"demo_{i:04d}",
            rollout="success",
            task="pick x",
            created=f"2026-09-0{i + 1}T10:00:00",
        )
    monkeypatch.setenv("LEVI_WORKSPACE", str(tmp_path / "ws2"))
    monkeypatch.setenv("LEVI_POOL_ROOTS", str(root))
    monkeypatch.setenv("LEVI_POOL_HELDOUT", "none")
    monkeypatch.setenv("LEVI_EXPORT_ROOTS", str(tmp_path))
    monkeypatch.setattr(export, "_levi_commit", lambda: "test")
    scanner.scan()
    return {"root": root, "tmp": tmp_path}


def _options(name, **kw):
    return export.ExportOptions(name=name, **kw)


def test_s1_export_needs_a_heldout_list_or_an_explicit_none(pool, monkeypatch):
    rec = Recipe(name="r", categories=["rollout"], tasks=["stack the plates"])
    opts = _options("s1", format="raw_capture", output_dir=str(pool["out"]))
    monkeypatch.delenv("LEVI_POOL_HELDOUT")
    with pytest.raises(ValueError, match="No held-out list"):
        jobs.plan_export(rec, opts)
    assert any(
        w["code"] == "heldout_unconfigured" for w in recipe.preview(rec)["warnings"]
    )
    # Scanned with a list, exported with another one: rescan first.
    other = pool["root"] / "other.json"
    other.write_text(json.dumps({"episodes": []}))
    monkeypatch.setenv("LEVI_POOL_HELDOUT", str(other))
    with pytest.raises(ValueError, match="changed since the last scan"):
        jobs.plan_export(rec, opts)
    # An explicit `none` is a statement, and is recorded.
    monkeypatch.setenv("LEVI_POOL_HELDOUT", "none")
    scanner.scan()
    job = jobs.plan_export(rec, opts)
    assert job["heldout_disabled"] and job["heldout_lists"] == []
    # A hand-edited plan without either is refused when it runs.
    job.update(heldout_disabled=False)
    with pytest.raises(ValueError, match="no held-out list"):
        export.run(job)


def test_s1_unmatched_heldout_entries_warn(pool, monkeypatch):
    lst = pool["root"] / "moved.json"
    lst.write_text(
        json.dumps(
            {"episodes": [{"path": "nowhere/demo_0000", "sha256": {"a.mp4": "0" * 64}}]}
        )
    )
    monkeypatch.setenv("LEVI_POOL_HELDOUT", str(lst))
    scanner.scan()
    rec = Recipe(name="r", categories=["rollout"], tasks=["stack the plates"])
    warnings = {w["code"]: w for w in recipe.preview(rec)["warnings"]}
    assert (
        warnings["heldout_unmatched"]["ids"]
        and not warnings["heldout_unmatched"]["blocking"]
    )


def test_s2_task_text_cannot_leave_the_staging_folder(pool):
    for text in ("..", ".", "...", "/"):
        rec = Recipe(
            name="t",
            categories=["rollout"],
            tasks=["stack the plates"],
            task_text={"stack the plates": text},
        )
        name = f"dots{abs(hash(text))}"
        _, _, record = _export(pool, rec, format="raw_capture", name=name)
        folders = {e["path"].split("/")[0] for e in record["episodes"]}
        assert folders == ({"task"} if text.strip("./") == "" else folders)
        assert all(not e["path"].startswith("..") for e in record["episodes"])
    exports = pool["out"] / "exports"
    assert not [p for p in exports.iterdir() if p.name.startswith("demo_")]
    assert not (pool["out"] / "demo_0000").exists()
    with pytest.raises(ValueError, match="one line"):
        Recipe(name="t", task_text={"a": "x\ny"})
    with pytest.raises(PermissionError, match="outside the export"):
        export._inside(exports / ".a.partial", exports / "demo_0000")


def test_s2_distinct_tasks_may_not_share_one_text(pool):
    rec = Recipe(
        name="t",
        categories=["human", "rollout"],
        tasks=["stack the plates", "pour water into cup"],
        task_text={"stack the plates": "Same", "pour water into cup": "Same"},
    )
    with pytest.raises(ValueError, match="both be written"):
        jobs.plan_export(
            rec,
            _options("same", format="raw_capture", output_dir=str(pool["out"])),
        )


def test_s3_exports_inside_the_pool_root_are_not_originals(small):
    root = small["root"]
    rec = Recipe(name="r", categories=["rollout"], tasks=["pick x"])
    # An export shallower than the originals, in the pool root.
    job = jobs.plan_export(
        rec, _options("x1", format="raw_capture", output_dir=str(root))
    )
    jobs.execute(job)
    scanner.scan()
    frame = index.frame()
    inside = frame[frame.key.str.contains("/x1/")]
    assert len(inside) == 3
    assert set(inside.category) == {"levi"}
    assert set(inside.category_reason) == {"levi_export"}
    originals = frame[frame.key.str.contains("/orig/")]
    assert originals.canonical.all() and set(originals.category) == {"rollout"}
    assert (inside.copies == 1).all() and not inside.canonical.any()
    # The default selection reads the originals.
    chosen, _ = recipe.select(rec)
    assert all("/orig/" in r["key"] for r in chosen) and len(chosen) == 3


def test_s4_raw_and_unlinked_lerobot_of_one_task_is_refused(small):
    root = small["root"]
    make_lerobot(root / "lerobot/pick_x_v", 7, task="pick x")
    scanner.scan()
    rec = Recipe(name="r", tasks=["pick x"])
    codes = {w["code"]: w for w in recipe.preview(rec)["warnings"]}
    assert codes["possible_unlinked_conversion"]["blocking"]
    opts = _options("s4", format="lerobot_v21")
    with pytest.raises(ValueError, match="same recordings twice"):
        jobs.plan_export(rec, opts)
    # Naming the sources, or saying so, lifts it.
    named = rec.model_copy(update={"sources": ["orig/models/pi"]})
    assert not any(
        w["blocking"] for w in recipe.preview(named)["warnings"]
    ) and jobs.plan_export(named, opts)
    allowed = rec.model_copy(update={"allow_unlinked_sources": True})
    assert jobs.plan_export(allowed, _options("s4b", format="lerobot_v21"))


def test_s5_hardlink_links_videos_only(pool):
    rec = Recipe(name="h", categories=["rollout"], tasks=["stack the plates"])
    _, _, record = _export(
        pool, rec, format="raw_capture", name="linked", hardlink=True
    )
    out = pool["out"] / "exports/linked"
    for row in record["episodes"]:
        src = Path(row["source_path"])
        dst = out / row["path"]
        assert row["hardlinked_files"] == 2
        assert (
            os.stat(src / "side_camera.mp4").st_ino
            == os.stat(dst / "side_camera.mp4").st_ino
        )
        for small_file in ("metadata.json", "frames.csv", "events.csv"):
            assert os.stat(src / small_file).st_ino != os.stat(dst / small_file).st_ino
    # Rewriting a copied file in place leaves the source alone.
    row = record["episodes"][0]
    before = (Path(row["source_path"]) / "metadata.json").read_text()
    (out / row["path"] / "metadata.json").write_text("{}")
    assert (Path(row["source_path"]) / "metadata.json").read_text() == before


def _with_conflict(rec_name="c"):
    df = index.frame()
    key = df[
        (df.task == "stack the plates")
        & (df.category == "rollout")
        & df.canonical
        & ~df.heldout.astype(bool)
    ].key.iloc[0]
    df = df.copy()
    df.loc[df.key == key, ["label_conflict", "human_label"]] = [True, None]
    return df, key


def test_s6_label_conflicts_leave_verified_and_recap_selections(pool):
    df, key = _with_conflict()
    rec = Recipe(name="c", categories=["rollout"], tasks=["stack the plates"])
    for outcome in ("verified_success", "human_verified_success"):
        _, excluded = recipe.select(rec.model_copy(update={"outcome": outcome}), df=df)
        assert {"key": key, "reason": "label_conflict"}.items() <= next(
            e for e in excluded if e["key"] == key
        ).items()
    _, excluded = recipe.select(rec, df=df, target="recap_value")
    assert any(e["key"] == key and e["reason"] == "label_conflict" for e in excluded)
    # ...but stays in a plain selection.
    chosen, _ = recipe.select(rec, df=df)
    assert key in {r["key"] for r in chosen}


def test_s6_human_only_outcome_and_visible_fallback(pool):
    rec = Recipe(
        name="v",
        categories=["rollout"],
        tasks=["stack the plates"],
        outcome="verified_success",
    )
    view = recipe.preview(rec)
    assert view["outcome_sources"].get("robot_flag", 0) >= 1
    assert any(w["code"] == "outcome_from_robot_flag" for w in view["warnings"])
    human = recipe.preview(rec.model_copy(update={"outcome": "human_verified_success"}))
    # The one human label in the fixture is a failure: nothing is verified.
    assert human["episodes"] == 0
    assert set(human["outcome_sources"]) <= {"human"}
    rows = index.episodes(limit=100, outcome="human_verified_success")["episodes"]
    assert all(r["human_label"] == "success" for r in rows)


def test_s7_recap_can_count_human_demonstrations_as_success(pool):
    rec = Recipe(
        name="d", categories=["human"], tasks=["pour water into cup"], per_task_cap=2
    )
    assert recipe.preview(rec, "recap_value")["episodes"] == 0
    with pytest.raises(ValueError, match="no exportable"):
        jobs.plan_export(
            rec, _options("d0", format="recap_value", output_dir=str(pool["out"]))
        )
    assert recipe.preview(rec, "recap_value", human_as_success=True)["episodes"] == 2
    _, result, record = _export(
        pool, rec, format="recap_value", name="demos", human_as_success=True
    )
    assert result["episodes"] == 2
    assert {e["outcome_source"] for e in record["episodes"]} == {"sft_demonstration"}
    out = pool["out"] / "exports/demos"
    labels = (out / "meta/episode_labels.csv").read_text().splitlines()
    assert labels[1:] == ["0,1", "1,1"]


def test_s8_sources_are_unchanged_by_every_export_and_by_a_failure(pool):
    before = _snapshot(pool["root"])
    rec = Recipe(
        name="s8",
        categories=["human", "rollout"],
        tasks=["stack the plates", "pour water into cup"],
        per_task_cap=2,
    )
    _export(pool, rec, format="lerobot_v21", name="s8-lerobot")
    _export(
        pool,
        Recipe(
            name="s8",
            categories=["rollout"],
            tasks=["stack the plates"],
            per_task_cap=2,
        ),
        format="recap_value",
        name="s8-recap",
    )
    _export(pool, rec, format="raw_capture", name="s8-raw", hardlink=True)
    _export(pool, rec, format="raw_capture", name="s8-raw2")
    assert _snapshot(pool["root"]) == before
    # A failing export (schema mismatch) leaves the sources and no partial.
    bad = Recipe(name="bad", sources=["lerobot/fr3_seven", "lerobot/two_dims"])
    with pytest.raises(ValueError, match="one schema"):
        _export(pool, bad, format="lerobot_v21", name="s8-bad")
    assert _snapshot(pool["root"]) == before
    assert not list(pool["out"].rglob("*.partial"))
    assert not (pool["out"] / "exports/s8-bad").exists()


def test_s9_export_record_has_conversion_and_groups(pool):
    rec = Recipe(
        name="r", categories=["human"], tasks=["pour water into cup"], per_task_cap=2
    )
    _, _, record = _export(pool, rec, format="lerobot_v21", name="s9")
    assert record["conversion"]["fps"] == 10 and record["conversion"]["filter_static"]
    assert record["conversion"]["timing"] == "resample"
    assert all(e["group"] for e in record["episodes"])
    assert record["heldout_disabled"] is False and record["heldout_lists"]
    _, _, raw = _export(pool, rec, format="raw_capture", name="s9raw")
    assert all(e["group"] for e in raw["episodes"]) and raw["conversion"] is None


def test_nits_camera_maps_must_be_distinct_and_state_names_agree(pool):
    with pytest.raises(ValueError, match="different output keys"):
        export.ExportOptions(
            format="lerobot_v21",
            name="c",
            camera_map={
                "a": "observation.images.hand",
                "b": "observation.images.hand",
            },
        )
    with pytest.raises(ValueError, match="different output keys"):
        export.ExportOptions(
            format="lerobot_v21",
            name="c",
            cameras={"x": "observation.images.hand", "y": "observation.images.hand"},
        )


# ------------------------------------------------------------------ selection


def test_preview_plan_and_export_record_the_same_selection(pool, client):
    rec = Recipe.model_validate(
        {
            "name": "pick",
            "categories": ["rollout"],
            "tasks": [
                {
                    "task": "stack the plates",
                    "count": 2,
                    "success_ratio": 0.5,
                    "strategy": "quality",
                }
            ],
            "seed": 4,
        }
    )
    view = recipe.preview(rec)
    task = view["tasks"][0]
    # demo_0001 is held out; demo_0002 has a human failure label.
    assert (task["available"], task["successes"], task["failures"]) == (3, 2, 1)
    assert (task["selected_successes"], task["selected_failures"]) == (1, 1)
    chosen = recipe.select(rec)[0]
    job, _, record = _export(pool, rec, format="raw_capture", name="picked")
    assert [e["key"] for e in job["episodes"]] == [r["key"] for r in chosen]
    assert [e["source_path"] for e in record["episodes"]] == [r["key"] for r in chosen]
    assert view["episodes"] == len(record["episodes"]) == 2
    for episode in record["episodes"]:
        assert 0 < episode["quality_score"] <= 1
        assert episode["selection_stratum"].startswith("direct | pi05_test")
        assert episode["outcome"] in ("success", "failure")
        assert episode["outcome_source"] in ("human", "robot_flag")
        assert (
            "efficient" in episode["selection_reason"]
            or "full_attempt" in (episode["selection_reason"])
        )
    [selection] = record["selection"]
    assert selection["task"] == "stack the plates" and selection["requested"] == 2
    assert selection["selected"] == selection["exported"] == 2
    assert selection["shortfall"] == 0 and selection["shortfall_failures"] == 0
    # Asking for more failures than exist is reported, and filled.
    more = Recipe.model_validate(
        {
            **rec.model_dump(),
            "name": "more",
            "tasks": [{"task": "stack the plates", "count": 3, "success_ratio": 0.0}],
        }
    )
    short = recipe.preview(more)["tasks"][0]
    assert short["selected"] == 3 and short["shortfall_failures"] == 2
    assert "failure_short" in short["notes"]
    # The API serves the same list and a balanced default for a new task.
    body = {"recipe": rec.model_dump(), "task": "stack the plates"}
    listed = client.post("/api/levi/pool/selection", json=body).json()
    assert [e["key"] for e in listed["episodes"]] == [r["key"] for r in chosen]
    assert listed["report"]["selected"] == 2
    assert (
        client.post(
            "/api/levi/pool/selection", json={**body, "task": "nothing"}
        ).status_code
        == 404
    )
    tip = client.post(
        "/api/levi/pool/suggest",
        json={
            "recipe": {"name": "x", "categories": ["rollout"]},
            "task": "stack the plates",
        },
    ).json()
    assert tip["available"] == 3 and tip["suggested_count"] == 3
    shown = client.post(
        "/api/levi/pool/preview", json={"recipe": rec.model_dump()}
    ).json()
    assert shown["mix"]["episodes"] == 2 and shown["tasks"][0]["available"] == 3


def test_cli_saves_per_task_specs(pool, capsys):
    from levi.pool import cli

    code = cli.main(
        [
            "recipe",
            "save",
            "spec",
            "--category",
            "rollout",
            "--task",
            "stack the plates:count=2,success=50%,strategy=first",
            "--task",
            "pour water into cup",
        ]
    )
    assert code == 0
    loaded = recipe.load("spec")
    assert loaded.tasks[0].count == 2 and loaded.tasks[0].success_ratio == 0.5
    assert loaded.tasks[0].strategy == "first" and loaded.tasks[1].count is None
    assert cli.main(["recipe", "suggest", "spec", "--task", "stack the plates"]) == 0
    assert cli.main(["recipe", "episodes", "spec", "--task", "stack the plates"]) == 0
    assert '"quality_score"' in capsys.readouterr().out


# ------------------------------------------------------------------ timing


@pytest.fixture
def slow(tmp_path, monkeypatch):
    """A pool root with a 10 fps rollout source and a 9.5 fps one (as online
    RL runs record), one task."""
    root = tmp_path / "slowpool"
    for i in range(2):
        make_demo(
            root / "direct/models/pi/pick_x" / f"demo_{i:04d}",
            rollout="success",
            task="pick x",
            created=f"2026-09-0{i + 1}T10:00:00",
        )
        make_demo(
            root / "online_rl/sfe/pi/pick_x" / f"demo_{i:04d}",
            rollout="success",
            task="pick x",
            created=f"2026-09-1{i + 1}T10:00:00",
            fps=9.5,
        )
    monkeypatch.setenv("LEVI_WORKSPACE", str(tmp_path / "ws3"))
    monkeypatch.setenv("LEVI_POOL_ROOTS", str(root))
    monkeypatch.setenv("LEVI_POOL_HELDOUT", "none")
    monkeypatch.setenv("LEVI_EXPORT_ROOTS", str(tmp_path))
    monkeypatch.setattr(export, "_levi_commit", lambda: "test")
    scanner.scan()
    return {"root": root, "tmp": tmp_path}


def _slow_recipe():
    return Recipe(name="slow", categories=["rollout"], tasks=["pick x"])


def _codes(view):
    return {w["code"]: w for w in view["warnings"]}


def test_timing_defaults_per_format_and_raw_capture_ignores_it():
    make = lambda **kw: export.ExportOptions(name="t", **kw)
    assert make(format="lerobot_v21").timing == "resample"
    assert make(format="recap_value").timing == "retime"
    assert make(format="lerobot_v21", timing="retime").timing == "retime"
    assert make(format="recap_value", timing="resample").timing == "resample"
    assert make(format="raw_capture").timing is None
    assert make(format="raw_capture", timing="retime").timing is None
    assert make(format="lerobot_v21", timing="retime").conversion().timing == "retime"
    with pytest.raises(ValueError, match="timing"):
        make(format="lerobot_v21", timing="stretch")


def test_index_holds_the_measured_rate(slow):
    by_source = {}
    for row in rows(categories=["rollout"]):
        by_source.setdefault(row["source"], set()).add(row["measured_fps"])
    assert by_source == {
        "direct/models/pi": {10.0},
        "online_rl/sfe/pi": {9.5},
    }


def test_preview_warns_when_resample_cannot_reach_the_export_fps(slow):
    rec = _slow_recipe()
    view = recipe.preview(rec, "lerobot_v21", fps=10, timing="resample")
    warning = _codes(view)["source_fps_below_export"]
    assert warning["episodes"] == 2 and warning["sources"] == ["online_rl/sfe/pi"]
    assert warning["min_source_fps"] == 9.5 and warning["suggested_fps"] == 9
    assert warning["suggested_timing"] == "retime"
    # Advisory in the preview (the plan and export decide), but flagged as
    # the case that would certainly be refused.
    assert warning["blocking"] is False and warning["refused"] is True
    assert "retime_time_scale" not in _codes(view)
    # The suggested fix, or a lower rate, clears it; so does retime.
    assert not _codes(recipe.preview(rec, "lerobot_v21", fps=9, timing="resample"))
    assert "source_fps_below_export" not in _codes(
        recipe.preview(rec, "lerobot_v21", fps=10, timing="retime")
    )
    # The format's default timing is judged when none is given.
    assert "source_fps_below_export" in _codes(
        recipe.preview(rec, "lerobot_v21", fps=10)
    )
    assert "source_fps_below_export" not in _codes(
        recipe.preview(rec, "recap_value", fps=10)
    )
    # Without an export fps, or for a raw capture copy, nothing to judge.
    assert not {"source_fps_below_export", "retime_time_scale"} & set(
        _codes(recipe.preview(rec, "lerobot_v21"))
    )
    assert not {"source_fps_below_export", "retime_time_scale"} & set(
        _codes(recipe.preview(rec, "raw_capture", fps=10, timing="retime"))
    )


def test_preview_notes_the_time_scale_of_retime(slow):
    view = recipe.preview(_slow_recipe(), "lerobot_v21", fps=10, timing="retime")
    note = _codes(view)["retime_time_scale"]
    assert note["level"] == "info" and note["blocking"] is False
    assert note["max_deviation_percent"] == 5.0 and note["direction"] == "shorter"
    assert note["episodes"] == 2  # only the 9.5 fps source is off by over 2%
    assert (note["min_source_fps"], note["max_source_fps"]) == (9.5, 10.0)
    # Within 2%: no note.
    only_fast = Recipe(
        name="fast", categories=["rollout"], sources=["direct/models/pi"]
    )
    assert not recipe.preview(only_fast, "lerobot_v21", fps=10, timing="retime")[
        "warnings"
    ]
    # Declared below the recorded rate, the axis grows instead.
    slower = _codes(recipe.preview(_slow_recipe(), "recap_value", fps=9.5))
    assert slower["retime_time_scale"]["direction"] == "longer"
    assert slower["retime_time_scale"]["max_deviation_percent"] == 5.3


def test_api_preview_and_export_take_timing(slow, client):
    body = {"recipe": _slow_recipe().model_dump(), "format": "lerobot_v21", "fps": 10}
    view = client.post("/api/levi/pool/preview", json=body).json()
    assert "source_fps_below_export" in _codes(view)
    view = client.post("/api/levi/pool/preview", json={**body, "timing": "retime"})
    assert "retime_time_scale" in _codes(view.json())
    bad = client.post("/api/levi/pool/preview", json={**body, "timing": "stretch"})
    assert bad.status_code == 422
    bad = client.post("/api/levi/pool/preview", json={**body, "fps": 0})
    assert bad.status_code == 422
    options = {
        "format": "lerobot_v21",
        "name": "api-t",
        "output_dir": str(slow["tmp"] / "exports"),
        "fps": 10,
    }
    dry = client.post(
        "/api/levi/pool/export",
        json={"recipe": body["recipe"], "options": options, "dry_run": True},
    ).json()
    assert dry["options"]["timing"] == "resample"
    # The dry run plans it (the export itself refuses) and carries the note.
    assert "source_fps_below_export" in {w["code"] for w in dry["warnings"]}
    dry = client.post(
        "/api/levi/pool/export",
        json={
            "recipe": body["recipe"],
            "options": {**options, "timing": "retime"},
            "dry_run": True,
        },
    ).json()
    assert dry["options"]["timing"] == "retime"
    assert "retime_time_scale" in {w["code"] for w in dry["warnings"]}
    raw = client.post(
        "/api/levi/pool/export",
        json={
            "recipe": body["recipe"],
            "options": {**options, "format": "raw_capture", "timing": "retime"},
            "dry_run": True,
        },
    ).json()
    assert raw["options"]["timing"] is None
    assert not {w["code"] for w in raw["warnings"]} & {
        "source_fps_below_export",
        "retime_time_scale",
    }
    refused = client.post(
        "/api/levi/pool/export",
        json={
            "recipe": body["recipe"],
            "options": {**options, "timing": "stretch"},
            "dry_run": True,
        },
    )
    assert refused.status_code == 422


def test_resample_below_the_export_fps_is_still_refused_before_converting(slow):
    options = export.ExportOptions(
        format="lerobot_v21",
        name="refused",
        output_dir=str(slow["tmp"] / "exports"),
        filter_static=False,
    )
    job = jobs.plan_export(_slow_recipe(), options)
    with pytest.raises(ValueError, match="lower the export fps to 9") as caught:
        jobs.execute(job)
    assert "timing retime" in str(caught.value)
    assert not (slow["tmp"] / "exports/refused").exists()
    assert not list((slow["tmp"] / "exports").glob(".*partial"))


def test_export_records_the_timing_and_each_episodes_time_scale(slow):
    def run(name, **kw):
        options = export.ExportOptions(
            name=name,
            output_dir=str(slow["tmp"] / "exports"),
            filter_static=False,
            **kw,
        )
        jobs.execute(jobs.plan_export(_slow_recipe(), options))
        out = slow["tmp"] / "exports" / name
        return out, json.loads((out / "pool_export.json").read_text())

    out, record = run("retimed", format="lerobot_v21", timing="retime")
    assert record["params"]["timing"] == "retime"
    assert record["conversion"]["timing"] == "retime"
    info = json.loads((out / "meta/info.json").read_text())
    assert info["fps"] == 10 and info["total_frames"] == 80
    scales = {
        e["source"]: (e["source_fps"], e["time_scale"]) for e in record["episodes"]
    }
    assert scales == {
        "direct/models/pi": (10.0, 1.0),
        "online_rl/sfe/pi": (9.5, 0.95),
    }
    assert all(e["frames"] == 20 for e in record["episodes"])
    assert record["timing"]["mode"] == "retime" and record["timing"]["export_fps"] == 10
    assert (record["timing"]["source_fps_min"], record["timing"]["source_fps_max"]) == (
        9.5,
        10.0,
    )
    assert record["timing"]["time_scale_min"] == 0.95
    assert record["timing"]["episodes_off_by_over_2_percent"] == 2
    assert any(
        w["code"] == "retime_time_scale"
        for w in record["warnings"]
        if isinstance(w, dict)
    )
    table = pq.read_table(out / "data/chunk-000/episode_000000.parquet")
    assert table.column("timestamp").to_pylist()[-1] == pytest.approx(1.9)
    # Resample at a rate every source reaches: the axis is true (scale 1).
    _, record = run("resampled", format="lerobot_v21", fps=9)
    assert (
        record["timing"]["mode"] == "resample" and record["timing"]["export_fps"] == 9
    )
    assert {e["time_scale"] for e in record["episodes"]} == {1.0}
    assert {e["source_fps"] for e in record["episodes"]} == {9.5, 10.0}
    # RECAP defaults to retime and records it too.
    _, record = run("recap", format="recap_value", human_as_success=True)
    assert record["timing"]["mode"] == "retime"
    # A raw capture copy has no time axis.
    _, record = run("rawcopy", format="raw_capture", timing="retime")
    assert record["timing"] is None and record["params"]["timing"] is None
    assert all("time_scale" not in e for e in record["episodes"])


def test_cli_timing_option_reaches_the_export_and_the_preview(slow, capsys):
    from levi.pool import cli

    recipe.save(_slow_recipe())
    base = ["export", "slow", "--name", "cli-t", "--dry-run"]
    base += ["--output-dir", str(slow["tmp"] / "exports")]
    assert cli.main([*base, "--format", "lerobot_v21"]) == 0
    assert json.loads(capsys.readouterr().out)["options"]["timing"] == "resample"
    assert cli.main([*base, "--format", "lerobot_v21", "--timing", "retime"]) == 0
    assert json.loads(capsys.readouterr().out)["options"]["timing"] == "retime"
    assert cli.main([*base, "--format", "raw_capture", "--timing", "retime"]) == 0
    assert json.loads(capsys.readouterr().out)["options"]["timing"] is None
    with pytest.raises(SystemExit):
        cli.main([*base, "--format", "lerobot_v21", "--timing", "stretch"])
    capsys.readouterr()
    show = ["recipe", "show", "slow", "--format", "lerobot_v21", "--fps", "10"]
    assert cli.main(show) == 0
    assert "source_fps_below_export" in capsys.readouterr().out
    assert cli.main([*show, "--timing", "retime"]) == 0
    out = capsys.readouterr().out
    assert "retime_time_scale" in out and "source_fps_below_export" not in out


def test_an_index_without_measured_rates_gives_no_timing_notes(slow):
    frame = pd.read_parquet(scanner.index_path()).drop(columns=["measured_fps"])
    frame.to_parquet(scanner.index_path(), index=False)
    for timing in ("resample", "retime"):
        view = recipe.preview(_slow_recipe(), "lerobot_v21", fps=10, timing=timing)
        assert not {"source_fps_below_export", "retime_time_scale"} & set(_codes(view))
