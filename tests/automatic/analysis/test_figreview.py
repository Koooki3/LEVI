"""Independent-review findings for the figure writers (review-CP7): layering
of interval bands, no silently lost intervals, shared legends and colours
across panels, visible PDF substitutions, bounded text layout, the
confusion-matrix figure and the interface rules for the analysis library."""

import json
import time
from dataclasses import replace

import pytest
import test_figfixtures as fx

from levi.automatic.analysis import figspec as fs
from levi.automatic.analysis import pdfplot, svgplot

P = fs.Point


def step_spec(series, **kw):
    return fs.FigureSpec(
        id="step",
        kind="step_curve",
        title="Time to success",
        summary="s",
        panels=(
            fs.Panel(
                fs.Axis(label="step"),
                fs.Axis(label="success", min=0, max=1, fmt="percent"),
                tuple(series),
                **kw,
            ),
        ),
    )


def texts(scene):
    return [i.s for i in scene.items if isinstance(i, fs.Label)]


def idx_of(scene, role, cls):
    return [
        k for k, i in enumerate(scene.items) if isinstance(i, cls) and i.role == role
    ]


# ------------------------------------------------------------------ B1 layering


def test_every_interval_band_is_drawn_below_every_curve():
    scene = fs.layout(fx.step_curve())
    bands = idx_of(scene, "band", fs.Poly)
    curves = idx_of(scene, "curve", fs.Poly)
    assert bands and curves
    assert max(bands) < min(curves)
    markers = [k for k, i in enumerate(scene.items) if i.role == "marker"]
    assert max(bands) < min(markers)


def test_a_later_arms_band_cannot_hide_the_reference_curve():
    # the review's example: the reference arm runs inside the next arm's band
    ref = fs.Series(
        "Arm A",
        (P(0, 0), P(60, 0.05), P(110, 0.2), P(170, 0.35), P(260, 0.4)),
        emphasis=True,
    )
    wide = fs.Series(
        "Arm C",
        (
            P(0, 0, 0, 0.1),
            P(80, 0.1, 0.0, 0.5),
            P(130, 0.3, 0.05, 0.7),
            P(200, 0.5, 0.2, 0.8),
        ),
    )
    scene = fs.layout(step_spec([ref, wide]))
    curve_a = next(
        k for k, i in enumerate(scene.items) if i.role == "curve" and i.ref[1] == 0
    )
    later_bands = [
        k for k, i in enumerate(scene.items) if i.role == "band" and k > curve_a
    ]
    assert later_bands == []


# ------------------------------------------------------------------ B2 intervals


def refs_with_interval(scene):
    out = set()
    for i in scene.items:
        if i.role in ("ci", "band"):
            out.update(getattr(i, "refs", ()) or ([i.ref] if i.ref else []))
    return out


def test_partial_intervals_break_the_band_and_are_marked_unavailable():
    s = fs.Series(
        "A",
        (
            P(50, 0.1),  # no interval
            P(100, 0.2, 0.1, 0.3),
            P(150, 0.3, 0.2, 0.4),
            P(200, 0.4),  # no interval
            P(250, 0.5, 0.4, 0.6),  # an isolated point: an error bar
        ),
    )
    scene = fs.layout(step_spec([s]))
    refs = refs_with_interval(scene)
    assert refs == {(0, 0, 1), (0, 0, 2), (0, 0, 4)}
    bands = [i for i in scene.items if i.role == "band"]
    assert len(bands) == 1 and set(bands[0].refs) == {(0, 0, 1), (0, 0, 2)}
    assert any(i.role == "ci" for i in scene.items), "the lone point has an error bar"
    assert "Interval unavailable for 2 points; see the table." in texts(scene)
    spec = step_spec([s])
    _, rows = fs.table(spec)
    notes = [r[6] for r in rows]
    assert notes == ["interval unavailable", "", "", "interval unavailable", ""]
    _, rows_zh = fs.table(spec, "zh-CN")
    assert rows_zh[0][6] == "区间不可用"
    zh = fs.layout(spec, lang="zh-CN")
    assert "2 个点的区间不可用，见表格。" in texts(zh)


