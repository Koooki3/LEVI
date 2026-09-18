"""Hash-free naming: catalog ids, per-run timestamps, per-dataset bare names."""

import json
import shutil
from concurrent.futures import ThreadPoolExecutor

from levi import catalog
from levi.naming import catalog_name, is_timestamp_id, timestamp_id, unique_name


def test_timestamp_ids_are_unique_under_concurrent_reservation(tmp_path):
    with ThreadPoolExecutor(max_workers=16) as pool:
        ids = list(pool.map(lambda _: timestamp_id(tmp_path, ".json"), range(64)))
    assert len(set(ids)) == 64
    assert all(is_timestamp_id(value) for value in ids)
    assert len(list(tmp_path.glob("*.json"))) == 64


def test_catalog_name_is_url_safe_and_never_a_relative_path():
    assert catalog_name("pick screws/v2") == "pick_screws_v2"
    assert catalog_name("..") == "dataset"
    assert catalog_name(".hidden") == "hidden"
    assert catalog_name("螺丝数据") == "dataset"
    assert unique_name("a", {"b"}) == "a"
    clashed = unique_name("a", {"a"})
    assert clashed.startswith("a_") and is_timestamp_id(clashed[2:])


def _make_dataset(path, info):
    (path / "meta").mkdir(parents=True)
    (path / "meta/info.json").write_text(json.dumps(info))
    return path


def test_catalog_ids_are_names_idempotent_and_disambiguated(client, dataset, tmp_path):
    info = json.loads((dataset / "meta/info.json").read_text())
    first = client.post("/api/levi/catalog", json={"path": str(dataset)}).json()
    assert first["id"] == f"local/{dataset.name}"
    # Registering the same path again returns the same entry.
    again = client.post("/api/levi/catalog", json={"path": str(dataset)}).json()
    assert again["id"] == first["id"]

    # Same folder name elsewhere → a timestamp suffix, never a hash.
    twin = _make_dataset(tmp_path / "elsewhere" / dataset.name, info)
    second = client.post("/api/levi/catalog", json={"path": str(twin)}).json()
    assert second["id"] != first["id"]
    assert second["name"].startswith(dataset.name + "_")
    assert is_timestamp_id(second["name"][len(dataset.name) + 1 :])

    # The legacy generic ".../dataset" layout is named after its parent.
    legacy = _make_dataset(tmp_path / "screws_run" / "dataset", info)
    third = client.post("/api/levi/catalog", json={"path": str(legacy)}).json()
    assert third["id"] == "local/screws_run"


def test_legacy_hash_ids_resolve_through_aliases(client, dataset):
    repo = client.post("/api/levi/catalog", json={"path": str(dataset)}).json()["id"]
    catalog.atomic(
        catalog.STATE / "dataset_aliases.json", {"0123456789abcdef": dataset.name}
    )
    assert catalog.canonical_id("local/0123456789abcdef") == repo
    url = "/api/levi/files/0123456789abcdef/meta/info.json"
    assert client.get(url).status_code == 200
    # Reviews written under the old id land in the same name-keyed file.
    client.post(
        "/api/levi/review", json={"repo_id": "local/0123456789abcdef", "flagged": [1]}
    )
    assert client.get("/api/levi/review", params={"repo_id": repo}).json()[
        "flagged"
    ] == [1]
    assert (catalog.STATE / "reviews" / f"{dataset.name}.json").is_file()


def test_export_directory_is_bare_name_and_timestamped_only_on_clash(
    client, dataset, tmp_path, monkeypatch
):
    from backend import app

    repo = client.post("/api/levi/catalog", json={"path": str(dataset)}).json()["id"]
    first = client.post("/annotations/api/export", json={"repo_id": repo}).json()
    assert first["output_dir"].endswith(f"/{dataset.name}_annotated")
    again = client.post("/annotations/api/export", json={"repo_id": repo}).json()
    assert (
        again["output_dir"] == first["output_dir"] and again["reused_existing_export"]
    )

    # A different source that resolves to the same name must not reuse it.
    twin = tmp_path / "twin"
    shutil.copytree(dataset, twin)
    monkeypatch.setattr(app, "dataset_display_slug", lambda *_: dataset.name)
    other = client.post(
        "/annotations/api/export", json={"local_path": str(twin)}
    ).json()
    assert other["output_dir"] != first["output_dir"]
    suffix = other["output_dir"].rsplit(f"{dataset.name}_annotated_", 1)[1]
    assert is_timestamp_id(suffix)


