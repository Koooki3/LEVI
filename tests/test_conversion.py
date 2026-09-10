import hashlib
import json
import subprocess
from pathlib import Path
import numpy as np
import pandas as pd
import pytest
from levi.conversion.options import Options
from levi.conversion import raw, media
from levi.conversion.dataset import validate, repair
from levi.conversion.engine import execute


@pytest.fixture
def capture(tmp_path):
    root = tmp_path / "capture"
    demo = root / "task/demo_001"
    demo.mkdir(parents=True)
    n = 30
    t = np.arange(n) / 10
    pd.DataFrame(
        {
            "timestamp_sec": t,
            "frame_index": np.arange(n),
            "success_flag": 1,
            "source_stamp_sec": t,
            "px": t * 0.003,
            "py": 0.0,
            "pz": 0.0,
            "qx": 0.0,
            "qy": 0.0,
            "qz": 0.0,
            "qw": 1.0,
        }
    ).to_csv(demo / "end_effector_pose.csv", index=False)
    pd.DataFrame(
        {
            "timestamp_sec": t,
            "frame_index": np.arange(n),
            "success_flag": 1,
            "source_stamp_sec": t,
            "finger_left": 0.01,
            "finger_right": 0.01,
            "gripper_width": 0.02,
            "last_gripper_command": ["open"] * 15 + ["close"] * 15,
        }
    ).to_csv(demo / "gripper_state.csv", index=False)
    (demo.parent / "task_description.txt").write_text("Move the gripper")
    for camera in Options().cameras:
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=64x48:rate=10:duration=3",
                "-c:v",
                "libx264",
                "-threads",
                "1",
                "-pix_fmt",
                "yuv420p",
                str(demo / (camera + ".mp4")),
            ],
            check=True,
        )
    return root


def hashes(root):
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def test_full_pipeline_real_media_and_provenance(capture, tmp_path):
    before = hashes(capture)
    result = execute("pipeline", capture, tmp_path / "result", Options())
    assert result["ok"] and result["validation"]["decoded_videos"] == 2
    target = Path(result["dataset_path"])
    frame = pd.read_parquet(next((target / "data").rglob("*.parquet")))
    assert np.allclose(frame.timestamp, np.arange(len(frame)) / 10)
    state = np.stack(frame["observation.state"])
    action = np.stack(frame.action)
    assert np.allclose(action[:-1], state[1:]) and np.allclose(action[-1], state[-1])
    info = json.loads((target / "meta/info.json").read_text())
    assert info["features"]["observation.images.hand"]["shape"] == [48, 64, 3]
    stored = json.loads((target / "meta/stats.json").read_text())[
        "observation.images.hand"
    ]
    actual = media.inspect(next((target / "videos").rglob("*.mp4")), pixels=True)[
        "stats"
    ]
    assert np.allclose(stored["mean"], actual["mean"])
    ledger = json.loads(
        (target / "meta/levi_provenance.jsonl").read_text().splitlines()[0]
    )
    assert ledger["source_demo"] == "task/demo_001"
    assert ledger["source_frame_ids"][0] == 0 and ledger["source_frame_ids"][-1] == 29
    assert hashes(capture) == before
    # Every raw read-only stage produces a real report without an output dataset.
    for stage in [
        "summary",
        "frozen",
        "stage-preview",
        "static",
        "filter-preview",
        "fps-preview",
    ]:
        r = execute(stage, capture, tmp_path / ("check-" + stage), Options())
        assert r["ok"], r
    assert execute("validate", target, tmp_path / "checked", Options())["ok"]


def test_no_silent_row_join_or_unknown_commands(capture):
    demo = next(capture.rglob("demo_*"))
    path = demo / "gripper_state.csv"
    frame = pd.read_csv(path)
    frame.loc[1, "frame_index"] = 0
    frame.to_csv(path, index=False)
    with pytest.raises(ValueError, match="unique"):
        raw.load(demo)
    frame.loc[1, "frame_index"] = 1
    frame.loc[0, "last_gripper_command"] = "invalid"
    frame.to_csv(path, index=False)
    with pytest.raises(ValueError, match="Unknown gripper"):
        raw.load(demo)


