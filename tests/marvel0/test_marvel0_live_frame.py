"""The live camera frame of the hosted run (``levi/marvel0/live_frame.py``,
``Supervisor.frame``, ``GET /run/frame``): the last complete JPEG from the tail
of the raw video file the client writes while an episode runs, the episode
folder it is picked from, and every folder or file that is refused. Fixtures
are temporary folders with hand-made files; no camera, robot, port or real
rollout folder is touched."""

import os
import struct
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_marvel0_run import (
    GROUP,
    TASK,
    make_machine,  # noqa: F401 - the fixture
)

from levi.marvel0 import live_frame, run_api

BASE = "/api/levi/marvel0"
OLD = 3600.0  # seconds before the run began


def jpeg(seed: int, size: int = 2000) -> bytes:
    """A hand-made JPEG-shaped frame: start of image, a marker, entropy-like
    bytes with no ``FF`` in them (as in a real one, where ``FF`` is followed by
    ``00``), end of image."""
    body = bytes([(seed + i) % 250 for i in range(size)])
    return b"\xff\xd8\xff\xe0" + body + b"\xff\xd9"


def avi(*frames: bytes, tail: bytes = b"") -> bytes:
    """A growing MJPG AVI as OpenCV writes it: a header, then one ``00dc``
    chunk per frame (word aligned); ``tail`` is a frame being written."""
    out = bytearray(b"RIFF\x00\x00\x00\x00AVI LIST" + b"\x00" * 200 + b"movi")
    for frame in frames:
        out += b"00dc" + struct.pack("<I", len(frame)) + frame
        out += b"\x00" * (len(frame) % 2)
    return bytes(out) + tail


# --- the tail of a file ---------------------------------------------------------------------


def test_the_last_complete_frame_is_returned(tmp_path):
    path = tmp_path / "side_camera_raw.avi"
    path.write_bytes(avi(jpeg(1), jpeg(2), jpeg(3)))
    assert live_frame.last_jpeg(path) == jpeg(3)


def test_a_frame_cut_short_gives_the_one_before_it(tmp_path):
    path = tmp_path / "side_camera_raw.avi"
    cut = jpeg(9)
    for keep in (4, 12, 600, len(cut) - 2, len(cut) - 1):
        path.write_bytes(avi(jpeg(1), jpeg(2), tail=b"00dc" + b"\x00" * 4 + cut[:keep]))
        assert live_frame.last_jpeg(path) == jpeg(2), keep
    path.write_bytes(avi(jpeg(1), jpeg(2), tail=b"00dc" + b"\x00" * 4 + cut))
    assert live_frame.last_jpeg(path) == jpeg(9)


def test_only_the_tail_is_read(tmp_path):
    path = tmp_path / "side_camera_raw.avi"
    frames = [jpeg(n, 60_000) for n in range(40)]  # 2.4 MB, over twice the window
    path.write_bytes(avi(*frames))
    assert path.stat().st_size > 2 * live_frame.TAIL_BYTES
    assert live_frame.last_jpeg(path) == frames[-1]
    # a window that holds only the middle of the last frame has no start of it
    assert live_frame.last_jpeg(path, tail_bytes=30_000) is None


@pytest.mark.parametrize(
    "content",
    [b"", b"RIFF" + b"\x00" * 300, avi(), avi(tail=jpeg(1)[:-2]), b"\xff\xd9" * 50],
    ids=["empty", "header only", "no frame", "first frame unfinished", "ends only"],
)
def test_no_complete_frame_is_none(tmp_path, content):
    path = tmp_path / "side_camera_raw.avi"
    path.write_bytes(content)
    assert live_frame.last_jpeg(path) is None


def test_something_too_small_or_too_large_is_not_a_camera_frame(tmp_path):
    path = tmp_path / "side_camera_raw.avi"
    path.write_bytes(avi(jpeg(1), jpeg(2, size=100)))
    assert live_frame.last_jpeg(path) is None
    path.write_bytes(avi(jpeg(1, 5000)))
    assert live_frame.last_jpeg(path, max_bytes=4000) is None
    assert live_frame.last_jpeg(path, max_bytes=6000) == jpeg(1, 5000)


def test_a_link_a_folder_and_a_missing_file_give_none(tmp_path):
    real = tmp_path / "real.avi"
    real.write_bytes(avi(jpeg(1)))
    link = tmp_path / "side_camera_raw.avi"
    link.symlink_to(real)
    assert live_frame.last_jpeg(real) == jpeg(1)
    assert live_frame.last_jpeg(link) is None
    assert live_frame.last_jpeg(tmp_path) is None
    assert live_frame.last_jpeg(tmp_path / "missing.avi") is None


# --- which episode ----------------------------------------------------------------------------


