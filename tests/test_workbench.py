import json
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from levi.diagnostics import diagnose


def test_local_registration_ranges_and_traversal(client, dataset):
    result = client.post("/api/levi/catalog", json={"path": str(dataset)})
    assert result.status_code == 200, result.text
    slug = result.json()["id"].split("/")[1]
    url = f"/api/levi/files/{slug}/data/chunk-000/episode_000000.parquet"
    assert client.head(url).status_code == 200
    response = client.get(url, headers={"Range": "bytes=0-3"})
    assert response.status_code == 206
    assert response.content == b"PAR1"
    assert (
        client.get(f"/api/levi/files/{slug}/meta/../../outside.json").status_code != 200
    )
    assert client.post("/api/levi/catalog", json={"path": "/etc"}).status_code == 400
    assert (
        client.post(
            "/api/levi/review",
            json={"repo_id": "test/a", "flagged": [1]},
            headers={"Origin": "https://attacker.example"},
        ).status_code
        == 403
    )


def test_review_isolation_and_durable_export(client):
    assert (
        client.post(
            "/api/levi/review",
            json={"repo_id": "test/a", "flagged": [2, 0, 2], "notes": "检查相机"},
        ).status_code
        == 200
    )
    assert (
        client.get("/api/levi/review", params={"repo_id": "test/b"}).json()["flagged"]
        == []
    )
    value = client.get("/api/levi/review/export", params={"repo_id": "test/a"}).json()
    assert value["excluded_episode_ids"] == [0, 2]
    assert value["notes"] == "检查相机"
    assert (
        client.post(
            "/api/levi/review", json={"repo_id": "test/a", "flagged": [-1]}
        ).status_code
        == 400
    )


def test_v2_annotation_snap_export_source_unchanged(client, dataset, tmp_path):
    repo = client.post("/api/levi/catalog", json={"path": str(dataset)}).json()["id"]
    before = (dataset / "data/chunk-000/episode_000000.parquet").read_bytes()
    payload = {
        "repo_id": repo,
        "episode_index": 0,
        "atoms": [
            {
                "role": "user",
                "content": "检查草莓",
                "style": "subtask",
                "timestamp": 0.0,
            },
            {
                "role": "user",
                "content": "hello",
                "style": "interjection",
                "timestamp": 0.141,
            },
        ],
    }
    result = client.post("/annotations/api/episodes/0/atoms", json=payload)
    assert result.status_code == 200, result.text
    atoms = client.get(
        "/annotations/api/episodes/0/atoms", params={"repo_id": repo}
    ).json()["atoms"]
    assert atoms[1]["timestamp"] == 0.1
    export = client.post(
        "/annotations/api/export",
        json={"repo_id": repo, "output_dir": str(tmp_path / "annotated")},
    )
    assert export.status_code == 200, export.text
    frame = pq.read_table(tmp_path / "annotated/data/chunk-000/episode_000000.parquet")
    assert (
        "language_persistent" in frame.column_names
        and "language_events" in frame.column_names
    )
    assert (dataset / "data/chunk-000/episode_000000.parquet").read_bytes() == before
    assert not (dataset / "meta/lerobot_annotations.json").exists()
    # Never allowed to export back into the source tree.
    assert (
        client.post(
            "/annotations/api/export",
            json={"repo_id": repo, "output_dir": str(dataset)},
        ).status_code
        == 409
    )
    # Re-exporting into the SAME directory this export just created is now
    # an intentional reuse-in-place, not a conflict (see _do_export).
    reexport = client.post(
        "/annotations/api/export",
        json={"repo_id": repo, "output_dir": str(tmp_path / "annotated")},
    )
    assert reexport.status_code == 200, reexport.text
    assert reexport.json()["reused_existing_export"] is True
    # A directory that already exists but was never created by a LEVI
    # export is still refused — never silently overwritten.
    foreign = tmp_path / "not-ours"
    foreign.mkdir()
    (foreign / "some_other_file.txt").write_text("not a LEVI export")
    assert (
        client.post(
            "/annotations/api/export",
            json={"repo_id": repo, "output_dir": str(foreign)},
        ).status_code
        == 409
    )


def test_diagnostics_version_aware_and_detects_corruption(dataset):
    good = diagnose(dataset)
    assert good["counts"]["fail"] == 0
    file = dataset / "data/chunk-000/episode_000000.parquet"
    data = pd.read_parquet(file)
    data.loc[3, "timestamp"] = data.loc[2, "timestamp"]
    data["action"] = [[0.0, 0.0]] * len(data)
    data.to_parquet(file)
    bad = diagnose(dataset)
    assert bad["status"] == "fail"
    assert 0 in bad["flagged_episodes"]
    assert any(x["check"] == "action" and x["status"] == "warn" for x in bad["results"])