def test_a_single_point_curve_with_an_interval_gets_an_error_bar():
    scene = fs.layout(step_spec([fs.Series("A", (P(100, 0.3, 0.1, 0.5),))]))
    assert refs_with_interval(scene) == {(0, 0, 0)}
    assert any(i.role == "ci" for i in scene.items)


def test_no_interval_at_x0_means_no_uncertainty():
    # the Kaplan-Meier output has no interval at t=0 (before the first event)
    s = fs.Series("A", (P(0, 0), P(60, 0.1, 0.02, 0.2), P(120, 0.3, 0.1, 0.5)))
    spec = step_spec([s])
    scene = fs.layout(spec)
    assert refs_with_interval(scene) == {(0, 0, 0), (0, 0, 1), (0, 0, 2)}
    (band,) = [i for i in scene.items if i.role == "band"]
    g = scene.panels[0]
    assert band.points[0] == pytest.approx((g.x.px(0), g.y.px(0)))  # hi = lo = y at t=0
    _, rows = fs.table(spec)
    assert rows[0][4:6] == ("0", "0")  # the table shows the value as its own interval
    assert "unavailable" not in rows[0][6] and "no uncertainty" in rows[0][6]
    assert not any("unavailable" in t for t in texts(scene))
    assert fs.layout(spec) == scene  # and it is deterministic


def test_stacked_bars_refuse_intervals():
    d = fx.stacked_bar().to_dict()
    d["panels"][0]["series"][0]["points"][0].update(lo=5, hi=7)
    with pytest.raises(ValueError, match="does not support intervals"):
        fs.FigureSpec.from_dict(d)


@pytest.mark.parametrize("spec", fx.all_specs(), ids=lambda s: s.kind)
def test_validate_render_passes_for_every_figure(spec):
    scene = fs.layout(spec)
    fs.validate_render(spec, scene)


def test_a_lost_interval_is_an_error_not_silence():
    spec = fx.grouped_bar()
    scene = fs.layout(spec)
    kept = tuple(i for i in scene.items if i.role not in ("ci", "ci-cap", "ci-halo"))
    with pytest.raises(ValueError, match="interval"):
        fs.validate_render(spec, replace(scene, items=kept))
    # and the same for a band
    spec = fx.step_curve()
    scene = fs.layout(spec)
    kept = tuple(i for i in scene.items if i.role != "band")
    with pytest.raises(ValueError, match="interval"):
        fs.validate_render(spec, replace(scene, items=kept))


@pytest.mark.parametrize(
    "fn",
    ["_bars", "_forest", "_step", "_lines"],
    ids=["bars", "forest", "step", "drift"],
)
def test_a_drawing_function_that_skips_intervals_is_caught_by_layout(monkeypatch, fn):
    real = getattr(fs, fn)

    def lossy(items, panel, pi, g):
        real(items, panel, pi, g)
        items[:] = [
            i for i in items if i.role not in ("ci", "ci-cap", "ci-halo", "band")
        ]

    monkeypatch.setattr(fs, fn, lossy)
    spec = {
        "_bars": fx.grouped_bar,
        "_forest": fx.forest,
        "_step": fx.step_curve,
        "_lines": fx.drift_lines,
    }[fn]()
    with pytest.raises(ValueError, match="interval"):
        fs.layout(spec)


# ------------------------------------------------------------------ I1 legends and colours


def two_panels(first, second):
    def panel(names, title):
        return fs.Panel(
            fs.Axis(kind="category", categories=("a", "b")),
            fs.Axis(label="rate", min=0, max=1),
            tuple(
                fs.Series(n, (P(0, 0.3 + 0.1 * i), P(1, 0.5)))
                for i, n in enumerate(names)
            ),
            title=title,
        )

    return fs.FigureSpec(
        id="two",
        kind="early_stop",
        title="t",
        summary="",
        panels=(panel(first, "one"), panel(second, "two")),
    )


def fill_by_name(spec, scene):
    out = {}
    for i in scene.items:
        if isinstance(i, fs.Rect) and i.role == "bar":
            pi, si, _ = i.ref
            name = spec.panels[pi].series[si].name.get("en")
            out.setdefault(name, set()).add(i.fill)
    return out


