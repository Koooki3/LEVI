"""A shared view must never move one namespace's labels onto another demo."""

import contextlib
import json
import sqlite3
from pathlib import Path

import pytest

from levi import catalog, views
from levi.agent.store import Store, resolve


@pytest.fixture
def shared(client, tmp_path):
    root = tmp_path / "capture"
    root.mkdir()
    old = tmp_path / "old-view"
    new = tmp_path / "next-view"
    for folder, rows in (
        (old, [{"episode_index": 0, "source_demo": "demo_a"}]),
        (
            new,
            [
                {"episode_index": 0, "source_demo": "earlier"},
                {"episode_index": 1, "source_demo": "demo_a"},
            ],
        ),
    ):
        (folder / "meta").mkdir(parents=True)
        (folder / "meta/episodes.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows)
        )
        (folder / "meta/info.json").write_text(
            json.dumps({"total_episodes": len(rows), "features": {}})
        )
    base = catalog.add_entry(
        root, {"kind": "raw", "view": str(old), "view_status": "ready"}
    )["name"]
    namespace = catalog.create_namespace(base, "review")["name"]
    for name in (base, namespace):
        labels = catalog.STATE / "annotations" / name
        (labels / "outcomes").mkdir(parents=True)
        (labels / "episode_000000.json").write_text(
            json.dumps({"episode_index": 0, "atoms": [{"content": name}]})
        )
        (labels / "outcomes/episode_000000.json").write_text(
            json.dumps({"episode_index": 0, "outcome": "success"})
        )
    return root, old, new, base, namespace


@pytest.mark.parametrize("versioned", [False, True])
def test_all_namespaces_follow_source_demo_with_real_labels(shared, versioned):
    root, _old, new, base, namespace = shared
    store = Store(catalog.STATE)
    if versioned:
        previous, revision, folder = store.prepare(namespace)
        store.publish(
            namespace, previous, revision, "fixture", "fixture", {"revision": revision}
        )
        immutable = (folder / "annotations/episode_000000.json").read_bytes()
    published = views.publish(root, new)
    for name in (base, namespace):
        labels = resolve(catalog.STATE, name, "annotations")
        assert (
            json.loads((labels / "episode_000001.json").read_text())["atoms"][0][
                "content"
            ]
            == name
        )
        assert (
            json.loads((labels / "outcomes/episode_000001.json").read_text())[
                "episode_index"
            ]
            == 1
        )
        assert not (labels / "episode_000000.json").exists()
    if versioned:
        assert (folder / "annotations/episode_000000.json").read_bytes() == immutable
        assert store.head(namespace) != revision
    assert Path(published["view"]).is_dir()


@pytest.mark.parametrize("versioned", [False, True])
def test_second_namespace_failure_restores_previous_labels_and_heads(
    shared, monkeypatch, versioned
):
    root, old, new, base, namespace = shared
    from levi.annotations import carryover

    store = Store(catalog.STATE)
    if versioned:
        previous, revision, _folder = store.prepare(base)
        store.publish(
            base, previous, revision, "fixture", "fixture", {"revision": revision}
        )
    before_heads = {name: store.head(name) for name in (base, namespace)}
    before = {
        name: (
            resolve(catalog.STATE, name, "annotations") / "episode_000000.json"
        ).read_bytes()
        for name in (base, namespace)
    }
    original = carryover._rekey_view

    def fail(name, old_view, new_view):
        if name == namespace:
            raise OSError("namespace write failed")
        return original(name, old_view, new_view)

    monkeypatch.setattr(carryover, "_rekey_view", fail)
    with pytest.raises(OSError, match="namespace write failed"):
        views.publish(root, new)
    assert old.is_dir() and new.is_dir()
    assert catalog.datasets()[base]["view"] == str(old)
    for name in (base, namespace):
        assert store.head(name) == before_heads[name]
        assert (
            resolve(catalog.STATE, name, "annotations") / "episode_000000.json"
        ).read_bytes() == before[name]
        assert not (
            resolve(catalog.STATE, name, "annotations") / "episode_000001.json"
        ).exists()