def test_geodesic_static_filter_preserves_slow_motion_and_grasps():
    xyz = np.zeros((30, 3))
    xyz[:, 0] = np.arange(30) * 0.001
    q = np.zeros((30, 4))
    q[:, 3] = 1
    q[::2] *= -1
    grip = np.ones(30)
    grip[15:] = 0
    keep = raw.keep_positions(xyz, q, grip, Options())
    assert len(keep) > 6 and len(keep) < 30
    assert set(range(13, 18)) <= set(keep) and keep[-1] == 29
    q = np.array(
        [[0, 0, np.sin(a / 2), np.cos(a / 2)] for a in [np.pi - 0.01, -np.pi + 0.01]]
    )
    assert abs(raw.euler(q)[1, 2] - raw.euler(q)[0, 2]) < 0.03


def test_video_mismatch_rejected_before_writing(capture, tmp_path):
    demo = next(capture.rglob("demo_*"))
    for name in ["end_effector_pose.csv", "gripper_state.csv"]:
        path = demo / name
        pd.read_csv(path).iloc[:-1].to_csv(path, index=False)
    with pytest.raises(ValueError, match="preflight"):
        execute("convert", capture, tmp_path / "bad", Options())
    assert not (tmp_path / "bad").exists()


def test_images_and_fps_resample_all_rows(capture, tmp_path):
    import cv2

    demo = next(capture.rglob("demo_*"))
    for camera in Options().cameras:
        folder = demo / camera
        folder.mkdir()
        for i, frame in enumerate(media.decode(demo / (camera + ".mp4"))):
            cv2.imwrite(str(folder / f"frame_{i}.png"), frame)
    opts = Options(source_fps=10, fps=5)
    out = tmp_path / "images"
    assert execute("images", capture, out, opts)["ok"]
    new = out / "task/demo_001"
    pose, *_ = raw.load(new)
    assert len(pose) == 15 and np.allclose(pose.timestamp_sec, np.arange(15) / 5)
    assert media.inspect(new / "wrist_camera.mp4")["frames"] == 15
    assert json.loads((new / "levi_frames.json").read_text())[
        "source_frame_ids"
    ] == list(range(0, 30, 2))
    assert execute("fps", out, tmp_path / "fps", Options(fps=5))["ok"]


def test_repairs_preserve_source_and_require_task_mapping(capture, tmp_path):
    target = tmp_path / "dataset"
    execute("convert", capture, target, Options())
    before = hashes(target)
    opts = Options(task_map={"Move the gripper": "移动夹爪"})
    assert repair(target, tmp_path / "unused", opts, "tasks-preview")["updates"]
    repair(target, tmp_path / "translated", opts, "tasks")
    assert "移动夹爪" in (tmp_path / "translated/meta/tasks.jsonl").read_text()
    repair(target, tmp_path / "timestamps", Options(), "timestamps")
    assert validate(tmp_path / "timestamps")["ok"]
    assert hashes(target) == before
    path = target / "meta/tasks.jsonl"
    rows = [{"task_index": 0, "task": "unknown_slug"}]
    path.write_text(json.dumps(rows[0]) + "\n")
    assert not repair(target, tmp_path / "preview", Options(), "tasks-preview")["ok"]
    with pytest.raises(ValueError, match="Unmapped"):
        repair(target, tmp_path / "unmapped", Options(), "tasks")
    assert not (tmp_path / "unmapped").exists()


def test_paths_options_and_failed_preflight(capture, tmp_path):
    with pytest.raises(ValueError):
        Options(cameras={"../camera": "observation.images.a"})
    with pytest.raises(ValueError):
        Options(xyz_threshold=float("nan"))
    with pytest.raises(ValueError):
        execute("stage", capture, capture / "nested", Options())
    with pytest.raises(ValueError):
        execute(
            "stage",
            capture,
            tmp_path / "excluded",
            Options(exclude_demos=["not_a_demo"]),
        )
    (capture / "link").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symlinks"):
        execute("summary", capture, tmp_path / "read", Options())


