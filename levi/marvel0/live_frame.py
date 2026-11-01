"""The newest camera frame of the episode the hosted evaluation client is
recording (``GET /api/levi/marvel0/run/frame``), read from files only.

While an episode runs, the client owns both RealSense cameras, so nothing else
can open them. It does write every frame it executes into
``<rollout root>/<group>/<task folder>/demo_NNNN/{wrist,side}_camera_raw.avi``
(``cv2.VideoWriter``, fourcc MJPG, about 10 frames a second, encoded off its
control thread). At the episode's end it turns them into mp4 files, deletes the
raw ones and creates an empty ``.complete``. An MJPG frame is a complete JPEG,
so the tail of the growing file holds the newest picture; this module returns
it. It opens no camera, connects to no port, signals nothing and writes
nothing; the client and its control loop are not touched.

Everything it opens is a regular file below the group folder. The rollout root
and the group folder themselves are resolved (``realpath``); a link below the
group folder (task folder, episode folder, raw file) is refused (``lstat`` while
walking, ``O_NOFOLLOW`` on the open).
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from levi.setup import probes

CAMERAS = ("side", "wrist")
RAW_NAME = "{camera}_camera_raw.avi"
COMPLETE_MARKER = ".complete"
DEMO = re.compile(r"demo_\d+")

SOI = b"\xff\xd8\xff"  # start of image and the marker that follows it
EOI = b"\xff\xd9"  # end of image
TAIL_BYTES = 1 << 20  # frames are 30-60 KB: a mebibyte holds a score of them
MIN_FRAME_BYTES = 1 << 10
MAX_FRAME_BYTES = 4 << 20

# reason -> English text of the 404 answer. No text holds a path.
REASONS = {
    "no_run": "No evaluation run of this page is active.",
    "no_episode": (
        "No episode is recording: the client writes frames only while an episode runs."
    ),
    "no_frame": "The recording has no complete frame yet.",
}


class NoFrame(Exception):
    """There is no picture to give, for one of ``REASONS``."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason
        self.message = REASONS[reason]


@dataclass(frozen=True)
class Frame:
    jpeg: bytes
    age_s: float  # seconds since the recording file was last written
    episode: str  # the demo folder's name, e.g. ``demo_0003``


def last_jpeg(
    path,
    *,
    tail_bytes: int = TAIL_BYTES,
    min_bytes: int = MIN_FRAME_BYTES,
    max_bytes: int = MAX_FRAME_BYTES,
) -> bytes | None:
    """The last complete JPEG in the last ``tail_bytes`` of the file, or None.

    A frame is complete when its end of image marker is there; the frame being
    written (cut anywhere) has none, so the one before it is returned. The
    start is the last ``FF D8 FF`` before that marker. A result smaller than
    ``min_bytes`` or larger than ``max_bytes`` is not a camera frame. A link, a
    directory, a FIFO or an unreadable file gives None."""
    try:
        data, _, _ = probes.read_regular(path, tail_bytes, tail=True)
    except OSError:  # includes probes.NotRegular, and ELOOP for a link
        return None
    end = data.rfind(EOI)
    if end < 0:
        return None
    start = data.rfind(SOI, 0, end)
    if start < 0:
        return None
    jpeg = data[start : end + len(EOI)]
    if not min_bytes <= len(jpeg) <= max_bytes:
        return None
    return jpeg


def _plain(name) -> bool:
    return isinstance(name, str) and name not in ("", ".", "..") and "/" not in name


def newest_recording(root, group, camera: str, since=None) -> tuple[Path, str, float]:
    """``(raw file, demo folder name, its mtime)`` of the episode being
    recorded: among ``<root>/<group>/*/demo_*/`` the folders without
    ``.complete`` that hold the camera's raw file (a regular file, not a
    link), the one written most recently, ignoring files last written before
    ``since`` (epoch seconds: the run's start, so a folder a killed client left
    earlier is not an episode of this run). The task folder is not fixed: the
    instruction can change between episodes. Raises ``NoFrame``
    (``no_episode``)."""
    if camera not in CAMERAS or not isinstance(root, (str, os.PathLike)):
        raise NoFrame("no_episode")
    if not _plain(group):
        raise NoFrame("no_episode")
    anchor = os.path.realpath(Path(root) / group)
    raw = RAW_NAME.format(camera=camera)
    best: tuple[Path, str, float] | None = None
    try:
        tasks = list(os.scandir(anchor))
    except OSError:
        raise NoFrame("no_episode") from None
    for task in tasks:
        try:
            if not task.is_dir(follow_symlinks=False):
                continue
            demos = list(os.scandir(task.path))
        except OSError:
            continue
        for demo in demos:
            if not DEMO.fullmatch(demo.name):
                continue
            try:
                if not demo.is_dir(follow_symlinks=False):
                    continue
                info = os.lstat(os.path.join(demo.path, raw))
            except OSError:
                continue  # a finished episode has no raw file any more
            if not stat.S_ISREG(info.st_mode):
                continue
            if os.path.lexists(os.path.join(demo.path, COMPLETE_MARKER)):
                continue
            if since is not None and info.st_mtime < since:
                continue
            if best is None or info.st_mtime > best[2]:
                best = (Path(demo.path) / raw, demo.name, info.st_mtime)
    if best is None:
        raise NoFrame("no_episode")
    # By construction every step was a plain directory below the anchor; say so
    # once more in the form the next reader looks for.
    if os.path.commonpath([anchor, os.path.realpath(best[0])]) != anchor:
        raise NoFrame("no_episode")
    return best


def newest_frame(root, group, camera: str, now: float, since=None) -> Frame:
    """The newest complete frame of ``camera`` in the episode being recorded,
    with its age at ``now``. Raises ``NoFrame``: ``no_episode`` or
    ``no_frame``."""
    path, episode, mtime = newest_recording(root, group, camera, since)
    jpeg = last_jpeg(path)
    if jpeg is None:
        raise NoFrame("no_frame")
    return Frame(jpeg, max(0.0, now - mtime), episode)
