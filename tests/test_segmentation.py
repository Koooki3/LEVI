"""CPU tests of fast instance segmentation (no Torch, no GPU).

The ``fake`` provider runs the real worker module (``integrations/
segmentation``) with LEVI's own Python: the same plans, processes, stdin/
stdout protocol, sidecar rows and publishing as the student, with discs in
place of model output.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pytest

CAMERAS = ("observation.images.front", "observation.images.wrist")


@pytest.fixture
def video_dataset(dataset):
    """The shared fixture dataset plus two 20-frame cameras per episode."""
    import cv2

    info = json.loads((dataset / "meta/info.json").read_text())
    for camera in CAMERAS:
        info["features"][camera] = {
            "dtype": "video",
            "shape": [48, 64, 3],
            "names": ["height", "width", "channels"],
        }
        for episode in range(2):
            path = dataset / f"videos/chunk-000/{camera}/episode_{episode:06d}.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            writer = cv2.VideoWriter(
                str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 48)
            )
            for frame in range(20):
                image = np.full((48, 64, 3), (frame * 10) % 255, np.uint8)
                writer.write(image)
            writer.release()
    (dataset / "meta/info.json").write_text(json.dumps(info))
    return dataset


@pytest.fixture
def seg(client, monkeypatch, tmp_path):
    from levi import paths
    from levi.segmentation import jobs, live

    monkeypatch.setattr(paths, "STATE", tmp_path / "outputs")
    monkeypatch.setenv("LEVI_SEG_MODEL_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("LEVI_SEG_LIVE_IDLE_SECONDS", "30")
    yield client
    live.stop_all(5)
    assert not jobs.wait_idle(60), "a segmentation watch thread outlived its test"


def _repo(client, dataset) -> str:
    return client.post("/api/levi/catalog", json={"path": str(dataset)}).json()["id"]


def _wait_job(client, repo, job_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(
            f"/annotations/api/segmentation/jobs/{job_id}", params={"repo_id": repo}
        )
        assert job.status_code == 200, job.text
        value = job.json()
        if value["status"] in {"succeeded", "failed", "cancelled"}:
            return value
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish: {value}")


def _objects(client, repo, episode):
    response = client.get(
        f"/annotations/api/sam3/episodes/{episode}/objects", params={"repo_id": repo}
    )
    assert response.status_code == 200, response.text
    return response.json()["objects"]


def _sam3_fake(client, repo, episode):
    payload = {
        "repo_id": repo,
        "episode_indices": [episode],
        "camera_keys": ["observation.images.front"],
        "prompts": ["cup"],
        "max_frames": 3,
        "provider": "fake",
    }
    response = client.post("/annotations/api/sam3/run", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------- sidecar


def test_sam3_run_on_one_episode_keeps_other_episodes_objects(seg, dataset):
    """Regression: publishing a run replaced the whole sidecar, so labelling
    episode 1 erased episode 0's objects."""
    repo = _repo(seg, dataset)
    _sam3_fake(seg, repo, 0)
    first = _objects(seg, repo, 0)
    assert first
    _sam3_fake(seg, repo, 1)
    assert _objects(seg, repo, 1)
    assert _objects(seg, repo, 0) == first
    # A re-run of episode 0 replaces its rows instead of adding to them.
    _sam3_fake(seg, repo, 0)
    assert len(_objects(seg, repo, 0)) == len(first)


def test_publish_merged_replaces_only_named_pairs(tmp_path):
    from levi.annotations import ObjectAnnotation, SidecarStore, encode_rle

    def row(episode, camera, frame=0, track=0):
        return ObjectAnnotation(
            episode_index=episode,
            frame_index=frame,
            timestamp=frame / 10,
            camera_key=camera,
            object_id=f"cup-{episode}-{camera}-{track}",
            track_id=track,
            concept="cup",
            bbox_xyxy=[1, 0, 3, 2],
            image_size=[2, 3],
            mask_rle=encode_rle([[False, True, False], [False, True, True]]),
            score=0.9,
            source="student",
        )

    store = SidecarStore(tmp_path / "sidecar")
    store.publish_merged([row(0, "a"), row(0, "b"), row(1, "a")], set())
    # An empty result for (0, "a") clears it; (0, "b") and episode 1 stay.
    info = store.publish_merged([], {(0, "a")})
    assert info["annotation_count"] == 2
    assert {r["camera_key"] for r in store.read_episode(0)} == {"b"}
    assert len(store.read_episode(1)) == 1
    tracks = {(t["episode_index"], t["camera_key"]) for t in _tracks(store)}
    assert tracks == {(0, "b"), (1, "a")}


