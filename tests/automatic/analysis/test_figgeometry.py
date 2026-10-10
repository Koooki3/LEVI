"""Interval geometry read back from the SVG itself (review-CP7-fixes N1).

The data-to-page mapping is fitted from the SVG's own tick marks and tick
labels, not taken from the layout code, so a drawing that is wrong in the
same way as its own bookkeeping is still caught. Every point with an
interval must show it with a visible extent equal to the interval: a step
band from the point to the next point, or an error bar where there is no
next point (the curve's end)."""

import re
import xml.etree.ElementTree as ET
from dataclasses import replace

import pytest
import test_figfixtures as fx

from levi.automatic.analysis import figspec as fs
from levi.automatic.analysis import svgplot

P = fs.Point
NS = "{http://www.w3.org/2000/svg}"
TOL = 0.06  # points: coordinates are written with two decimals


def step_spec(*series):
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
            ),
        ),
    )


def _value(label):
    s = label.replace("−", "-").replace(",", "")
    if s.endswith("%"):
        return float(s[:-1]) / 100
    return float(s)


def _fit(pairs):
    (v0, p0), (v1, p1) = pairs[0], pairs[-1]
    b = (p1 - p0) / (v1 - v0)
    for v, p in pairs:  # every tick on one straight line
        assert abs(p0 + b * (v - v0) - p) < TOL
    return lambda v: p0 + b * (v - v0)


class Svg:
    """Read back an SVG: axis maps from the ticks, lines and polygons."""

    def __init__(self, text):
        root = ET.fromstring(text.encode("utf-8"))
        self.lines = [
            tuple(float(e.get(k)) for k in ("x1", "y1", "x2", "y2"))
            + (e.get("stroke"),)
            for e in root.iter(f"{NS}line")
        ]
        self.texts = [
            (float(e.get("x")), float(e.get("y")), e.get("text-anchor"), e.text or "")
            for e in root.iter(f"{NS}text")
        ]
        self.bands = [
            [tuple(map(float, p.split(","))) for p in e.get("points").split()]
            for e in root.iter(f"{NS}polygon")
            if e.get("stroke") is None
        ]
        ticks = [ln for ln in self.lines if ln[4] == fs.AXIS]
        ytick = [ln for ln in ticks if ln[1] == ln[3] and abs(ln[2] - ln[0] - 3) < 1e-6]
        xtick = [ln for ln in ticks if ln[0] == ln[2] and abs(ln[3] - ln[1] - 3) < 1e-6]
        ypairs, xpairs = [], []
        for x1, y, _x2, _y2, _ in ytick:
            lab = [
                t for t in self.texts if t[2] == "end" and abs(t[1] - y - 3.15) < TOL
            ]
            ypairs.append((_value(lab[0][3]), y))
        for x, y1, _x2, _y2, _ in xtick:
            lab = [
                t
                for t in self.texts
                if t[2] == "middle" and abs(t[0] - x) < TOL and t[1] > y1
            ]
            if lab and re.fullmatch(r"[\d.,%−-]+", lab[0][3]):
                xpairs.append((_value(lab[0][3]), x))
        self.y = _fit(sorted(ypairs))
        self.x = _fit(sorted(xpairs))

    def band_section(self, x):
        """(top, bottom) of the band polygons on the vertical line X = x."""
        ys = []
        for poly in self.bands:
            for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
                if (x1 <= x < x2) or (x2 <= x < x1):
                    ys.append(y1 + (y2 - y1) * (x - x1) / (x2 - x1))
        return (min(ys), max(ys)) if ys else None

    def error_bar(self, x, y_lo, y_hi):
        """A vertical line at x from y_lo to y_hi (either direction)."""
        want = sorted((y_lo, y_hi))
        return any(
            abs(x1 - x) < TOL
            and abs(x2 - x) < TOL
            and abs(min(y1, y2) - want[0]) < TOL
            and abs(max(y1, y2) - want[1]) < TOL
            for x1, y1, x2, y2, _ in self.lines
        )


def assert_every_interval_visible(spec, series_index=0):
    svg = Svg(svgplot.render_svg(spec))
    s = spec.panels[0].series[series_index]
    pts = s.points
    for j, p in enumerate(pts):
        lo, hi, kind = fs.effective_interval("step", s, p)
        if kind not in ("given", "zero"):
            continue
        y_lo, y_hi = svg.y(lo), svg.y(hi)
        if j + 1 < len(pts) and pts[j + 1].x > p.x:
            x_mid = svg.x((p.x + pts[j + 1].x) / 2)
            section = svg.band_section(x_mid)
            assert section is not None, f"point {j}: no band between it and the next"
            assert section == pytest.approx((min(y_lo, y_hi), max(y_lo, y_hi)), abs=TOL)
        else:
            assert svg.error_bar(svg.x(p.x), y_lo, y_hi), f"point {j}: no error bar"
            if hi > lo:
                assert abs(y_hi - y_lo) > 1, "a real interval spans more than a point"