def test_migration_rehomes_rekeys_and_renames_legacy_state(
    tmp_path, monkeypatch, dataset
):
    """Mirrors the shapes found in a real pre-migration workspace."""
    from levi import migrations

    state = tmp_path / "state"
    monkeypatch.setattr(catalog, "STATE", state)
    monkeypatch.setattr(migrations, "EXPORTS", tmp_path / "exports")
    # A plain dataset under a hash key, and a legacy <run>/dataset conversion.
    run = tmp_path / "screws_lerobot"
    shutil.copytree(dataset, run / "dataset")
    for name in ("capture", "filtered"):
        (run / name / "task/demo_0000").mkdir(parents=True)
    (run / "preflight.json").write_text("{}")
    (run / "validation.json").write_text('{"ok": true}')
    items = {
        "bc74bcb96b83c8b4": {
            "id": "local/bc74bcb96b83c8b4",
            "name": "fixture",
            "path": str(dataset),
            "info": {},
        },
        "8f917a8356446445": {
            "id": "local/8f917a8356446445",
            "name": "dataset",
            "path": str(run / "dataset"),
            "info": {},
        },
    }
    catalog.atomic(state / "datasets.json", items)
    catalog.atomic(
        state / "reviews" / ("3f5c" * 16 + ".json"),
        {"repo_id": "local/bc74bcb96b83c8b4", "flagged": [3], "notes": ""},
    )
    catalog.atomic(
        state / "diagnostics" / "fixture_bc74bcb96b.json",
        {"repo_id": "local/bc74bcb96b83c8b4"},
    )
    catalog.atomic(
        state / "jobs" / "20260914-163025_c18e7d72.json",
        {
            "id": "20260914-163025_c18e7d72",
            "status": "failed",
            "argv": ["--result", "jobs/20260914-163025_c18e7d72.result.json"],
        },
    )
    (state / "jobs" / "20260914-163025_c18e7d72.log").write_text("log")
    before = json.loads((state / "datasets.json").read_text())

    dry = migrations.Migration(apply=False).run()
    assert dry and json.loads((state / "datasets.json").read_text()) == before

    migrations.Migration(apply=True).run()
    items = catalog.datasets()
    assert set(items) == {"fixture", "screws_lerobot"}
    assert items["screws_lerobot"]["path"] == str(run)
    assert (run / "meta/info.json").is_file() and not (run / "dataset").exists()
    assert (run / "meta/levi_validation.json").is_file()
    assert (run / "meta/levi_preflight.json").is_file()
    staged = list((state / "jobs").glob("*/intermediate/staged/task/demo_0000"))
    assert len(staged) == 1 and not (run / "capture").exists()
    assert catalog.canonical_id("local/bc74bcb96b83c8b4") == "local/fixture"
    assert catalog.canonical_id("local/8f917a8356446445") == "local/screws_lerobot"
    review = json.loads((state / "reviews" / "fixture.json").read_text())
    assert review == {"repo_id": "local/fixture", "flagged": [3], "notes": ""}
    assert (state / "diagnostics" / "fixture.json").is_file()
    job = json.loads((state / "jobs" / "20260914-163025-000.json").read_text())
    assert job["id"] == "20260914-163025-000"
    assert job["argv"][1] == "jobs/20260914-163025-000.result.json"
    assert (state / "jobs" / "20260914-163025-000.log").is_file()
    # Idempotent.
    assert migrations.Migration(apply=True).run() == []
