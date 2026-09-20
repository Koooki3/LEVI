"""Pinned LeRobot evidence, including v3 shared video offsets. CPU decode only."""

import hashlib
import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

from .schema import EvidenceRef, TaskContext
from .store import digest, file_hash


def backend():
    # Existing reader/validator is the authority for supported dataset versions.
    from backend import app

    return app


def state_for(context: TaskContext):
    app = backend()
    return app._ensure_state(
        app.DatasetRef(repo_id=context.repo_id, revision=context.revision)
    )


def pin_remote(context: TaskContext):
    if context.repo_id.startswith("local/"):
        return context
    from huggingface_hub import HfApi

    from levi.auth import token

    commit = (
        HfApi(token=token())
        .dataset_info(context.repo_id, revision=context.revision or "main")
        .sha
    )
    return context.model_copy(update={"revision": commit})


def video_relative(state, episode, camera):
    row = (
        state.episodes_df[state.episodes_df.episode_index == episode].iloc[0].to_dict()
    )
    info = state.info
    if info["codebase_version"].startswith("v2"):
        chunk = episode // int(info.get("chunks_size", 1000))
        return info["video_path"].format(
            episode_chunk=chunk,
            chunk_index=chunk,
            episode_index=episode,
            video_key=camera,
        ), 0.0
    prefix = f"videos/{camera}"
    chunk = int(row[f"{prefix}/chunk_index"])
    file = int(row[f"{prefix}/file_index"])
    return info["video_path"].format(
        chunk_index=chunk,
        file_index=file,
        video_chunk_index=chunk,
        video_file_index=file,
        video_key=camera,
    ), float(row[f"{prefix}/from_timestamp"])


def inspect(context: TaskContext):
    state = state_for(context)
    known = set(map(int, state.episodes_df.episode_index))
    if not set(context.episodes) <= known:
        raise ValueError("Unknown episode in selected scope")
    for camera in context.cameras:
        if state.info.get("features", {}).get(camera, {}).get("dtype") != "video":
            raise ValueError("Selected camera is not a supported video feature")
    files = {
        p.relative_to(state.root).as_posix(): p.stat().st_size
        for p in (state.root / "meta").rglob("*")
        if p.is_file()
    }
    app = backend()
    for episode in context.episodes:
        path = app._episode_data_path(state, episode)
        if path is None:
            raise ValueError("Episode table unavailable")
        files[path.relative_to(state.root).as_posix()] = path.stat().st_size
        for camera in context.cameras:
            relative, _ = video_relative(state, episode, camera)
            path = app.inside(relative, state.root)
            if path.exists():
                files[relative] = path.stat().st_size
            elif state.repo_id:
                # Resolve size without downloading the video. Execution downloads
                # the same pinned commit only after the user accepts this plan.
                from huggingface_hub import HfApi

                from levi.auth import token

                rows = HfApi(token=token()).get_paths_info(
                    state.repo_id,
                    [relative],
                    repo_type="dataset",
                    revision=context.revision,
                )
                if not rows or not hasattr(rows[0], "size"):
                    raise ValueError("Pinned video is unavailable")
                files[relative] = rows[0].size
            else:
                raise ValueError("Selected video is missing")
    return state, files


