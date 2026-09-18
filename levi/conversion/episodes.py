"""Format-neutral episode records passed between inputs, pipeline and outputs.

Plain, picklable dataclasses: the pipeline ships them to worker processes.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SourceEpisode:
    # Path relative to the source root (e.g. "task/demo_0042"); the stable
    # identity used for provenance and for carrying annotations over.
    source_id: str
    path: str
    task: str
    outcome: str | None = None
    frames: int | None = None
    # camera name (as in Options.cameras) -> media path (file or image dir)
    cameras: dict[str, str] = field(default_factory=dict)
    camera_fps: dict[str, float] = field(default_factory=dict)
    image_mode: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class EpisodePlan:
    episode: SourceEpisode
    index: int
    # 0-based source rows kept in the output, in order.
    positions: list[int]
    fps: float
    # "encode": decode source + encode selected frames; "remux": every frame
    # kept, stream-copied and retimed (no decode, no encode).
    video_mode: str = "encode"