def test_v3_annotation_metadata(client, dataset, tmp_path):
    info = json.loads((dataset / "meta/info.json").read_text())
    info["codebase_version"] = "v3.1"
    info["data_path"] = "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet"
    (dataset / "meta/info.json").write_text(json.dumps(info))
    (dataset / "meta/episodes/chunk-000").mkdir(parents=True)
    pd.DataFrame(
        {
            "episode_index": [0, 1],
            "data/chunk_index": [0, 0],
            "data/file_index": [0, 0],
            "dataset_from_index": [0, 20],
            "dataset_to_index": [20, 40],
        }
    ).to_parquet(dataset / "meta/episodes/chunk-000/file-000.parquet")
    parts = [pd.read_parquet(p) for p in sorted((dataset / "data").rglob("*.parquet"))]
    for p in (dataset / "data").rglob("*.parquet"):
        p.unlink()
    pd.concat(parts).to_parquet(dataset / "data/chunk-000/file-000.parquet")
    result = client.get(
        "/annotations/api/episodes/1/frame_timestamps",
        params={"local_path": str(dataset)},
    )
    assert result.status_code == 200, result.text
    assert len(result.json()["timestamps"]) == 20


def test_invalid_conversion_rejected(client, dataset):
    assert (
        client.post(
            "/api/levi/jobs/plan",
            json={"stage": "arbitrary shell", "source": str(dataset)},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/levi/jobs/plan", json={"stage": "convert", "source": "/etc"}
        ).status_code
        == 400
    )


def test_dataset_asset_symlink_is_not_an_escape(client, dataset, tmp_path):
    secret = tmp_path / "secret.json"
    secret.write_text('{"private":true}')
    (dataset / "meta/escape.json").symlink_to(secret)
    repo = client.post("/api/levi/catalog", json={"path": str(dataset)}).json()["id"]
    response = client.get(f"/api/levi/files/{repo.split('/')[1]}/meta/escape.json")
    assert response.status_code == 400
    assert "private" not in response.text


def test_missing_episode_is_failure_when_all_requested(dataset):
    (dataset / "data/chunk-000/episode_000001.parquet").unlink()
    report = diagnose(dataset, max_episodes=0)
    assert report["status"] == "fail"
    assert any("Episode total" in row["message"] for row in report["results"])


def test_annotation_rejects_invalid_repo_before_creating_cache(client):
    result = client.post(
        "/annotations/api/dataset/load", json={"repo_id": "owner/name/../../outside"}
    )
    assert result.status_code == 400


def test_export_reuses_deterministic_dir_and_writes_language_mirror(
    client, dataset
):
    """Two "导出标注数据集" clicks on the same dataset (no explicit
    output_dir) must resolve to the same directory, refresh the annotation
    payload, and never touch videos or the marker's created_at a second
    time — the fix for the old "new random directory every click" behavior.
    """
    repo = client.post("/api/levi/catalog", json={"path": str(dataset)}).json()["id"]
    payload = {
        "repo_id": repo,
        "episode_index": 0,
        "atoms": [
            {
                "role": "assistant",
                "content": "grasp the sponge",
                "style": "subtask",
                "timestamp": 0.0,
                "to": 1.2,
            }
        ],
    }
    assert (
        client.post("/annotations/api/episodes/0/atoms", json=payload).status_code
        == 200
    )

    first = client.post("/annotations/api/export", json={"repo_id": repo})
    assert first.status_code == 200, first.text
    out_dir = Path(first.json()["output_dir"])
    assert first.json()["reused_existing_export"] is False
    marker = json.loads((out_dir / ".levi-export.json").read_text())
    created_at = marker["created_at"]

    manifest = json.loads(
        (out_dir / "annotations/language/manifest.json").read_text()
    )
    assert manifest["episodes"]["0"]["persistent_count"] == 1
    episode_atoms = json.loads(
        (out_dir / "annotations/language/episode_000000.json").read_text()
    )["atoms"]
    assert episode_atoms[0]["to"] == 1.2

    second = client.post("/annotations/api/export", json={"repo_id": repo})
    assert second.status_code == 200, second.text
    assert second.json()["output_dir"] == str(out_dir)
    assert second.json()["reused_existing_export"] is True
    marker_after = json.loads((out_dir / ".levi-export.json").read_text())
    assert marker_after["created_at"] == created_at
    assert marker_after["updated_at"] >= created_at


