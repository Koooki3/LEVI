"""PDF writer (levi/automatic/analysis/pdfplot.py): structure checked with the
module's own reader (header, xref offsets, trailer, page tree, stream length,
fonts, extracted text), byte determinism, the Latin-only rule, and, when the
machine has them, pdftotext and pdfinfo as an independent reader."""

import re
import shutil
import subprocess
from dataclasses import replace

import pytest
import test_figfixtures as fx

from levi.automatic.analysis import figspec as fs
from levi.automatic.analysis import pdfplot

ALL = fx.all_specs()


def _texts(spec, lang="en"):
    scene = fs.layout(spec, lang=lang, latin_only=True)
    return [i.s for i in scene.items if isinstance(i, fs.Label) and i.s]


@pytest.mark.parametrize("spec", ALL, ids=lambda s: s.kind)
@pytest.mark.parametrize("lang", ["en", "zh-CN"])
def test_structure_verifies_and_text_is_extractable(spec, lang):
    data = pdfplot.render_pdf(spec, lang=lang)
    rep = pdfplot.verify_pdf(data)
    scene = fs.layout(spec, lang=lang, latin_only=True)
    assert rep.objects == 7
    assert (rep.page_width, rep.page_height) == (scene.width, scene.height)
    assert rep.fonts == ("Helvetica", "Helvetica-Bold")
    assert list(rep.text) == _texts(spec, lang)  # every label, in drawing order
    assert spec.title.get("en") in rep.text
    assert data.startswith(b"%PDF-1.4\n") and data.endswith(b"%%EOF\n")
    assert (
        b"/Encrypt" not in data and b"/JavaScript" not in data and b"/URI" not in data
    )


@pytest.mark.parametrize("spec", ALL, ids=lambda s: s.kind)
def test_two_runs_give_the_same_bytes(spec):
    a = pdfplot.render_pdf(spec)
    b = pdfplot.render_pdf(fs.FigureSpec.from_json(spec.to_json()))
    assert a == b
    # nothing time-dependent or random is written
    assert b"CreationDate" not in a and b"ModDate" not in a and b"/ID" not in a


def test_chinese_figures_fall_back_to_english_in_the_pdf():
    spec = fx.grouped_bar()
    zh = pdfplot.verify_pdf(pdfplot.render_pdf(spec, lang="zh-CN"))
    assert "Success rate by arm (automatic verdict)" in zh.text  # the English form
    assert not any(ord(c) > 255 for t in zh.text for c in t)
    # a text with no Latin form shows question marks rather than failing
    only_zh = replace(spec, title={"zh-CN": "成功率"}, summary={"zh-CN": "高"})
    rep = pdfplot.verify_pdf(pdfplot.render_pdf(only_zh, lang="zh-CN"))
    assert "[n/a]" in rep.text


def test_latin_1_text_and_special_characters_survive():
    spec = fx.tiny_bars()
    odd = replace(
        spec,
        title="Café (A\\B) 50% ± 3 · naïve",
        summary="Parentheses ) and ( and a backslash \\ inside",
    )
    rep = pdfplot.verify_pdf(pdfplot.render_pdf(odd))
    assert "Café (A\\B) 50% ± 3 · naïve" in rep.text
    assert "Parentheses ) and ( and a backslash \\ inside" in rep.text


def test_rotated_axis_label_and_hatching_are_in_the_stream():
    data = pdfplot.render_pdf(fx.grouped_bar())
    assert b" 0 1 -1 0 " in data  # the y-axis label runs upward
    assert data.count(b" re ") >= 10 and data.count(b" l S Q") > 100
    assert b"[6 3] 0 d" not in data  # bars have no dashes; curves do:
    assert b"[6 3] 0 d" in pdfplot.render_pdf(fx.step_curve())
    assert b" c h " in pdfplot.render_pdf(fx.forest())  # a Bezier circle marker


def test_title_and_summary_are_in_the_document_info():
    spec = fx.tiny_bars()
    data = pdfplot.render_pdf(spec)
    info = re.search(rb"/Title <FEFF([0-9A-F]+)>", data)
    assert bytes.fromhex(info.group(1).decode()).decode("utf-16-be") == spec.title.get(
        "en"
    )
    assert b"/Producer (LEVI figspec)" in data