def test_colours_follow_the_series_name_across_panels():
    spec = two_panels(["Arm X", "Arm Y"], ["Arm Y", "Arm X"])
    fills = fill_by_name(spec, fs.layout(spec))
    assert all(len(v) == 1 for v in fills.values()), fills
    assert fills["Arm X"] != fills["Arm Y"]


def test_the_legend_is_the_union_of_all_panels():
    spec = two_panels(["Arm X", "Arm Y"], ["Arm Y", "Arm Z"])
    scene = fs.layout(spec)
    legend = [
        i.s for i in scene.items if isinstance(i, fs.Label) and i.role == "legend"
    ]
    assert legend == ["Arm X", "Arm Y", "Arm Z"]
    fills = fill_by_name(spec, scene)
    assert len({next(iter(v)) for v in fills.values()}) == 3


def test_duplicate_series_names_in_a_panel_are_refused():
    d = fx.grouped_bar().to_dict()
    d["panels"][0]["series"][1]["name"] = d["panels"][0]["series"][0]["name"]
    with pytest.raises(ValueError, match="unique"):
        fs.FigureSpec.from_dict(d)


# ------------------------------------------------------------------ I2 PDF substitutions


def stats_spec():
    spec = fx.tiny_bars()
    return replace(
        spec,
        title="Δ success rate (B − A), α = 0.05, κ ≥ 0.6",
        panels=(
            replace(
                spec.panels[0],
                series=(
                    fs.Series("A", (P(0, 0.4, 0.2, 0.6, "成功 8/20"), P(1, 0.2))),
                    fs.Series("B", (P(0, 0.7), P(1, 0.5))),
                ),
            ),
        ),
    )


def test_pdf_transliterates_symbols_and_reports_every_substitution():
    res = pdfplot.render_pdf_report(stats_spec())
    rep = pdfplot.verify_pdf(res.data)
    assert "Delta success rate (B - A), alpha = 0.05, kappa >= 0.6" in rep.text
    reasons = {(s["text"], s["to"], s["reason"]) for s in res.substitutions}
    assert ("Δ success rate (B − A), α = 0.05, κ ≥ 0.6",) == tuple(
        sorted({s["text"] for s in res.substitutions if s["text"].startswith("Δ")})
    )
    assert any(r[2] == "transliterated" for r in reasons)
    # the CJK point label is not silent either
    lab = [s for s in res.substitutions if "成功" in s["text"]]
    assert lab and lab[0]["reason"] == "unencodable" and "?" in lab[0]["to"]
    assert "?? 8/20" in rep.text
    assert not any("Δ" in t or "−" in t for t in rep.text)
    # render_pdf gives the same bytes
    assert pdfplot.render_pdf(stats_spec()) == res.data


def test_pdf_strict_mode_raises_instead_of_substituting():
    with pytest.raises(pdfplot.LossyTextError) as e:
        pdfplot.render_pdf_report(stats_spec(), strict=True)
    assert e.value.substitutions
    clean = pdfplot.render_pdf_report(fx.tiny_bars(), strict=True)
    assert clean.substitutions == ()


def test_pdf_chinese_fallback_is_recorded_and_the_language_tag_is_true():
    res = pdfplot.render_pdf_report(fx.grouped_bar(), lang="zh-CN")
    assert {s["reason"] for s in res.substitutions} == {"fallback_en"}
    assert res.lang == "en" and b"/Lang (en)" in res.data
    en = pdfplot.render_pdf_report(fx.grouped_bar(), lang="en")
    assert en.substitutions == () and en.lang == "en"
    only_zh = replace(fx.tiny_bars(), title={"zh-CN": "成功率"}, summary="")
    res = pdfplot.render_pdf_report(only_zh, lang="zh-CN")
    assert any(
        s["reason"] == "unencodable" and s["to"] == "[n/a]" for s in res.substitutions
    )
    assert "[n/a]" in pdfplot.verify_pdf(res.data).text


