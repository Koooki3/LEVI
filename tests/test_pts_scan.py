"""``LEVI_PTS_SCAN``: packet timestamps stand in for the frame scan only when
they give the same list; every other case runs the original scan."""

import json
import shutil
import subprocess

import numpy as np
import pytest

from levi.agent import media
from levi.agent import video_evidence as ve
from levi.agent.schema import TaskContext
from levi.agent.store import file_hash

pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg"), reason="ffmpeg is needed for the fixtures"
)


def encode(path, *extra_in, params="bframes=3", frames=60, vf=None):
    command = ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i"]
    command += [f"testsrc2=size=64x48:rate=10:duration={frames / 10}"]
    command += ["-frames:v", str(frames)]
    if vf:
        command += ["-vf", vf, "-fps_mode", "vfr"]
    command += ["-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p"]
    command += ["-x264-params", params, *extra_in, str(path)]
    subprocess.run(command, check=True, timeout=120)
    return path


def packet_order(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "packet=pts_time", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.split()  # fmt: skip
    return [float(x) for x in out]


@pytest.fixture(scope="module")
def bframes(tmp_path_factory):
    path = encode(
        tmp_path_factory.mktemp("pts") / "b.mp4", params="bframes=4:b-adapt=0"
    )
    # The fixture must really reorder: decode order differs from presentation.
    order = packet_order(path)
    assert order != sorted(order)
    return path


@pytest.fixture(scope="module")
def vfr(tmp_path_factory):
    # Two jumps in presentation time: a variable-frame-rate stream.
    path = encode(
        tmp_path_factory.mktemp("pts") / "v.mp4",
        vf="setpts='N/10/TB+if(gte(N\\,20)\\,0.7\\,0)/TB+if(gte(N\\,40)\\,0.3\\,0)/TB'",
    )
    gaps = np.diff(ve.scan_frames(path))
    assert gaps.max() > 3 * np.median(gaps)
    return path


def test_packet_scan_matches_the_frame_scan_value_for_value(bframes, vfr):
    for path in (bframes, vfr):
        frames = ve.scan_frames(path)
        assert len(frames) == 60
        assert ve.scan_packets(path) == frames
        assert ve.scan_times(path, "packet") == ve.scan_times(path, "frame") == frames


def test_the_default_scan_and_cache_layout_are_the_original(
    bframes, tmp_path, monkeypatch
):
    monkeypatch.delenv("LEVI_PTS_SCAN", raising=False)
    assert ve.scan_mode() == "frame"
    cache = tmp_path / "idx.json"
    times = ve.frame_index(bframes, cache)
    assert json.loads(cache.read_text()) == {
        "source_sha256": file_hash(bframes),
        "timestamps": times,
    }


def test_packet_mode_records_itself_and_a_cache_is_used_for_its_own_scan(
    bframes, tmp_path, monkeypatch
):
    monkeypatch.setenv("LEVI_PTS_SCAN", "packet")
    ve._MEMO.clear()
    cache = tmp_path / "idx.json"
    times = ve.frame_index(bframes, cache)
    assert json.loads(cache.read_text())["scan"] == "packet"
    assert times == ve.scan_frames(bframes)
    # A cache written by the other scan is not trusted: it is recomputed.
    other = tmp_path / "other.json"
    ve.frame_index(bframes, other, mode="frame")
    assert "scan" not in json.loads(other.read_text())
    calls = []
    real = ve.scan_times
    monkeypatch.setattr(ve, "scan_times", lambda p, m: calls.append(m) or real(p, m))
    ve._MEMO.clear()
    assert ve.frame_index(bframes, other) == times
    assert calls == ["packet"]
    assert json.loads(other.read_text())["scan"] == "packet"
    # Its own cache is used without scanning.
    calls.clear()
    assert ve.frame_index(bframes, other) == times and calls == []


def test_a_changed_video_is_never_served_from_a_cache(bframes, tmp_path):
    cache = tmp_path / "idx.json"
    ve.frame_index(bframes, cache, mode="packet")
    saved = json.loads(cache.read_text())
    saved["source_sha256"] = "0" * 64
    cache.write_text(json.dumps(saved))
    ve._MEMO.clear()
    # The stale checksum is ignored; the scan runs and the file is rewritten.
    assert ve.frame_index(bframes, cache, mode="packet") == ve.scan_frames(bframes)
    assert json.loads(cache.read_text())["source_sha256"] == file_hash(bframes)


def test_packets_that_cannot_stand_in_fall_back_to_the_frame_scan(bframes, monkeypatch):
    real = ve._ffprobe

    def fake(path, entries, fmt):
        if entries.startswith("packet"):
            return "0.0\nN/A\n0.2\n"
        return real(path, entries, fmt)

    monkeypatch.setattr(ve, "_ffprobe", fake)
    assert ve.scan_packets(bframes) is None
    assert ve.scan_times(bframes, "packet") == ve.scan_frames(bframes)
    monkeypatch.setattr(
        ve,
        "_ffprobe",
        lambda p, e, f: "0.1\n0.1\n0.2\n" if e.startswith("packet") else real(p, e, f),
    )
    assert ve.scan_packets(bframes) is None  # duplicate timestamps
    monkeypatch.setattr(
        ve, "_ffprobe", lambda p, e, f: "" if e.startswith("packet") else real(p, e, f)
    )
    assert ve.scan_packets(bframes) is None  # no packets


def test_an_unknown_scan_is_refused(monkeypatch):
    monkeypatch.setenv("LEVI_PTS_SCAN", "fast")
    with pytest.raises(ValueError, match="LEVI_PTS_SCAN"):
        ve.scan_mode()


def test_the_scan_runs_once_for_one_video_content_and_again_for_another(
    bframes, vfr, tmp_path, monkeypatch
):
    ve._MEMO.clear()
    calls = []
    real = ve.scan_times
    monkeypatch.setattr(
        ve, "scan_times", lambda p, m: calls.append(p.name) or real(p, m)
    )
    copy = tmp_path / "copy.mp4"
    shutil.copyfile(bframes, copy)
    first = ve.frame_index(bframes, tmp_path / "a.json", mode="packet")
    second = ve.frame_index(copy, tmp_path / "b.json", mode="packet")
    assert first == second and calls == ["b.mp4"]
    assert json.loads((tmp_path / "b.json").read_text())["timestamps"] == first
    ve.frame_index(vfr, tmp_path / "c.json", mode="packet")
    assert calls == ["b.mp4", "v.mp4"]
    second.append(1.0)  # a caller cannot corrupt what the next one gets
    assert ve.frame_index(copy, tmp_path / "d.json", mode="packet") == first


def test_the_scan_memo_is_bounded(bframes, tmp_path, monkeypatch):
    ve._MEMO.clear()
    monkeypatch.setattr(ve, "MEMO_ENTRIES", 2)
    for name in "abc":
        ve.frame_index(bframes, tmp_path / f"{name}.json", checksum=name * 64)
    assert [key[0][0] for key in ve._MEMO] == ["b", "c"]


def test_a_caller_with_the_checksum_does_not_hash_the_video_again(
    bframes, tmp_path, monkeypatch
):
    monkeypatch.setattr(ve, "file_hash", lambda p: pytest.fail("hashed again"))
    sha = file_hash(bframes)
    assert ve.frame_index(bframes, tmp_path / "i.json", checksum=sha, mode="frame")


@pytest.fixture
def v21(dataset, tmp_path):
    """The shared 2-episode fixture dataset with one camera of B-frame video;
    both episodes use the same file content."""
    camera = "observation.images.front"
    info_path = dataset / "meta/info.json"
    info = json.loads(info_path.read_text())
    info["features"][camera] = {"dtype": "video", "shape": [48, 64, 3]}
    info_path.write_text(json.dumps(info))
    folder = dataset / f"videos/chunk-000/{camera}"
    folder.mkdir(parents=True)
    encode(folder / "episode_000000.mp4", frames=20)
    shutil.copyfile(folder / "episode_000000.mp4", folder / "episode_000001.mp4")
    return camera


def test_sampling_gives_the_same_evidence_with_either_scan(
    v21, dataset, tmp_path, monkeypatch
):
    context = TaskContext(
        repo_id="local/fixture",
        episodes=[0, 1],
        instruction="x",
        provider="fixture",
        cameras=[v21],
    )
    rows = {}
    for mode in ("frame", "packet"):
        monkeypatch.setenv("LEVI_PTS_SCAN", mode)
        ve._MEMO.clear()
        scans = []
        real = ve.scan_times
        monkeypatch.setattr(
            ve,
            "scan_times",
            lambda p, m, real=real, scans=scans: scans.append(m) or real(p, m),
        )
        out = []
        for episode in (0, 1):
            _, evidence = media.sample(
                context, dataset, episode, tmp_path / mode / str(episode)
            )
            out.append(
                [{k: v for k, v in e.items() if k != "artifact"} for e in evidence]
            )
        rows[mode] = out
        # Both episodes use one content, so it is scanned once.
        assert scans == [mode]
        monkeypatch.undo()
    assert rows["frame"] == rows["packet"] and rows["frame"][0]


def test_the_comparison_command_reports_each_file(bframes, vfr, tmp_path, capsys):
    from levi.performance import __main__ as cli

    folder = tmp_path / "videos"
    folder.mkdir()
    shutil.copyfile(bframes, folder / "b.mp4")
    shutil.copyfile(vfr, folder / "v.mp4")
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video")
    assert cli.main(["pts-compare", str(folder)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["files"] == 2 and out["all_identical"] and out["different"] == []
    assert cli.main(["pts-compare", str(bframes), str(broken)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["different"] == ["broken.mp4"] and out["rows"][1]["error"]