def snapshot(
    context: TaskContext, destination: Path, expected_files: dict, expected_hashes=None
):
    app = backend()
    state, files = inspect(context)
    if files != expected_files:
        raise ValueError(
            "Input file set or sizes changed since planning; create a new plan"
        )
    if sum(files.values()) > context.budget.max_snapshot_bytes:
        raise ValueError("Snapshot exceeds storage budget")
    destination.mkdir(parents=True, exist_ok=False)
    manifest = {}
    for relative in sorted(files):
        source = app.inside(relative, state.root)
        if not source.exists() and state.repo_id:
            from huggingface_hub import hf_hub_download

            from levi.auth import token

            hf_hub_download(
                state.repo_id,
                filename=relative,
                repo_type="dataset",
                revision=context.revision,
                token=token(),
                local_dir=state.root,
            )
        if not source.is_file() or source.stat().st_size != files[relative]:
            raise ValueError("Input changed before snapshot")
        target = app.inside(relative, destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        before = file_hash(source)
        if (
            expected_hashes
            and relative in expected_hashes
            and expected_hashes[relative] != before
        ):
            raise ValueError("Input content changed since planning; create a new plan")
        shutil.copyfile(source, target)
        if file_hash(target) != before or file_hash(source) != before:
            raise ValueError("Input changed during snapshot")
        manifest[relative] = before
    (destination.parent / "input-manifest.json").write_text(
        json.dumps(manifest, sort_keys=True)
    )
    return {"files": manifest, "sha256": digest(manifest)}


def verify_source(context, manifest):
    state = state_for(context)
    for relative, expected in manifest["files"].items():
        path = backend().inside(relative, state.root)
        if not path.is_file() or file_hash(path) != expected:
            raise ValueError("Source changed since snapshot; create a new run")


def snapshot_state(context, root):
    app = backend()
    # _load_state avoids the stat-only cache and works with private snapshot roots.
    return app._load_state(
        app.DatasetRef(local_path=str(root)), "agent::" + str(root), annotations=False
    )


def episode_table(state, episode):
    path = backend()._episode_data_path(state, episode)
    if path is None:
        raise ValueError("Episode table missing")
    df = pd.read_parquet(path)
    if "episode_index" in df:
        df = df[df.episode_index == episode]
    if df.empty or "timestamp" not in df or "frame_index" not in df:
        raise ValueError("Episode has no frame/timestamp ledger")
    if not np.isfinite(df.timestamp.to_numpy(dtype=float)).all():
        raise ValueError("Non-finite timestamps")
    return df.sort_values("frame_index").reset_index(drop=True)


def sample(context, root, episode, artifact_dir, *, frame_indices=None):
    import cv2

    state = snapshot_state(context, root)
    table = episode_table(state, episode)
    positions = sorted(
        set(np.linspace(0, len(table) - 1, context.samples_per_episode).astype(int))
    )
    if frame_indices is not None:
        positions = [
            i for i, row in table.iterrows() if int(row.frame_index) in frame_indices
        ]
        if len(positions) != len(frame_indices):
            raise ValueError("Evidence frame missing from source")
    ledger_path = root / "meta/levi_provenance.jsonl"
    ledger = {}
    if ledger_path.exists():
        for line in ledger_path.read_text().splitlines():
            item = json.loads(line)
            if item.get("episode_index") == episode:
                ledger = item
                break
    artifact_dir.mkdir(parents=True, exist_ok=True)
    evidence = []
    for camera in context.cameras or [None]:
        cap = None
        offset = 0.0
        if camera:
            relative, offset = video_relative(state, episode, camera)
            cap = cv2.VideoCapture(str(backend().inside(relative, root)))
            if not cap.isOpened():
                raise ValueError("Cannot decode the selected video")
            from urllib.parse import quote

            from .video_evidence import frame_index

            video_sha = file_hash(backend().inside(relative, root))
            pts = frame_index(
                backend().inside(relative, root),
                artifact_dir
                / f"episode_{episode:06d}--{quote(camera, safe='._-')}--video-index.json",
            )
        try:
            for position in positions:
                row = table.iloc[position]
                timestamp = float(row.timestamp)
                frame = int(row.frame_index)
                from urllib.parse import quote

                evidence_id = f"episode_{episode:06d}--{quote(camera or 'table', safe='._-')}--frame_{frame:06d}"
                artifact = None
                size = None
                sha = digest({"frame": frame, "timestamp": timestamp})
                decoded_time = None
                decoded_frame = None
                if cap:
                    from .video_evidence import locate

                    decoded_frame, decoded_time = locate(
                        pts,
                        offset + timestamp,
                        context.workflow["boundary_tolerance_seconds"],
                    )
                    cache_path = artifact_dir / (evidence_id + ".json")
                    image_path = artifact_dir / (evidence_id + ".png")
                    cache_key = digest(
                        {
                            "video": video_sha,
                            "frame": decoded_frame,
                            "timestamp": timestamp,
                            "preprocess": "native-png-v1",
                        }
                    )
                    if cache_path.is_file() and image_path.is_file():
                        cached = json.loads(cache_path.read_text())
                        if (
                            cached["key"] == cache_key
                            and file_hash(image_path) == cached["row"]["sha256"]
                        ):
                            evidence.append(cached["row"])
                            continue
                    cap.set(cv2.CAP_PROP_POS_FRAMES, decoded_frame)
                    ok, image = cap.read()
                    if (
                        not ok
                        or abs(cap.get(cv2.CAP_PROP_POS_FRAMES) - 1 - decoded_frame)
                        > 0.5
                    ):
                        raise ValueError("Evidence frame decoding/seek failed")
                    size = [int(image.shape[0]), int(image.shape[1])]
                    # Lossless evidence; native coordinates are never resized.
                    ok, encoded = cv2.imencode(".png", image)
                    if not ok:
                        raise ValueError("Evidence PNG encoding failed")
                    data = encoded.tobytes()
                    used = sum(
                        p.stat().st_size for p in artifact_dir.iterdir() if p.is_file()
                    )
                    if used + len(data) > context.budget.max_artifact_bytes:
                        raise ValueError(
                            "Evidence artifacts exceed the approved storage budget"
                        )
                    artifact = evidence_id + ".png"
                    (artifact_dir / artifact).write_bytes(data)
                    sha = hashlib.sha256(data).hexdigest()
                evidence.append(
                    EvidenceRef(
                        id=evidence_id,
                        episode_index=episode,
                        camera_key=camera,
                        frame_index=frame,
                        timestamp=timestamp,
                        video_timestamp=offset + timestamp if camera else None,
                        artifact=artifact,
                        sha256=sha,
                        source_size=size,
                        decoded_video_timestamp=decoded_time,
                        decoded_video_frame=decoded_frame,
                        temporal_error_seconds=abs(decoded_time - offset - timestamp)
                        if decoded_time is not None
                        else None,
                        source_timestamp=(
                            ledger.get("source_capture_timestamps")
                            or [None] * len(table)
                        )[position],
                        source_frame_id=(
                            ledger.get("source_frame_ids") or [None] * len(table)
                        )[position],
                    ).model_dump()
                )
                if cap:
                    cache_path.write_text(
                        json.dumps({"key": cache_key, "row": evidence[-1]})
                    )
        finally:
            if cap:
                cap.release()
    # Model sees small summaries, never arbitrary local paths or raw video blobs.
    summary = {
        "episode_index": episode,
        "frames": len(table),
        "start": float(table.timestamp.min()),
        "end": float(table.timestamp.max()),
        "tasks": state.episodes_df[state.episodes_df.episode_index == episode]
        .iloc[0]
        .get("tasks", []),
    }
    if hasattr(summary["tasks"], "tolist"):
        summary["tasks"] = summary["tasks"].tolist()
    return summary, evidence