def test_svg_has_no_substitutions_and_keeps_the_symbols():
    text = svgplot.render_svg(stats_spec())
    assert "Δ success rate (B − A), α = 0.05, κ ≥ 0.6" in text
    assert fs.layout(stats_spec()).substitutions == ()


# ------------------------------------------------------------------ I3 bounded layout


def test_a_4000_character_title_is_cut_and_fast():
    spec = replace(fx.tiny_bars(), title="x" * 4000)
    t0 = time.perf_counter()
    scene = fs.layout(spec)
    assert time.perf_counter() - t0 < 0.2
    assert "title" in scene.truncated
    title_lines = [i for i in scene.items if i.role == "title"]
    assert 1 <= len(title_lines) <= 3 and title_lines[-1].s.endswith("...")
    pdfplot.render_pdf(spec)  # the PDF path is bounded too


def test_summary_and_notes_are_capped_and_the_page_cannot_grow_without_limit():
    spec = replace(
        fx.tiny_bars(),
        summary="word " * 20000,
        notes=("note " * 20000, "n2 " * 20000, "n3 " * 20000),
    )
    t0 = time.perf_counter()
    scene = fs.layout(spec)
    assert time.perf_counter() - t0 < 0.5
    assert {"summary", "note"} <= set(scene.truncated)
    assert scene.height < 700
    assert len([i for i in scene.items if i.role == "summary"]) <= 4
    assert len([i for i in scene.items if i.role == "note"]) <= 3 * 6


def test_short_text_is_not_marked_truncated():
    assert fs.layout(fx.grouped_bar()).truncated == ()


def test_wrap_of_one_long_word_is_linear():
    t0 = time.perf_counter()
    lines = fs.wrap("y" * 20000, 10, 100)
    assert time.perf_counter() - t0 < 0.2
    assert "".join(lines) == "y" * 20000


# ------------------------------------------------------------------ I4 interface rules


def test_an_estimate_outside_its_interval_is_kept_with_a_warning():
    spec = fx.forest()
    pt = spec.panels[0].series[1].points[0]
    bad = replace(pt, x=0.55)  # bootstrap interval [0.08, 0.50] does not contain 0.55
    series = list(spec.panels[0].series)
    series[1] = replace(series[1], points=(bad, *series[1].points[1:]))
    spec = replace(spec, panels=(replace(spec.panels[0], series=tuple(series)),))
    fs.validate(spec)  # no longer an error
    assert fs.warnings(spec) == ["estimate_outside_interval:panel0/series1/point0"]
    _, rows = fs.table(spec)
    assert any(r[6] == "estimate outside interval" for r in rows)
    scene = fs.layout(spec)
    assert "1 estimate lies outside its interval; see the table." in texts(scene)
    assert fs.warnings(fx.forest()) == []
    inverted = replace(pt, lo=0.6, hi=0.5)
    series[1] = replace(series[1], points=(inverted, *series[1].points[1:]))
    with pytest.raises(ValueError, match="lo must not exceed hi"):
        fs.validate(
            replace(spec, panels=(replace(spec.panels[0], series=tuple(series)),))
        )


def test_an_unavailable_series_is_labelled_and_draws_nothing():
    spec = fx.grouped_bar()
    series = (*spec.panels[0].series, fs.Series("Arm D", (), unavailable=True))
    spec = replace(spec, panels=(replace(spec.panels[0], series=series),))
    fs.validate(spec)
    again = fs.FigureSpec.from_json(spec.to_json())
    assert again == spec and again.panels[0].series[-1].unavailable
    scene = fs.layout(spec)
    assert "Arm D (unavailable)" in [
        i.s for i in scene.items if isinstance(i, fs.Label) and i.role == "legend"
    ]
    assert not [i for i in scene.items if getattr(i, "ref", None) and i.ref[1] == 3]
    _, rows = fs.table(spec)
    assert ("Arm D", "unavailable") in {(r[1], r[6]) for r in rows}
    assert "1 series unavailable; see the table." in texts(scene)
    with pytest.raises(
        ValueError, match="no points"
    ):  # empty is only for unavailable ones
        fs.validate(
            replace(
                spec, panels=(replace(spec.panels[0], series=(fs.Series("e", ()),)),)
            )
        )
    with pytest.raises(ValueError, match="unavailable"):
        fs.validate(
            replace(
                spec,
                panels=(
                    replace(
                        spec.panels[0],
                        series=(fs.Series("e", (P(0, 1),), unavailable=True),),
                    ),
                ),
            )
        )


