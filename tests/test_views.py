"""Raw-capture browsing views and annotation carry-over into conversions."""

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
from test_formats import capture_fixture, make_demo

from levi import catalog
from levi.annotations.rle import encode_rle
from levi.annotations.schema import ObjectAnnotation
from levi.annotations.sidecar import SidecarStore
from levi.conversion.dataset import validate
from levi.conversion.media import frame_interval


def wait(client, job_id, limit=600):
    for _ in range(limit):
        item = next(j for j in client.get("/api/levi/jobs").json() if j["id"] == job_id)
        if item["status"] not in ("planned", "queued", "running"):
            return item
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def register_raw(client, root):
    entry = client.post("/api/levi/catalog", json={"path": str(root)}).json()
    assert entry["kind"] == "raw" and entry["view_status"] == "building"
    job = wait(client, entry["view_job"])
    assert job["status"] == "succeeded", job
    return catalog.datasets()[entry["name"]]


def test_raw_capture_view_is_browsable_and_lossless(client, tmp_path):
    root = capture_fixture(tmp_path / "screws", fps=(9.5, 9.5))
    entry = register_raw(client, root)
    view = Path(entry["view"])
    assert entry["view_status"] == "ready" and view.name == entry["name"]
    assert entry["path"] == str(root.resolve())
    info = json.loads((view / "meta/info.json").read_text())
    assert info["fps"] == 9.5 and info["total_frames"] == 30 + 34  # every frame
    meta = json.loads((view / "meta/levi_view.json").read_text())
    assert (
        meta["input_format"] == "robot_capture" and meta["variant"] == "policy_rollout"
    )
    assert validate(view)["ok"]
    video = next((view / "videos").rglob("*.mp4"))
    assert abs(frame_interval(video) - 1 / 9.5) < 1e-4
    # Served under the raw dataset's own id; export is refused.
    url = f"/api/levi/files/{entry['name']}/meta/info.json"
    assert client.get(url).status_code == 200
    refused = client.post("/annotations/api/export", json={"repo_id": entry["id"]})
    assert refused.status_code == 409
    # Registering again is idempotent while the capture is unchanged.
    again = client.post("/api/levi/catalog", json={"path": str(root)}).json()
    assert again["view"] == entry["view"] and again["view_status"] == "ready"


def test_annotations_on_raw_view_carry_into_the_conversion(client, tmp_path):
    root = capture_fixture(tmp_path / "screws")
    entry = register_raw(client, root)
    repo, name = entry["id"], entry["name"]
    fps = 10.0

    # Language atoms on episode 1 (demo_0001): a persistent span and an event.
    atoms = [
        {
            "role": "user",
            "content": "grasp",
            "style": "subtask",
            "timestamp": 1.2,
            "to": 2.0,
        },
        {"role": "assistant", "content": "done?", "style": "vqa", "timestamp": 3.0},
    ]
    saved = client.post(
        "/annotations/api/episodes/1/atoms",
        json={"repo_id": repo, "episode_index": 1, "atoms": atoms},
    )
    assert saved.status_code == 200, saved.text
    # Human outcome relabel of episode 1 (metadata says failure).
    client.post(
        "/annotations/api/episodes/1/outcome",
        json={"repo_id": repo, "outcome": "success"},
    )
    # SAM3 masks on episode 1, frames 0..3 of the hand camera.
    mask = [[0] * 64 for _ in range(48)]
    mask[10][10] = 1
    rows = [
        ObjectAnnotation(
            episode_index=1,
            frame_index=f,
            timestamp=f / fps,
            camera_key="observation.images.hand",
            object_id="screw-1",
            track_id=0,
            concept="screw",
            bbox_xyxy=[10, 10, 11, 11],
            image_size=[48, 64],
            mask_rle=encode_rle(mask),
            score=0.95,
        )
        for f in range(4)
    ]
    SidecarStore(catalog.STATE / "object_annotations" / name).publish(rows)

    # Convert with the default resample + static filter: frames are dropped.
    plan = client.post(
        "/api/levi/jobs/plan", json={"stage": "pipeline", "source": str(root)}
    ).json()
    assert plan["options"]["outcome_labels"] == {"pick_screws/demo_0001": "success"}
    client.post(f"/api/levi/jobs/{plan['id']}/run", json={})
    job = wait(client, plan["id"])
    assert job["status"] == "succeeded", job
    out = Path(job["output"])
    report = job["carryover"]
    assert report["matched_episodes"] == 2
    assert report["language"]["atoms"] == 2 and report["outcomes"]["labels"] == 1
    assert (out / "meta/levi_annotation_carryover.json").is_file()

    new_name = job["dataset"].split("/", 1)[1]
    moved = json.loads(
        (catalog.STATE / "annotations" / new_name / "episode_000001.json").read_text()
    )["atoms"]
    ledger = [
        json.loads(x)
        for x in (out / "meta/levi_provenance.jsonl").read_text().splitlines()
    ][1]
    positions = np.array(ledger["source_positions"])
    out_fps = json.loads((out / "meta/info.json").read_text())["fps"]
    for before, after in zip(atoms, moved, strict=True):
        j = round(after["timestamp"] * out_fps)
        assert abs(after["timestamp"] - j / out_fps) < 1e-9  # on a frame
        raw_row = round(before["timestamp"] * fps)
        assert abs(positions[j] - raw_row) == np.abs(positions - raw_row).min()
    # Outcome label, SAM3 masks only on exactly retained frames.
    labels = client.get(
        "/annotations/api/episodes/outcomes", params={"repo_id": job["dataset"]}
    ).json()["labels"]
    assert labels["1"]["outcome"] == "success"
    episodes = [
        json.loads(x) for x in (out / "meta/episodes.jsonl").read_text().splitlines()
    ]
    assert episodes[1]["levi_outcome"] == "success"
    assert episodes[1]["levi_outcome_source"] == "human"
    kept = sorted(set(range(4)) & set(positions.tolist()))
    carried = SidecarStore(
        catalog.STATE / "object_annotations" / new_name
    ).read_annotations()
    assert sorted(r["frame_index"] for r in carried) == [
        int(np.flatnonzero(positions == f)[0]) for f in kept
    ]
    assert report["sam3"]["masks"] + report["sam3"]["dropped_frames"] == 4


