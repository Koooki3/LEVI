"""FigureSpec (levi/automatic/analysis/figspec.py): the data model and its
validation, the axis tick algorithm, the palette, the accessible table and the
scene layout that the SVG and PDF writers share."""

import itertools
import json
import math
from dataclasses import replace

import pytest
import test_figfixtures as fx

from levi.automatic.analysis import figspec as fs

# ---------------------------------------------------------------- ticks


def covers(sc, lo, hi):
    return sc.lo <= lo + 1e-12 and sc.hi >= hi - 1e-12


@pytest.mark.parametrize(
    "lo,hi",
    [
        (0, 1),
        (0.03, 0.87),
        (-3.2, -0.4),  # all negative
        (-5, 5),
        (0, 100000),
        (1e12, 1.5e12),  # large
        (1.0, 1.0000001),  # tiny span next to a big value
        (0, 1e-7),
        (3, 3),  # constant
        (0, 0),  # constant zero
        (-2.5, -2.5),  # constant negative
        (7, 2),  # swapped bounds
    ],
)
def test_nice_scale_covers_data_with_sane_ticks(lo, hi):
    sc = fs.nice_scale(lo, hi)
    a, b = min(lo, hi), max(lo, hi)
    assert sc.lo < sc.hi
    assert covers(sc, a, b)
    assert 2 <= len(sc.ticks) <= 12
    assert list(sc.ticks) == sorted(set(sc.ticks)), "ticks strictly increase"
    assert sc.ticks[0] >= sc.lo - 1e-12 and sc.ticks[-1] <= sc.hi + 1e-12
    # the step is 1, 2 or 5 times a power of ten
    mant = sc.step / 10 ** math.floor(math.log10(sc.step))
    assert round(mant, 6) in (1.0, 2.0, 5.0)
    # every tick is a whole number of steps (no float drift in the labels)
    for t in sc.ticks:
        k = round(t / sc.step)
        assert abs(t - k * sc.step) <= 1e-9 * max(abs(t), sc.step)
    # labels are distinct
    labels = [fs.tick_label(t, sc) for t in sc.ticks]
    assert len(set(labels)) == len(labels)


