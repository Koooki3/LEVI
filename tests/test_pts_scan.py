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
    command = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i"]
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


@pytest.fixture(autouse=True)
def fresh_memo():
    ve._MEMO.clear()
    yield
    ve._MEMO.clear()


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


def test_the_default_scan_is_the_packet_scan_and_the_cache_records_it(
    bframes, tmp_path, monkeypatch
):
    monkeypatch.delenv("LEVI_PTS_SCAN", raising=False)
    ve._MEMO.clear()
    assert ve.scan_mode() == "packet"
    calls = []
    real = ve.scan_frames
    monkeypatch.setattr(ve, "scan_frames", lambda p: calls.append(p) or real(p))
    cache = tmp_path / "idx.json"
    times = ve.frame_index(bframes, cache)
    assert calls == []  # the frame scan did not run
    assert json.loads(cache.read_text()) == {
        "source_sha256": file_hash(bframes),
        "timestamps": times,
        "scan": "packet",
    }


def test_the_frame_scan_is_still_selectable_with_the_original_cache_layout(
    bframes, tmp_path, monkeypatch
):
    monkeypatch.setenv("LEVI_PTS_SCAN", "frame")
    ve._MEMO.clear()
    assert ve.scan_mode() == "frame"
    cache = tmp_path / "idx.json"
    times = ve.frame_index(bframes, cache)
    assert json.loads(cache.read_text()) == {
        "source_sha256": file_hash(bframes),
        "timestamps": times,
    }
    # A cache written before the switch (no "scan" key) is a frame-scan cache:
    # the default now rescans once, then keeps the packet layout.
    monkeypatch.delenv("LEVI_PTS_SCAN")
    ve._MEMO.clear()
    assert ve.frame_index(bframes, cache) == times
    assert json.loads(cache.read_text())["scan"] == "packet"


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


@pytest.mark.parametrize(
    "packets, stderr, why",
    [
        ("0.0,K__\nN/A,___\n0.2,___\n", "", "no presentation timestamp"),
        ("0.1,K__\n0.1,___\n0.2,___\n", "", "not strictly increasing"),
        ("", "", "no packets"),
        ("0.0,K__\n0.1,__C\n", "", "discard or corrupt"),
        ("0.0,KD_\n0.1,___\n", "", "discard or corrupt"),
        ("-0.1,K__\n0.1,___\n", "", "negative"),
        ("0.0,K__\n0.1,___\n", "Invalid NAL unit size\n", "ffprobe reported"),
    ],
)
def test_packets_that_cannot_stand_in_fall_back_to_the_frame_scan(
    bframes, monkeypatch, caplog, packets, stderr, why
):
    real = ve._ffprobe

    def fake(path, entries, fmt):
        if entries.startswith("packet"):
            return packets, stderr
        return real(path, entries, fmt)

    monkeypatch.setattr(ve, "_ffprobe", fake)
    with caplog.at_level("WARNING", logger=ve.log.name):
        assert ve.scan_packets(bframes) is None
        assert ve.scan_times(bframes, "packet") == ve.scan_frames(bframes)
    assert why in caplog.text and bframes.name in caplog.text


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


# --- containers where packets and shown frames differ --------------------------


def outcome(path, mode):
    """What a scan gives: its list, or the kind of error it ends in."""
    try:
        return "ok", ve.scan_times(path, mode)
    except (ValueError, subprocess.SubprocessError) as error:
        return "error", type(error).__name__


@pytest.fixture(scope="module")
def odd(tmp_path_factory):
    """The four cases a review found: an edit list, a negative start, a
    truncated file and a damaged one."""
    folder = tmp_path_factory.mktemp("odd")
    src = encode(
        folder / "src.mp4",
        "-movflags",
        "+faststart",
        params="bframes=3:keyint=25",
        frames=100,
    )

    def run(*command):
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", *command], check=True
        )

    run("-ss", "1.35", "-i", str(src), "-c", "copy", str(folder / "cut.mp4"))
    run(
        "-i",
        str(src),
        "-c",
        "copy",
        "-output_ts_offset",
        "-0.5",
        str(folder / "neg.mp4"),
    )
    data = src.read_bytes()
    (folder / "trunc.mp4").write_bytes(data[:9000])
    damaged = bytearray(data)
    middle = len(damaged) // 2
    for i in range(middle, middle + 4000):
        damaged[i] ^= 0xA5
    (folder / "corrupt.mp4").write_bytes(bytes(damaged))
    return {p.stem: p for p in folder.glob("*.mp4")}


def decoded_frames(path):
    import cv2

    cap = cv2.VideoCapture(str(path))
    count = 0
    while cap.grab():
        count += 1
    cap.release()
    return count


@pytest.mark.parametrize("name", ["cut", "neg", "trunc", "corrupt"])
def test_packet_mode_gives_the_frame_modes_answer_on_odd_containers(odd, name):
    path = odd[name]
    assert ve.packet_times(path)[0] is None  # none of them may use the packets
    assert outcome(path, "packet") == outcome(path, "frame")


@pytest.mark.parametrize("name", ["cut", "neg"])
def test_the_index_has_one_entry_per_decoded_frame(odd, name):
    times = ve.scan_times(odd[name], "packet")
    assert len(times) == decoded_frames(odd[name]) and times[0] >= 0


@pytest.mark.parametrize("name", ["cut", "neg", "trunc", "corrupt"])
def test_a_requested_time_gets_the_same_frame_or_the_same_refusal(odd, tmp_path, name):
    def pick(mode):
        try:
            times = ve.frame_index(
                odd[name], tmp_path / f"{name}-{mode}.json", mode=mode
            )
            return ve.locate(times, 0.5, 0.05)
        except ValueError as error:
            return type(error).__name__

    ve._MEMO.clear()
    frame = pick("frame")
    ve._MEMO.clear()
    assert pick("packet") == frame


def test_a_rewritten_file_is_scanned_again_in_the_same_mode(tmp_path):
    path = tmp_path / "v.mp4"
    encode(path, frames=20)
    first = ve.frame_index(path, tmp_path / "a.json", mode="packet")
    encode(path, frames=30)
    second = ve.frame_index(path, tmp_path / "b.json", mode="packet")
    assert len(first) == 20 and len(second) == 30