# ------------------------------------------------------------------ the agreement matrix (F7)


def confusion(counts=((12, 3, 5), (2, 14, 4)), title="Arm A"):
    pts = []
    for r, row in enumerate(counts):
        for c, n in enumerate(row):
            pts.append(P(c, r, value=n))
    return fs.FigureSpec(
        id="f7-agreement",
        kind="confusion_matrix",
        title={"en": "Judge agreement", "zh-CN": "判定一致性"},
        summary="Automatic verdict against the operator label.",
        panels=(
            fs.Panel(
                fs.Axis(
                    label={"en": "Automatic verdict", "zh-CN": "自动判定"},
                    kind="category",
                    categories=("success", "failure", "undecided"),
                ),
                fs.Axis(
                    label={"en": "Operator label", "zh-CN": "操作员标签"},
                    kind="category",
                    categories=("success", "failure"),
                ),
                (fs.Series("agreement", tuple(pts)),),
                title=title,
            ),
        ),
    )


def test_confusion_matrix_draws_counts_shares_and_shades_by_share():
    spec = confusion()
    scene = fs.layout(spec)
    cells = [i for i in scene.items if isinstance(i, fs.Rect) and i.role == "cell"]
    assert len(cells) == 6
    by_ref = {c.ref: c for c in cells}
    # row 0 total is 20: shares 60%, 15%, 25%; row 1 total is 20: 10%, 70%, 20%
    assert fs.luminance(by_ref[(0, 0, 0)].fill) < fs.luminance(by_ref[(0, 0, 1)].fill)
    assert fs.luminance(by_ref[(0, 0, 4)].fill) < fs.luminance(by_ref[(0, 0, 3)].fill)
    t = texts(scene)
    assert "12" in t and "60%" in t and "14" in t and "70%" in t
    assert t.count("n = 20") == 2  # row totals
    g = scene.panels[0]
    for (pi, si, pj), c in by_ref.items():
        p = spec.panels[pi].series[si].points[pj]
        assert c.x == pytest.approx(g.x.px(p.x) - c.w / 2, abs=1e-6)
        assert c.y == pytest.approx(g.y.px(p.y) - c.h / 2, abs=1e-6)
    fs.validate_render(spec, scene)
    assert g.y.px(0) < g.y.px(1)  # first row on top


def test_confusion_matrix_table_has_counts_and_shares():
    headers, rows = fs.table(confusion())
    assert headers == (
        "Panel",
        "Series",
        "Row",
        "Column",
        "Count",
        "Share of row",
        "Note",
    )
    assert len(rows) == 6
    assert rows[0] == ("Arm A", "agreement", "success", "success", "12", "0.6", "")
    assert rows[4][4:6] == ("14", "0.7")
    zh = fs.table(confusion(), "zh-CN")[0]
    assert zh[4] == "计数"


@pytest.mark.parametrize(
    "mut,msg",
    [
        (
            lambda d: d["panels"][0]["series"][0]["points"][0].pop("value"),
            "needs a value",
        ),
        (
            lambda d: d["panels"][0]["series"][0]["points"][0].update(value=-1),
            "negative",
        ),
        (
            lambda d: d["panels"][0]["series"][0]["points"].append(
                {"x": 0, "y": 0, "value": 3}
            ),
            "twice",
        ),
        (
            lambda d: d["panels"][0]["x_axis"].update(kind="linear", categories=[]),
            "category",
        ),
    ],
)
def test_confusion_matrix_validation(mut, msg):
    d = confusion().to_dict()
    mut(d)
    with pytest.raises(ValueError, match=msg):
        fs.FigureSpec.from_dict(d)


def test_value_is_only_for_the_matrix():
    d = fx.grouped_bar().to_dict()
    d["panels"][0]["series"][0]["points"][0]["value"] = 3
    with pytest.raises(ValueError, match="only for confusion_matrix"):
        fs.FigureSpec.from_dict(d)