def started(m):
    """A run that is on (the scripted client) and a clock a few seconds past its
    start, so a file written 'now' is newer than the run."""
    m.start()
    assert m.record()["phase"] in ("intent", "started")
    m.clock.advance(10)


def episode(
    m,
    name="demo_0001",
    *,
    task=TASK,
    group=GROUP,
    cameras=("side", "wrist"),
    seed=1,
    age=0.3,
    complete=False,
    tail=b"",
):
    """An episode folder as the client leaves it while it records: a raw video
    per camera whose last write was ``age`` seconds ago."""
    folder = Path(m.site.rollout_root) / group / task / name
    folder.mkdir(parents=True, exist_ok=True)
    for index, camera in enumerate(cameras):
        path = folder / f"{camera}_camera_raw.avi"
        path.write_bytes(
            avi(jpeg(seed + 10 * index), jpeg(seed + 10 * index + 1), tail=tail)
        )
        stamp = m.host.now() - age
        os.utime(path, (stamp, stamp))
    if complete:
        (folder / ".complete").write_bytes(b"")
    return folder


def reason(call, *args):
    with pytest.raises(live_frame.NoFrame) as caught:
        call(*args)
    return caught.value.reason


def test_without_a_run_there_is_no_picture_whatever_is_on_disk(machine):
    episode(machine)
    assert reason(machine.sup.frame, "side") == "no_run"


def test_a_run_that_ended_has_no_picture_either(machine):
    started(machine)
    episode(machine)
    assert machine.sup.frame("side").episode == "demo_0001"
    machine.sup._update(phase="ended")
    assert reason(machine.sup.frame, "side") == "no_run"


def test_each_camera_gets_its_own_newest_frame(machine):
    started(machine)
    episode(machine, seed=1, age=0.25)
    side, wrist = machine.sup.frame("side"), machine.sup.frame("wrist")
    assert side.jpeg == jpeg(2) and wrist.jpeg == jpeg(12)
    assert side.episode == wrist.episode == "demo_0001"
    assert side.age_s == pytest.approx(0.25, abs=0.01)


def test_the_recording_with_the_newest_write_is_the_episode(machine):
    started(machine)
    episode(machine, "demo_0001", age=9.0, seed=1)
    episode(machine, "demo_0002", age=0.2, seed=21)
    episode(machine, "demo_0003", age=4.0, seed=41)
    found = machine.sup.frame("side")
    assert (found.episode, found.jpeg) == ("demo_0002", jpeg(22))


def test_the_task_folder_may_change_between_episodes(machine):
    started(machine)
    episode(machine, "demo_0001", task=TASK, age=8.0, seed=1)
    episode(machine, "demo_0001", task="stack_the_plates", age=0.2, seed=51)
    found = machine.sup.frame("wrist")
    assert found.jpeg == jpeg(62)


def test_a_finished_episode_is_not_a_picture(machine):
    started(machine)
    episode(machine, "demo_0001", complete=True, age=0.1, seed=1)
    assert reason(machine.sup.frame, "side") == "no_episode"
    episode(machine, "demo_0002", age=5.0, seed=21)
    assert machine.sup.frame("side").episode == "demo_0002"


def test_another_group_is_not_this_run(machine):
    started(machine)
    episode(machine, group=f"{GROUP}_other", age=0.1)
    assert reason(machine.sup.frame, "side") == "no_episode"


def test_a_folder_that_is_not_an_episode_is_ignored(machine):
    started(machine)
    for name in ("incomplete_0001", "discarded_0002", "demo_x", "demo_0003.bak"):
        episode(machine, name, age=0.1)
    assert reason(machine.sup.frame, "side") == "no_episode"


def test_a_recording_older_than_the_run_is_not_this_run(machine):
    machine.start()
    began = machine.record()["created_epoch"]
    machine.clock.advance(10)
    folder = episode(machine, age=0.1)
    old = began - OLD
    for path in folder.glob("*.avi"):
        os.utime(path, (old, old))
    assert reason(machine.sup.frame, "side") == "no_episode"


def test_an_episode_with_no_complete_frame_yet(machine):
    started(machine)
    folder = episode(machine, age=0.1)
    (folder / "side_camera_raw.avi").write_bytes(avi(tail=jpeg(1)[:300]))
    assert reason(machine.sup.frame, "side") == "no_frame"
    assert machine.sup.frame("wrist").episode == "demo_0001"


def test_a_frame_being_written_is_not_returned(machine):
    started(machine)
    episode(machine, seed=1, tail=b"00dc\x00\x00\x00\x00" + jpeg(99)[:700])
    assert machine.sup.frame("side").jpeg == jpeg(2)


