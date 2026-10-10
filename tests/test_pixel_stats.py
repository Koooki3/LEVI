"""``LEVI_PIXEL_STATS=histogram`` gives the conversion's pixel statistics from
integer counts: the same numbers as the float method, far faster."""

import json
import os
import shutil
import subprocess
import time

import numpy as np
import pytest

from levi.conversion import media
from levi.performance import __main__ as cli
from levi.performance import bench

TOLERANCE = 1e-9

pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg"), reason="ffmpeg is needed for the fixtures"
)


def encode(path, source, frames):
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", source,
         "-frames:v", str(frames), "-c:v", "libx264", "-threads", "1",
         "-pix_fmt", "yuv420p", str(path)],
        check=True, timeout=120,
    )  # fmt: skip
    return path


@pytest.fixture(scope="module")
def videos(tmp_path_factory):
    folder = tmp_path_factory.mktemp("pixels")
    return {
        "testsrc": encode(folder / "testsrc.mp4", "testsrc2=size=320x240:rate=10", 30),
        "noise": encode(
            folder / "noise.mp4",
            "nullsrc=size=128x96:rate=10,geq=random(1)*255:128:128",
            20,
        ),
        "gradient": encode(
            folder / "gradient.mp4",
            "nullsrc=size=96x64:rate=10,geq=X*255/96:Y*255/64:128",
            20,
        ),
        "flat": encode(folder / "flat.mp4", "color=c=0x808080:size=64x48:rate=10", 20),
    }


def stats_of(path, method, monkeypatch):
    monkeypatch.setenv("LEVI_PIXEL_STATS", method)
    return media.inspect(path, pixels=True)


@pytest.mark.parametrize("name", ["testsrc", "noise", "gradient", "flat"])
def test_histogram_statistics_equal_the_float_ones_element_by_element(
    name, videos, monkeypatch
):
    old = stats_of(videos[name], "float", monkeypatch)
    new = stats_of(videos[name], "histogram", monkeypatch)
    # Everything but the statistics is the same value.
    assert {k: v for k, v in old.items() if k != "stats"} == {
        k: v for k, v in new.items() if k != "stats"
    }
    assert old["stats"].keys() == new["stats"].keys()
    for key in old["stats"]:
        a = np.array(old["stats"][key], dtype=float)
        b = np.array(new["stats"][key], dtype=float)
        assert a.shape == b.shape == ((1,) if key == "count" else (3, 1, 1))
        if key == "std":  # compared as variance: see test_a_constant_channel...
            a, b = a * a, b * b
        assert np.abs(a - b).max() <= TOLERANCE, key


def test_the_default_is_the_histogram_method(videos, monkeypatch):
    monkeypatch.delenv("LEVI_PIXEL_STATS", raising=False)
    assert media.stats_method() == "histogram"
    calls = []
    real = media.PixelHistogram.add
    monkeypatch.setattr(
        media.PixelHistogram,
        "add",
        lambda self, frame: calls.append(1) or real(self, frame),
    )
    assert media.inspect(videos["flat"], pixels=True)["stats"]["count"] == [20]
    assert len(calls) == 20
    assert "stats" not in media.inspect(videos["flat"])  # no pixels asked, none counted


def test_float_is_still_selectable_and_never_builds_histograms(videos, monkeypatch):
    monkeypatch.setenv("LEVI_PIXEL_STATS", "float")
    assert media.stats_method() == "float"
    monkeypatch.setattr(
        media.PixelHistogram, "add", lambda self, frame: pytest.fail("histogram used")
    )
    assert media.inspect(videos["flat"], pixels=True)["stats"]["count"] == [20]


def test_an_unknown_method_is_refused(monkeypatch):
    monkeypatch.setenv("LEVI_PIXEL_STATS", "approx")
    with pytest.raises(ValueError, match="LEVI_PIXEL_STATS"):
        media.stats_method()


def test_counts_give_exact_values_on_known_pixels():
    histogram = media.PixelHistogram()
    frame = np.zeros((2, 2, 3), dtype=np.uint8)  # BGR
    frame[..., 0], frame[..., 1], frame[..., 2] = 51, 255, 0
    frame[0, 0] = (255, 0, 102)
    histogram.add(frame)
    lo, hi, mean, std, pixels = histogram.result()
    assert pixels == 4
    # Reported as RGB: red is the BGR frame's last channel.
    assert lo.tolist() == [0, 0, 51 / 255] and hi.tolist() == [102 / 255, 1, 1]
    assert mean[0] == pytest.approx(102 / 255 / 4) and mean[1] == pytest.approx(0.75)
    assert std[2] == pytest.approx(
        np.std([1, 51 / 255, 51 / 255, 51 / 255] * np.ones(4))
    )
    assert media.PixelHistogram().counts.dtype == np.int64


def test_both_counting_paths_agree(monkeypatch):
    rng = np.random.default_rng(3)
    frames = [rng.integers(0, 256, (37, 53, 3), dtype=np.uint8) for _ in range(3)]
    results = []
    for limit in (media.EXACT_HIST_PIXELS, 0):  # calcHist, then bincount
        monkeypatch.setattr(media, "EXACT_HIST_PIXELS", limit)
        histogram = media.PixelHistogram()
        for frame in frames:
            histogram.add(frame)
        results.append(histogram.counts.copy())
    assert np.array_equal(results[0], results[1])
    assert results[0].sum() == 3 * 37 * 53 * 3
    expected = np.stack(
        [np.bincount(f[:, :, 0].ravel(), minlength=256) for f in frames]
    ).sum(0)
    assert np.array_equal(results[0][0], expected)


