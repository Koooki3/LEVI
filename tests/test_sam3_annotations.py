"""CPU-only tests for the model-neutral SAM3 annotation contract."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from levi.annotations import (
    ObjectAnnotation,
    ObjectEdit,
    ReviewStatus,
    Sam3Plan,
    SidecarStore,
    decode_rle,
    encode_rle,
)


def make_annotation(frame: int = 0, *, status: ReviewStatus = ReviewStatus.SUGGESTED):
    mask = [[False, True, False], [False, True, True]]
    return ObjectAnnotation(
        episode_index=0,
        frame_index=frame,
        timestamp=frame / 10,
        camera_key="observation.images.front",
        object_id="cup-0",
        track_id=0,
        concept="cup",
        category="cup",
        bbox_xyxy=[1, 0, 3, 2],
        image_size=[2, 3],
        mask_rle=encode_rle(mask),
        score=0.95,
        status=status,
        source="fake",
        prompt="cup",
    )


def test_rle_round_trip_is_lossless_and_fortran_order():
    mask = [[False, True, False], [False, True, True]]
    encoded = encode_rle(mask)
    assert encoded == {"size": [2, 3], "counts": [2, 2, 1, 1]}
    assert decode_rle(encoded) == mask


def test_rle_rejects_wrong_coverage():
    with pytest.raises(ValueError, match="cover"):
        decode_rle({"size": [2, 2], "counts": [1]})


def test_worker_scope_validation_rejects_rows_outside_plan():
    plan = Sam3Plan(
        episode_indices=[0],
        camera_keys=["observation.images.front"],
        prompts=["cup"],
        start_frame=2,
        max_frames=3,
        provider="fake",
    )
    with pytest.raises(ValueError, match="precedes"):
        from levi.annotations.sam3_protocol import validate_annotations_for_plan

        validate_annotations_for_plan(plan, [make_annotation(frame=1)])


def test_sidecar_revision_and_human_edit(tmp_path: Path):
    store = SidecarStore(tmp_path / "annotations", identity={"repo_id": "demo/a"})
    first = store.publish([make_annotation(0), make_annotation(1)])
    assert first["annotation_count"] == 2
    assert store.current_revision() == first["revision_id"]
    rows = store.read_episode(0)
    assert len(rows) == 2
    edited = store.apply_edit(
        ObjectEdit(
            episode_index=0,
            camera_key="observation.images.front",
            operation="accept",
            object_id="cup-0",
            base_revision=first["revision_id"],
        )
    )
    assert edited["parent_revision"] == first["revision_id"]
    assert edited["revision_id"] != first["revision_id"]
    assert {row["status"] for row in store.read_episode(0)} == {"accepted"}
    assert len(store.list_revisions()) == 2


def test_sidecar_rejects_invalid_human_refinement(tmp_path: Path):
    store = SidecarStore(tmp_path / "annotations", identity={"repo_id": "demo/a"})
    first = store.publish([make_annotation()])
    with pytest.raises(ValueError, match="bbox"):
        store.apply_edit(
            ObjectEdit(
                episode_index=0,
                camera_key="observation.images.front",
                operation="refine",
                object_id="cup-0",
                base_revision=first["revision_id"],
                payload={"bbox_xyxy": [-1, 0, 2, 2]},
            )
        )
    assert store.current_revision() == first["revision_id"]


def test_sidecar_publish_partitions_multi_episode_multi_camera_batch(tmp_path: Path):
    """A batch spanning many episodes/cameras (as worker.py's run_plan now
    produces in one publish() call) must partition masks per (episode,
    camera) pair rather than merging or dropping rows across pairs."""
    from levi.annotations.sam3_protocol import fake_annotations

    plan = Sam3Plan(
        episode_indices=[0, 1],
        camera_keys=["observation.images.front", "observation.images.hand"],
        prompts=["cup"],
        provider="fake",
    )
    annotations = fake_annotations(plan)
    assert len(annotations) == 2 * 2 * 1 * 4  # episodes x cameras x prompts x frames

    store = SidecarStore(tmp_path / "annotations", identity={"repo_id": "demo/batch"})
    revision = store.publish(annotations)
    assert revision["annotation_count"] == len(annotations)

    for episode_index in (0, 1):
        rows = store.read_episode(episode_index)
        assert len(rows) == 2 * 1 * 4
        assert {row["camera_key"] for row in rows} == {
            "observation.images.front",
            "observation.images.hand",
        }


def test_prompt_presets_save_list_and_delete(tmp_path: Path, monkeypatch):
    import backend.app as backend_app

    monkeypatch.setattr(
        backend_app, "_SAM3_PROMPT_PRESETS_PATH", tmp_path / "presets.json"
    )

    def body(response):
        return json.loads(response.body)

    assert body(backend_app.sam3_list_prompt_presets())["presets"] == []

    saved = body(
        backend_app.sam3_save_prompt_preset(
            backend_app.Sam3PromptPresetRequest(
                name="kitchen", prompts=["cup", "cup", "  plate  ", ""]
            )
        )
    )
    assert saved["presets"] == [{"name": "kitchen", "prompts": ["cup", "plate"]}]

    # Saving the same name again replaces rather than duplicates it.
    body(
        backend_app.sam3_save_prompt_preset(
            backend_app.Sam3PromptPresetRequest(name="kitchen", prompts=["gripper"])
        )
    )
    listed = body(backend_app.sam3_list_prompt_presets())["presets"]
    assert listed == [{"name": "kitchen", "prompts": ["gripper"]}]

    deleted = body(backend_app.sam3_delete_prompt_preset("kitchen"))
    assert deleted["presets"] == []


def test_prompt_presets_reject_empty_name_or_prompts(tmp_path: Path, monkeypatch):
    from fastapi import HTTPException

    import backend.app as backend_app

    monkeypatch.setattr(
        backend_app, "_SAM3_PROMPT_PRESETS_PATH", tmp_path / "presets.json"
    )

    with pytest.raises(HTTPException):
        backend_app.sam3_save_prompt_preset(
            backend_app.Sam3PromptPresetRequest(name="  ", prompts=["cup"])
        )
    with pytest.raises(HTTPException):
        backend_app.sam3_save_prompt_preset(
            backend_app.Sam3PromptPresetRequest(name="x", prompts=["  ", ""])
        )


def test_api_cpu_fake_provider_and_export_are_sidecar_only(client, dataset, tmp_path):
    repo = client.post("/api/levi/catalog", json={"path": str(dataset)}).json()["id"]
    payload = {
        "repo_id": repo,
        "episode_indices": [0],
        "camera_keys": ["observation.images.front"],
        "prompts": ["cup", "plate"],
        "start_frame": 0,
        "max_frames": 3,
        "review_threshold": 0.6,
        "accept_threshold": 0.9,
        "provider": "fake",
    }
    before = (dataset / "data/chunk-000/episode_000000.parquet").read_bytes()
    plan = client.post("/annotations/api/sam3/plan", json=payload)
    assert plan.status_code == 200, plan.text
    assert plan.json()["status"] == "planned"
    # Internal worker paths are kept in staging files, never exposed to the UI.
    assert "dataset_root" not in plan.json()
    assert "local_path" not in plan.json()
    run_payload = {**payload, "plan_id": plan.json()["plan_id"]}
    run = client.post("/annotations/api/sam3/run", json=run_payload)
    assert run.status_code == 200, run.text
    result = run.json()
    assert result["provider"] == "fake" and result["count"] == 6
    assert result["plan_id"] == plan.json()["plan_id"]
    assert result["review_status"] == "suggested"
    changed_plan = client.post(
        "/annotations/api/sam3/run",
        json={**run_payload, "prompts": ["plate"]},
    )
    assert changed_plan.status_code == 409, changed_plan.text
    objects = client.get(
        "/annotations/api/sam3/episodes/0/objects",
        params={"repo_id": repo, "camera_key": "observation.images.front"},
    )
    assert objects.status_code == 200, objects.text
    rows = objects.json()["objects"]
    assert len(rows) == 6 and {row["status"] for row in rows} == {"suggested"}
    revision = objects.json()["revision"]
    edit = client.post(
        "/annotations/api/sam3/edits",
        params={"repo_id": repo},
        json={
            "episode_index": 0,
            "camera_key": "observation.images.front",
            "operation": "accept",
            "object_id": rows[0]["object_id"],
            "base_revision": revision,
        },
    )
    assert edit.status_code == 200, edit.text
    assert edit.json()["revision_id"] != revision
    out = tmp_path / "annotated"
    export = client.post(
        "/annotations/api/export",
        json={"repo_id": repo, "output_dir": str(out)},
    )
    assert export.status_code == 200, export.text
    assert export.json()["object_annotation_revision"]
    assert (out / "annotations/sam3/current.json").is_file()
    assert not (out / "annotations/sam3/staging").exists()
    assert (dataset / "data/chunk-000/episode_000000.parquet").read_bytes() == before


def test_sam3_status_reports_global_model_without_cuda(client, monkeypatch):
    import backend.app as annotations

    monkeypatch.setattr(annotations.HfApi, "whoami", lambda self: {"name": "fixture"})
    response = client.get("/annotations/api/sam3/status")
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["model_repo"] == "1038lab/sam3"
    assert value["model_filename"] == "sam3.pt"
    assert value["gpu_probe_performed"] is False
    assert value["hf_auth"]["username"] == "fixture"
    assert "HF_TOKEN" not in response.text


def test_checkpoint_download_requires_hf_session(client, tmp_path, monkeypatch):
    import backend.app as annotations

    monkeypatch.delenv("LEVI_SAM3_CHECKPOINT_DIR", raising=False)
    monkeypatch.delenv("LEVI_SAM3_CHECKPOINT", raising=False)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.setattr("huggingface_hub.get_token", lambda: None)
    monkeypatch.setattr(annotations, "SAM3_CHECKPOINT_DIR", tmp_path / "checkpoints")
    response = client.post("/annotations/api/sam3/checkpoint/download")
    assert response.status_code == 401, response.text
    assert "token" not in response.text.lower()


def test_checkpoint_download_uses_browser_token_and_workspace_path(
    client, tmp_path, monkeypatch
):
    import backend.app as annotations

    checkpoint_dir = tmp_path / "checkpoints"
    monkeypatch.delenv("LEVI_SAM3_CHECKPOINT_DIR", raising=False)
    monkeypatch.delenv("LEVI_SAM3_CHECKPOINT", raising=False)
    monkeypatch.setattr(annotations, "SAM3_CHECKPOINT_DIR", checkpoint_dir)
    annotations._SAM3_AUTH_CACHE.clear()
    monkeypatch.setattr(annotations.HfApi, "whoami", lambda self: {"name": "fixture"})
    monkeypatch.setattr(annotations, "_sam3_remote_checkpoint_size", lambda _token: 4)

    def fake_download(*, repo_id, filename, revision, token, local_dir):
        assert repo_id == "1038lab/sam3"
        assert filename == "sam3.pt"
        assert revision == "main"
        assert token == "hf-browser-test"
        target = Path(local_dir) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"test")
        return str(target)

    monkeypatch.setattr(annotations, "hf_hub_download", fake_download)
    client.cookies.set("hf_access_token", "hf-browser-test")
    response = client.post("/annotations/api/sam3/checkpoint/download")
    assert response.status_code == 202, response.text
    assert response.json()["download_started"] is True

    key = str((checkpoint_dir / "sam3.pt").resolve())
    thread = annotations._SAM3_DOWNLOAD_THREADS.get(key)
    # The fixture download is intentionally instantaneous; the daemon may
    # already have finished and removed itself before the response is asserted.
    if thread is not None:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert (checkpoint_dir / "sam3.pt").is_file()

    status = client.get("/annotations/api/sam3/status")
    assert status.status_code == 200, status.text
    value = status.json()
    assert value["checkpoint_cached"] is True
    assert value["checkpoint_path"] == str(checkpoint_dir / "sam3.pt")
    assert value["download"]["phase"] == "ready"
    assert value["download"]["bytes"] == 4
    assert "hf-browser-test" not in status.text


def test_real_sam3_run_requires_explicit_checkpoint_download(
    client, dataset, monkeypatch
):
    import backend.app as annotations

    monkeypatch.delenv("LEVI_SAM3_CHECKPOINT_DIR", raising=False)
    monkeypatch.delenv("LEVI_SAM3_CHECKPOINT", raising=False)
    monkeypatch.setattr(annotations, "SAM3_CHECKPOINT_DIR", dataset / "checkpoints")
    response = client.post(
        "/annotations/api/sam3/run",
        json={
            "local_path": str(dataset),
            "episode_indices": [0],
            "camera_keys": ["observation.images.front"],
            "prompts": ["cup"],
            "provider": "sam3",
        },
    )
    assert response.status_code == 409, response.text
    assert "checkpoint" in response.text.lower()


def test_api_rejects_real_provider_when_disabled(client, dataset, monkeypatch):
    monkeypatch.setenv("LEVI_SAM3_ENABLED", "0")
    response = client.post(
        "/annotations/api/sam3/run",
        json={
            "local_path": str(dataset),
            "episode_indices": [0],
            "camera_keys": ["observation.images.front"],
            "prompts": ["cup"],
            "provider": "sam3",
        },
    )
    assert response.status_code == 503


def test_cpu_safe_cli_checks_do_not_import_model():
    env = {**os.environ, "CUDA_VISIBLE_DEVICES": "", "LEVI_SAM3_ENABLED": "0"}
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "levi.cli", "sam3", "check"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "model_imported=0" in result.stdout
    assert "cuda_probe_performed=0" in result.stdout


def test_v3_worker_resolves_shared_video_metadata(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    worker_root = Path(__file__).resolve().parents[1] / "integrations" / "sam3"
    sys.path.insert(0, str(worker_root))
    try:
        from levi_sam3_worker.worker import _episode_video
    finally:
        sys.path.pop(0)

    (tmp_path / "meta/episodes/chunk-000").mkdir(parents=True)
    (tmp_path / "videos/chunk-002/observation.images.front").mkdir(parents=True)
    video = tmp_path / "videos/chunk-002/observation.images.front/file-003.mp4"
    video.write_bytes(b"fixture")
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "episode_index": 7,
                    "videos/observation.images.front/chunk_index": 2,
                    "videos/observation.images.front/file_index": 3,
                    "videos/observation.images.front/from_timestamp": 4.5,
                }
            ]
        ),
        tmp_path / "meta/episodes/chunk-000/file-000.parquet",
    )
    resolved, start = _episode_video(
        tmp_path,
        {
            "video_path": "videos/chunk-{video_chunk_index:03d}/{video_key}/file-{video_file_index:03d}.mp4"
        },
        7,
        "observation.images.front",
    )
    assert resolved == video
    assert start == 4.5


def test_v3_worker_uses_episode_local_frame_window(tmp_path):
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    worker_root = Path(__file__).resolve().parents[1] / "integrations" / "sam3"
    sys.path.insert(0, str(worker_root))
    try:
        from levi_sam3_worker.worker import _run_episode_camera
    finally:
        sys.path.pop(0)

    (tmp_path / "meta/episodes/chunk-000").mkdir(parents=True)
    (tmp_path / "videos/chunk-002/observation.images.front").mkdir(parents=True)
    (tmp_path / "videos/chunk-002/observation.images.front/file-003.mp4").write_bytes(
        b"fixture"
    )
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "episode_index": 7,
                    "length": 3,
                    "videos/observation.images.front/chunk_index": 2,
                    "videos/observation.images.front/file_index": 3,
                    "videos/observation.images.front/from_timestamp": 4.5,
                }
            ]
        ),
        tmp_path / "meta/episodes/chunk-000/file-000.parquet",
    )

    def output(frame_index: int) -> dict[str, object]:
        return {
            "frame_index": frame_index,
            "out_obj_ids": np.array([2]),
            "out_binary_masks": np.array([[[True, False], [True, True]]]),
            "out_boxes_xywh": np.array([[0.0, 0.0, 1.0, 1.0]]),
            "out_probs": np.array([0.9]),
        }

    class Predictor:
        def __init__(self):
            self.calls: list[dict[str, object]] = []

        def handle_request(self, request):
            self.calls.append(request)
            if request["type"] == "start_session":
                return {"session_id": "session"}
            if request["type"] == "add_prompt":
                return {"frame_index": 136, "outputs": output(136)}
            return {}

        def handle_stream_request(self, request):
            self.calls.append(request)
            yield {"frame_index": 137, "outputs": output(137)}

    predictor = Predictor()
    rows = _run_episode_camera(
        predictor,
        tmp_path,
        {
            "fps": 30,
            "video_path": "videos/chunk-{video_chunk_index:03d}/{video_key}/file-{video_file_index:03d}.mp4",
        },
        plan={"prompts": ["cup"], "start_frame": 1, "max_frames": None},
        episode_index=7,
        camera_key="observation.images.front",
        np=np,
    )
    assert [row["frame_index"] for row in rows] == [1, 2]
    assert rows[0]["timestamp"] == 1 / 30
    assert predictor.calls[1]["frame_index"] == 136
    assert predictor.calls[2]["start_frame_index"] == 136
    assert predictor.calls[2]["max_frame_num_to_track"] == 2


def test_worker_timestamps_are_episode_local():
    import numpy as np

    worker_root = Path(__file__).resolve().parents[1] / "integrations" / "sam3"
    sys.path.insert(0, str(worker_root))
    try:
        from levi_sam3_worker.worker import _append_outputs
    finally:
        sys.path.pop(0)

    rows: list[dict[str, object]] = []
    _append_outputs(
        rows,
        {
            "frame_index": 3,
            "out_obj_ids": np.array([2]),
            "out_binary_masks": np.array([[[True, False], [True, True]]]),
            "out_boxes_xywh": np.array([[0.0, 0.0, 1.0, 1.0]]),
            "out_probs": np.array([0.9]),
        },
        episode_index=7,
        camera_key="observation.images.front",
        concepts={2: "cup"},
        fps=30,
        np=np,
    )
    assert rows[0]["timestamp"] == 0.1


def test_capabilities_does_not_probe_gpu(client):
    capabilities = client.get("/annotations/api/sam3/capabilities")
    assert capabilities.status_code == 200
    value = capabilities.json()
    assert value["gpu_probe_performed"] is False
    assert value["manual_annotation_available"] is True


def test_plan_rejects_unknown_episode(client, dataset):
    repo = client.post("/api/levi/catalog", json={"path": str(dataset)}).json()["id"]
    response = client.post(
        "/annotations/api/sam3/plan",
        json={
            "repo_id": repo,
            "episode_indices": [99],
            "camera_keys": ["observation.images.front"],
            "prompts": ["cup"],
            "provider": "fake",
        },
    )
    assert response.status_code == 400


def test_stored_rows_are_read_back_as_annotations(tmp_path: Path):
    """The parquet layout must not reach a reader.

    Masks are stored as flat ``rle_size``/``rle_counts`` columns because that
    is what parquet holds well, but every reader -- including the endpoint the
    viewer calls -- wants ``mask_rle``. Serving the raw columns left the
    overlay with no mask and threw on ``mask_rle.size``.
    """
    store = SidecarStore(tmp_path / "annotations", identity={"repo_id": "demo/a"})
    original = make_annotation(0)
    store.publish([original])

    for row in (store.read_episode(0)[0], store.read_annotations()[0]):
        assert "mask_rle" in row, "reader leaked the storage columns"
        assert row["mask_rle"]["size"] == original.image_size
        assert row["mask_rle"]["counts"] == original.mask_rle["counts"]
        assert "rle_size" not in row and "rle_counts" not in row
        # Round-trips through the model the rest of LEVI shares.
        assert ObjectAnnotation.model_validate(row).mask_rle == original.mask_rle


def test_episode_objects_endpoint_serves_drawable_masks(tmp_path, client, monkeypatch):
    """What the browser receives must be what the overlay can draw."""
    import types

    from backend import app

    store = SidecarStore(tmp_path / "sidecar", identity={"repo_id": "demo/a"})
    store.publish([make_annotation(0)])
    monkeypatch.setattr(app, "_sidecar", lambda state: store)
    monkeypatch.setattr(
        app,
        "_ensure_state",
        lambda ref: types.SimpleNamespace(display_slug="demo__a", root=tmp_path),
    )

    payload = client.get(
        "/annotations/api/sam3/episodes/0/objects", params={"repo_id": "demo/a"}
    ).json()
    served = payload["objects"][0]
    assert served["mask_rle"]["size"] and served["mask_rle"]["counts"]
    assert "rle_size" not in served and "rle_counts" not in served
