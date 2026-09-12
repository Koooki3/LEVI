"""Lazy SAM3 video adapter.

The module intentionally imports no Torch, NumPy, or SAM3 at import time. The
heavy runtime is only reached from ``run_plan`` after the caller has enabled the
global integration with ``LEVI_SAM3_ENABLED=1``. All output is plain JSON so the
LEVI control plane remains independent from model packages.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SAM3_COMMIT = "660a5e9e1b8b4c02c0ad97229b88a09a6e4ff5b"
SAM3_MODEL_REPO = os.environ.get("LEVI_SAM3_MODEL_REPO", "1038lab/sam3")
SAM3_MODEL_FILENAME = os.environ.get("LEVI_SAM3_MODEL_FILENAME", "sam3.pt")
SAM3_MODEL_REVISION = os.environ.get("LEVI_SAM3_MODEL_REVISION", "main")
SAM3_PROGRESS_FILENAME = "download-progress.json"


def _enabled() -> bool:
    return os.environ.get("LEVI_SAM3_ENABLED", "1").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
    os.replace(temp, path)


def _checkpoint_dir() -> Path:
    configured = os.environ.get("LEVI_SAM3_CHECKPOINT_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.cwd() / ".state" / "checkpoints" / "sam3"


def _progress_path() -> Path:
    return _checkpoint_dir() / SAM3_PROGRESS_FILENAME


def _safe_progress_message(value: object) -> str:
    # Download errors can echo signed Hub URLs. Persist only a redacted,
    # human-readable message in the workspace progress file.
    message = str(value)
    return re.sub(
        r"(?i)(token|authorization|x-amz-signature|x-amz-credential)=[^&\s]+",
        r"\1=<redacted>",
        message,
    )


def _write_checkpoint_progress(
    phase: str,
    *,
    bytes_downloaded: int = 0,
    total_bytes: int | None = None,
    percent: float | None = None,
    path: Path | None = None,
    message: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "phase": phase,
        "repo_id": SAM3_MODEL_REPO,
        "filename": SAM3_MODEL_FILENAME,
        "revision": SAM3_MODEL_REVISION,
        "bytes": max(0, int(bytes_downloaded)),
        "total_bytes": total_bytes,
        "percent": percent,
        "path": str(path or (_checkpoint_dir() / SAM3_MODEL_FILENAME)),
        "updated_at": time.time(),
    }
    if message:
        payload["message"] = _safe_progress_message(message)
    try:
        _atomic_json(_progress_path(), payload)
    except OSError:
        # Checkpoint download must not fail solely because progress telemetry
        # cannot be written.
        return


def _remote_checkpoint_size(token_value: str | None) -> int | None:
    """Read the model file size without downloading model bytes."""
    try:
        from huggingface_hub import HfApi

        api = HfApi(token=token_value)
        try:
            info = api.model_info(
                SAM3_MODEL_REPO,
                revision=SAM3_MODEL_REVISION,
                files_metadata=True,
            )
        except TypeError:
            info = api.model_info(SAM3_MODEL_REPO, revision=SAM3_MODEL_REVISION)
        for sibling in getattr(info, "siblings", []) or []:
            name = getattr(sibling, "rfilename", None)
            size = getattr(sibling, "size", None)
            if name == SAM3_MODEL_FILENAME and isinstance(size, int):
                return size
    except Exception as exc:  # noqa: BLE001
        # A missing metadata HEAD request should only make the UI progress bar
        # indeterminate; hf_hub_download remains the source of truth.
        _ = exc
    return None


def _incomplete_bytes(root: Path) -> int:
    total = 0
    try:
        for path in root.rglob("*.incomplete"):
            if path.is_file():
                total = max(total, path.stat().st_size)
    except OSError:
        pass
    return total


def _ensure_checkpoint() -> tuple[Path, str]:
    """Resolve a local checkpoint or download the configured mirror once."""
    configured = os.environ.get("LEVI_SAM3_CHECKPOINT")
    if configured:
        checkpoint = Path(configured).expanduser().resolve()
        if not checkpoint.is_file() or checkpoint.stat().st_size <= 0:
            raise FileNotFoundError(f"LEVI_SAM3_CHECKPOINT not found or empty: {checkpoint}")
        _write_checkpoint_progress(
            "ready", bytes_downloaded=checkpoint.stat().st_size, total_bytes=checkpoint.stat().st_size, percent=100.0, path=checkpoint
        )
        return checkpoint, "local"

    target_dir = _checkpoint_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / SAM3_MODEL_FILENAME
    if target.is_file() and target.stat().st_size > 0:
        size = target.stat().st_size
        _write_checkpoint_progress(
            "ready", bytes_downloaded=size, total_bytes=size, percent=100.0, path=target
        )
        return target, "huggingface"

    token_value = os.environ.get("HF_TOKEN") or None
    total_bytes = _remote_checkpoint_size(token_value)
    _write_checkpoint_progress(
        "downloading",
        total_bytes=total_bytes,
        percent=0.0 if total_bytes else None,
        path=target,
        message=f"Downloading {SAM3_MODEL_REPO}/{SAM3_MODEL_FILENAME}",
    )
    stop = threading.Event()

    def monitor() -> None:
        while not stop.is_set():
            current = _incomplete_bytes(target_dir)
            percent = (
                min(100.0, current * 100.0 / total_bytes)
                if total_bytes
                else None
            )
            _write_checkpoint_progress(
                "downloading",
                bytes_downloaded=current,
                total_bytes=total_bytes,
                percent=percent,
                path=target,
            )
            stop.wait(0.5)

    thread = threading.Thread(target=monitor, name="sam3-checkpoint-progress", daemon=True)
    thread.start()
    try:
        from huggingface_hub import hf_hub_download

        downloaded = Path(
            hf_hub_download(
                repo_id=SAM3_MODEL_REPO,
                filename=SAM3_MODEL_FILENAME,
                revision=SAM3_MODEL_REVISION,
                token=token_value,
                local_dir=str(target_dir),
            )
        )
    except Exception as exc:
        _write_checkpoint_progress(
            "error",
            bytes_downloaded=_incomplete_bytes(target_dir),
            total_bytes=total_bytes,
            percent=None,
            path=target,
            message=str(exc),
        )
        raise
    finally:
        stop.set()
        thread.join(timeout=2)
    size = downloaded.stat().st_size
    _write_checkpoint_progress(
        "ready", bytes_downloaded=size, total_bytes=size, percent=100.0, path=downloaded
    )
    return downloaded, "huggingface"


def _rle(mask: Any) -> dict[str, object]:
    """Encode a 2-D bool NumPy-like array as uncompressed COCO RLE."""
    # Keep the implementation independent from LEVI's package so this worker
    # can run in its own uv environment without the core dependency set.
    height, width = int(mask.shape[-2]), int(mask.shape[-1])
    flat = mask.astype(bool).T.reshape(-1).tolist()
    counts: list[int] = []
    current = False
    run = 0
    for value in flat:
        value = bool(value)
        if value == current:
            run += 1
        else:
            counts.append(run)
            current = value
            run = 1
    counts.append(run)
    return {"size": [height, width], "counts": counts}


def _as_numpy(value: Any, np: Any) -> Any:
    if hasattr(value, "detach"):
        value = value.detach().cpu()
    if hasattr(value, "numpy"):
        value = value.numpy()
    return np.asarray(value)


def _value_at(values: Any, index: int, default: float) -> float:
    if values is None:
        return default
    array = values
    try:
        if hasattr(array, "detach"):
            array = array.detach().cpu().numpy()
        item = array[index]
        if hasattr(item, "item"):
            item = item.item()
        return float(item)
    except (IndexError, TypeError, ValueError):
        return default


def _normalise_outputs(outputs: Any, np: Any) -> tuple[list[int], list[Any], Any, Any]:
    if not isinstance(outputs, dict):
        raise TypeError("SAM3 returned a non-object output")
    ids = outputs.get("out_obj_ids", outputs.get("object_ids", outputs.get("obj_ids")))
    masks = outputs.get("out_binary_masks", outputs.get("masks", outputs.get("binary_masks")))
    boxes = outputs.get("out_boxes_xywh", outputs.get("boxes", outputs.get("boxes_xywh")))
    scores = outputs.get("out_probs", outputs.get("scores", outputs.get("probabilities")))
    if ids is None or masks is None:
        raise RuntimeError("SAM3 output must include out_obj_ids and out_binary_masks")
    ids_array = _as_numpy(ids, np).reshape(-1)
    masks_array = _as_numpy(masks, np)
    if masks_array.ndim == 2:
        masks_array = masks_array[None, ...]
    if masks_array.ndim != 3:
        raise RuntimeError(f"SAM3 masks must be [objects,height,width], got {masks_array.shape}")
    ids_list = [int(value) for value in ids_array.tolist()]
    return ids_list, [masks_array[index] for index in range(min(len(ids_list), len(masks_array)))], boxes, scores


def _bbox_from_mask(mask: Any, np: Any) -> list[float] | None:
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return None
    return [float(xs.min()), float(ys.min()), float(xs.max() + 1), float(ys.max() + 1)]


def _bbox_xywh(boxes: Any, index: int, mask: Any, np: Any) -> list[float] | None:
    if boxes is not None:
        try:
            value = _as_numpy(boxes, np)[index].tolist()
            if len(value) >= 4 and all(math.isfinite(float(item)) for item in value[:4]):
                x, y, width, height = (float(item) for item in value[:4])
                # SAM3's video API returns pixel xywh. Defensive support for
                # normalized boxes makes the adapter tolerant of future APIs.
                if max(abs(x), abs(y), abs(width), abs(height)) <= 1.0:
                    h, w = int(mask.shape[-2]), int(mask.shape[-1])
                    x, width = x * w, width * w
                    y, height = y * h, height * h
                return [x, y, x + max(0.0, width), y + max(0.0, height)]
        except (IndexError, TypeError, ValueError):
            pass
    return _bbox_from_mask(mask, np)


def _episode_metadata(root: Path, episode_index: int) -> dict[str, Any]:
    """Read one v2 JSONL or v3 Parquet episode metadata row."""
    metadata_path = root / "meta" / "episodes.jsonl"
    if metadata_path.is_file():
        for line in metadata_path.read_text().splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if int(value.get("episode_index", -1)) == episode_index:
                return value

    metadata_root = root / "meta" / "episodes"
    if metadata_root.is_dir():
        # pyarrow is part of the optional worker environment. Keep this import
        # lazy so importing the adapter for CPU-safe checks remains model-free.
        import pyarrow.parquet as pq

        for path in sorted(metadata_root.glob("**/*.parquet")):
            for value in pq.read_table(path).to_pylist():
                if int(value.get("episode_index", -1)) == episode_index:
                    return value
    return {}


def _episode_video(
    root: Path, info: dict[str, Any], episode_index: int, camera_key: str
) -> tuple[Path, float]:
    """Resolve a v2/v3 video using metadata first, then a conservative glob."""
    episode = _episode_metadata(root, episode_index)
    camera_prefix = f"videos/{camera_key}"
    segment_start = float(
        episode.get(
            f"{camera_prefix}/from_timestamp",
            episode.get("video_from_timestamp", 0),
        )
        or 0
    )
    template = info.get("video_path")
    if template:
        default_chunk = episode_index // int(info.get("chunks_size", 1000) or 1000)
        data_chunk = episode.get(
            "data/chunk_index",
            episode.get("data_chunk_index", episode.get("chunk_index", 0)),
        )
        data_file = episode.get(
            "data/file_index",
            episode.get("data_file_index", episode.get("file_index", 0)),
        )
        video_chunk = episode.get(
            f"{camera_prefix}/chunk_index",
            episode.get("video_chunk_index", default_chunk),
        )
        video_file = episode.get(
            f"{camera_prefix}/file_index",
            episode.get("video_file_index", 0),
        )
        values = {
            "episode_index": episode_index,
            "episode_chunk": episode.get("episode_chunk", default_chunk),
            "chunk_index": data_chunk,
            "file_index": data_file,
            "video_chunk": video_chunk,
            "video_chunk_index": video_chunk,
            "video_file_index": video_file,
            "video_key": camera_key,
        }
        try:
            candidate = root / str(template).format(**values)
            if candidate.is_file():
                return candidate, segment_start
        except (KeyError, ValueError):
            pass
    names = {camera_key, camera_key.replace(".", "_"), camera_key.split(".")[-1]}
    candidates = sorted(root.glob(f"videos/**/episode_{episode_index:06d}.mp4"))
    candidates += sorted(root.glob(f"videos/**/*{episode_index:06d}*.mp4"))
    for candidate in dict.fromkeys(candidates):
        if any(name in candidate.as_posix() for name in names):
            return candidate, segment_start
    if len(candidates) == 1:
        return candidates[0], segment_start
    raise FileNotFoundError(
        f"No video found for episode {episode_index}, camera {camera_key!r}; "
        "check meta/info.json video_path and the selected camera feature"
    )


def _append_outputs(
    rows: list[dict[str, Any]],
    outputs: Any,
    *,
    episode_index: int,
    camera_key: str,
    concepts: dict[int, str],
    fps: float,
    np: Any,
    frame_offset: int = 0,
) -> None:
    ids, masks, boxes, scores = _normalise_outputs(outputs, np)
    for index, (object_id, mask) in enumerate(zip(ids, masks, strict=False)):
        mask = _as_numpy(mask, np).astype(bool)
        bbox = _bbox_xywh(boxes, index, mask, np)
        if bbox is None:
            continue
        source_frame_index = (
            int(outputs.get("frame_index", 0)) if isinstance(outputs, dict) else 0
        )
        frame_index = source_frame_index - frame_offset
        if frame_index < 0:
            continue
        score = max(0.0, min(1.0, _value_at(scores, index, 1.0)))
        rows.append(
            {
                "episode_index": episode_index,
                "frame_index": frame_index,
                # The annotation API and player use episode-local seconds.
                # ``segment_start`` is only a seek offset inside a shared
                # v3 video shard; including it here would make review clicks
                # jump to the wrong position for episodes whose source video
                # starts at a non-zero timestamp.
                "timestamp": frame_index / fps,
                "camera_key": camera_key,
                "object_id": f"sam3-{episode_index}-{camera_key.replace('.', '_')}-{object_id}",
                "track_id": max(0, object_id),
                "concept": concepts.get(object_id, "object"),
                "category": concepts.get(object_id),
                "bbox_xyxy": bbox,
                "image_size": [int(mask.shape[-2]), int(mask.shape[-1])],
                "mask_rle": _rle(mask),
                "score": score,
                "visible": True,
                "occluded": False,
                # Model results stay suggested; human review is required even
                # when the confidence score is above the configured threshold.
                "status": "suggested",
                "source": "sam3",
                "prompt": concepts.get(object_id),
            }
        )


def _run_episode_camera(
    predictor: Any,
    root: Path,
    info: dict[str, Any],
    *,
    plan: dict[str, Any],
    episode_index: int,
    camera_key: str,
    np: Any,
) -> list[dict[str, Any]]:
    video, segment_start = _episode_video(root, info, episode_index, camera_key)
    fps = float(info.get("fps", 30) or 30)
    episode = _episode_metadata(root, episode_index)
    # v3 video shards may contain multiple episodes. Predictor frame indices
    # are relative to the shared file, while the sidecar contract is episode
    # local, so carry the metadata timestamp offset through the whole run.
    segment_start_frame = max(0, round(segment_start * fps))
    local_start_frame = max(0, int(plan.get("start_frame", 0)))
    source_start_frame = segment_start_frame + local_start_frame
    episode_length = int(episode.get("length", 0) or 0)
    if episode_length <= 0:
        segment_end = float(
            episode.get(
                f"videos/{camera_key}/to_timestamp",
                episode.get("video_to_timestamp", 0),
            )
            or 0
        )
        if segment_end > segment_start:
            episode_length = max(0, round((segment_end - segment_start) * fps))
    requested_max_frames = plan.get("max_frames")
    remaining_frames = max(0, episode_length - local_start_frame)
    if requested_max_frames is None:
        max_frames = remaining_frames or None
    else:
        max_frames = (
            min(int(requested_max_frames), remaining_frames)
            if remaining_frames
            else int(requested_max_frames)
        )
    session = predictor.handle_request({"type": "start_session", "resource_path": str(video)})
    session_id = session["session_id"]
    concepts: dict[int, str] = {}
    rows: list[dict[str, Any]] = []
    try:
        for prompt in plan["prompts"]:
            response = predictor.handle_request(
                {
                    "type": "add_prompt",
                    "session_id": session_id,
                    "frame_index": source_start_frame,
                    "text": prompt,
                    "output_prob_thresh": float(plan.get("review_threshold", 0.6)),
                }
            )
            outputs = response.get("outputs", {})
            if isinstance(outputs, dict):
                ids = outputs.get("out_obj_ids", outputs.get("object_ids", []))
                ids = _as_numpy(ids, np).reshape(-1).tolist() if ids is not None else []
                for object_id in ids:
                    concepts.setdefault(int(object_id), prompt)
                if "frame_index" not in outputs:
                    outputs = dict(outputs)
                    outputs["frame_index"] = response.get("frame_index", source_start_frame)
                _append_outputs(
                    rows,
                    outputs,
                    episode_index=episode_index,
                    camera_key=camera_key,
                    concepts=concepts,
                    fps=fps,
                    np=np,
                    frame_offset=segment_start_frame,
                )
        request = {
            "type": "propagate_in_video",
            "session_id": session_id,
            "propagation_direction": "forward",
            "start_frame_index": source_start_frame,
            "output_prob_thresh": float(plan.get("review_threshold", 0.6)),
        }
        if max_frames is not None:
            request["max_frame_num_to_track"] = int(max_frames)
        for response in predictor.handle_stream_request(request):
            outputs = response.get("outputs", {})
            if isinstance(outputs, dict):
                outputs = dict(outputs)
                outputs["frame_index"] = response.get("frame_index", outputs.get("frame_index", source_start_frame))
                _append_outputs(
                    rows,
                    outputs,
                    episode_index=episode_index,
                    camera_key=camera_key,
                    concepts=concepts,
                    fps=fps,
                    np=np,
                    frame_offset=segment_start_frame,
                )
    finally:
        predictor.handle_request({"type": "close_session", "session_id": session_id})
    # The official predictor can emit the conditioning frame twice. Keep one
    # record per frame/object while preserving the first model output.
    unique: dict[tuple[int, int], dict[str, Any]] = {}
    for row in rows:
        unique.setdefault((row["frame_index"], row["track_id"]), row)
    return list(unique.values())


def _write_batch_progress(
    path: Path | None,
    *,
    done: int,
    total: int,
    episode_index: int | None,
    camera_key: str | None,
) -> None:
    if path is None:
        return
    payload = {
        "done": done,
        "total": total,
        "current_episode": episode_index,
        "current_camera": camera_key,
        "updated_at": time.time(),
    }
    try:
        _atomic_json(path, payload)
    except OSError:
        # Progress telemetry must not fail an otherwise-successful batch item.
        return


def _run_batch(
    predictor: Any,
    root: Path,
    info: dict[str, Any],
    *,
    plan: dict[str, Any],
    np: Any,
    progress_path: Path | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run every (episode, camera) pair, isolating one bad pair from the rest.

    A single missing video or malformed episode must not discard annotations
    already computed for the rest of a large batch.
    """
    pairs = [
        (int(episode_index), camera_key)
        for episode_index in plan["episode_indices"]
        for camera_key in plan["camera_keys"]
    ]
    total = len(pairs)
    annotations: list[dict[str, Any]] = []
    item_errors: list[dict[str, Any]] = []
    _write_batch_progress(progress_path, done=0, total=total, episode_index=None, camera_key=None)
    for done, (episode_index, camera_key) in enumerate(pairs, start=1):
        try:
            annotations.extend(
                _run_episode_camera(
                    predictor,
                    root,
                    info,
                    plan=plan,
                    episode_index=episode_index,
                    camera_key=camera_key,
                    np=np,
                )
            )
        except Exception as exc:  # noqa: BLE001 - one bad pair must not sink the batch
            item_errors.append(
                {
                    "episode_index": episode_index,
                    "camera_key": camera_key,
                    "error": str(exc),
                }
            )
        _write_batch_progress(
            progress_path, done=done, total=total, episode_index=episode_index, camera_key=camera_key
        )
    return annotations, item_errors