def test_links_are_refused_at_every_level(machine, tmp_path):
    started(machine)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    root = Path(machine.site.rollout_root)
    fresh = machine.host.now() - 0.05  # newer than anything else: it would win

    def raw_in(folder: Path, camera: str) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{camera}_camera_raw.avi"
        path.write_bytes(avi(jpeg(5), jpeg(6)))
        os.utime(path, (fresh, fresh))
        return path

    # a linked raw file
    real = episode(machine, "demo_0007", age=0.1, seed=71)
    (real / "side_camera_raw.avi").unlink()
    (real / "side_camera_raw.avi").symlink_to(raw_in(outside / "a", "side"))
    assert reason(machine.sup.frame, "side") == "no_episode"
    # a linked episode folder
    raw_in(outside / "demo_0008", "wrist")
    (root / GROUP / TASK / "demo_0008").symlink_to(outside / "demo_0008")
    assert machine.sup.frame("wrist").episode == "demo_0007"
    # a linked task folder
    raw_in(outside / "tasks" / "demo_0009", "wrist")
    (root / GROUP / "linked_task").symlink_to(outside / "tasks")
    assert machine.sup.frame("wrist").episode == "demo_0007"


def test_a_linked_group_folder_stays_inside_what_it_points_at(machine, tmp_path):
    """The group folder itself may be a link (the anchor is its real path); the
    episode below it still may not leave it."""
    started(machine)
    root = Path(machine.site.rollout_root)
    target = tmp_path / "moved_group"
    (target / TASK / "demo_0001").mkdir(parents=True)
    path = target / TASK / "demo_0001" / "side_camera_raw.avi"
    path.write_bytes(avi(jpeg(1), jpeg(2)))
    stamp = machine.host.now() - 0.1
    os.utime(path, (stamp, stamp))
    (root / GROUP).symlink_to(target)
    assert machine.sup.frame("side").jpeg == jpeg(2)


@pytest.mark.parametrize("group", ["", ".", "..", "a/b", "../x", None, 3])
def test_a_group_that_is_not_one_folder_name_is_refused(tmp_path, group):
    (tmp_path / "x" / TASK / "demo_0001").mkdir(parents=True)
    (tmp_path / "x" / TASK / "demo_0001" / "side_camera_raw.avi").write_bytes(
        avi(jpeg(1))
    )
    assert reason(live_frame.newest_recording, tmp_path, group, "side") == "no_episode"


def test_a_camera_that_is_not_one_of_the_two_is_refused(machine):
    started(machine)
    episode(machine, cameras=("side", "wrist", "top"))
    assert reason(machine.sup.frame, "top") == "no_episode"


# --- the route ----------------------------------------------------------------------------------


@pytest.fixture
def served(machine, monkeypatch):
    monkeypatch.setattr(run_api, "supervisor", lambda: machine.sup)
    web = FastAPI()
    web.include_router(run_api.router)
    return TestClient(web), machine


def test_the_route_answers_a_jpeg_that_is_never_cached(served):
    client, m = served
    started(m)
    episode(m, "demo_0004", seed=3, age=0.25)
    response = client.get(f"{BASE}/run/frame", params={"camera": "wrist"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-levi-frame-episode"] == "demo_0004"
    assert int(response.headers["x-levi-frame-age-ms"]) == pytest.approx(250, abs=10)
    assert response.content == jpeg(14)
    side = client.get(f"{BASE}/run/frame", params={"camera": "side"})
    assert side.content == jpeg(4) and side.status_code == 200
    assert m.spawned and len(m.signals) == 0, "nothing was signalled"
    assert str(m.tmp) not in response.headers.get("x-levi-frame-episode", "")


@pytest.mark.parametrize(
    "setup, code",
    [
        ("idle", "no_run"),
        ("running", "no_episode"),
        ("empty", "no_frame"),
    ],
)
def test_no_picture_is_a_404_that_names_why(served, setup, code):
    client, m = served
    if setup != "idle":
        started(m)
    if setup == "empty":
        folder = episode(m, age=0.1)
        (folder / "side_camera_raw.avi").write_bytes(avi())
    response = client.get(f"{BASE}/run/frame", params={"camera": "side"})
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["detail"]["code"] == code
    assert str(m.tmp) not in response.text


@pytest.mark.parametrize("params", [{"camera": "front"}, {"camera": ""}, {}])
def test_a_camera_that_is_not_side_or_wrist_is_rejected(served, params):
    client, m = served
    started(m)
    episode(m)
    response = client.get(f"{BASE}/run/frame", params=params)
    assert response.status_code == 422


def test_the_route_is_a_read_that_needs_no_person(served):
    """Like the log: a Bearer credential may read it (it is not ``person_only``)."""
    client, m = served
    started(m)
    episode(m)
    response = client.get(
        f"{BASE}/run/frame",
        params={"camera": "side"},
        headers={"authorization": "Bearer not-an-agent-credential"},
    )
    assert response.status_code == 200