def test_validation_detects_metadata_lies(capture, tmp_path):
    target = tmp_path / "dataset"
    execute("convert", capture, target, Options())
    path = target / "meta/info.json"
    info = json.loads(path.read_text())
    info["features"]["observation.images.hand"]["shape"] = [480, 640, 3]
    path.write_text(json.dumps(info))
    report = validate(target)
    assert not report["ok"] and any("resolution" in x for x in report["failures"])


def test_builtin_web_job_and_changed_source(client, capture):
    import time

    body = {
        "stage": "pipeline",
        "source": str(capture),
        "fps": 10,
        "options": {"orientation": "quaternion", "action_mode": "state"},
    }
    plan = client.post("/api/levi/jobs/plan", json=body)
    assert plan.status_code == 200, plan.text
    job = plan.json()
    assert job["engine"] == "levi.builtin.v1" and "levi.conversion" in job["argv"]
    assert (
        client.post("/api/levi/jobs/" + job["id"] + "/run", json={}).status_code == 200
    )
    for _ in range(120):
        item = next(
            j for j in client.get("/api/levi/jobs").json() if j["id"] == job["id"]
        )
        if item["status"] not in ("queued", "running"):
            break
        time.sleep(0.1)
    assert item["status"] == "succeeded", item
    assert item["dataset"].startswith("local/") and item["exit_code"] == 0
    frame = pd.read_parquet(next((Path(item["output"]) / "data").rglob("*.parquet")))
    assert len(frame.action.iloc[0]) == 8 and np.allclose(
        np.stack(frame.action), np.stack(frame["observation.state"])
    )
    assert (
        client.post("/api/levi/jobs/" + job["id"] + "/run", json={}).status_code == 400
    )
    planned = client.post("/api/levi/jobs/plan", json=body).json()
    (capture / "changed.txt").write_text("capture changed")
    client.post("/api/levi/jobs/" + planned["id"] + "/run", json={})
    for _ in range(30):
        changed = next(
            j for j in client.get("/api/levi/jobs").json() if j["id"] == planned["id"]
        )
        if changed["status"] == "failed":
            break
        time.sleep(0.05)
    assert "changed" in changed["error"]


def test_cache_cleanup_allowlist_preserves_durable_data(tmp_path, monkeypatch):
    from levi import maintenance

    project = tmp_path / "project"
    root = tmp_path / "data"
    cache = project / ".pytest_cache"
    cache.mkdir(parents=True)
    (cache / "file").write_text("cache")
    durable = root / "datasets"
    durable.mkdir(parents=True)
    (durable / "file").write_text("data")
    private = project / ".env"
    private.write_text("local config")
    link = project / ".ruff_cache"
    link.symlink_to(durable, target_is_directory=True)
    external = tmp_path / "unrelated" / "cache"
    external.mkdir(parents=True)
    (external / "keep").write_text("keep")
    (project / ".next").symlink_to(external.parent, target_is_directory=True)
    original = maintenance.candidates
    monkeypatch.setattr(maintenance, "candidates", lambda: original(project, root))
    monkeypatch.setattr(maintenance, "STATE", tmp_path / "state")
    protected = root / ".cache/levi/registered"
    protected.mkdir(parents=True)
    (protected / "keep").write_text("registered dataset")
    state = tmp_path / "state"
    state.mkdir()
    (state / "datasets.json").write_text(json.dumps({"id": {"path": str(protected)}}))
    assert maintenance.clean(False)["bytes"] > 0 and cache.exists()
    maintenance.clean(True)
    assert (
        not cache.exists()
        and (durable / "file").exists()
        and private.exists()
        and link.is_symlink()
    )