def run_plan(plan_path: Path, output_path: Path, progress_path: Path | None = None) -> None:
    if not _enabled():
        raise RuntimeError("SAM3 is disabled; set LEVI_SAM3_ENABLED=1 to enable it")
    plan = json.loads(plan_path.read_text())
    root = Path(plan["dataset_root"]).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"dataset root not found: {root}")
    info = json.loads((root / "meta" / "info.json").read_text())
    # All heavy/model imports stay below the explicit opt-in guard.
    import numpy as np
    import torch
    from sam3.model_builder import build_sam3_video_predictor

    if not torch.cuda.is_available():
        raise RuntimeError("SAM3 worker requires a CUDA device; LEVI core remains CPU-safe")
    checkpoint, checkpoint_source = _ensure_checkpoint()
    # Passing an explicit path prevents the pinned official builder from
    # looking up facebook/sam3. The mirror is resolved above with the user's
    # current Hugging Face credential.
    predictor = build_sam3_video_predictor(checkpoint_path=str(checkpoint))
    try:
        annotations, item_errors = _run_batch(
            predictor, root, info, plan=plan, np=np, progress_path=progress_path
        )
    finally:
        shutdown = getattr(predictor, "shutdown", None)
        if callable(shutdown):
            shutdown()
    model = {
        "provider": "sam3",
        "model_version": f"sam3@{SAM3_COMMIT}",
        "checkpoint": checkpoint_source,
        "checkpoint_path": str(checkpoint),
        "model_repo": SAM3_MODEL_REPO,
        "model_filename": SAM3_MODEL_FILENAME,
        "model_revision": SAM3_MODEL_REVISION,
    }
    if not annotations and item_errors:
        first = item_errors[0]
        _atomic_json(
            output_path,
            {
                "status": "failed",
                "error": (
                    f"All {len(item_errors)} episode/camera item(s) failed; "
                    f"first error (episode {first['episode_index']}, "
                    f"{first['camera_key']}): {first['error']}"
                ),
                "item_errors": item_errors,
                "model": model,
            },
        )
        return
    _atomic_json(
        output_path,
        {
            "status": "succeeded",
            "annotations": annotations,
            "item_errors": item_errors,
            "model": model,
        },
    )