def test_nice_scale_known_values():
    sc = fs.nice_scale(0, 1)
    assert sc.ticks == (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
    assert (sc.lo, sc.hi) == (0.0, 1.0)
    sc = fs.nice_scale(0, 0)
    assert (sc.lo, sc.hi) == (0.0, 1.0)
    sc = fs.nice_scale(3, 3)  # a window around the value
    assert sc.lo < 3 < sc.hi


def test_nice_scale_fixed_bounds_are_kept():
    sc = fs.nice_scale(0.05, 0.93, fixed_lo=0.0, fixed_hi=1.0)
    assert (sc.lo, sc.hi) == (0.0, 1.0)
    assert sc.ticks[0] == 0.0 and sc.ticks[-1] == 1.0
    sc = fs.nice_scale(0.1, 0.9, fixed_lo=0.0)  # one free bound moves out
    assert sc.lo == 0.0 and sc.hi >= 0.9
    # a window narrower than one step still gives two ticks
    sc = fs.nice_scale(0.31, 0.33, fixed_lo=0.31, fixed_hi=0.33)
    assert (sc.lo, sc.hi) == (0.31, 0.33) and len(sc.ticks) >= 2


@pytest.mark.parametrize("bad", [(math.nan, 1), (0, math.inf), (-math.inf, 0)])
def test_nice_scale_rejects_non_finite(bad):
    with pytest.raises(ValueError):
        fs.nice_scale(*bad)


def test_tick_labels():
    sc = fs.nice_scale(0, 1)
    assert fs.tick_label(0.0, sc, "percent") == "0%"
    assert fs.tick_label(1.0, sc, "percent") == "100%"
    neg = fs.nice_scale(-1, 1)
    assert [fs.tick_label(t, neg) for t in neg.ticks] == [
        "-1.0",
        "-0.5",
        "0.0",
        "0.5",
        "1.0",
    ]
    assert fs.tick_label(-0.0, neg) in ("0", "0.0", "0.00")
    assert fs.num(-0.0) == "0"
    assert fs.coord(-0.0001) == "0"
    assert fs.coord(12.3456) == "12.35"
    big = fs.nice_scale(0, 5e9)
    assert "e" in fs.tick_label(5e9, big) or float(fs.tick_label(5e9, big)) == 5e9


# ---------------------------------------------------------------- text and model


def test_text_languages_and_latin_fallback():
    t = fs.Text.of({"en": "Success", "zh-CN": "成功率"})
    assert t.get("zh-CN") == "成功率"
    assert t.get("en") == "Success"
    assert t.get("zh-CN", latin_only=True) == "Success"  # the PDF cannot show it
    only_zh = fs.Text.of({"zh-CN": "成功"})
    assert only_zh.get("en") == "成功"  # falls back to what exists
    assert only_zh.get("en", latin_only=True) == "??"
    assert fs.Text.of("plain").get("zh-CN") == "plain"
    assert not fs.Text.of(None)
    with pytest.raises(ValueError):
        fs.Text.of({"fr": "x"})
    with pytest.raises(TypeError):
        fs.Text.of({"en": 3})


@pytest.mark.parametrize("make", fx.all_specs(), ids=lambda s: s.kind)
def test_json_round_trip_and_digest(make):
    spec = make
    again = fs.FigureSpec.from_json(spec.to_json())
    assert again == spec
    assert again.digest() == spec.digest()
    d = json.loads(spec.to_json())
    assert d["schema"] == fs.SCHEMA
    assert spec.to_json() == spec.to_json()  # canonical: sorted keys, no spaces
    assert replace(spec, id="other").digest() != spec.digest()


def test_from_dict_checks_schema_and_validates():
    d = fx.grouped_bar().to_dict()
    with pytest.raises(ValueError, match="schema"):
        fs.FigureSpec.from_dict({**d, "schema": "levi.aeri.figure_spec.v0"})
    d["panels"][0]["series"][0]["points"][0]["lo"] = 0.9  # above y
    with pytest.raises(ValueError, match="does not contain"):
        fs.FigureSpec.from_dict(d)


def _mutate(spec, fn):
    d = json.loads(spec.to_json())
    fn(d)
    return d


def _pt(d, si=0, pj=0):
    return d["panels"][0]["series"][si]["points"][pj]


@pytest.mark.parametrize(
    "fn,msg",
    [
        (lambda d: d.update(kind="pie"), "unknown kind"),
        (lambda d: d.update(id="bad id"), "slug"),
        (lambda d: d.update(lang="fr"), "unknown lang"),
        (lambda d: d.update(title={}), "title"),
        (lambda d: d.update(panels=[]), "panels"),
        (
            lambda d: d["panels"][0]["x_axis"].update(kind="linear"),
            "takes no categories",
        ),
        (lambda d: d["panels"][0]["x_axis"].update(categories=[]), "needs categories"),
        (lambda d: _pt(d).update(x=7), "category index"),
        (lambda d: _pt(d).update(x=0.5), "category index"),
        (lambda d: _pt(d).update(x="0"), "finite number"),
        (lambda d: _pt(d).update(y=True), "finite number"),
        (lambda d: _pt(d).pop("hi"), "come together"),
        (lambda d: _pt(d).update(lo=0.9), "does not contain"),
        (lambda d: d["panels"][0]["series"][0].update(marker="star"), "marker"),
        (lambda d: d["panels"][0]["series"][0].update(dash="wavy"), "dash"),
        (lambda d: d["panels"][0]["series"][0].update(points=[]), "no points"),
        (lambda d: d["panels"][0]["y_axis"].update(min=2, max=1), "min must be below"),
        (lambda d: d["panels"][0]["y_axis"].update(fmt="hex"), "fmt"),
        (
            lambda d: d["panels"][0]["series"].extend(d["panels"][0]["series"] * 3),
            "series required",
        ),
    ],
)
def test_validation_rejects(fn, msg):
    with pytest.raises(ValueError, match=msg):
        fs.FigureSpec.from_dict(_mutate(fx.grouped_bar(), fn))


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_validation_rejects_non_finite_numbers(bad):
    spec = fx.grouped_bar()
    series = spec.panels[0].series
    broken = replace(series[0], points=(fs.Point(0, bad), *series[0].points[1:]))
    spec = replace(
        spec, panels=(replace(spec.panels[0], series=(broken, *series[1:])),)
    )
    with pytest.raises(ValueError, match="finite"):
        fs.validate(spec)
    with pytest.raises(ValueError, match="finite"):
        fs.layout(spec)  # the writers refuse it too
    with pytest.raises(ValueError):
        spec.to_json()  # and it never reaches a JSON file


def test_kind_specific_rules():
    stack = fx.stacked_bar()
    d = _mutate(stack, lambda d: d["panels"][0]["series"][0]["points"][0].update(y=-1))
    with pytest.raises(ValueError, match="negative"):
        fs.FigureSpec.from_dict(d)
    step = fx.step_curve()
    d = _mutate(step, lambda d: d["panels"][0]["series"][0]["points"].reverse())
    with pytest.raises(ValueError, match="must not decrease"):
        fs.FigureSpec.from_dict(d)
    forest = fx.forest()
    d = _mutate(
        forest,
        lambda d: d["panels"][0].update(
            y_axis={"kind": "linear", "label": {}, "fmt": "plain"}
        ),
    )
    with pytest.raises(ValueError, match="forest needs"):
        fs.FigureSpec.from_dict(d)
    bars = fx.grouped_bar()
    d = _mutate(
        bars,
        lambda d: d["panels"][0].update(
            reflines=[{"axis": "x", "value": 1, "label": {}}]
        ),
    )
    with pytest.raises(ValueError, match="category axis"):
        fs.FigureSpec.from_dict(d)


# ---------------------------------------------------------------- palette and styles


def _lin(c):
    c /= 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


# Machado, Oliveira and Fernandes (2009) matrices for full dichromacy, applied
# in linear RGB: a sanity check for the palette, not a clinical model.
CVD = {
    "protan": (
        (0.152286, 1.052583, -0.204868),
        (0.114503, 0.786281, 0.099216),
        (-0.003882, -0.048116, 1.051998),
    ),
    "deutan": (
        (0.367322, 0.860646, -0.227968),
        (0.280085, 0.672501, 0.047413),
        (-0.011820, 0.042940, 0.968881),
    ),
    "tritan": (
        (1.255528, -0.076749, -0.178779),
        (-0.078411, 0.930809, 0.147602),
        (0.004733, 0.691367, 0.303900),
    ),
}


def _lab(rgb):
    r, g, b = rgb
    x = 0.4124 * r + 0.3576 * g + 0.1805 * b
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = 0.0193 * r + 0.1192 * g + 0.9505 * b

    def f(t):
        return t ** (1 / 3) if t > 216 / 24389 else (24389 / 27 * t + 16) / 116

    fx_, fy, fz = f(x / 0.95047), f(y), f(z / 1.08883)
    return 116 * fy - 16, 500 * (fx_ - fy), 200 * (fy - fz)


def _seen(hex_colour, kind):
    c = [_lin(v) for v in fs.rgb_of(hex_colour)]
    if kind != "normal":
        m = CVD[kind]
        c = [
            min(1.0, max(0.0, sum(m[i][j] * c[j] for j in range(3)))) for i in range(3)
        ]
    return _lab(c)


@pytest.mark.parametrize("kind", ["normal", "protan", "deutan", "tritan"])
def test_palette_colours_stay_apart_for_colour_vision_deficiency(kind):
    for a, b in itertools.combinations(fs.PALETTE, 2):
        de = math.dist(_seen(a, kind), _seen(b, kind))
        assert de >= 15, f"{a} and {b} are too close under {kind}: {de:.1f}"


def test_palette_neighbours_differ_in_grayscale():
    lum = [fs.luminance(c) for c in fs.PALETTE]
    for a, b in itertools.pairwise(lum):
        assert abs(a - b) >= 0.12
    assert fs.luminance("#000000") == 0 and fs.luminance("#FFFFFF") == pytest.approx(1)


def test_series_styles_are_pairwise_distinguishable_without_colour():
    styles = [
        fs.series_style(i, fs.Series("s", (fs.Point(0, 0),)))
        for i in range(fs.MAX_SERIES)
    ]
    assert len({(s.marker, s.dash) for s in styles}) == fs.MAX_SERIES
    assert len({s.hatch for s in styles}) == fs.MAX_SERIES
    assert len({s.color for s in styles}) == fs.MAX_SERIES
    forced = fs.series_style(
        0, fs.Series("s", (fs.Point(0, 0),), marker="plus", dash="dotted")
    )
    assert (forced.marker, forced.dash) == ("plus", "dotted")


def test_blend_is_a_lighter_tint():
    t = fs.blend("#0072B2")
    assert fs.luminance(t) > fs.luminance("#0072B2")
    assert fs.blend("#000000", t=1.0) == "#FFFFFF"


# ---------------------------------------------------------------- table fallback


def _nums(rows, col):
    return [float(r[col]) for r in rows if r[col] != ""]


@pytest.mark.parametrize("spec", fx.all_specs(), ids=lambda s: s.kind)
def test_table_matches_the_figure_data(spec):
    headers, rows = fs.table(spec)
    assert len(headers) == 7 and all(len(r) == 7 for r in rows)
    points = [
        (pi, p)
        for pi, pn in enumerate(spec.panels)
        for s in pn.series
        for p in s.points
    ]
    refs = sum(len(pn.reflines) for pn in spec.panels)
    assert len(rows) == len(points) + refs
    est = [p.x if spec.kind == "forest" else p.y for _, p in points]
    got = [float(r[3]) for r in rows[: len(points)]] if len(spec.panels) == 1 else None
    if got is not None:
        assert got == pytest.approx(est, rel=1e-5, abs=1e-9)
        lo = [p.lo for _, p in points if p.lo is not None]
        hi = [p.hi for _, p in points if p.hi is not None]
        assert _nums(rows[: len(points)], 4) == pytest.approx(lo, rel=1e-5, abs=1e-9)
        assert _nums(rows[: len(points)], 5) == pytest.approx(hi, rel=1e-5, abs=1e-9)
        assert [r[6] for r in rows[: len(points)]] == [p.label for _, p in points]
    # category names, not indexes, label the rows
    first = {r[2] for r in rows}
    for pn in spec.panels:
        axis = pn.y_axis if spec.kind == "forest" else pn.x_axis
        for c in axis.categories:
            assert c.get("en") in first


def test_table_is_localised_and_exports():
    spec = fx.grouped_bar()
    h_en, r_en = fs.table(spec, "en")
    h_zh, r_zh = fs.table(spec, "zh-CN")
    assert h_en[0] == "Panel" and h_zh[0] == "子图"
    assert r_en[0][2] == "Pick cube" and r_zh[0][2] == "拿方块"
    csv_text = fs.table_csv(spec, "zh-CN")
    assert csv_text.splitlines()[0].startswith("子图,系列")
    assert len(csv_text.splitlines()) == 1 + len(r_zh)
    html_text = fs.table_html(spec, "en")
    assert html_text.startswith("<table><caption>Success rate by arm")
    assert html_text.count("<tr>") == 1 + len(r_en)  # header row + data rows
    hostile = replace(spec, title="a <b> & c")
    assert "a &lt;b&gt; &amp; c" in fs.table_html(hostile)
    forest_headers, _ = fs.table(fx.forest())
    assert forest_headers[2:4] == ("Row", "Estimate")


# ---------------------------------------------------------------- text measurement


def test_text_width_and_wrap():
    assert fs.text_width("iii", 10) < fs.text_width("WWW", 10)
    assert fs.text_width("0123456789", 10) == pytest.approx(55.6)  # digits are 556/1000
    assert fs.text_width("成功", 10) == pytest.approx(20.0)
    lines = fs.wrap("a quick brown fox jumps over the lazy dog", 10, 60)
    assert len(lines) > 1 and all(fs.text_width(ln, 10) <= 60 + 1e-9 for ln in lines)
    assert " ".join(lines) == "a quick brown fox jumps over the lazy dog"
    # a word wider than the line is broken
    long = fs.wrap("Supercalifragilisticexpialidocious", 10, 50)
    assert len(long) > 1 and "".join(long) == "Supercalifragilisticexpialidocious"
    # CJK breaks between characters and keeps them
    zh = fs.wrap("自动判定成功率的区间估计", 10, 50)
    assert len(zh) > 1 and "".join(zh) == "自动判定成功率的区间估计"
    # spaces between CJK and Latin text survive; a closing mark never starts a line
    assert fs.wrap("差值 B 减 A", 14, 1000) == ["差值 B 减 A"]
    assert fs.wrap("总体", 9, 20) == ["总体"]
    for ln in fs.wrap("自动判定成功率，区间估计。", 10, 52):
        assert ln[0] not in "，。"
    capped = fs.wrap("one two three four five six seven", 10, 40, max_lines=2)
    assert len(capped) == 2 and capped[-1].endswith("...")
    assert fs.wrap("", 10, 50) == [""]


# ---------------------------------------------------------------- the scene


def _bounds_ok(scene):
    w, h = scene.width, scene.height
    for it in scene.items:
        if isinstance(it, fs.Label):
            tw = fs.text_width(it.s, it.size, it.bold)
            if it.rotate:
                x0 = x1 = it.x
                y0 = it.y - {"start": 0, "middle": tw / 2, "end": tw}[it.anchor]
                y1 = y0 + tw
                x0 -= it.size
            else:
                x0 = it.x - {"start": 0, "middle": tw / 2, "end": tw}[it.anchor]
                x1 = x0 + tw
                y0, y1 = it.y - it.size, it.y + it.size * 0.25
            box = (x0, y0, x1, y1)
        elif isinstance(it, fs.Line):
            box = (
                min(it.x1, it.x2),
                min(it.y1, it.y2),
                max(it.x1, it.x2),
                max(it.y1, it.y2),
            )
        elif isinstance(it, fs.Rect):
            box = (it.x, it.y, it.x + it.w, it.y + it.h)
        elif isinstance(it, fs.Poly):
            xs, ys = zip(*it.points, strict=True)
            box = (min(xs), min(ys), max(xs), max(ys))
        else:
            box = (it.cx - it.r, it.cy - it.r, it.cx + it.r, it.cy + it.r)
        assert (
            box[0] >= -0.5
            and box[1] >= -0.5
            and box[2] <= w + 0.5
            and box[3] <= h + 0.5
        ), f"{it!r} leaves the page {w}x{h}"


@pytest.mark.parametrize("lang", ["en", "zh-CN"])
@pytest.mark.parametrize("latin_only", [False, True])
@pytest.mark.parametrize("spec", fx.all_specs(), ids=lambda s: s.kind)
def test_scene_stays_on_the_page_and_is_deterministic(spec, lang, latin_only):
    a = fs.layout(spec, lang=lang, latin_only=latin_only)
    b = fs.layout(spec, lang=lang, latin_only=latin_only)
    assert a == b
    _bounds_ok(a)
    assert a.title == spec.title.get(lang, latin_only)
    if latin_only:
        for it in a.items:
            if isinstance(it, fs.Label):
                it.s.encode("cp1252")


def _scene_by_ref(scene):
    out = {}
    for it in scene.items:
        ref = getattr(it, "ref", None)
        if ref is not None:
            out.setdefault((ref, it.role), []).append(it)
    return out


def test_bars_sit_on_their_values_and_intervals_are_drawn():
    spec = fx.grouped_bar()
    scene = fs.layout(spec)
    g = scene.panels[0]
    by = _scene_by_ref(scene)
    for si, s in enumerate(spec.panels[0].series):
        for pj, p in enumerate(s.points):
            (bar,) = by[((0, si, pj), "bar")]
            assert bar.y == pytest.approx(min(g.y.px(p.y), g.y.px(0)), abs=1e-6)
            assert bar.h == pytest.approx(abs(g.y.px(p.y) - g.y.px(0)), abs=1e-6)
            stem = by[((0, si, pj), "ci")][0]
            ys = sorted((stem.y1, stem.y2))
            assert ys[0] == pytest.approx(g.y.px(p.hi), abs=1e-6)
            assert ys[1] == pytest.approx(g.y.px(p.lo), abs=1e-6)
            assert len(by[((0, si, pj), "ci-cap")]) == 2
    texts = [it.s for it in scene.items if isinstance(it, fs.Label)]
    assert (
        "14/20" in texts and "100%" in texts and "Arm C" in texts
    )  # annotation, tick, legend


def test_forest_marks_follow_the_estimates_and_reference_line():
    spec = fx.forest()
    scene = fs.layout(spec)
    g = scene.panels[0]
    by = _scene_by_ref(scene)
    for si, s in enumerate(spec.panels[0].series):
        for pj, p in enumerate(s.points):
            stem = by[((0, si, pj), "ci")][0]
            assert min(stem.x1, stem.x2) == pytest.approx(g.x.px(p.lo), abs=1e-6)
            assert max(stem.x1, stem.x2) == pytest.approx(g.x.px(p.hi), abs=1e-6)
            marks = [
                i
                for (r, role), v in by.items()
                if r == (0, si, pj) and role == "marker"
                for i in v
            ]
            assert marks, "each estimate has a marker"
    (ref,) = [i for i in scene.items if isinstance(i, fs.Line) and i.role == "refline"]
    assert ref.x1 == pytest.approx(g.x.px(0)) and ref.dash
    # rows run top to bottom in category order
    assert g.y.px(0) < g.y.px(1) < g.y.px(2)


def test_step_curve_passes_through_every_point_and_marks_the_end():
    spec = fx.step_curve()
    scene = fs.layout(spec)
    g = scene.panels[0]
    curves = {
        it.ref[1]: it
        for it in scene.items
        if isinstance(it, fs.Poly) and it.role == "curve"
    }
    for si, s in enumerate(spec.panels[0].series):
        verts = {(round(x, 4), round(y, 4)) for x, y in curves[si].points}
        for p in s.points:
            assert (round(g.x.px(p.x), 4), round(g.y.px(p.y), 4)) in verts
        # staircase: consecutive vertices share an x or a y
        for (x0, y0), (x1, y1) in itertools.pairwise(curves[si].points):
            assert x0 == pytest.approx(x1) or y0 == pytest.approx(y1)
    assert any(
        it.role == "band" for it in scene.items if isinstance(it, fs.Poly)
    )  # arm C has an interval
    assert curves[0].width > curves[1].width  # the reference arm is heavier
    assert curves[0].dash != curves[1].dash  # and the arms differ without colour


def test_stacked_segments_add_up():
    spec = fx.stacked_bar()
    scene = fs.layout(spec)
    g = scene.panels[0]
    bars = [i for i in scene.items if isinstance(i, fs.Rect) and i.role == "bar"]
    tops = {}
    for r in bars:
        ci = r.ref[2]
        tops.setdefault(ci, []).append(r)
    for ci, rects in tops.items():
        total = sum(
            fs_p.y
            for s in spec.panels[0].series
            for fs_p in s.points
            if int(fs_p.x) == ci
        )
        top = min(r.y for r in rects)
        bottom = max(r.y + r.h for r in rects)
        assert bottom == pytest.approx(g.y.px(0), abs=1e-6)
        assert top == pytest.approx(g.y.px(total), abs=1e-6)
    texts = [it.s for it in scene.items if isinstance(it, fs.Label)]
    assert "12" in texts and "6" in texts and "11" in texts  # totals


def test_early_stop_has_one_plot_per_panel_and_a_shared_legend_rule():
    spec = fx.early_stop()
    scene = fs.layout(spec)
    assert len(scene.panels) == 2
    assert scene.panels[0].left + scene.panels[0].width < scene.panels[1].left
    assert not any(i.role == "legend" for i in scene.items), (
        "a single series needs no legend"
    )
    texts = [it.s for it in scene.items if isinstance(it, fs.Label)]
    assert "Steps saved" in texts and "Error rate" in texts


def test_drift_lines_pad_the_x_range_and_offset_series():
    spec = fx.drift_lines()
    scene = fs.layout(spec)
    g = scene.panels[0]
    assert g.x.scale.lo < 1 and g.x.scale.hi > 4
    curves = [i for i in scene.items if isinstance(i, fs.Poly) and i.role == "curve"]
    assert curves[0].width > curves[1].width
    assert curves[0].points[0][0] != curves[1].points[0][0]  # the arms do not overprint


def test_constant_and_negative_data_lay_out():
    base = fx.drift_lines()
    flat = tuple(fs.Point(r, 0.5) for r in (1, 2, 3))
    spec = replace(
        base,
        panels=(
            replace(
                base.panels[0],
                y_axis=fs.Axis(label="v"),
                series=(fs.Series("s", flat),),
            ),
        ),
    )
    scene = fs.layout(spec)
    _bounds_ok(scene)
    neg = tuple(fs.Point(r, -v) for r, v in ((1, 3.2), (2, 0.4), (3, 1.0)))
    spec = replace(
        spec, panels=(replace(spec.panels[0], series=(fs.Series("s", neg),)),)
    )
    sc = fs.layout(spec).panels[0].y.scale
    assert sc.lo <= -3.2 and sc.hi >= -0.4
    # a bar chart whose values are all negative draws bars down from zero
    bars = fx.grouped_bar()
    d = bars.to_dict()
    for s in d["panels"][0]["series"]:
        for p in s["points"]:
            p["y"], p["lo"], p["hi"] = -p["y"], -p["hi"], -p["lo"]
    d["panels"][0]["y_axis"].pop("min")
    d["panels"][0]["y_axis"].pop("max")
    scene = fs.layout(fs.FigureSpec.from_dict(d))
    _bounds_ok(scene)
    g = scene.panels[0]
    assert all(
        r.y >= g.y.px(0) - 1e-6
        for r in scene.items
        if isinstance(r, fs.Rect) and r.role == "bar"
    )


def test_data_outside_a_fixed_axis_is_refused_not_clipped():
    spec = fx.grouped_bar()
    narrow = replace(spec.panels[0], y_axis=replace(spec.panels[0].y_axis, max=0.5))
    with pytest.raises(ValueError, match="outside the axis range"):
        fs.layout(replace(spec, panels=(narrow,)))
    step = fx.step_curve()  # the cap line at 300 must fit too
    short = replace(step.panels[0], x_axis=fs.Axis(label="step", min=0, max=250))
    with pytest.raises(ValueError, match="outside the axis range"):
        fs.layout(replace(step, panels=(short,)))


def test_crowded_and_degenerate_figures_still_lay_out():
    long_names = tuple(
        f"A very long category name number {i} that must wrap" for i in range(12)
    )
    series = tuple(
        fs.Series(
            f"Arm {i} with a long legend name",
            tuple(fs.Point(c, 0.1 * (i + 1), 0.0, 0.9) for c in range(12)),
        )
        for i in range(fs.MAX_SERIES)
    )
    crowded = fs.FigureSpec(
        id="crowded",
        kind="grouped_bar",
        title="A title that is long enough to need more than one line on a page of this width, "
        * 2,
        summary="s " * 120,
        panels=(
            fs.Panel(
                x_axis=fs.Axis(kind="category", categories=long_names),
                y_axis=fs.Axis(label="rate", min=0, max=1),
                series=series,
            ),
        ),
        notes=("note " * 80,),
    )
    scene = fs.layout(crowded)
    _bounds_ok(scene)
    assert len([i for i in scene.items if i.role == "title"]) >= 2
    assert any(
        i.s.endswith("...")
        for i in scene.items
        if isinstance(i, fs.Label) and i.role == "tick-label"
    )
    one = fs.FigureSpec(  # single-point series of every curve kind
        id="one",
        kind="step_curve",
        title="t",
        summary="",
        panels=(
            fs.Panel(
                fs.Axis(label="x"),
                fs.Axis(label="y"),
                (fs.Series("s", (fs.Point(1, 2),)),),
            ),
        ),
    )
    _bounds_ok(fs.layout(one))
    zeros = replace(
        fx.stacked_bar(),
        panels=(
            replace(
                fx.stacked_bar().panels[0],
                series=(fs.Series("none", tuple(fs.Point(c, 0) for c in range(3))),),
            ),
        ),
    )
    _bounds_ok(fs.layout(zeros))  # nothing stacked: an empty plot, not a crash
    many_rows = replace(
        fx.forest(),
        panels=(
            replace(
                fx.forest().panels[0],
                y_axis=fs.Axis(
                    kind="category", categories=tuple(f"row {i}" for i in range(15))
                ),
                series=(
                    fs.Series(
                        "s", tuple(fs.Point(0.1 * i, i, -0.2, 1.8) for i in range(15))
                    ),
                ),
            ),
        ),
    )
    scene = fs.layout(many_rows)
    _bounds_ok(scene)
    assert scene.height > fs.layout(fx.forest()).height  # the page grows with the rows