def test_confusion_matrix_renders_in_svg_and_pdf_for_two_arms():
    a, b = confusion(), confusion(((8, 1, 1), (3, 9, 0)), title="Arm B")
    spec = replace(a, panels=(a.panels[0], b.panels[0]))
    svgplot.render_svg(spec, lang="zh-CN")
    rep = pdfplot.verify_pdf(pdfplot.render_pdf(spec))
    assert "Arm B" in rep.text and "60%" in rep.text
    # no legend for a heat map
    assert not [i for i in fs.layout(spec).items if i.role == "legend"]


# ------------------------------------------------------------------ smaller review items


def test_control_characters_in_text_are_refused():
    with pytest.raises(ValueError, match="control character"):
        fs.validate(replace(fx.tiny_bars(), title="bad\x01title"))


def test_two_points_in_one_slot_are_refused():
    d = fx.grouped_bar().to_dict()
    d["panels"][0]["series"][0]["points"].append({"x": 0, "y": 0.5})
    with pytest.raises(ValueError, match="twice"):
        fs.FigureSpec.from_dict(d)


def test_whole_numbers_print_exactly():
    assert fs.num(1234567) == "1234567"
    assert fs.num(1234567.0) == "1234567"
    assert fs.num(0.1234567891) == "0.123457"


def test_json_round_trip_keeps_new_fields():
    for spec in (
        confusion(),
        step_spec([fs.Series("A", (P(0, 0), P(5, 0.2, 0.1, 0.3)))]),
    ):
        assert fs.FigureSpec.from_json(spec.to_json()) == spec
        json.loads(spec.to_json())


# ------------------------------------------------------------------ review-CP7-fixes S-b, S-c


def test_point_labels_are_cut_to_their_short_limit_and_reported():
    spec = fx.tiny_bars()
    panel = spec.panels[0]
    s0 = panel.series[0]
    long = "x" * 400
    s0 = replace(s0, points=(replace(s0.points[0], label=long),) + s0.points[1:])
    spec = replace(spec, panels=(replace(panel, series=(s0,) + panel.series[1:]),))
    for latin in (False, True):
        scene = fs.layout(spec, latin_only=latin)
        labels = [
            i.s
            for i in scene.items
            if isinstance(i, fs.Label) and i.role == "point-label"
        ]
        assert max(len(s) for s in labels) <= fs.SHORT_LIMITS["label"]
        assert any(s.endswith("...") for s in labels)
        assert "label" in scene.truncated
        for i in scene.items:
            if isinstance(i, fs.Label) and i.role == "point-label":
                half = fs.text_width(i.s, i.size) / 2
                assert 0 <= i.x - half and i.x + half <= scene.width
    _, rows = fs.table(spec)
    assert long in [c for r in rows for c in r]  # the table keeps the full label


def test_chinese_only_series_names_are_numbered_in_the_pdf():
    spec = fx.tiny_bars()
    panel = spec.panels[0]
    named = tuple(
        replace(s, name={"zh-CN": zh})
        for s, zh in zip(panel.series, ("甲组", "乙组", "丙组"), strict=False)
    )
    spec = replace(spec, panels=(replace(panel, series=named),))
    res = pdfplot.render_pdf_report(spec, lang="zh-CN")
    numbered = [s for s in res.substitutions if s["reason"] == "numbered"]
    assert [s["to"] for s in numbered] == [f"Series {k + 1}" for k in range(len(named))]
    assert [s["text"] for s in numbered] == ["甲组", "乙组", "丙组"][: len(named)]
    assert res.manifest()["pdf"].startswith("lossy(")
    text = pdfplot.verify_pdf(res.data).text
    assert all(f"Series {k + 1}" in text for k in range(len(named)))
    assert "[n/a]" not in [
        i.s
        for i in fs.layout(spec, lang="zh-CN", latin_only=True).items
        if isinstance(i, fs.Label) and i.role == "legend"
    ]
    # the SVG keeps the Chinese names
    assert "甲组" in svgplot.render_svg(spec, lang="zh-CN")