# ---------------------------------------------------------------- the reader refuses damaged files


def _good():
    return pdfplot.render_pdf(fx.tiny_bars())


def _rebuild_xref(data):
    """Recompute the xref table and startxref after an edit that changed the
    file's length, so the reader gets past the offsets to the damage itself."""
    body = data[: data.index(b"xref\n0 ")]
    offsets = [m.start() for m in re.finditer(rb"^\d+ 0 obj\n", body, re.MULTILINE)]
    xref = f"xref\n0 {len(offsets) + 1}\n0000000000 65535 f \n".encode()
    xref += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    trailer = data[data.index(b"trailer") : data.index(b"startxref")]
    return body + xref + trailer + f"startxref\n{len(body)}\n%%EOF\n".encode()


@pytest.mark.parametrize(
    "damage,msg",
    [
        (lambda d: d[:-6], "%%EOF"),
        (lambda d: b"%PDX" + d[4:], "header"),
        (lambda d: d.replace(b"startxref", b"startxrex"), "startxref"),
        (
            lambda d: (
                d[: d.index(b"xref\n0 ")] + b"xrex" + d[d.index(b"xref\n0 ") + 4 :]
            ),
            "xref",
        ),
        (lambda d: d.replace(b"/Size 8", b"/Size 9"), "Size"),
        (lambda d: d.replace(b"0000000015 00000 n", b"0000000016 00000 n"), "offset"),
        (lambda d: d.replace(b"/Count 1", b"/Count 2"), "exactly one page"),
        (lambda d: _rebuild_xref(d.replace(b"/Kids [3 0 R]", b"/Kids [ ]")), "no kid"),
        (lambda d: d.replace(b"endstream", b"endstreXm"), "Length"),
        (
            lambda d: _rebuild_xref(re.sub(rb"/Length \d+", b"/Length 7", d, count=1)),
            "Length",
        ),
        (
            lambda d: _rebuild_xref(
                d.replace(b"/BaseFont /Helvetica ", b"/BaseFont /Zzz /Q ")
            ),
            None,
        ),
        (lambda d: _rebuild_xref(d.replace(b"/Contents 4 0 R", b"")), "Contents"),
        (lambda d: d.replace(b"/MediaBox [0 0 ", b"/MediaBoX [0 0 "), "MediaBox"),
        (lambda d: d.replace(b"/Type /Catalog", b"/Type /Catalox"), "catalog"),
    ],
)
def test_verify_rejects_damage(damage, msg):
    bad = damage(_good())
    if msg is None:  # a renamed font is still a font: the reader reports what it found
        assert "Zzz" in pdfplot.verify_pdf(bad).fonts
        return
    with pytest.raises(pdfplot.PdfError, match=msg):
        pdfplot.verify_pdf(bad)
    assert pdfplot.verify_pdf(_good()).objects == 7  # and the good file is untouched


def test_verify_handles_octal_escapes_and_empty_input():
    with pytest.raises(pdfplot.PdfError):
        pdfplot.verify_pdf(b"")
    assert pdfplot._decode_literal(rb"caf\351 \050x\051 \\ \n") == "café (x) \\ \n"


# ---------------------------------------------------------------- an independent reader, if present


