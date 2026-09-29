"""Which video file, first frame and length hold one episode's camera.

Workers are handed explicit paths so they never guess a dataset layout.
LeRobot v2.x keeps one file per episode; v3.x shards hold several episodes,
so the episode starts ``from_timestamp`` seconds into the shared file. The
same rules as the SAM3 worker's resolver (integrations/sam3), kept here so
LEVI's core never imports a worker package.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class EpisodeVideo:
    episode_index: int
    camera_key: str
    video: Path
    start_frame: int
    length: int
    fps: float

    def plan(self) -> dict[str, Any]:
        return {
            "episode_index": self.episode_index,
            "camera_key": self.camera_key,
            "video": str(self.video),
            "start_frame": self.start_frame,
            "length": self.length,
            "fps": self.fps,
        }


def episode_metadata(root: Path, episode_index: int) -> dict[str, Any]:
    """One v2 JSONL or v3 Parquet episode metadata row ({} when absent)."""
    jsonl = root / "meta" / "episodes.jsonl"
    if jsonl.is_file():
        for line in jsonl.read_text().splitlines():
            if line.strip():
                value = json.loads(line)
                if int(value.get("episode_index", -1)) == episode_index:
                    return value
    folder = root / "meta" / "episodes"
    if folder.is_dir():
        import pyarrow.parquet as pq

        for path in sorted(folder.glob("**/*.parquet")):
            table = pq.read_table(path)
            if "episode_index" not in table.column_names:
                continue
            indices = table.column("episode_index").to_pylist()
            if episode_index in indices:
                return table.slice(indices.index(episode_index), 1).to_pylist()[0]
    return {}


def camera_keys(info: dict[str, Any]) -> list[str]:
    features = info.get("features") or {}
    return sorted(
        key
        for key, value in features.items()
        if key.startswith("observation.images.") and (value or {}).get("dtype") == "video"
    )


def resolve(root: Path, info: dict[str, Any], episode_index: int, camera_key: str) -> EpisodeVideo:
    episode = episode_metadata(root, episode_index)
    fps = float(info.get("fps") or 30)
    prefix = f"videos/{camera_key}"
    start_s = float(episode.get(f"{prefix}/from_timestamp", episode.get("video_from_timestamp", 0)) or 0)
    video = None
    template = info.get("video_path")
    if template:
        default_chunk = episode_index // int(info.get("chunks_size", 1000) or 1000)
        # A v3 ``video_path`` ("videos/{video_key}/chunk-{chunk_index:03d}/
        # file-{file_index:03d}.mp4") takes this camera's own chunk/file
        # indices: video and data files roll over independently, so the
        # data file's indices point at another episode's video.
        video_chunk = episode.get(f"{prefix}/chunk_index", episode.get("video_chunk_index"))
        video_file = episode.get(f"{prefix}/file_index", episode.get("video_file_index"))
        data_chunk = episode.get("data/chunk_index", episode.get("chunk_index", 0))
        data_file = episode.get("data/file_index", episode.get("file_index", 0))
        values = {
            "episode_index": episode_index,
            "episode_chunk": episode.get("episode_chunk", default_chunk),
            "chunk_index": data_chunk if video_chunk is None else video_chunk,
            "file_index": data_file if video_file is None else video_file,
            "video_chunk": default_chunk if video_chunk is None else video_chunk,
            "video_chunk_index": default_chunk if video_chunk is None else video_chunk,
            "video_file_index": 0 if video_file is None else video_file,
            "video_key": camera_key,
        }
        try:
            candidate = root / str(template).format(**values)
            if candidate.is_file():
                video = candidate
        except (KeyError, ValueError, IndexError):
            pass
    if video is None:
        names = {camera_key, camera_key.replace(".", "_"), camera_key.split(".")[-1]}
        candidates = sorted(root.glob(f"videos/**/episode_{episode_index:06d}.mp4"))
        candidates += sorted(root.glob(f"videos/**/*{episode_index:06d}*.mp4"))
        for candidate in dict.fromkeys(candidates):
            if any(name in candidate.as_posix() for name in names):
                video = candidate
                break
    if video is None:
        raise FileNotFoundError(
            f"No video found for episode {episode_index}, camera {camera_key!r}; "
            "check meta/info.json video_path and the camera feature"
        )
    length = int(episode.get("length", 0) or 0)
    if length <= 0:
        end_s = float(episode.get(f"{prefix}/to_timestamp", episode.get("video_to_timestamp", 0)) or 0)
        if end_s > start_s:
            length = max(0, round((end_s - start_s) * fps))
    return EpisodeVideo(
        episode_index=episode_index,
        camera_key=camera_key,
        # Keep symlinked capture folders as they are: resolving would leave
        # the workspace for raw captures linked in from elsewhere.
        video=video,
        start_frame=max(0, round(start_s * fps)),
        length=length,
        fps=fps,
    )
