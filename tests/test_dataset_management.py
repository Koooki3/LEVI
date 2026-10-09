"""Physical episode removal uses temporary data, never the product workspace."""

import json
from pathlib import Path

import pytest

from levi import catalog, paths
from levi import dataset_management as management


@pytest.fixture
def registered(tmp_path, dataset, monkeypatch):
    state = tmp_path / "outputs/LEVI/workbench"
    state.mkdir(parents=True)
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(catalog, "STATE", state)
    monkeypatch.setenv("LEVI_LINK_WORKSPACES", "")
    monkeypatch.delenv("LEVI_LIVE_WORKSPACE", raising=False)
    monkeypatch.delenv("LEVI_POOL_HELDOUT", raising=False)
    item = catalog.register(str(dataset))
    return tmp_path, state, dataset, item["name"]


def test_delete_keeps_sparse_ids_and_surviving_files(registered):
    root, state, dataset, name = registered
    annotation = state / "annotations" / name / "episode_000000.json"
    annotation.parent.mkdir(parents=True)
    annotation.write_text('{"annotations":[]}')
    plan = management.deletion_plan(name, [0])
    assert plan["files"] == 2
    result = management.delete_episodes(name, [0], plan["confirmation"])
    assert result["remaining"] == 1
    assert result["first_episode_index"] == 1
    assert not (dataset / "data/chunk-000/episode_000000.parquet").exists()
    assert (dataset / "data/chunk-000/episode_000001.parquet").is_file()
    assert not annotation.exists()
    assert management.episodes(name)["indices"] == [1]
    info = json.loads((dataset / "meta/info.json").read_text())
    assert (
        info["total_episodes"],
        info["total_frames"],
        info["levi_episode_indices"],
    ) == (1, 20, [1])
    assert catalog.datasets()[name]["info"]["total_episodes"] == 1
    assert not list(root.rglob(".levi-delete-*"))


def test_preview_changed_metadata_refuses_without_deletion(registered):
    _, _, dataset, name = registered
    plan = management.deletion_plan(name, [0])
    (dataset / "meta/episodes.jsonl").write_text(
        (dataset / "meta/episodes.jsonl").read_text() + "\n"
    )
    with pytest.raises(management.ManagementError, match="changed after the preview"):
        management.delete_episodes(name, [0], plan["confirmation"])
    assert (dataset / "data/chunk-000/episode_000000.parquet").exists()


def test_rollback_restores_files_and_metadata(registered, monkeypatch):
    root, _, dataset, name = registered
    plan = management.deletion_plan(name, [0])
    before = (dataset / "meta/info.json").read_bytes()
    original = Path.write_bytes

    def fail_once(path, content):
        if path == dataset / "meta/info.json":
            raise OSError("test disk failure")
        return original(path, content)

    monkeypatch.setattr(Path, "write_bytes", fail_once)
    with pytest.raises(OSError, match="disk failure"):
        management.delete_episodes(name, [0], plan["confirmation"])
    assert (dataset / "data/chunk-000/episode_000000.parquet").exists()
    assert (dataset / "meta/info.json").read_bytes() == before
    assert management.episodes(name)["indices"] == [0, 1]
    assert not list(root.rglob(".levi-delete-*"))


def test_running_conversion_refused_even_if_started_after_preview(registered):
    _, state, dataset, name = registered
    plan = management.deletion_plan(name, [0])
    job = state / "jobs/j.json"
    job.parent.mkdir(parents=True)
    job.write_text(json.dumps({"status": "running", "source": str(dataset)}))
    with pytest.raises(management.Busy):
        management.delete_episodes(name, [0], plan["confirmation"])
    assert management.episodes(name)["indices"] == [0, 1]


def test_build_lock_and_invalid_selection_refused(registered):
    root, _, _, name = registered
    with management.lock_for(root, name), pytest.raises(management.Busy):
        management.deletion_plan(name, [0])
    for invalid in ([], [True], [-1], ["0"]):
        with pytest.raises(management.ManagementError):
            management.deletion_plan(name, invalid)


def test_symlink_media_never_deleted(registered, tmp_path):
    _, _, dataset, name = registered
    video = dataset / "data/chunk-000/episode_000000.parquet"
    outside = tmp_path / "untouched.parquet"
    video.rename(outside)
    video.symlink_to(outside)
    with pytest.raises(management.ManagementError, match="symbolic links"):
        management.deletion_plan(name, [0])
    assert outside.is_file()