def test_the_end_of_a_curve_shows_its_interval():
    # the review's example: Arm C ends at x=200 with 50 % [25 %, 75 %]
    spec = fx.step_curve()
    svg = Svg(svgplot.render_svg(spec))
    assert svg.error_bar(svg.x(200), svg.y(0.25), svg.y(0.75))
    assert_every_interval_visible(spec, series_index=2)


def test_a_point_before_a_gap_keeps_its_interval_up_to_the_next_point():
    # x=20 has [0.4, 0.6]; x=30 has none: the band must reach from 20 to 30
    s = fs.Series("A", (P(10, 0.3, 0.2, 0.4), P(20, 0.5, 0.4, 0.6), P(30, 0.6)))
    spec = step_spec(s)
    assert_every_interval_visible(spec)
    svg = Svg(svgplot.render_svg(spec))
    assert svg.band_section(svg.x(29.5)) == pytest.approx(
        (svg.y(0.6), svg.y(0.4)), abs=TOL
    )
    assert svg.band_section(svg.x(30.5)) is None  # and stops there


@pytest.mark.parametrize(
    "points",
    [
        (P(0, 0), P(60, 0.1, 0.02, 0.2), P(120, 0.3, 0.1, 0.5)),
        (P(50, 0.1), P(100, 0.2, 0.1, 0.3), P(150, 0.3, 0.2, 0.4), P(200, 0.4)),
        (P(100, 0.3, 0.1, 0.5),),
        (P(10, 0.2, 0.1, 0.3), P(10, 0.4, 0.3, 0.5), P(20, 0.5, 0.4, 0.6)),
        (P(0, 0, 0, 0.1), P(80, 0.1, 0.02, 0.3), P(130, 0.3, 0.1, 0.5)),
    ],
    ids=["km-shape", "gaps", "single", "same-x", "from-zero"],
)
def test_every_step_interval_is_visible(points):
    assert_every_interval_visible(step_spec(fs.Series("A", points)))


# ----------------------------------------------------------- validate_render measures


def _scene(spec):
    return fs.layout(spec)


def test_validate_render_refuses_a_band_of_zero_width():
    spec = step_spec(fs.Series("A", (P(0, 0.1, 0.0, 0.2), P(50, 0.3, 0.2, 0.4))))
    scene = _scene(spec)
    items = [
        replace(i, spans=tuple((a, a) for a, _b in i.spans)) if i.role == "band" else i
        for i in scene.items
    ]
    with pytest.raises(ValueError, match="not drawn visibly"):
        fs.validate_render(spec, replace(scene, items=tuple(items)))


def test_validate_render_refuses_a_band_with_no_height():
    spec = step_spec(fs.Series("A", (P(0, 0.1, 0.0, 0.2), P(50, 0.3, 0.2, 0.4))))
    scene = _scene(spec)
    items = []
    for i in scene.items:
        if i.role == "band":  # the upper edge replaced by the lower one
            n = len(i.points) // 2
            lower = i.points[n:][::-1]
            i = replace(i, points=tuple(lower) + tuple(lower[::-1]))
        items.append(i)
    with pytest.raises(ValueError, match="not drawn visibly"):
        fs.validate_render(spec, replace(scene, items=tuple(items)))


@pytest.mark.parametrize(
    "spec_fn", [fx.drift_lines, fx.grouped_bar, fx.forest, fx.step_curve]
)
def test_validate_render_refuses_an_error_bar_collapsed_to_a_point(spec_fn):
    spec = spec_fn()
    scene = _scene(spec)
    items = []
    for i in scene.items:
        if i.role == "ci" and isinstance(i, fs.Line):
            i = replace(i, x2=i.x1, y2=i.y1)
        items.append(i)
    with pytest.raises(ValueError, match="not drawn visibly"):
        fs.validate_render(spec, replace(scene, items=tuple(items)))


def test_a_degenerate_interval_needs_no_extent():
    # S = 0 gives the Kaplan-Meier interval [0, 0]: a single value, nothing to span
    spec = step_spec(fs.Series("A", (P(0, 0), P(40, 0.5, 0.2, 0.7), P(80, 1, 1, 1))))
    fs.layout(spec)
    assert_every_interval_visible(spec)