def test_atom_to_before_timestamp_is_rejected(client, dataset):
    repo = client.post("/api/levi/catalog", json={"path": str(dataset)}).json()["id"]
    payload = {
        "repo_id": repo,
        "episode_index": 0,
        "atoms": [
            {
                "role": "assistant",
                "content": "bad range",
                "style": "subtask",
                "timestamp": 1.0,
                "to": 0.5,
            }
        ],
    }
    result = client.post("/annotations/api/episodes/0/atoms", json=payload)
    assert result.status_code == 400


def test_annotation_summary_reports_language_presence_per_episode(client, dataset):
    repo = client.post("/api/levi/catalog", json={"path": str(dataset)}).json()["id"]
    payload = {
        "repo_id": repo,
        "episode_index": 0,
        "atoms": [
            {
                "role": "assistant",
                "content": "grasp",
                "style": "subtask",
                "timestamp": 0.0,
            }
        ],
    }
    assert (
        client.post("/annotations/api/episodes/0/atoms", json=payload).status_code
        == 200
    )
    summary = client.get(
        "/annotations/api/episodes/annotation-summary", params={"repo_id": repo}
    )
    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert body["language"]["0"] is True
    assert body["language"]["1"] is False
    assert body["vision"]["0"] is False
    assert body["vision"]["1"] is False



def test_annotation_files_are_per_episode_and_named_by_episode_index(
    client, dataset
):
    """Every saved episode gets its own file named episode_{index:06d}.json
    inside a human-named sidecar directory (the dataset's own folder name,
    with no hash suffix — see ``DatasetState.display_slug``) — not one
    opaque-hash blob for the whole dataset."""
    from backend.app import DatasetRef, _ensure_state

    repo = client.post("/api/levi/catalog", json={"path": str(dataset)}).json()["id"]
    state = _ensure_state(DatasetRef(local_path=str(dataset)))

    payload = {
        "repo_id": repo,
        "episode_index": 0,
        "atoms": [
            {
                "role": "assistant",
                "content": "grasp",
                "style": "subtask",
                "timestamp": 0.0,
            }
        ],
    }
    result = client.post("/annotations/api/episodes/0/atoms", json=payload)
    assert result.status_code == 200, result.text
    saved_path = Path(result.json()["path"])

    assert saved_path == state.annotation_file(0)
    assert saved_path.name == "episode_000000.json"
    assert dataset.name in saved_path.parent.name
    assert saved_path.parent == state.annotations_dir
    assert json.loads(saved_path.read_text())["atoms"][0]["content"] == "grasp"

    result1 = client.post(
        "/annotations/api/episodes/1/atoms",
        json={**payload, "episode_index": 1},
    )
    assert Path(result1.json()["path"]).name == "episode_000001.json"


def test_legacy_single_blob_annotations_migrate_to_per_episode_files(
    client, dataset
):
    """A pre-refactor single-file sidecar (all episodes in one JSON, named
    by a bare identity hash) is transparently split into per-episode files
    the first time the dataset loads — and left in place afterward, unread.
    """
    import backend.app as annotations_module
    from backend.app import DatasetRef, _ensure_state

    repo = client.post("/api/levi/catalog", json={"path": str(dataset)}).json()["id"]
    state = _ensure_state(DatasetRef(local_path=str(dataset)))
    legacy_path = state.legacy_annotations_path
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        json.dumps(
            {
                "version": 2,
                "episodes": {
                    "0": {
                        "atoms": [
                            {
                                "role": "assistant",
                                "content": "legacy atom",
                                "style": "subtask",
                                "timestamp": 1.0,
                            }
                        ]
                    }
                },
            }
        )
    )
    # Force a fresh load so migration runs against the file we just planted
    # (the fixture already loaded — and cached — state without it).
    annotations_module._states.clear()

    result = client.get(
        "/annotations/api/episodes/0/atoms", params={"repo_id": repo}
    )
    assert result.status_code == 200, result.text
    assert result.json()["atoms"][0]["content"] == "legacy atom"
    assert state.annotation_file(0).exists()
    assert legacy_path.exists()  # left in place, not deleted