def test_frozen_paths_protected(registered, monkeypatch):
    root, _, dataset, name = registered
    manifest = root / "heldout.json"
    manifest.write_text(
        json.dumps(
            {
                "episodes": [
                    {"path": str(dataset / "data/chunk-000/episode_000000.parquet")}
                ]
            }
        )
    )
    monkeypatch.setenv("LEVI_POOL_HELDOUT", str(manifest))
    with pytest.raises(management.ManagementError, match="Frozen test data"):
        management.deletion_plan(name, [0])


def test_v3_keeps_files_shared_with_surviving_episode(registered):
    import pyarrow as pa
    import pyarrow.parquet as pq

    _, _state, dataset, name = registered
    info_path = dataset / "meta/info.json"
    info = json.loads(info_path.read_text())
    info.update(
        codebase_version="v3.0",
        data_path="data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
    )
    info_path.write_text(json.dumps(info))
    (dataset / "meta/episodes/chunk-000").mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "episode_index": ep,
                    "length": 20,
                    "data/chunk_index": 0,
                    "data/file_index": 0,
                    "dataset_from_index": ep * 20,
                    "dataset_to_index": (ep + 1) * 20,
                }
                for ep in (0, 1)
            ]
        ),
        dataset / "meta/episodes/chunk-000/file-000.parquet",
    )
    shared = dataset / "data/chunk-000/file-000.parquet"
    pq.write_table(pa.table({"episode_index": [0, 1], "index": [0, 20]}), shared)
    plan = management.deletion_plan(name, [0])
    assert plan["shared_files_retained"] == 1
    management.delete_episodes(name, [0], plan["confirmation"])
    assert management.episodes(name)["indices"] == [1]
    assert shared.is_file()
    plan = management.deletion_plan(name, [1])
    management.delete_episodes(name, [1], plan["confirmation"])
    assert not shared.exists()
    assert management.episodes(name)["indices"] == []


def test_raw_source_folder_removed_and_view_fingerprint_updated(registered):
    from levi.conversion.engine import fingerprint

    root, state, dataset, name = registered
    capture = root / "capture"
    for ep in (0, 1):
        demo = capture / f"demo_{ep}"
        demo.mkdir(parents=True)
        (demo / "view.mp4").write_bytes(b"fixture video")
        (demo / "metadata.json").write_text("{}")
    rows_path = dataset / "meta/episodes.jsonl"
    rows = [json.loads(line) for line in rows_path.read_text().splitlines()]
    for row in rows:
        row["source_demo"] = f"demo_{row['episode_index']}"
    rows_path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    view_meta = dataset / "meta/levi_view.json"
    view_meta.write_text(json.dumps({"source_fingerprint": fingerprint(capture)}))
    items = catalog.datasets()
    items[name].update(
        kind="raw", path=str(capture), view=str(dataset), view_status="ready"
    )
    catalog.atomic(state / "datasets.json", items)
    plan = management.deletion_plan(name, [0])
    assert plan["source_files_included"]
    management.delete_episodes(name, [0], plan["confirmation"])
    assert not (capture / "demo_0").exists()
    assert (capture / "demo_1/view.mp4").exists()
    assert json.loads(view_meta.read_text())["source_fingerprint"] == fingerprint(
        capture
    )
    assert not list(root.rglob(".levi-delete-*"))


def test_delete_api_is_human_only_and_requires_confirmation(
    client, dataset, tmp_path, monkeypatch
):
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    name = catalog.register(str(dataset))["name"]
    route = f"/api/levi/datasets/{name}/episodes"
    assert (
        client.post(route + "/deletion-plan", json={"episodes": [True]}).status_code
        == 422
    )
    assert client.request("DELETE", route, json={"episodes": [0]}).status_code == 422
    assert client.post(
        route + "/deletion-plan",
        json={"episodes": [0]},
        headers={"authorization": "Bearer agent"},
    ).status_code in {401, 403}
    preview = client.post(route + "/deletion-plan", json={"episodes": [0]})
    assert preview.status_code == 200, preview.text
    result = client.request(
        "DELETE",
        route,
        json={"episodes": [0], "confirmation": preview.json()["confirmation"]},
    )
    assert result.status_code == 200, result.text
    assert client.get(route).json()["indices"] == [1]


@pytest.mark.parametrize("relative", ["meta/episodes.jsonl", "meta/info.json"])
def test_symlink_metadata_refused(registered, relative):
    root, _, dataset, name = registered
    path = dataset / relative
    outside = root / (path.name + ".outside")
    path.rename(outside)
    path.symlink_to(outside)
    before = outside.read_bytes()
    with pytest.raises(management.ManagementError, match="symbolic links"):
        management.deletion_plan(name, [0])
    assert path.is_symlink() and outside.read_bytes() == before