def test_view_rebuild_rekeys_annotations_by_demo(client, tmp_path):
    root = capture_fixture(tmp_path / "screws")
    entry = register_raw(client, root)
    client.post(
        "/annotations/api/episodes/1/outcome",
        json={"repo_id": entry["id"], "outcome": "success"},
    )
    # A new demo sorts first: demo_0001 becomes episode 2.
    make_demo(root / "pick_screws/demo_0000a", n=20, outcome="failure")
    (root / "pick_screws/demo_0000a").rename(root / "pick_screws/demo_00000")
    loaded = client.post("/annotations/api/dataset/load", json={"repo_id": entry["id"]})
    assert loaded.json()["num_episodes"] == 2
    rebuilt = client.post("/api/levi/catalog", json={"path": str(root)}).json()
    assert wait(client, rebuilt["view_job"])["status"] == "succeeded"
    entry = catalog.datasets()[entry["name"]]
    rows = [
        json.loads(x)
        for x in (Path(entry["view"]) / "meta/episodes.jsonl").read_text().splitlines()
    ]
    assert [r["source_demo"] for r in rows][2] == "pick_screws/demo_0001"
    labels = client.get(
        "/annotations/api/episodes/outcomes", params={"repo_id": entry["id"]}
    ).json()["labels"]
    assert list(labels) == ["2"]
    # The annotation backend notices the rebuilt view (no stale cache).
    loaded = client.post("/annotations/api/dataset/load", json={"repo_id": entry["id"]})
    assert loaded.json()["num_episodes"] == 3


def test_broken_demo_is_left_out_of_the_view_not_fatal(client, tmp_path):
    root = capture_fixture(tmp_path / "screws")
    demo = root / "pick_screws/demo_0001"
    pose = pd.read_csv(demo / "end_effector_pose.csv").iloc[:-3]
    pose.to_csv(demo / "end_effector_pose.csv", index=False)  # rows ≠ video frames
    entry = register_raw(client, root)
    meta = json.loads((Path(entry["view"]) / "meta/levi_view.json").read_text())
    assert list(meta["excluded"]) == ["pick_screws/demo_0001"]
    assert entry["info"]["total_episodes"] == 1


def test_catalog_describes_format_origin_and_capabilities(client, dataset, tmp_path):
    root = capture_fixture(tmp_path / "screws")
    raw = register_raw(client, root)
    # The view decodes nothing: no pixel statistics, still a valid dataset.
    stats = json.loads((Path(raw["view"]) / "meta/stats.json").read_text())
    assert "observation.images.hand" not in stats
    client.post("/api/levi/catalog", json={"path": str(dataset)})
    plan = client.post(
        "/api/levi/jobs/plan",
        json={
            "stage": "pipeline",
            "source": str(root),
            "options": {"target": "recap_value"},
        },
    ).json()
    client.post(f"/api/levi/jobs/{plan['id']}/run", json={})
    assert wait(client, plan["id"])["status"] == "succeeded"

    local = {
        d["name"]: d["format"] for d in client.get("/api/levi/catalog").json()["local"]
    }
    view = local[raw["name"]]
    assert view["kind"] == "raw" and view["origin"] == "raw_capture"
    assert view["variant"] == "policy_rollout" and view["view_fps"] == 10
    assert not view["capabilities"]["export_annotated"]
    assert view["capabilities"]["annotate"]
    assert local[dataset.name]["origin"] == "external"
    assert local[dataset.name]["capabilities"]["export_annotated"]
    recap = next(f for n, f in local.items() if n.startswith("screws_recap_"))
    assert recap["origin"] == "levi_recap" and recap["timing"] == "retime"
    assert recap["recap"]["outcomes"] == {"success": 1, "failure": 1}
    assert recap["input_format"] == "robot_capture"
