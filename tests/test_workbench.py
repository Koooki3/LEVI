import json
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
    assert (
        client.post(
            "/annotations/api/export",
            json={"repo_id": repo, "output_dir": str(dataset)},
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/annotations/api/export",
            json={"repo_id": repo, "output_dir": str(tmp_path / "annotated")},
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
