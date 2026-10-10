"""Figure files (levi/automatic/figure_files.py): the analysis package only
returns bytes; writing is outside it, durable (temporary file, fsync,
replace, fsync of the folder) and leaves no half-written file when anything
fails on the way."""

import hashlib
import os
import threading
from dataclasses import replace

import pytest
import test_figfixtures as fx

from levi.automatic import figure_files as ff
from levi.automatic.analysis import pdfplot, svgplot


def _names(folder):
    return sorted(p.name for p in folder.iterdir())


def test_writes_both_formats_exactly_and_reports_them(tmp_path):
    spec = fx.tiny_bars()
    out = ff.write_figure(spec, tmp_path / "f1")
    assert _names(tmp_path) == ["f1.pdf", "f1.svg"]
    svg = (tmp_path / "f1.svg").read_bytes()
    pdf = (tmp_path / "f1.pdf").read_bytes()
    assert svg == svgplot.render_svg(spec).encode("utf-8")
    assert pdf == pdfplot.render_pdf(spec)
    assert out["svg"] == {
        "path": str(tmp_path / "f1.svg"),
        "bytes": len(svg),
        "sha256": hashlib.sha256(svg).hexdigest(),
    }
    assert out["pdf"]["sha256"] == hashlib.sha256(pdf).hexdigest()
    assert out["pdf"]["pdf"] == "ok" and out["pdf"]["substitutions"] == []
    if hasattr(os, "fchmod"):
        assert (tmp_path / "f1.svg").stat().st_mode & 0o777 == ff.FILE_MODE


def test_overwrites_and_passes_the_options(tmp_path):
    spec = fx.tiny_bars()
    ff.write_figure(spec, tmp_path / "f", ["svg"])
    ff.write_figure(spec, tmp_path / "f", ("svg",), lang="zh-CN", embed_spec=True)
    text = (tmp_path / "f.svg").read_text(encoding="utf-8")
    assert "各组成功率" in text and 'id="fig-spec"' in text
    assert _names(tmp_path) == ["f.svg"]


def test_pdf_record_lists_the_substitutions(tmp_path):
    spec = replace(fx.tiny_bars(), title={"en": "Δ success rate (B − A)"})
    out = ff.write_figure(spec, tmp_path / "f", ["pdf"])
    assert out["pdf"]["pdf"].startswith("lossy(")
    assert out["pdf"]["substitutions"][0]["text"] == "Δ success rate (B − A)"
    assert _names(tmp_path) == ["f.pdf"]


def test_a_render_error_writes_nothing(tmp_path):
    spec = replace(fx.tiny_bars(), title={"zh-CN": "只有中文"})
    with pytest.raises(pdfplot.LossyTextError):
        ff.write_figure(spec, tmp_path / "f", ["svg", "pdf"], strict=True)
    assert _names(tmp_path) == []  # the SVG was rendered but not written


@pytest.mark.parametrize("formats", [(), ("png",), ("svg", "eps")])
def test_unknown_or_empty_formats_are_refused(tmp_path, formats):
    with pytest.raises(ValueError):
        ff.write_figure(fx.tiny_bars(), tmp_path / "f", formats)
    assert _names(tmp_path) == []


def test_missing_folder_is_an_error_not_a_new_folder(tmp_path):
    with pytest.raises(FileNotFoundError):
        ff.write_figure(fx.tiny_bars(), tmp_path / "nope" / "f", ["svg"])
    assert _names(tmp_path) == []


@pytest.mark.parametrize("fail_at", ["write", "fsync", "replace"])
def test_a_crash_mid_write_keeps_the_old_file_and_no_temporary(
    tmp_path, monkeypatch, fail_at
):
    target = tmp_path / "f.svg"
    target.write_bytes(b"old figure")

    class Boom(RuntimeError):
        pass

    def boom(*_a, **_k):
        raise Boom(fail_at)

    if fail_at == "write":
        real = os.fdopen

        def fdopen(fd, *a, **k):
            f = real(fd, *a, **k)

            class Half:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    f.close()

                def fileno(self):
                    return f.fileno()

                def write(self, data):
                    f.write(data[: len(data) // 2])
                    raise Boom("disk full")

                def flush(self):
                    f.flush()

            return Half()

        monkeypatch.setattr(ff.os, "fdopen", fdopen)
    elif fail_at == "fsync":
        monkeypatch.setattr(ff.os, "fsync", boom)
    else:
        monkeypatch.setattr(ff.os, "replace", boom)
    with pytest.raises(Boom):
        ff.write_figure(fx.tiny_bars(), tmp_path / "f", ["svg"])
    assert target.read_bytes() == b"old figure"
    assert _names(tmp_path) == ["f.svg"]  # no .partial left behind


def test_the_folder_is_synced_after_the_rename(tmp_path, monkeypatch):
    calls = []
    real_replace, real_fsync_dir = os.replace, ff._fsync_dir
    monkeypatch.setattr(
        ff.os, "replace", lambda a, b: (calls.append("replace"), real_replace(a, b))
    )
    monkeypatch.setattr(
        ff, "_fsync_dir", lambda d: (calls.append(("dir", d)), real_fsync_dir(d))
    )
    ff.write_figure(fx.tiny_bars(), tmp_path / "f", ["svg"])
    assert calls == ["replace", ("dir", tmp_path)]


def test_concurrent_writers_never_leave_a_mixed_or_partial_file(tmp_path):
    a, b = fx.tiny_bars(), fx.tiny_forest()
    want = {svgplot.render_svg(a).encode(), svgplot.render_svg(b).encode()}
    errors = []

    def run(spec):
        try:
            for _ in range(15):
                ff.write_figure(spec, tmp_path / "f", ["svg"])
        except (OSError, ValueError) as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(s,)) for s in (a, b, a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert (tmp_path / "f.svg").read_bytes() in want
    assert _names(tmp_path) == ["f.svg"]


def test_the_analysis_package_has_no_file_writer():
    assert not hasattr(svgplot, "write_svg") and not hasattr(pdfplot, "write_pdf")