def _tracks(store):
    import pyarrow.parquet as pq

    return pq.read_table(
        store.revision_path(store.current_revision()) / "tracks.parquet"
    ).to_pylist()


# ---------------------------------------------------------------- jobs


def test_status_lists_cameras_and_reports_missing_worker(
    seg, video_dataset, monkeypatch
):
    from levi.segmentation import jobs

    monkeypatch.setenv("LEVI_SEG_WORKER_PYTHON", "/nonexistent/python")
    repo = _repo(seg, video_dataset)
    status = seg.get("/annotations/api/segmentation/status", params={"repo_id": repo})
    assert status.status_code == 200, status.text
    value = status.json()
    assert value["cameras"] == list(CAMERAS)
    assert value["worker"]["ready"] is False and "setup.sh" in value["worker"]["reason"]
    assert value["models"] == [] and value["jobs"] == [] and value["live"] == []
    assert jobs.worker_state()["ready"] is False
    refused = seg.post(
        "/annotations/api/segmentation/label",
        json={"repo_id": repo, "model": "missing"},
    )
    assert refused.status_code == 404, refused.text


def test_fake_label_job_publishes_every_episode_and_camera(seg, video_dataset):
    repo = _repo(seg, video_dataset)
    _sam3_fake(seg, repo, 0)
    started = seg.post(
        "/annotations/api/segmentation/label",
        json={"repo_id": repo, "provider": "fake", "episodes": [1]},
    )
    assert started.status_code == 202, started.text
    job = _wait_job(seg, repo, started.json()["id"])
    assert job["status"] == "succeeded", job
    assert job["annotation_count"] == 2 * 20 * 2  # 2 discs x 20 frames x 2 cameras
    rows = _objects(seg, repo, 1)
    assert {r["camera_key"] for r in rows} == set(CAMERAS)
    assert {r["source"] for r in rows} == {"fake"}
    # Episode 0's SAM3 objects survive a labelling job on episode 1.
    assert {r["source"] for r in _objects(seg, repo, 0)} == {"fake"}
    assert _objects(seg, repo, 0)[0]["prompt"] == "cup"
    status = seg.get(
        "/annotations/api/segmentation/status", params={"repo_id": repo}
    ).json()
    assert [j["id"] for j in status["jobs"]] == [job["id"]]