def test_frames_that_are_not_8_bit_use_the_float_method(monkeypatch):
    frames = [np.full((8, 8, 3), v, dtype=np.uint16) for v in (0, 65535, 255)]
    monkeypatch.setattr(
        media,
        "probe",
        lambda path: {"width": 8, "height": 8, "declared_frames": 3},
    )
    monkeypatch.setattr(media, "decode", lambda path: iter(frames))
    monkeypatch.setattr(
        media.PixelHistogram, "add", lambda self, frame: pytest.fail("histogram used")
    )
    monkeypatch.setenv("LEVI_PIXEL_STATS", "histogram")
    out = media.inspect(media.Path("x.mp4"), pixels=True)
    assert out["stats"]["max"][0][0][0] == 65535 / 255


def test_the_histogram_method_is_at_least_five_times_faster(monkeypatch):
    # Frames are already decoded and the probe is stubbed: what is timed is the
    # per-frame work of ``inspect`` (thumbnail and statistics), not ffmpeg.
    rng = np.random.default_rng(1)
    frames = [rng.integers(0, 256, (480, 640, 3), dtype=np.uint8) for _ in range(12)]
    monkeypatch.setattr(
        media, "probe", lambda p: {"width": 640, "height": 480, "declared_frames": None}
    )
    monkeypatch.setattr(media, "decode", lambda p: iter(frames))

    def best(method):
        monkeypatch.setenv("LEVI_PIXEL_STATS", method)
        runs = []
        for _ in range(3):
            start = time.perf_counter()
            media.inspect(media.Path("x.mp4"), pixels=True)
            runs.append(time.perf_counter() - start)
        return min(runs)

    assert best("float") / best("histogram") >= 5


def test_the_comparison_command_reports_each_file(videos, tmp_path, capsys):
    folder = tmp_path / "v"
    folder.mkdir()
    shutil.copyfile(videos["gradient"], folder / "g.mp4")
    shutil.copyfile(videos["flat"], folder / "f.mp4")
    assert cli.main(["pixel-compare", str(folder)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["files"] == 2 and out["all_within_tolerance"] and out["different"] == []
    assert all(
        row["max_abs_diff"] <= TOLERANCE and row["max_variance_diff"] <= TOLERANCE
        for row in out["rows"]
    )
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"nope")
    assert cli.main(["pixel-compare", str(broken)]) == 1
    assert json.loads(capsys.readouterr().out)["rows"][0]["error"]


def test_the_benchmark_reports_both_pixel_methods(tmp_path, monkeypatch):
    monkeypatch.delenv("LEVI_PIXEL_STATS", raising=False)
    out = bench.run(["pixels"], frames=20, size="160x120", repeat=1, scratch=tmp_path)
    case = out["cases"]["pixels"]
    assert case["within_tolerance"] and case["max_abs_diff"] <= TOLERANCE
    assert set(case["methods"]) == {"float", "histogram"} and case["speedup"] > 1
    import os

    assert "LEVI_PIXEL_STATS" not in os.environ  # the override is undone


def test_a_constant_channel_differs_in_std_only_by_the_float_methods_cancellation(
    monkeypatch,
):
    # 720p of one colour, 60 frames: the true standard deviation is 0. The float
    # method computes mean(x^2) - mean(x)^2, which does not cancel exactly, so
    # its std is a tiny noise value (about 1e-6 here); the integer counts give 0.
    frames = [np.full((720, 1280, 3), (77, 200, 3), dtype=np.uint8)] * 60
    monkeypatch.setattr(
        media,
        "probe",
        lambda p: {"width": 1280, "height": 720, "declared_frames": None},
    )
    monkeypatch.setattr(media, "decode", lambda p: iter(frames))
    got = {}
    for method in ("float", "histogram"):
        monkeypatch.setenv("LEVI_PIXEL_STATS", method)
        got[method] = media.inspect(media.Path("x.mp4"), pixels=True)["stats"]
    for key in ("min", "max", "mean", "count"):
        gap = np.abs(np.array(got["float"][key]) - np.array(got["histogram"][key]))
        assert gap.max() <= TOLERANCE, key
    assert np.array(got["histogram"]["std"]).max() == 0.0  # exact
    old_std = np.array(got["float"]["std"])
    assert old_std.max() <= 1e-5  # the float method's error bound, not the new one's
    assert np.abs(old_std**2).max() <= TOLERANCE  # as a variance it is within 1e-9
    assert bench._within(bench._stats_gap(got["float"], got["histogram"]))


def test_the_comparison_commands_limit_the_cores(videos, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(bench, "limit_cores", lambda n: seen.append(n) or [])
    for command in ("pixel-compare", "pts-compare"):
        cli.main([command, str(videos["flat"])])
        cli.main([command, str(videos["flat"]), "--cores", "2"])
        cli.main([command, str(videos["flat"]), "--cores", "0"])
    capsys.readouterr()
    assert seen == [bench.MAX_CORES, 2, bench.MAX_CORES, 2]


def test_limit_cores_never_exceeds_four_and_lowers_priority(monkeypatch):
    calls = {}
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: set(range(16)))
    monkeypatch.setattr(
        os, "sched_setaffinity", lambda pid, cpus: calls.update(cpus=cpus)
    )
    monkeypatch.setattr(os, "nice", lambda n: calls.update(nice=n))
    assert len(bench.limit_cores(32)) == bench.MAX_CORES == len(calls["cpus"])
    assert calls["nice"] == 15
