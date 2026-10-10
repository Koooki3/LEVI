"""How often LEVI hashes a source video, and that no hash that guards the
immutability of the evidence can be skipped. A count that goes down is only
acceptable if the matching tamper test still fails."""

import shutil
import subprocess
from types import SimpleNamespace

import pytest

from levi.agent import media
from levi.agent import video_evidence as ve
from levi.agent.store import file_hash


@pytest.fixture
def counted(monkeypatch):
    """Every ``file_hash`` call in the two modules, by file name."""
    calls = []

    def counting(path):
        calls.append(path.name if hasattr(path, "name") else str(path))
        return file_hash(path)

    monkeypatch.setattr(media, "file_hash", counting)
    monkeypatch.setattr(ve, "file_hash", counting)
    return calls


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "src"
    (root / "videos").mkdir(parents=True)
    (root / "videos/a.bin").write_bytes(b"alpha" * 100)
    (root / "videos/b.bin").write_bytes(b"beta" * 100)
    return root


@pytest.fixture
def snap(monkeypatch, source):
    files = {
        p.relative_to(source).as_posix(): p.stat().st_size
        for p in source.rglob("*.bin")
    }
    state = SimpleNamespace(root=source, repo_id=None)
    monkeypatch.setattr(media, "inspect", lambda context: (state, files))
    context = SimpleNamespace(budget=SimpleNamespace(max_snapshot_bytes=10**9))
    return context, files


def test_a_snapshot_hashes_each_file_three_times_and_no_more(snap, tmp_path, counted):
    context, files = snap
    manifest = media.snapshot(context, tmp_path / "dest/input", files)
    assert set(manifest["files"]) == set(files)
    # source before the copy, the copy, source after the copy: the bracket that
    # proves the bytes did not move while they were copied.
    assert sorted(counted) == ["a.bin"] * 3 + ["b.bin"] * 3


def test_a_planned_hash_that_no_longer_matches_stops_before_any_copy(
    snap, tmp_path, counted
):
    context, files = snap
    planned = {name: "0" * 64 for name in files}
    with pytest.raises(ValueError, match="content changed"):
        media.snapshot(context, tmp_path / "dest/input", files, planned)
    assert counted == ["a.bin"]
    assert not list((tmp_path / "dest").rglob("*.bin"))


def test_a_source_that_changes_while_it_is_copied_fails(
    snap, tmp_path, source, monkeypatch
):
    context, files = snap
    real = shutil.copyfile

    def copy_then_touch(src, dst, **kw):
        out = real(src, dst, **kw)
        # Same size, different bytes, after the pre-copy hash.
        (source / "videos/a.bin").write_bytes(b"ALPHA" * 100)
        return out

    monkeypatch.setattr(media.shutil, "copyfile", copy_then_touch)
    with pytest.raises(ValueError, match="changed during snapshot"):
        media.snapshot(context, tmp_path / "dest/input", files)


def test_a_copy_that_is_not_the_source_fails(snap, tmp_path, monkeypatch):
    context, files = snap

    def bad_copy(src, dst, **kw):
        with open(dst, "wb") as handle:
            handle.write(b"x" * 500)

    monkeypatch.setattr(media.shutil, "copyfile", bad_copy)
    with pytest.raises(ValueError, match="changed during snapshot"):
        media.snapshot(context, tmp_path / "dest/input", files)


def test_the_source_check_before_commit_hashes_every_file_and_fails_on_a_change(
    monkeypatch, source, counted
):
    state = SimpleNamespace(root=source)
    monkeypatch.setattr(media, "state_for", lambda context: state)
    manifest = {
        "files": {
            "videos/a.bin": file_hash(source / "videos/a.bin"),
            "videos/b.bin": file_hash(source / "videos/b.bin"),
        }
    }
    counted.clear()
    media.verify_source(None, manifest)
    assert sorted(counted) == ["a.bin", "b.bin"]
    (source / "videos/b.bin").write_bytes(b"beta!" * 100)
    with pytest.raises(ValueError, match="Source changed"):
        media.verify_source(None, manifest)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is needed")
def test_sampling_hashes_a_video_once_per_camera_and_each_cached_frame_once(
    dataset, tmp_path, counted
):
    from levi.agent.schema import TaskContext

    camera = "observation.images.front"
    info_path = dataset / "meta/info.json"
    import json

    info = json.loads(info_path.read_text())
    info["features"][camera] = {"dtype": "video", "shape": [48, 64, 3]}
    info_path.write_text(json.dumps(info))
    folder = dataset / f"videos/chunk-000/{camera}"
    folder.mkdir(parents=True)
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
         "testsrc2=size=64x48:rate=10:duration=2", "-c:v", "libx264",
         "-threads", "1", "-pix_fmt", "yuv420p", str(folder / "episode_000000.mp4")],
        check=True,
    )  # fmt: skip
    context = TaskContext(
        repo_id="local/fixture",
        episodes=[0],
        instruction="x",
        provider="fixture",
        cameras=[camera],
    )
    artifacts = tmp_path / "art"
    _, first = media.sample(context, dataset, 0, artifacts)
    # One hash of the video; the PTS index reuses it instead of hashing again.
    assert counted.count("episode_000000.mp4") == 1
    counted.clear()
    _, second = media.sample(context, dataset, 0, artifacts)
    assert first == second
    # The cache is checked, not trusted: the video once, every cached PNG once.
    assert counted.count("episode_000000.mp4") == 1
    assert sum(name.endswith(".png") for name in counted) == len(first)
