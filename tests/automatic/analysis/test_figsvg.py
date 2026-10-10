"""SVG writer (levi/automatic/analysis/svgplot.py): byte-for-byte snapshots,
determinism, well-formed XML, accessibility metadata and escaping.

Regenerate the golden files after an intended change with
``LEVI_UPDATE_GOLDEN=1 pytest tests/automatic/analysis/test_figsvg.py`` and
read the diff."""

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

import pytest
import test_figfixtures as fx

from levi.automatic.analysis import figspec as fs
from levi.automatic.analysis import svgplot

GOLDEN = Path(__file__).resolve().parent / "test_fig_golden"
SVG = "{http://www.w3.org/2000/svg}"

CASES = {
    "bars_en.svg": (fx.tiny_bars, "en"),
    "forest_zh.svg": (fx.tiny_forest, "zh-CN"),
    "forest_en.svg": (fx.tiny_forest, "en"),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_snapshot(name):
    make, lang = CASES[name]
    got = svgplot.render_svg(make(), lang=lang)
    path = GOLDEN / name
    if os.environ.get("LEVI_UPDATE_GOLDEN") == "1":
        path.write_text(got, encoding="utf-8")
    assert path.read_text(encoding="utf-8") == got, (
        f"{name} changed; if intended, rerun with LEVI_UPDATE_GOLDEN=1"
    )


@pytest.mark.parametrize("spec", fx.all_specs(), ids=lambda s: s.kind)
@pytest.mark.parametrize("lang", ["en", "zh-CN"])
def test_two_runs_give_the_same_bytes(spec, lang):
    a = svgplot.render_svg(spec, lang=lang).encode("utf-8")
    b = svgplot.render_svg(fs.FigureSpec.from_json(spec.to_json()), lang=lang).encode(
        "utf-8"
    )
    assert a == b
    assert svgplot.render_svg(spec, lang=lang, embed_spec=True) == svgplot.render_svg(
        spec, lang=lang, embed_spec=True
    )


@pytest.mark.parametrize("spec", fx.all_specs(), ids=lambda s: s.kind)
@pytest.mark.parametrize("lang", ["en", "zh-CN"])
def test_well_formed_and_accessible(spec, lang):
    text = svgplot.render_svg(spec, lang=lang)
    root = ET.fromstring(text.encode("utf-8"))
    assert root.tag == SVG + "svg"
    assert root.get("role") == "img" and root.get("lang") == lang
    assert root.get("viewBox") == f"0 0 {root.get('width')} {root.get('height')}"
    title = root.find(SVG + "title")
    desc = root.find(SVG + "desc")
    assert title.text == spec.title.get(lang)
    assert desc.text == spec.summary.get(lang)
    assert root.get("aria-labelledby") == f"{title.get('id')} {desc.get('id')}"
    # no external references, scripts or non-finite numbers
    assert (
        "href" not in text
        and "<script" not in text
        and "http://" not in text.replace("http://www.w3.org/2000/svg", "")
    )
    assert not re.search(r"\b(nan|inf)\b", text, re.IGNORECASE)
    # every number the writer produced has at most two decimals
    assert not re.search(r'="-?\d+\.\d{3,}"', text)
    # generic font families only (no embedded or named-file fonts)
    assert "font-family" in text and "@font-face" not in text


def test_chinese_text_is_live_text_and_english_is_the_fallback():
    spec = fx.tiny_bars()
    zh = svgplot.render_svg(spec, lang="zh-CN")
    assert "各组成功率" in zh and "拿取" in zh and "Noto Sans CJK SC" in zh
    en = svgplot.render_svg(spec, lang="en")
    assert "各组成功率" not in en and "Success rate by arm" in en and "CJK" not in en
    # a text with no Chinese form shows its English form in a Chinese figure
    assert ">A</text>" in zh and ">8/20</text>" in zh


def test_every_series_is_distinguishable_in_the_file_without_colour():
    text = svgplot.render_svg(fx.step_curve())
    # dashed, dotted and solid curves all appear, plus differently shaped markers
    assert 'stroke-dasharray="6 3"' in text and 'stroke-dasharray="1.5 2.5"' in text
    assert "<circle" in text and "<polygon" in text
    bars = svgplot.render_svg(fx.grouped_bar())
    assert bars.count("<line") > 100  # hatch lines separate the filled arms


def test_intervals_are_drawn():
    spec = fx.tiny_bars()
    text = svgplot.render_svg(spec)
    scene = fs.layout(spec)
    stems = [i for i in scene.items if isinstance(i, fs.Line) and i.role == "ci"]
    assert len(stems) == 4
    for s in stems:
        assert (
            f'x1="{fs.coord(s.x1)}" y1="{fs.coord(s.y1)}" x2="{fs.coord(s.x2)}" y2="{fs.coord(s.y2)}"'
            in text
        )


def test_embedded_spec_round_trips():
    spec = fx.forest()
    text = svgplot.render_svg(spec, embed_spec=True)
    root = ET.fromstring(text.encode("utf-8"))
    meta = root.find(SVG + "metadata")
    assert fs.FigureSpec.from_json(meta.text) == spec
    assert root.find(SVG + "metadata") is not None
    assert svgplot.render_svg(spec).find("<metadata") == -1  # off by default


def test_markup_in_names_is_escaped():
    spec = fx.tiny_bars()
    nasty = replace(spec, title='A <b>"&"</b> title', summary="x < y & z")
    text = svgplot.render_svg(nasty)
    ET.fromstring(text.encode("utf-8"))  # still well formed
    assert "<b>" not in text and "&lt;b&gt;" in text and "&amp;" in text


def test_width_option_scales_the_page():
    spec = fx.tiny_bars()
    narrow = svgplot.render_svg(spec, width=480)
    root = ET.fromstring(narrow.encode("utf-8"))
    assert root.get("width") == "480"
    fs.layout(spec, width=480)  # and the layout is still valid