@pytest.mark.skipif(
    shutil.which("pdftotext") is None, reason="pdftotext (poppler) not installed"
)
@pytest.mark.parametrize("spec", ALL, ids=lambda s: s.kind)
def test_pdftotext_reads_the_same_words(spec, tmp_path):
    path = tmp_path / "f.pdf"
    path.write_bytes(pdfplot.render_pdf(spec))
    out = subprocess.run(
        ["pdftotext", "-layout", str(path), "-"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout
    squashed = " ".join(out.split())
    # the title and every series name come out; so does a numeric tick label
    assert " ".join(spec.title.get("en").split()) in squashed
    if len(spec.panels[0].series) > 1:  # a single series has no legend
        for s in spec.panels[0].series:
            assert s.name.get("en") in squashed
    ticks = [
        i.s for i in fs.layout(spec, latin_only=True).items if i.role == "tick-label"
    ]
    assert ticks and all(t in squashed for t in ticks[:3])


@pytest.mark.skipif(
    shutil.which("pdfinfo") is None, reason="pdfinfo (poppler) not installed"
)
def test_pdfinfo_accepts_the_file_and_reports_one_page(tmp_path):
    spec = fx.grouped_bar()
    path = tmp_path / "f.pdf"
    path.write_bytes(pdfplot.render_pdf(spec))
    out = subprocess.run(
        ["pdfinfo", str(path)], capture_output=True, text=True, check=True, timeout=60
    )
    assert re.search(r"^Pages:\s+1$", out.stdout, re.MULTILINE)
    scene = fs.layout(spec, latin_only=True)
    assert f"{scene.width:g} x {scene.height:g} pts" in out.stdout
    assert re.search(
        r"^Title:\s+Success rate by arm \(automatic verdict\)$",
        out.stdout,
        re.MULTILINE,
    )
    assert re.search(r"^Encrypted:\s+no$", out.stdout, re.MULTILINE)


# ---------------------------------------------------------------- the reader looks inside the content stream


def _swap_stream(data, old, new):
    assert old in data
    return _rebuild_xref_len(data.replace(old, new, 1))


def _rebuild_xref_len(data):
    """Fix /Length and the xref after an edit inside the content stream."""
    s0 = data.index(b"stream\n") + 7
    s1 = data.index(b"endstream")
    data = re.sub(rb"/Length \d+", b"/Length %d" % (s1 - s0), data, count=1)
    return _rebuild_xref(data)


@pytest.mark.parametrize(
    "edit,msg",
    [
        (lambda d: _swap_stream(d, b" l S Q", b" zz S Q"), "unknown operator"),
        (lambda d: _swap_stream(d, b" Tj ET Q", b" Tj Q"), "ET"),
        (lambda d: _swap_stream(d, b"q 1 1 1 rg", b"Q 1 1 1 rg"), "Q"),
        (lambda d: _swap_stream(d, b"BT /F2", b"/F2"), "Tf"),
        (lambda d: _swap_stream(d, b" re f Q", b" re re f Q"), "operand"),
        (lambda d: _swap_stream(d, b"/F1 ", b"/F9 "), "font"),
        (lambda d: _swap_stream(d, b" Tj", b" Tj Tj"), "operand"),
        (lambda d: d + b"", None),
    ],
)
def test_verify_reads_the_content_stream(edit, msg):
    bad = edit(_good())
    if msg is None:
        assert pdfplot.verify_pdf(bad).objects == 7
        return
    with pytest.raises(pdfplot.PdfError, match=msg):
        pdfplot.verify_pdf(bad)


def test_content_stream_must_end_with_every_q_closed():
    d = _good()
    bad = _swap_stream(d, b"\nendstream", b" q\nendstream")
    with pytest.raises(pdfplot.PdfError, match="unbalanced"):
        pdfplot.verify_pdf(bad)


@pytest.mark.parametrize(
    "old,new",
    [
        (b"/Root 1 0 R", b"/Root 9 0 R"),
        (b"/F1 5 0 R", b"/F1 9 0 R"),
        (b"/Pages 2 0 R", b"/Pages 9 0 R"),
        (b"/Contents 4 0 R", b"/Contents 9 0 R"),
    ],
)
def test_dangling_references_raise_pdf_error_not_key_error(old, new):
    with pytest.raises(pdfplot.PdfError):
        pdfplot.verify_pdf(_good().replace(old, new))


def test_page_parent_must_be_the_pages_object():
    with pytest.raises(pdfplot.PdfError, match="Parent"):
        pdfplot.verify_pdf(_good().replace(b"/Parent 2 0 R", b"/Parent 5 0 R"))


@pytest.mark.skipif(
    shutil.which("pdftoppm") is None, reason="pdftoppm (poppler) not installed"
)
@pytest.mark.parametrize("spec", ALL, ids=lambda s: s.kind)
def test_poppler_reads_every_figure_without_a_warning(spec, tmp_path):
    path = tmp_path / "f.pdf"
    path.write_bytes(pdfplot.render_pdf(spec))
    for cmd in (
        ["pdftotext", "-layout", str(path), "-"],
        ["pdftoppm", "-png", "-r", "20", str(path), str(tmp_path / "img")],
    ):
        run = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, check=False
        )
        assert run.returncode == 0 and run.stderr == "", (cmd[0], run.stderr)