def test_catalog_lock_symlink_refused_before_writing(registered):
    root, state, dataset, name = registered
    plan = management.deletion_plan(name, [0])
    outside = root / "outside.lock"
    outside.write_text("untouched")
    lock = state / ".catalog.lock"
    lock.unlink(missing_ok=True)
    lock.symlink_to(outside)
    with pytest.raises(management.ManagementError, match="symbolic links"):
        management.delete_episodes(name, [0], plan["confirmation"])
    assert outside.read_text() == "untouched"
    assert (dataset / "data/chunk-000/episode_000000.parquet").is_file()


def test_live_segmentation_and_waiting_publish_refused(registered, monkeypatch):
    from types import SimpleNamespace

    from levi.segmentation import live

    _, state, _, name = registered
    monkeypatch.setattr(
        live,
        "sessions",
        lambda dataset: [SimpleNamespace(stopped_at=None)] if dataset == name else [],
    )
    with pytest.raises(management.Busy, match="live segmentation session"):
        management.deletion_plan(name, [0])
    monkeypatch.setattr(live, "sessions", lambda dataset: [])
    folder = state / "segmentation" / name / "live/s-1"
    folder.mkdir(parents=True)
    (folder / "plan.json").write_text("{}")
    (folder / "result.json").write_text("{}")
    (folder / "rows").mkdir()
    (folder / "rows/objects.jsonl").write_text("{}\n")
    with pytest.raises(management.Busy, match="waiting to be published"):
        management.deletion_plan(name, [0])
    (folder / "rows/objects.jsonl").unlink()
    assert management.deletion_plan(name, [0])["confirmation"]


def test_linked_live_deletion_removes_original_and_mirror_without_losing_other_run(
    registered, monkeypatch
):
    import shutil

    from levi import links
    from levi.live import config, jsonio

    root, _product_state, dataset, _ = registered
    owner = root / "live-workspace"
    live_name, catalog_name = "model@checkpoint__task", "model-checkpoint__task"
    capture = owner / "captures" / live_name
    source_root = root / "rollouts"
    source = source_root / "model@checkpoint/task"
    browse = owner / "outputs/LEVI/workbench/views" / catalog_name
    shutil.copytree(dataset, browse)
    row_file = browse / "meta/episodes.jsonl"
    rows = [json.loads(line) for line in row_file.read_text().splitlines()]
    for row in rows:
        demo = f"demo_{row['episode_index']:04d}"
        row["source_demo"] = demo
        for folder in (capture / demo, source / demo):
            folder.mkdir(parents=True)
            (folder / ".complete").write_text("")
            (folder / "metadata.json").write_text('{"stopped_at":"done"}')
            (folder / "view.mp4").write_bytes(b"fixture")
    row_file.write_bytes(management._jsonl_bytes(rows))
    c = config.Config()
    c.service.workspace = str(owner)
    c.watch.roots = [str(source_root)]
    jsonio.write(owner / "live/workspace.json", {"schema": "levi.live.workspace.v1"})
    (owner / "live/effective.toml").write_text(config.render(c))
    live = {
        "name": live_name,
        "root": str(source_root),
        "group": "model@checkpoint",
        "task_folder": "task",
        "source": str(source),
        "capture": str(capture),
        "demos": {
            f"demo_{ep:04d}": {"episode_index": ep, "state": "done", "run_id": f"s{ep}"}
            for ep in (0, 1)
        },
    }
    jsonio.write(owner / "live/datasets" / (live_name + ".json"), live)
    jsonio.write(
        owner / "outputs/LEVI/workbench/datasets.json",
        {
            catalog_name: {
                "id": "local/" + catalog_name,
                "name": catalog_name,
                "kind": "raw",
                "path": str(capture),
                "view": str(browse),
                "view_status": "ready",
            }
        },
    )
    monkeypatch.setattr(links, "workspaces", lambda: [owner])
    alias = "live." + catalog_name
    assert management.episodes(alias, "s1")["indices"] == [1]
    plan = management.deletion_plan(alias, [0])
    management.delete_episodes(alias, [0], plan["confirmation"])
    assert not (source / "demo_0000").exists()
    assert not (capture / "demo_0000").exists()
    assert (source / "demo_0001/view.mp4").exists()
    assert management.episodes(alias, "s1")["indices"] == [1]
    assert jsonio.read(owner / "live/datasets" / (live_name + ".json"))["demos"][
        "demo_0000"
    ]["deleted"]