def test_delete_episode_annotation_file(client, dataset):
    repo = client.post("/api/levi/catalog", json={"path": str(dataset)}).json()["id"]
    payload = {
        "repo_id": repo,
        "episode_index": 0,
        "atoms": [
            {
                "role": "assistant",
                "content": "temp",
                "style": "subtask",
                "timestamp": 0.0,
            }
        ],
    }
    saved = client.post("/annotations/api/episodes/0/atoms", json=payload)
    file_path = Path(saved.json()["path"])
    assert file_path.exists()

    deleted = client.delete(
        "/annotations/api/episodes/0/atoms", params={"repo_id": repo}
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"ok": True, "deleted": True}
    assert not file_path.exists()

    # Re-reading now finds nothing (source parquet has no baked-in atoms
    # for this fixture either).
    reread = client.get(
        "/annotations/api/episodes/0/atoms", params={"repo_id": repo}
    )
    assert reread.json()["atoms"] == []

    # Deleting again is a safe no-op, not an error.
    again = client.delete(
        "/annotations/api/episodes/0/atoms", params={"repo_id": repo}
    )
    assert again.json() == {"ok": True, "deleted": False}


def test_episode_atoms_stay_in_sync_across_concurrent_processes(client, dataset):
    """Two collaborators, each running their own ``levi serve`` on the same
    host against the same dataset, must see each other's saves without
    restarting: annotations_dir has no hash suffix (see display_slug) so
    both resolve to the identical directory, and every lookup re-reads the
    per-episode file from disk rather than trusting an in-process cache
    forever — otherwise one process's edit is invisible to the other."""
    import dataclasses

    from backend.app import DatasetRef, _ensure_state

    repo = client.post("/api/levi/catalog", json={"path": str(dataset)}).json()["id"]
    state_a = _ensure_state(DatasetRef(local_path=str(dataset)))
    # A second, independent DatasetState sharing the same on-disk identity —
    # standing in for a second OS process that hasn't cached anything yet.
    state_b = dataclasses.replace(state_a, annotations={})

    client.post(
        "/annotations/api/episodes/0/atoms",
        json={
            "repo_id": repo,
            "episode_index": 0,
            "atoms": [
                {
                    "role": "assistant",
                    "content": "from process A",
                    "style": "subtask",
                    "timestamp": 0.0,
                }
            ],
        },
    )
    from backend.app import _lookup_episode_annotations

    # Process B, never having touched episode 0 before, must see A's save.
    seen_by_b = _lookup_episode_annotations(state_b, 0)
    assert seen_by_b is not None
    assert seen_by_b.atoms[0]["content"] == "from process A"

    # Now simulate B overwriting the same episode directly on disk (as its
    # own save endpoint would), and confirm A's stale cache picks it up too.
    from backend.app import _write_episode_annotations

    _write_episode_annotations(
        state_b,
        0,
        [
            {
                "role": "assistant",
                "content": "from process B",
                "style": "subtask",
                "timestamp": 0.0,
            }
        ],
    )
    seen_by_a = _lookup_episode_annotations(state_a, 0)
    assert seen_by_a is not None
    assert seen_by_a.atoms[0]["content"] == "from process B"
    # state_a's own cache from its earlier write is still sitting there
    # (nothing evicts it proactively) — the point is that a *lookup* never
    # trusts it once a different, real file exists on disk.
    assert state_a.annotations[0].atoms[0]["content"] == "from process A"


def test_diagnostics_report_named_after_local_dataset_not_catalog_hash(
    client, dataset
):
    from backend.app import STATE

    repo = client.post("/api/levi/catalog", json={"path": str(dataset)}).json()["id"]
    result = client.post(
        "/api/levi/diagnostics", json={"repo_id": repo, "checks": ["metadata"]}
    )
    assert result.status_code == 200, result.text

    diagnostics_dir = STATE / "diagnostics"
    matches = sorted(diagnostics_dir.glob(f"{dataset.name}_*.json"))
    assert len(matches) == 1, list(diagnostics_dir.glob("*.json"))
    assert matches[0].name != f"{repo.split('/')[1]}.json"

    # Re-running updates the same file in place, not a second one.
    client.post(
        "/api/levi/diagnostics", json={"repo_id": repo, "checks": ["metadata"]}
    )
    assert sorted(diagnostics_dir.glob(f"{dataset.name}_*.json")) == matches


def test_object_annotations_dir_named_after_dataset_not_bare_hash(client, dataset):
    """SAM3's sidecar directory follows the same naming convention as the
    language-annotation sidecar: the dataset's own name, with no hash
    suffix, so multiple LEVI processes on the same host annotating the same
    dataset resolve to the exact same directory (see
    ``DatasetState.display_slug``)."""
    from backend.app import DatasetRef, _ensure_state

    client.post("/api/levi/catalog", json={"path": str(dataset)})
    state = _ensure_state(DatasetRef(local_path=str(dataset)))
    assert state.object_annotations_path.name == dataset.name
    assert state.object_annotations_path.parent.name == "object_annotations"