def test_label_job_cancel_stops_worker(seg, video_dataset, monkeypatch):
    from levi.segmentation import jobs

    repo = _repo(seg, video_dataset)
    real_start = jobs.start

    def slow_start(*args, **kwargs):
        plan = args[3]
        plan["model"]["fake_delay_ms"] = 400
        return real_start(*args, **kwargs)

    monkeypatch.setattr(jobs, "start", slow_start)
    started = seg.post(
        "/annotations/api/segmentation/label",
        json={"repo_id": repo, "provider": "fake"},
    )
    assert started.status_code == 202, started.text
    job_id = started.json()["id"]
    second = seg.post(
        "/annotations/api/segmentation/label",
        json={"repo_id": repo, "provider": "fake"},
    )
    assert second.status_code == 409, second.text
    cancelled = seg.post(
        f"/annotations/api/segmentation/jobs/{job_id}/cancel", params={"repo_id": repo}
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert _objects(seg, repo, 0) == []


def test_fake_distil_registers_model_with_manifest(seg, video_dataset):
    repo = _repo(seg, video_dataset)
    request = {
        "repo_id": repo,
        "name": "plates-test",
        "concepts": ["pink plate", "human hand", "robot arm"],
        "provider": "fake",
        "sources": [{"train": [0], "test": [1]}],
        "epochs": 1,
    }
    started = seg.post("/annotations/api/segmentation/distil", json=request)
    assert started.status_code == 202, started.text
    job = _wait_job(seg, repo, started.json()["id"])
    assert job["status"] == "succeeded", job
    assert job["model"] == "plates-test"
    listed = seg.get(
        "/annotations/api/segmentation/models", params={"repo_id": repo}
    ).json()["models"]
    assert [m["name"] for m in listed] == ["plates-test"]
    model = listed[0]
    assert model["concepts"] == request["concepts"]
    assert model["licence"]["model"] == "Apache-2.0"
    assert "SAM License" in model["licence"]["teacher"]
    assert model["for_this_dataset"] is True
    manifest = json.loads((Path(model["path"]) / "manifest.json").read_text())
    assert manifest["datasets"][0]["train"] == [0] and manifest["datasets"][0][
        "test"
    ] == [1]
    again = seg.post("/annotations/api/segmentation/distil", json=request)
    assert again.status_code == 409, again.text
    overlap = seg.post(
        "/annotations/api/segmentation/distil",
        json={**request, "name": "other", "sources": [{"train": [0], "test": [0]}]},
    )
    assert overlap.status_code == 400, overlap.text
    deleted = seg.delete("/annotations/api/segmentation/models/plates-test")
    assert deleted.status_code == 200 and deleted.json()["models"] == []


# ---------------------------------------------------------------- live


def _events(client, repo, session_id, after=0, want=("result",), limit=200, until=None):
    """Read SSE frames until every kind in ``want`` was seen (and ``until``
    holds for a result, when given)."""
    seen: dict[str, list[dict]] = {}
    with client.stream(
        "GET",
        f"/annotations/api/segmentation/live/{session_id}/events",
        params={"repo_id": repo, "after": after},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        kind = None
        for count, line in enumerate(response.iter_lines()):
            if line.startswith("event: "):
                kind = line[7:]
            elif line.startswith("data: ") and kind:
                value = json.loads(line[6:])
                if kind == "result" and until is not None and not until(value):
                    continue
                seen.setdefault(kind, []).append(value)
                if all(k in seen for k in want) or kind == "closed":
                    break
            if count > limit * 4:
                break
    return seen


def test_live_session_follows_clock_and_saves_only_its_episode(seg, video_dataset):
    repo = _repo(seg, video_dataset)
    _sam3_fake(seg, repo, 1)
    before = _objects(seg, repo, 1)
    started = seg.post(
        "/annotations/api/segmentation/live",
        json={"repo_id": repo, "episode_index": 0, "provider": "fake"},
    )
    assert started.status_code == 201, started.text
    session = started.json()
    assert session["state"] == "running" and session["cameras"] == list(CAMERAS)
    sid = session["id"]
    clock = seg.post(
        f"/annotations/api/segmentation/live/{sid}/clock",
        params={"repo_id": repo},
        json={"playing": False, "time": 0.55, "rate": 1},
    )
    assert clock.status_code == 200 and clock.json()["ok"] is True
    # The paused player at 0.55 s shows frame 5 (a result for frame 0 may
    # come first, from before the clock arrived).
    seen = _events(
        seg, repo, sid, want=("result",), until=lambda r: r["frame_index"] == 5
    )
    result = seen["result"][-1]
    assert result["camera_key"] in CAMERAS
    assert result["frame_index"] == 5
    concepts = {o["concept"] for o in result["objects"]}
    assert concepts == {"robot arm", "cup"}
    assert all(o["mask_rle"]["size"] == [48, 64] for o in result["objects"])
    # Play at 3x for a moment: frames advance, ids stay stable.
    seg.post(
        f"/annotations/api/segmentation/live/{sid}/clock",
        params={"repo_id": repo},
        json={"playing": True, "time": 0.0, "rate": 3},
    )
    time.sleep(0.5)
    stopped = seg.post(
        f"/annotations/api/segmentation/live/{sid}/stop", params={"repo_id": repo}
    )
    assert stopped.status_code == 200, stopped.text
    value = stopped.json()
    assert value["state"] == "stopped", value
    assert value["revision_id"] and value["annotation_count"] > 0
    rows = _objects(seg, repo, 0)
    assert rows and {r["source"] for r in rows} == {"fake"}
    assert len({r["track_id"] for r in rows if r["concept"] == "robot arm"}) == 1
    # The other episode's objects are untouched.
    assert _objects(seg, repo, 1) == before
    late = seg.post(
        f"/annotations/api/segmentation/live/{sid}/clock",
        params={"repo_id": repo},
        json={"playing": True, "time": 0.0},
    )
    assert late.status_code == 409


def test_live_stream_drops_stale_results_for_slow_readers():
    """A reader that fell behind gets only the newest result per camera."""
    from levi.segmentation.live import LiveSession

    session = LiveSession(
        "x",
        "d",
        {
            "episode_index": 0,
            "cameras": [{"camera_key": "a"}, {"camera_key": "b"}],
            "model": {},
        },
        Path("."),
        "fake",
    )
    for frame in range(5):
        for camera in ("a", "b"):
            session._push(
                "result",
                camera,
                json.dumps({"camera_key": camera, "frame_index": frame}),
            )
    session._push("stats", None, json.dumps({"type": "stats"}))
    session.stopped_at = time.time()
    frames = [
        chunk for chunk in session.stream(0, keepalive=0.01) if chunk.startswith("id:")
    ]
    results = [
        json.loads(f.split("data: ", 1)[1]) for f in frames if "event: result" in f
    ]
    assert sorted((r["camera_key"], r["frame_index"]) for r in results) == [
        ("a", 4),
        ("b", 4),
    ]
    assert any("event: stats" in f for f in frames)


def test_live_without_model_is_refused(seg, video_dataset):
    repo = _repo(seg, video_dataset)
    response = seg.post(
        "/annotations/api/segmentation/live",
        json={"repo_id": repo, "episode_index": 0, "provider": "student"},
    )
    assert response.status_code in (400, 503), response.text
    unknown = seg.post(
        "/annotations/api/segmentation/live",
        json={"repo_id": repo, "episode_index": 7, "provider": "fake"},
    )
    assert unknown.status_code == 400, unknown.text


def test_worker_clock_follows_each_camera_and_removes_transport_delay(monkeypatch):
    """Cameras of one player drift apart (more at high speed), and a proxy
    can delay a clock message by hundreds of milliseconds: each decoder
    follows its own camera's time, measured when the player read it."""
    import sys

    sys.path.insert(
        0, str(Path(__file__).resolve().parents[1] / "integrations" / "segmentation")
    )
    from levi_seg_worker.live import Clock

    clock = Clock()
    now = time.time()
    clock.set(
        playing=True,
        position=2.0,
        rate=3.0,
        sent_at=now - 0.5,
        cameras={"a": 2.0, "b": 1.5},
    )
    at = time.monotonic()
    # Half a second in transit at 3x: the player is 1.5 s further on.
    assert clock.position(at, "a") == pytest.approx(3.5, abs=0.05)
    assert clock.position(at, "b") == pytest.approx(3.0, abs=0.05)
    assert clock.position(at) == pytest.approx(3.5, abs=0.05)
    # A timestamp from another machine's clock (future or stale) is ignored.
    clock.set(playing=False, position=1.0, sent_at=now + 30, cameras={"a": 1.2})
    assert clock.position(None, "a") == pytest.approx(1.2)
    assert clock.position(None, "unknown") == pytest.approx(1.0)


# ---------------------------------------------------------------- review fixes


def _dataset_key(repo):
    from backend import app

    return app._ensure_state(app.DatasetRef(repo_id=repo)).display_slug


def _count_bundle_copies(monkeypatch, repo):
    """Move the dataset onto the annotation bundle store (where every write
    transaction copies the whole bundle) and count those copies."""
    from backend import app
    from levi.agent.store import Store

    name = _dataset_key(repo)
    store = Store(app.STATE)
    base, revision, _ = store.prepare(name)
    store.publish(name, base, revision, "seg-test", "seg-test", {"revision": revision})
    calls: list[str] = []
    real = Store.prepare

    def counted(self, dataset, kind=None):
        calls.append(dataset)
        return real(self, dataset, kind)

    monkeypatch.setattr(Store, "prepare", counted)
    return calls


def test_polling_is_read_only_until_a_result_waits(seg, video_dataset, monkeypatch):
    """Regression: every 15 s status poll opened a write transaction, which
    copied and hashed the whole annotation bundle under the dataset lock."""
    from levi.segmentation import jobs

    repo = _repo(seg, video_dataset)
    copies = _count_bundle_copies(monkeypatch, repo)
    for _ in range(3):
        assert (
            seg.get(
                "/annotations/api/segmentation/status", params={"repo_id": repo}
            ).status_code
            == 200
        )
    assert copies == []
    started = seg.post(
        "/annotations/api/segmentation/label",
        json={"repo_id": repo, "provider": "fake", "episodes": [1]},
    )
    assert started.status_code == 202, started.text
    job_id = started.json()["id"]
    record = jobs.find(_dataset_key(repo), job_id)
    deadline = time.time() + 60
    while not Path(record["result_path"]).is_file() and time.time() < deadline:
        time.sleep(0.1)
    assert copies == []
    # The first poll that sees the finished job publishes it, once.
    status = seg.get(
        "/annotations/api/segmentation/status", params={"repo_id": repo}
    ).json()
    assert [j["status"] for j in status["jobs"]] == ["succeeded"]
    assert len(copies) == 1
    seg.get("/annotations/api/segmentation/status", params={"repo_id": repo})
    job = seg.get(
        f"/annotations/api/segmentation/jobs/{job_id}", params={"repo_id": repo}
    ).json()
    assert job["status"] == "succeeded" and len(copies) == 1
    assert {r["camera_key"] for r in _objects(seg, repo, 1)} == set(CAMERAS)


def test_failed_sam3_pair_keeps_its_objects(seg, dataset):
    """Regression: a SAM3 re-run whose (episode, camera) pair failed cleared
    that pair's existing (possibly reviewed) objects."""
    from backend import app
    from levi.annotations import Sam3Plan
    from levi.annotations.sam3_protocol import fake_annotations

    camera = "observation.images.front"
    repo = _repo(seg, dataset)
    _sam3_fake(seg, repo, 0)
    _sam3_fake(seg, repo, 1)
    kept = _objects(seg, repo, 1)
    assert kept
    state = app._ensure_state(app.DatasetRef(repo_id=repo))
    staging = app._sidecar(state).staging_root / "jobs"
    staging.mkdir(parents=True, exist_ok=True)
    plan = {
        "episode_indices": [0, 1],
        "camera_keys": [camera],
        "prompts": ["plate"],
        "max_frames": 3,
    }
    only_first = Sam3Plan.model_validate(
        {**plan, "episode_indices": [0], "provider": "fake"}
    )
    (staging / "plan.json").write_text(json.dumps(plan))
    (staging / "result.json").write_text(
        json.dumps(
            {
                "status": "succeeded",
                "annotations": [
                    row.model_dump(mode="json") for row in fake_annotations(only_first)
                ],
                "item_errors": [
                    {"episode_index": 1, "camera_key": camera, "error": "decode failed"}
                ],
            }
        )
    )
    job_id = "a" * 16
    (staging / f"{job_id}.json").write_text(
        json.dumps(
            {
                "job_id": job_id,
                "status": "running",
                "plan_path": str(staging / "plan.json"),
                "result_path": str(staging / "result.json"),
            }
        )
    )
    job = seg.get(f"/annotations/api/sam3/jobs/{job_id}", params={"repo_id": repo})
    assert job.status_code == 200, job.text
    assert job.json()["status"] == "succeeded", job.json()
    assert {r["prompt"] for r in _objects(seg, repo, 0)} == {"plate"}
    assert _objects(seg, repo, 1) == kept


def _v3_multi_file(root: Path) -> tuple[dict, Path]:
    """A v3 dataset whose episode 3 lives in data file 0 but video file 1:
    both video files exist, so the wrong indices find the wrong video."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    camera = "observation.images.front"
    for index in (0, 1):
        path = root / f"videos/{camera}/chunk-000/file-{index:03d}.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    (root / "meta/episodes/chunk-000").mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "episode_index": 3,
                    "length": 20,
                    "data/chunk_index": 0,
                    "data/file_index": 0,
                    f"videos/{camera}/chunk_index": 0,
                    f"videos/{camera}/file_index": 1,
                    f"videos/{camera}/from_timestamp": 2.0,
                    f"videos/{camera}/to_timestamp": 4.0,
                }
            ]
        ),
        root / "meta/episodes/chunk-000/file-000.parquet",
    )
    info = {
        "codebase_version": "v3.0",
        "fps": 10,
        "chunks_size": 1000,
        "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
        "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
    }
    return info, root / f"videos/{camera}/chunk-000/file-001.mp4"


def test_v3_video_uses_the_cameras_own_file_indices(tmp_path):
    """Regression: the v3 video path took the data file's chunk/file indices."""
    import sys

    from levi.segmentation import media

    info, expected = _v3_multi_file(tmp_path)
    video = media.resolve(tmp_path, info, 3, "observation.images.front")
    assert video.video == expected
    assert video.start_frame == 20 and video.length == 20
    sys.path.insert(
        0, str(Path(__file__).resolve().parents[1] / "integrations" / "sam3")
    )
    try:
        from levi_sam3_worker.worker import _episode_video
    finally:
        sys.path.pop(0)
    assert _episode_video(tmp_path, info, 3, "observation.images.front") == (
        expected,
        2.0,
    )


def test_service_shutdown_discards_unsaved_live_rows(seg, video_dataset):
    """Regression: rows a live worker wrote at service shutdown were never
    published (sessions live only in memory) and stayed on disk."""
    from levi.segmentation import jobs, live

    repo = _repo(seg, video_dataset)
    started = seg.post(
        "/annotations/api/segmentation/live",
        json={"repo_id": repo, "episode_index": 0, "provider": "fake"},
    )
    assert started.status_code == 201, started.text
    sid = started.json()["id"]
    seg.post(
        f"/annotations/api/segmentation/live/{sid}/clock",
        params={"repo_id": repo},
        json={"playing": False, "time": 0.55, "rate": 1},
    )
    _events(seg, repo, sid, want=("result",), until=lambda r: r["frame_index"] == 5)
    live.stop_all(5)
    session = live.get(sid)
    assert session.stopped_at is not None
    assert not (session.folder / "rows").exists()
    status = seg.get(
        "/annotations/api/segmentation/status", params={"repo_id": repo}
    ).json()
    assert [s["revision_id"] for s in status["live"] if s["id"] == sid] == [None]
    assert _objects(seg, repo, 0) == []
    # A killed service's leftovers are removed by the next start's sweep.
    orphan = jobs.root(_dataset_key(repo)) / "live" / "20260101-0000" / "rows"
    orphan.mkdir(parents=True)
    (orphan / "episode-000000.parquet").write_bytes(b"x")
    live.discard_orphans()
    assert not orphan.exists()


def _manifest(directory: Path, **fields):
    from levi.segmentation import models

    directory.mkdir(parents=True, exist_ok=True)
    (directory / "weights.pth").write_bytes(b"w")
    manifest = {
        "schema": models.SCHEMA,
        "provider": "fake",
        "concepts": ["cup"],
        **fields,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest))


def test_manifest_weights_must_be_a_plain_file_name(tmp_path, monkeypatch):
    from levi.segmentation import models

    monkeypatch.setenv("LEVI_SEG_MODEL_DIR", str(tmp_path / "models"))
    (tmp_path / "outside.pth").write_bytes(b"w")
    _manifest(tmp_path / "models" / "escape", weights="../../outside.pth")
    _manifest(tmp_path / "models" / "plain", weights="weights.pth")
    assert [m["name"] for m in models.listing()] == ["plain"]
    with pytest.raises(ValueError, match="plain file name"):
        models.worker_spec("escape")
    assert models.worker_spec("plain")["weights"] == str(
        tmp_path / "models" / "plain" / "weights.pth"
    )


def test_segmentation_status_capability_is_scoped(tmp_path, monkeypatch):
    """A scoped agent sees neither workspace paths nor the names of datasets
    outside its scope; a person sees both."""
    import sys

    from levi.agent.capabilities import invoke
    from levi.agent.runtime import Workbench
    from levi.agent.security import Principal
    from levi.catalog import display_name

    monkeypatch.setenv("LEVI_SEG_MODEL_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("LEVI_SEG_WORKER_PYTHON", sys.executable)
    mine = display_name("org/mine", None)
    _manifest(
        tmp_path / "models" / "shared",
        datasets=[{"name": mine}, {"name": "someone-elses-data"}],
    )
    wb = Workbench(tmp_path / "state")
    agent = invoke(
        wb,
        Principal("agent", datasets=("org/mine",)),
        "segmentation.status",
        {"repo_id": "org/mine"},
    )
    text = json.dumps(agent)
    assert str(tmp_path) not in text and "someone-elses-data" not in text
    assert agent["models"][0]["datasets"] == [mine]
    assert agent["worker"]["ready"] is True
    with pytest.raises(PermissionError):
        invoke(
            wb,
            Principal("agent", datasets=("org/mine",)),
            "segmentation.status",
            {"repo_id": "org/other"},
        )
    person = invoke(
        wb,
        Principal("human", human=True),
        "segmentation.status",
        {"repo_id": "org/mine"},
    )
    assert person["models"][0]["path"] == str(tmp_path / "models" / "shared")
    assert "someone-elses-data" in person["models"][0]["datasets"]


def test_segmentation_routes_require_the_ui_token(seg, video_dataset, monkeypatch):
    repo = _repo(seg, video_dataset)
    monkeypatch.setenv("LEVI_UI_TOKEN", "test-ui-token")
    monkeypatch.setenv("LEVI_AGENT_TOKEN", "test-scoped-token")
    ui = {"x-levi-ui-token": "test-ui-token"}
    agent = {"Authorization": "Bearer test-scoped-token"}
    routes = [
        ("GET", "/annotations/api/segmentation/status", None),
        ("GET", "/annotations/api/segmentation/models", None),
        ("DELETE", "/annotations/api/segmentation/models/anything", None),
        (
            "POST",
            "/annotations/api/segmentation/label",
            {"repo_id": repo, "provider": "fake"},
        ),
        ("POST", "/annotations/api/segmentation/distil", {"repo_id": repo}),
        ("GET", "/annotations/api/segmentation/jobs/20260101-0000", None),
        ("POST", "/annotations/api/segmentation/jobs/20260101-0000/cancel", None),
        (
            "POST",
            "/annotations/api/segmentation/live",
            {"repo_id": repo, "episode_index": 0, "provider": "fake"},
        ),
        ("GET", "/annotations/api/segmentation/live/x", None),
        (
            "POST",
            "/annotations/api/segmentation/live/x/clock",
            {"playing": False, "time": 0},
        ),
        ("GET", "/annotations/api/segmentation/live/x/events", None),
        ("POST", "/annotations/api/segmentation/live/x/stop", None),
    ]
    for method, url, body in routes:
        params = {"repo_id": repo}
        assert seg.request(method, url, params=params, json=body).status_code == 401, (
            url
        )
        assert (
            seg.request(
                method, url, params=params, json=body, headers=agent
            ).status_code
            == 403
        ), url
    assert (
        seg.get(
            "/annotations/api/segmentation/status", params={"repo_id": repo}, headers=ui
        ).status_code
        == 200
    )