@pytest.mark.parametrize("versioned", [False, True])
@pytest.mark.parametrize("failure", ["catalog", "catalog_after_write", "rename"])
def test_publication_failure_restores_catalog_view_and_every_namespace(
    shared, monkeypatch, versioned, failure
):
    root, old, new, base, namespace = shared
    store = Store(catalog.STATE)
    if versioned:
        previous, revision, _folder = store.prepare(namespace)
        store.publish(namespace, previous, revision, "fixture", "fixture", {})
    before_catalog = catalog.datasets()
    before_heads = {name: store.head(name) for name in (base, namespace)}
    original_add = catalog.add_entry
    original_rename = Path.rename

    def fail_add(*args, **kwargs):
        if failure == "catalog_after_write":
            original_add(*args, **kwargs)
        raise OSError("publication failed")

    def fail_rename(path, target):
        if path == new:
            raise OSError("publication failed")
        return original_rename(path, target)

    if failure == "rename":
        monkeypatch.setattr(Path, "rename", fail_rename)
    else:
        monkeypatch.setattr(catalog, "add_entry", fail_add)
    with pytest.raises(OSError, match="publication failed"):
        views.publish(root, new)
    assert old.is_dir() and new.is_dir()
    assert catalog.datasets() == before_catalog
    for name in (base, namespace):
        assert store.head(name) == before_heads[name]
        folder = resolve(catalog.STATE, name, "annotations")
        assert (
            json.loads((folder / "episode_000000.json").read_text())["atoms"][0][
                "content"
            ]
            == name
        )
        assert (folder / "outcomes/episode_000000.json").is_file()
        assert not (folder / "episode_000001.json").exists()
    assert not list(catalog.STATE.glob(".rekey-*"))


def test_failed_file_recovery_preserves_complete_snapshots(shared, monkeypatch):
    root, old, new, base, namespace = shared
    from levi.annotations import carryover

    original_copy = carryover.shutil.copytree

    def fail_copy(source, target, *args, **kwargs):
        source = Path(source)
        if source.parent.name.startswith(".rekey-") and source.name == namespace:
            raise OSError("restore disk failure")
        return original_copy(source, target, *args, **kwargs)

    def fail_publish(*_args, **_kwargs):
        raise OSError("publication failed")

    monkeypatch.setattr(carryover.shutil, "copytree", fail_copy)
    monkeypatch.setattr(catalog, "add_entry", fail_publish)
    with pytest.raises(RuntimeError, match="backups retained at") as error:
        views.publish(root, new)
    (scratch,) = catalog.STATE.glob(".rekey-*")
    assert str(scratch) in str(error.value)
    assert old.is_dir() and new.is_dir()
    for name in (base, namespace):
        assert (scratch / name / "episode_000000.json").is_file()
        assert (scratch / name / "outcomes/episode_000000.json").is_file()
    assert (catalog.STATE / "annotations" / base / "episode_000000.json").is_file()


def test_database_recovery_failure_still_restores_legacy_and_keeps_journal(
    shared, monkeypatch
):
    root, old, new, base, namespace = shared
    from levi.annotations import carryover

    store = Store(catalog.STATE)
    previous, revision, _folder = store.prepare(base)
    store.publish(base, previous, revision, "fixture", "fixture", {})
    original_rekey = carryover._rekey_view
    original_connect = Store.connect
    recovering = False

    def fail_rekey(name, old_view, new_view):
        nonlocal recovering
        if name == namespace:
            recovering = True
            raise OSError("namespace failed")
        return original_rekey(name, old_view, new_view)

    @contextlib.contextmanager
    def fail_connect(instance):
        nonlocal recovering
        if recovering:
            recovering = False
            raise sqlite3.OperationalError("database is locked")
        with original_connect(instance) as db:
            yield db

    monkeypatch.setattr(carryover, "_rekey_view", fail_rekey)
    monkeypatch.setattr(Store, "connect", fail_connect)
    with pytest.raises(RuntimeError, match="backups retained at"):
        views.publish(root, new)
    (scratch,) = catalog.STATE.glob(".rekey-*")
    journal = json.loads((scratch / "recovery.json").read_text())
    assert journal["heads"][base] == revision
    assert (scratch / namespace / "episode_000000.json").is_file()
    assert (catalog.STATE / "annotations" / namespace / "episode_000000.json").is_file()
    assert store.head(base) != revision
    assert old.is_dir() and new.is_dir()
