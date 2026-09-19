"""Runtime sync: datasets changing, appearing and disappearing on disk."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_formats import capture_fixture, make_demo
from test_views import wait

from levi import catalog
from levi.sync import Synchronizer


@pytest.fixture
def sync(client, tmp_path, monkeypatch):
    monkeypatch.setenv("LEVI_SYNC_SETTLE", "0")
    return Synchronizer(root=tmp_path)


def _append_episode(root: Path):
    info_path = root / "meta/info.json"
    info = json.loads(info_path.read_text())
    info["total_episodes"] += 1
    info_path.write_text(json.dumps(info))
    with (root / "meta/episodes.jsonl").open("a") as handle:
        handle.write(
            json.dumps(
                {"episode_index": 2, "length": 20, "tasks": ["Move the gripper"]}
            )
            + "\n"
        )


def test_in_place_changes_refresh_catalog_viewer_and_backend(client, dataset, sync):
    repo = client.post("/api/levi/catalog", json={"path": str(dataset)}).json()["id"]
    sync.scan()  # records the first revision silently
    before = client.get(f"/api/levi/catalog/{dataset.name}").json()
    loaded = client.post("/annotations/api/dataset/load", json={"repo_id": repo}).json()
    assert loaded["num_episodes"] == 2

    _append_episode(dataset)
    after = client.get(f"/api/levi/catalog/{dataset.name}").json()
    assert after["revision"] != before["revision"]  # live, even before a scan
    assert after["format"]["episodes"] == 3
    changes = sync.scan()
    assert [c["kind"] for c in changes] == ["updated"]
    assert catalog.datasets()[dataset.name]["info"]["total_episodes"] == 3
    loaded = client.post("/annotations/api/dataset/load", json={"repo_id": repo}).json()
    assert loaded["num_episodes"] == 3
    assert sync.scan() == []  # nothing new


def test_removed_dataset_is_dropped_and_reattaches_its_annotations(
    client, dataset, sync, tmp_path
):
    repo = client.post("/api/levi/catalog", json={"path": str(dataset)}).json()["id"]
    atoms = [{"role": "user", "content": "grasp", "style": "subtask", "timestamp": 0.5}]
    client.post(
        "/annotations/api/episodes/0/atoms",
        json={"repo_id": repo, "episode_index": 0, "atoms": atoms},
    )
    parked = tmp_path / ".parked"
    shutil.move(dataset, parked)
    changes = sync.scan()
    assert [c["kind"] for c in changes] == ["removed"]
    assert dataset.name not in catalog.datasets()
    assert client.get(f"/api/levi/catalog/{dataset.name}").status_code == 404
    assert (
        catalog.STATE / "annotations" / dataset.name / "episode_000000.json"
    ).is_file()

    shutil.move(parked, dataset)  # it comes back: discovered, same name
    changes = sync.scan()
    assert [c["kind"] for c in changes] == ["added"]
    assert catalog.datasets()[dataset.name]["registered_by"] == "sync"
    got = client.get(
        "/annotations/api/episodes/0/atoms", params={"repo_id": repo}
    ).json()
    assert got["atoms"][0]["content"] == "grasp"


def test_discovery_waits_for_a_stable_copy_and_skips_levi_folders(
    client, dataset, tmp_path, monkeypatch
):
    monkeypatch.setenv("LEVI_SYNC_SETTLE", "30")
    sync = Synchronizer(root=tmp_path)
    clock = [1000.0]
    monkeypatch.setattr("levi.sync.time.time", lambda: clock[0])
    for skipped in ("outputs/copy", "tmp/copy", ".hidden/copy", ".x.partial"):
        shutil.copytree(dataset, tmp_path / skipped)
    assert sync.scan() == []  # first sighting of "fixture": wait
    clock[0] += 10
    _append_episode(dataset)  # still being written
    assert sync.scan() == []
    clock[0] += 31
    assert [c["kind"] for c in sync.scan()] == ["added"]
    assert set(catalog.datasets()) == {dataset.name}


def test_raw_capture_changes_rebuild_its_view(client, sync, tmp_path):
    root = capture_fixture(tmp_path / "screws")
    sync.scan()  # discovered → view build starts
    entry = next(iter(catalog.datasets().values()))
    assert entry["kind"] == "raw" and entry["registered_by"] == "sync"
    assert wait(client, entry["view_job"])["status"] == "succeeded"
    client.post(
        "/annotations/api/episodes/1/outcome",
        json={"repo_id": entry["id"], "outcome": "success"},
    )
    assert sync.scan() == []  # up to date

    make_demo(root / "pick_screws/demo_0002", n=20, outcome="failure")
    changes = sync.scan()
    assert [c["kind"] for c in changes] == ["rebuilding"]
    entry = catalog.datasets()[entry["name"]]
    assert sync.scan() == []  # build running: not started twice
    assert wait(client, entry["view_job"])["status"] == "succeeded"
    entry = catalog.datasets()[entry["name"]]
    assert entry["info"]["total_episodes"] == 3
    sync.scan()
    assert (
        client.get(f"/api/levi/catalog/{entry['name']}").json()["format"]["episodes"]
        == 3
    )

    shutil.rmtree(root / "pick_screws/demo_0000")  # demo_0001 becomes episode 0
    sync.scan()
    entry = catalog.datasets()[entry["name"]]
    assert wait(client, entry["view_job"])["status"] == "succeeded"
    labels = client.get(
        "/annotations/api/episodes/outcomes", params={"repo_id": entry["id"]}
    ).json()
    assert list(labels["labels"]) == ["0"]

    shutil.rmtree(root)
    assert [c["kind"] for c in sync.scan()] == ["removed"]
    assert not Path(entry["view"]).exists()  # LEVI's generated view goes too


def test_failed_view_is_retried_only_after_the_capture_changes(client, sync, tmp_path):
    root = capture_fixture(tmp_path / "screws")
    (root / "link").symlink_to(tmp_path)  # conversion inputs refuse symlinks
    assert [c["kind"] for c in sync.scan()] == ["failed"]
    name = next(iter(catalog.datasets()))
    entry = catalog.datasets()[name]
    assert entry["view_status"] == "failed" and "symlink" in entry["view_error"]
    jobs_before = len(list((catalog.STATE / "jobs").glob("*.json")))
    assert sync.scan() == []
    assert len(list((catalog.STATE / "jobs").glob("*.json"))) == jobs_before
    (root / "link").unlink()  # fixed → changed → retried
    assert [c["kind"] for c in sync.scan()] == ["rebuilding"]


def test_catalog_writes_are_safe_across_processes(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    script = (
        "import sys; from pathlib import Path; from levi import catalog\n"
        "for i in range(40):\n"
        "    catalog.add_entry(Path(sys.argv[1]) / f'{sys.argv[2]}_{i}', {'kind': 'lerobot'})\n"
    )
    env = {**os.environ, "LEVI_WORKSPACE": str(workspace)}
    procs = [
        subprocess.Popen([sys.executable, "-c", script, str(workspace), tag], env=env)
        for tag in ("a", "b", "c")
    ]
    assert all(p.wait(timeout=120) == 0 for p in procs)
    items = json.loads((workspace / "outputs/LEVI/workbench/datasets.json").read_text())
    assert len(items) == 120  # no lost update


def test_sync_endpoints(client, dataset, monkeypatch, tmp_path):
    from levi import service

    monkeypatch.setenv("LEVI_SYNC_SETTLE", "0")
    monkeypatch.setattr(service, "SYNC", Synchronizer(root=tmp_path))
    result = client.post("/api/levi/sync").json()
    assert [c["kind"] for c in result["changes"]] == ["added"]
    status = client.get("/api/levi/sync").json()
    assert status["changes"][0]["name"] == dataset.name and status["last_scan"]
    assert client.delete(f"/api/levi/catalog/{dataset.name}").json() == {
        "removed": dataset.name
    }
    assert client.delete(f"/api/levi/catalog/{dataset.name}").status_code == 404


def test_invalid_lerobot_folder_is_reported_once(client, sync, tmp_path):
    bad = tmp_path / "broken"
    (bad / "meta").mkdir(parents=True)
    (bad / "meta/info.json").write_text("{}")
    assert [c["kind"] for c in sync.scan()] == ["skipped"]
    assert sync.scan() == []
    assert catalog.datasets() == {}
