"""FigureSpec: a renderer-independent description of one campaign figure.

The analysis library (T-CP-01) turns numbers into a ``FigureSpec``; the web
page maps the same JSON onto Recharts, and the two static writers in this
folder (``svgplot.py``, ``pdfplot.py``) draw it for a paper. This module holds
everything the writers share, so the two outputs show the same figure:

* the data model (``FigureSpec``, ``Panel``, ``Axis``, ``Series``, ``Point``,
  ``RefLine``, ``Text``) with validation and a JSON round trip;
* the colour-vision-safe print palette and the marker, line and hatch styles
  that keep series apart in grayscale (colour alone never carries a series);
* the "nice" axis tick algorithm;
* the accessible table fallback (every number in the figure, as a table);
* ``layout()``: turns a spec into a ``Scene`` of plain drawing primitives
  (lines, rectangles, polygons, circles, text). Both writers only serialise
  the scene, so geometry, text wrapping and annotations are decided once.

Pure standard library; no clock, no random numbers, no environment: the same
spec always gives the same scene, and the writers give the same bytes.
Coordinates are in points with the origin at the top left (y grows down).
Text is measured with the Helvetica advance widths (the PDF base-14 font), so
wrapping is right for the PDF and close for any sans-serif face in the SVG.
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

SCHEMA = "levi.aeri.figure_spec.v1"
LANGS = ("en", "zh-CN")
KINDS = (
    "grouped_bar",  # F1: one bar per arm and category, with an interval
    "forest",  # F2: paired differences, one row per comparison
    "step_curve",  # F3: time-to-success staircase, one curve per arm
    "stacked_bar",  # F4: failure modes stacked per arm
    "early_stop",  # F5: bars with intervals, usually two panels
    "drift_lines",  # F6: per-round rate per arm, with intervals
)
# How each kind is drawn.
STYLE = {
    "grouped_bar": "bars",
    "early_stop": "bars",
    "stacked_bar": "stack",
    "forest": "forest",
    "step_curve": "step",
    "drift_lines": "lines",
}
MARKERS = ("circle", "square", "triangle", "diamond", "cross", "plus")
DASHES = ("solid", "dashed", "dotted", "dashdot")
DASH_PATTERNS = {
    "solid": (),
    "dashed": (6.0, 3.0),
    "dotted": (1.5, 2.5),
    "dashdot": (6.0, 2.5, 1.5, 2.5),
}
HATCHES = ("", "/", "\\", "x", "|", "-", "+", "//")
MAX_SERIES = 8
MAX_PANELS = 4
TICK_FORMATS = ("plain", "percent")

# A print palette that stays apart under protan, deutan and tritan vision
# (pairwise CIELAB distance >= 15 in all three simulations; checked in the
# tests). The order alternates light and dark so neighbouring series also
# differ in grayscale; the hatch, marker and line style differ per series as
# well, so a black-and-white copy still works.
PALETTE = (
    "#0072B2",  # blue
    "#E69F00",  # orange
    "#D55E00",  # vermillion
    "#56B4E9",  # sky blue
    "#009E73",  # green
    "#000000",  # black
    "#CC79A7",  # pink
    "#F0E442",  # yellow
)

INK = "#222222"
INK_SOFT = "#555555"
AXIS = "#444444"
GRID = "#E1E1E1"
REF = "#666666"
PAPER = "#FFFFFF"


# --------------------------------------------------------------------------
# Text in two languages
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Text:
    """A string in one or more languages (``en`` is the fallback)."""

    by_lang: tuple[tuple[str, str], ...] = ()

    @staticmethod
    def of(value: Any) -> Text:
        if isinstance(value, Text):
            return value
        if value is None:
            return Text()
        if isinstance(value, str):
            return Text((("en", value),))
        if isinstance(value, Mapping):
            pairs = []
            for lang in sorted(value):
                if lang not in LANGS:
                    raise ValueError(f"unknown language {lang!r}; use one of {LANGS}")
                if not isinstance(value[lang], str):
                    raise TypeError(f"text for {lang!r} must be a string")
                pairs.append((lang, value[lang]))
            return Text(tuple(pairs))
        raise ValueError(f"cannot read a text from {type(value).__name__}")

    def get(self, lang: str = "en", latin_only: bool = False) -> str:
        """The text in ``lang``; falls back to English, then to any language.
        With ``latin_only`` a text that Windows-1252 cannot encode is replaced
        by its English form (or by ``?`` marks) so the PDF can show it."""
        d = dict(self.by_lang)
        order = [lang, "en"] + [k for k in d if k not in (lang, "en")]
        for key in order:
            if key in d and d[key] != "":
                s = d[key]
                if not latin_only or _latin(s):
                    return s
        s = next((d[k] for k in order if k in d and d[k] != ""), "")
        return s.encode("cp1252", "replace").decode("cp1252") if latin_only else s

    def to_json(self) -> Any:
        return {k: v for k, v in self.by_lang}

    def __bool__(self) -> bool:
        return any(v for _, v in self.by_lang)


def _latin(s: str) -> bool:
    try:
        s.encode("cp1252")
        return True
    except UnicodeEncodeError:
        return False


# --------------------------------------------------------------------------
# The spec
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Axis:
    """One axis. ``category`` axes carry ``categories`` (points use the index);
    ``linear`` axes take ``min``/``max`` (either may be None: chosen from the
    data) and a tick format."""

    label: Text = field(default_factory=Text)
    kind: str = "linear"
    categories: tuple[Text, ...] = ()
    min: float | None = None
    max: float | None = None
    fmt: str = "plain"
    unit: str = ""

    def __post_init__(self):
        object.__setattr__(self, "label", Text.of(self.label))
        object.__setattr__(
            self, "categories", tuple(Text.of(c) for c in self.categories)
        )
        object.__setattr__(self, "min", _opt_num(self.min))
        object.__setattr__(self, "max", _opt_num(self.max))


@dataclass(frozen=True)
class Point:
    """One datum. ``lo``/``hi`` are the interval on the value axis (y; for a
    forest figure x). ``label`` is a short note drawn next to the mark."""

    x: float
    y: float
    lo: float | None = None
    hi: float | None = None
    label: str = ""


@dataclass(frozen=True)
class Series:
    name: Text
    points: tuple[Point, ...]
    marker: str | None = None  # default: by index
    dash: str | None = None
    emphasis: bool = False  # the reference arm: drawn heavier

    def __post_init__(self):
        object.__setattr__(self, "name", Text.of(self.name))
        object.__setattr__(
            self,
            "points",
            tuple(p if isinstance(p, Point) else Point(**p) for p in self.points),
        )


@dataclass(frozen=True)
class RefLine:
    """A reference line across the plot (0 in a forest plot, a step cap...)."""

    axis: str  # "x" or "y": the axis whose value the line sits at
    value: float
    label: Text = field(default_factory=Text)

    def __post_init__(self):
        object.__setattr__(self, "label", Text.of(self.label))


@dataclass(frozen=True)
class Panel:
    x_axis: Axis
    y_axis: Axis
    series: tuple[Series, ...]
    title: Text = field(default_factory=Text)
    reflines: tuple[RefLine, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "title", Text.of(self.title))
        object.__setattr__(self, "series", tuple(self.series))
        object.__setattr__(self, "reflines", tuple(self.reflines))


@dataclass(frozen=True)
class FigureSpec:
    id: str
    kind: str
    title: Text
    summary: Text
    panels: tuple[Panel, ...]
    notes: tuple[Text, ...] = ()
    lang: str = "en"

    def __post_init__(self):
        object.__setattr__(self, "title", Text.of(self.title))
        object.__setattr__(self, "summary", Text.of(self.summary))
        object.__setattr__(self, "panels", tuple(self.panels))
        object.__setattr__(self, "notes", tuple(Text.of(n) for n in self.notes))

    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA,
            "id": self.id,
            "kind": self.kind,
            "lang": self.lang,
            "title": self.title.to_json(),
            "summary": self.summary.to_json(),
            "notes": [n.to_json() for n in self.notes],
            "panels": [_panel_json(p) for p in self.panels],
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )

    def digest(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    @staticmethod
    def from_dict(d: Mapping[str, Any]) -> FigureSpec:
        if d.get("schema") != SCHEMA:
            raise ValueError(f"expected schema {SCHEMA!r}, got {d.get('schema')!r}")
        spec = FigureSpec(
            id=d["id"],
            kind=d["kind"],
            lang=d.get("lang", "en"),
            title=Text.of(d["title"]),
            summary=Text.of(d.get("summary", "")),
            notes=tuple(Text.of(n) for n in d.get("notes", ())),
            panels=tuple(_panel_from(p) for p in d["panels"]),
        )
        validate(spec)
        return spec

    @staticmethod
    def from_json(s: str) -> FigureSpec:
        return FigureSpec.from_dict(json.loads(s))


def _opt_num(v):
    if v is None:
        return None
    return float(v)


def _axis_json(a: Axis) -> dict:
    d: dict[str, Any] = {"label": a.label.to_json(), "kind": a.kind, "fmt": a.fmt}
    if a.categories:
        d["categories"] = [c.to_json() for c in a.categories]
    if a.min is not None:
        d["min"] = a.min
    if a.max is not None:
        d["max"] = a.max
    if a.unit:
        d["unit"] = a.unit
    return d


def _axis_from(d: Mapping[str, Any]) -> Axis:
    return Axis(
        label=Text.of(d.get("label", "")),
        kind=d.get("kind", "linear"),
        categories=tuple(Text.of(c) for c in d.get("categories", ())),
        min=d.get("min"),
        max=d.get("max"),
        fmt=d.get("fmt", "plain"),
        unit=d.get("unit", ""),
    )


def _point_json(p: Point) -> dict:
    d: dict[str, Any] = {"x": p.x, "y": p.y}
    if p.lo is not None:
        d["lo"] = p.lo
    if p.hi is not None:
        d["hi"] = p.hi
    if p.label:
        d["label"] = p.label
    return d


def _series_json(s: Series) -> dict:
    d: dict[str, Any] = {
        "name": s.name.to_json(),
        "points": [_point_json(p) for p in s.points],
    }
    if s.marker:
        d["marker"] = s.marker
    if s.dash:
        d["dash"] = s.dash
    if s.emphasis:
        d["emphasis"] = True
    return d


def _panel_json(p: Panel) -> dict:
    return {
        "title": p.title.to_json(),
        "x_axis": _axis_json(p.x_axis),
        "y_axis": _axis_json(p.y_axis),
        "series": [_series_json(s) for s in p.series],
        "reflines": [
            {"axis": r.axis, "value": r.value, "label": r.label.to_json()}
            for r in p.reflines
        ],
    }


def _panel_from(d: Mapping[str, Any]) -> Panel:
    return Panel(
        title=Text.of(d.get("title", "")),
        x_axis=_axis_from(d["x_axis"]),
        y_axis=_axis_from(d["y_axis"]),
        series=tuple(
            Series(
                name=Text.of(s["name"]),
                points=tuple(Point(**p) for p in s["points"]),
                marker=s.get("marker"),
                dash=s.get("dash"),
                emphasis=bool(s.get("emphasis", False)),
            )
            for s in d["series"]
        ),
        reflines=tuple(
            RefLine(axis=r["axis"], value=r["value"], label=Text.of(r.get("label", "")))
            for r in d.get("reflines", ())
        ),
    )


def validate(spec: FigureSpec) -> None:
    """Raise ``ValueError`` (naming the offender) if the spec cannot be drawn."""

    def bad(msg: str):
        raise ValueError(f"figure {spec.id!r}: {msg}")

    if not spec.id or not all(c.isalnum() or c in "-_." for c in spec.id):
        bad("id must be a non-empty slug of letters, digits, '-', '_' and '.'")
    if spec.kind not in KINDS:
        bad(f"unknown kind {spec.kind!r}; use one of {KINDS}")
    if spec.lang not in LANGS:
        bad(f"unknown lang {spec.lang!r}")
    if not spec.title:
        bad("a title is required")
    if not 1 <= len(spec.panels) <= MAX_PANELS:
        bad(f"1 to {MAX_PANELS} panels required, got {len(spec.panels)}")
    style = STYLE[spec.kind]
    for pi, panel in enumerate(spec.panels):
        where = f"panel {pi}"
        for ax, name in ((panel.x_axis, "x_axis"), (panel.y_axis, "y_axis")):
            if ax.kind not in ("linear", "category"):
                bad(f"{where} {name}: kind must be 'linear' or 'category'")
            if ax.fmt not in TICK_FORMATS:
                bad(f"{where} {name}: fmt must be one of {TICK_FORMATS}")
            if ax.kind == "category" and not ax.categories:
                bad(f"{where} {name}: a category axis needs categories")
            if ax.kind == "linear" and ax.categories:
                bad(f"{where} {name}: a linear axis takes no categories")
            for v in (ax.min, ax.max):
                if v is not None and not math.isfinite(v):
                    bad(f"{where} {name}: min/max must be finite")
            if ax.min is not None and ax.max is not None and ax.min >= ax.max:
                bad(f"{where} {name}: min must be below max")
        cat_x = style in ("bars", "stack")
        if cat_x and panel.x_axis.kind != "category":
            bad(f"{where}: {spec.kind} needs a category x axis")
        if style in ("bars", "stack", "step") and panel.y_axis.kind != "linear":
            bad(f"{where}: {spec.kind} needs a linear y axis")
        if style == "forest" and (
            panel.y_axis.kind != "category" or panel.x_axis.kind != "linear"
        ):
            bad(f"{where}: forest needs a category y axis and a linear x axis")
        if style == "step" and panel.x_axis.kind != "linear":
            bad(f"{where}: step_curve needs a linear x axis")
        if style == "lines" and panel.y_axis.kind != "linear":
            bad(f"{where}: drift_lines needs a linear y axis")
        if not 1 <= len(panel.series) <= MAX_SERIES:
            bad(f"{where}: 1 to {MAX_SERIES} series required")
        index_axis = panel.x_axis if style != "forest" else panel.y_axis
        for si, s in enumerate(panel.series):
            sw = f"{where} series {si}"
            if not s.name:
                bad(f"{sw}: a name is required")
            if s.marker is not None and s.marker not in MARKERS:
                bad(f"{sw}: marker must be one of {MARKERS}")
            if s.dash is not None and s.dash not in DASHES:
                bad(f"{sw}: dash must be one of {DASHES}")
            if not s.points:
                bad(f"{sw}: no points")
            prev_x = None
            for pj, p in enumerate(s.points):
                pw = f"{sw} point {pj}"
                for name in ("x", "y"):
                    v = getattr(p, name)
                    if (
                        isinstance(v, bool)
                        or not isinstance(v, (int, float))
                        or not math.isfinite(v)
                    ):
                        bad(f"{pw}: {name} must be a finite number")
                for name in ("lo", "hi"):
                    v = getattr(p, name)
                    if v is not None and (
                        isinstance(v, bool)
                        or not isinstance(v, (int, float))
                        or not math.isfinite(v)
                    ):
                        bad(f"{pw}: {name} must be a finite number")
                if (p.lo is None) != (p.hi is None):
                    bad(f"{pw}: lo and hi come together")
                value = p.x if style == "forest" else p.y
                if p.lo is not None and not (p.lo <= value <= p.hi):
                    bad(f"{pw}: interval [{p.lo}, {p.hi}] does not contain {value}")
                if index_axis.kind == "category":
                    idx = p.y if style == "forest" else p.x
                    if idx != int(idx) or not 0 <= idx < len(index_axis.categories):
                        bad(
                            f"{pw}: category index {idx} is outside 0..{len(index_axis.categories) - 1}"
                        )
                if style == "stack" and p.y < 0:
                    bad(f"{pw}: stacked values cannot be negative")
                if style in ("step", "lines") and panel.x_axis.kind == "linear":
                    if prev_x is not None and p.x < prev_x:
                        bad(f"{pw}: x must not decrease within a series")
                    prev_x = p.x
        for ri, r in enumerate(panel.reflines):
            if r.axis not in ("x", "y"):
                bad(f"{where} refline {ri}: axis must be 'x' or 'y'")
            if not math.isfinite(r.value):
                bad(f"{where} refline {ri}: value must be finite")
            target = panel.x_axis if r.axis == "x" else panel.y_axis
            if target.kind != "linear":
                bad(f"{where} refline {ri}: sits on a category axis")


# --------------------------------------------------------------------------
# Numbers
# --------------------------------------------------------------------------


def coord(v: float) -> str:
    """A coordinate with at most two decimals, never ``-0``."""
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def num(v: float) -> str:
    """A data value for tables: up to six significant digits, never ``-0``."""
    s = f"{float(v):.6g}"
    return "0" if s == "-0" else s


@dataclass(frozen=True)
class Scale:
    """A "nice" numeric axis: ``lo``/``hi`` are the drawn range."""

    lo: float
    hi: float
    step: float
    ticks: tuple[float, ...]
    decimals: int

    def frac(self, v: float) -> float:
        return (v - self.lo) / (self.hi - self.lo)


def _nice_num(x: float, round_: bool) -> float:
    exp = math.floor(math.log10(x))
    f = x / 10.0**exp
    if round_:
        nf = 1 if f < 1.5 else 2 if f < 3 else 5 if f < 7 else 10
    else:
        nf = 1 if f <= 1 else 2 if f <= 2 else 5 if f <= 5 else 10
    return nf * 10.0**exp


def nice_scale(
    lo: float,
    hi: float,
    target: int = 5,
    fixed_lo: float | None = None,
    fixed_hi: float | None = None,
) -> Scale:
    """Tick positions on 1, 2 or 5 times a power of ten (Heckbert's "nice
    numbers" for graph labels). A fixed bound is kept exactly (ticks stay
    inside it); a free bound moves out to the next tick. A constant series
    gets a small window around the value; non-finite input is an error."""
    if not (math.isfinite(lo) and math.isfinite(hi)):
        raise ValueError("axis range must be finite")
    if lo > hi:
        lo, hi = hi, lo
    if fixed_lo is not None:
        lo = fixed_lo
    if fixed_hi is not None:
        hi = fixed_hi
    if lo > hi:
        raise ValueError("axis minimum is above the maximum")
    scale_ref = max(abs(lo), abs(hi))
    if hi - lo <= 1e-9 * scale_ref or hi == lo:
        # constant: open a window the user can read
        if fixed_lo is not None and fixed_hi is None:
            hi = lo + (abs(lo) * 0.1 or 1.0)
        elif fixed_hi is not None and fixed_lo is None:
            lo = hi - (abs(hi) * 0.1 or 1.0)
        elif scale_ref == 0:
            lo, hi = 0.0, 1.0
        else:
            half = abs(lo) * 0.1
            lo, hi = lo - half, hi + half
    span = _nice_num(hi - lo, False)
    step = _nice_num(span / max(target - 1, 1), True)
    klo = math.floor(lo / step + 1e-9)
    khi = math.ceil(hi / step - 1e-9)
    decimals = max(0, -math.floor(math.log10(step))) + 1
    if fixed_lo is not None:
        klo = math.ceil(lo / step - 1e-9)
    if fixed_hi is not None:
        khi = math.floor(hi / step + 1e-9)
    ticks = tuple(round(k * step, decimals + 1) + 0.0 for k in range(klo, khi + 1))
    if not ticks:  # a fixed window narrower than one step
        ticks = (lo, hi)
    out_lo = fixed_lo if fixed_lo is not None else ticks[0]
    out_hi = fixed_hi if fixed_hi is not None else ticks[-1]
    return Scale(
        lo=out_lo, hi=out_hi, step=step, ticks=ticks, decimals=max(0, decimals - 1)
    )


def tick_label(v: float, sc: Scale, fmt: str = "plain") -> str:
    if fmt == "percent":
        d = max(0, sc.decimals - 2)
        s = f"{v * 100:.{d}f}%"
    elif abs(v) >= 1e9 or sc.step < 1e-6:
        s = f"{v:.6g}"
    else:
        s = f"{v:.{sc.decimals}f}"
    return (
        s.replace("-0%", "0%")
        if s.startswith("-0") and float(s.rstrip("%") or 0) == 0
        else s
    )


# --------------------------------------------------------------------------
# Colours and style
# --------------------------------------------------------------------------


def _srgb_to_lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def rgb_of(hex_colour: str) -> tuple[int, int, int]:
    h = hex_colour.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def luminance(hex_colour: str) -> float:
    """WCAG relative luminance, 0 (black) to 1 (white)."""
    r, g, b = (_srgb_to_lin(c / 255.0) for c in rgb_of(hex_colour))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def blend(hex_colour: str, other: str = PAPER, t: float = 0.8) -> str:
    """``hex_colour`` mixed ``t`` of the way to ``other`` (a light tint)."""
    a, b = rgb_of(hex_colour), rgb_of(other)
    r, g, bl = (round(a[i] + (b[i] - a[i]) * t) for i in range(3))
    return f"#{r:02X}{g:02X}{bl:02X}"


@dataclass(frozen=True)
class Style:
    color: str
    marker: str
    dash: str
    hatch: str
    heavy: bool


def series_style(index: int, s: Series) -> Style:
    return Style(
        color=PALETTE[index % len(PALETTE)],
        marker=s.marker or MARKERS[index % len(MARKERS)],
        dash=s.dash or DASHES[index % len(DASHES)],
        hatch=HATCHES[index % len(HATCHES)],
        heavy=s.emphasis,
    )


# --------------------------------------------------------------------------
# The accessible table fallback
# --------------------------------------------------------------------------

_HEADERS = {
    "en": {
        "forest": ("Panel", "Series", "Row", "Estimate", "Lower", "Upper", "Note"),
        "default": ("Panel", "Series", "X", "Y", "Lower", "Upper", "Note"),
        "reference": "reference",
    },
    "zh-CN": {
        "forest": ("子图", "系列", "行", "估计值", "下限", "上限", "备注"),
        "default": ("子图", "系列", "X", "Y", "下限", "上限", "备注"),
        "reference": "参考线",
    },
}


def table(
    spec: FigureSpec, lang: str | None = None
) -> tuple[tuple[str, ...], list[tuple[str, ...]]]:
    """Every number in the figure as a table: one row per point (and per
    reference line). This is what a screen reader, a text-only report and the
    consistency tests read; the figure's marks are drawn from the same data."""
    validate(spec)
    lang = lang or spec.lang
    h = _HEADERS[lang]
    style = STYLE[spec.kind]
    headers = h["forest"] if style == "forest" else h["default"]
    rows: list[tuple[str, ...]] = []
    for pi, panel in enumerate(spec.panels):
        ptitle = panel.title.get(lang) or str(pi + 1)
        for s in panel.series:
            sname = s.name.get(lang)
            for p in s.points:
                if style == "forest":
                    first = panel.y_axis.categories[int(p.y)].get(lang)
                    est = p.x
                else:
                    first = (
                        panel.x_axis.categories[int(p.x)].get(lang)
                        if panel.x_axis.kind == "category"
                        else num(p.x)
                    )
                    est = p.y
                rows.append(
                    (
                        ptitle,
                        sname,
                        first,
                        num(est),
                        "" if p.lo is None else num(p.lo),
                        "" if p.hi is None else num(p.hi),
                        p.label,
                    )
                )
        for r in panel.reflines:
            rows.append(
                (
                    ptitle,
                    h["reference"],
                    r.label.get(lang) or r.axis,
                    num(r.value),
                    "",
                    "",
                    "",
                )
            )
    return headers, rows


def table_csv(spec: FigureSpec, lang: str | None = None) -> str:
    headers, rows = table(spec, lang)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(headers)
    w.writerows(rows)
    return buf.getvalue()


def table_html(spec: FigureSpec, lang: str | None = None) -> str:
    """A ``<table>`` fragment with a caption (the title) for a report page."""
    lang = lang or spec.lang
    headers, rows = table(spec, lang)
    e = html.escape
    out = [f"<table><caption>{e(spec.title.get(lang))}</caption>", "<thead><tr>"]
    out += [f'<th scope="col">{e(c)}</th>' for c in headers]
    out.append("</tr></thead><tbody>")
    for r in rows:
        out.append("<tr>" + "".join(f"<td>{e(c)}</td>" for c in r) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


# --------------------------------------------------------------------------
# Text measurement (Helvetica advance widths, 1000 units per em)
# --------------------------------------------------------------------------

_W = {
    " ": 278,
    "!": 278,
    '"': 355,
    "#": 556,
    "$": 556,
    "%": 889,
    "&": 667,
    "'": 191,
    "(": 333,
    ")": 333,
    "*": 389,
    "+": 584,
    ",": 278,
    "-": 333,
    ".": 278,
    "/": 278,
    ":": 278,
    ";": 278,
    "<": 584,
    "=": 584,
    ">": 584,
    "?": 556,
    "@": 1015,
    "A": 667,
    "B": 667,
    "C": 722,
    "D": 722,
    "E": 667,
    "F": 611,
    "G": 778,
    "H": 722,
    "I": 278,
    "J": 500,
    "K": 667,
    "L": 556,
    "M": 833,
    "N": 722,
    "O": 778,
    "P": 667,
    "Q": 778,
    "R": 722,
    "S": 667,
    "T": 611,
    "U": 722,
    "V": 667,
    "W": 944,
    "X": 667,
    "Y": 667,
    "Z": 611,
    "[": 278,
    "\\": 278,
    "]": 278,
    "^": 469,
    "_": 556,
    "`": 333,
    "a": 556,
    "b": 556,
    "c": 500,
    "d": 556,
    "e": 556,
    "f": 278,
    "g": 556,
    "h": 556,
    "i": 222,
    "j": 222,
    "k": 500,
    "l": 222,
    "m": 833,
    "n": 556,
    "o": 556,
    "p": 556,
    "q": 556,
    "r": 333,
    "s": 500,
    "t": 278,
    "u": 556,
    "v": 500,
    "w": 722,
    "x": 500,
    "y": 500,
    "z": 500,
    "{": 334,
    "|": 260,
    "}": 334,
    "~": 584,
}
for _d in "0123456789":
    _W[_d] = 556


def _is_wide(ch: str) -> bool:
    o = ord(ch)
    return (
        0x2E80 <= o <= 0xA4CF
        or 0xAC00 <= o <= 0xD7A3
        or 0xF900 <= o <= 0xFAFF
        or 0xFE30 <= o <= 0xFE6F
        or 0xFF00 <= o <= 0xFF60
        or 0xFFE0 <= o <= 0xFFE6
    )


def text_width(s: str, size: float, bold: bool = False) -> float:
    total = 0
    for ch in s:
        total += 1000 if _is_wide(ch) else _W.get(ch, 556)
    return total * size / 1000.0 * (1.06 if bold else 1.0)


def wrap(
    s: str, size: float, max_w: float, bold: bool = False, max_lines: int | None = None
) -> list[str]:
    """Greedy word wrap; CJK text breaks between any two characters. A word
    longer than the line is broken by character. Too many lines end in ``...``."""
    tokens: list[str] = []
    cur = ""
    for ch in s:
        if ch == " ":
            if cur:
                tokens.append(cur)
                cur = ""
        elif _is_wide(ch):
            if cur:
                tokens.append(cur)
                cur = ""
            tokens.append(ch + "\0")  # "\0" marks: no space before the next token
        else:
            cur += ch
    if cur:
        tokens.append(cur)
    lines: list[str] = []
    line = ""
    for tok in tokens:
        glue = tok.endswith("\0")
        tok = tok.rstrip("\0")
        sep = "" if (not line or line_glue(line)) else " "
        trial = line + sep + tok
        if not line or text_width(trial, size, bold) <= max_w:
            line = trial
        else:
            lines.append(line)
            line = tok
        # a single token wider than the line: break it
        while text_width(line, size, bold) > max_w and len(line) > 1:
            cut = len(line)
            while cut > 1 and text_width(line[:cut], size, bold) > max_w:
                cut -= 1
            lines.append(line[:cut])
            line = line[cut:]
        if glue:
            line += "\0"
    if line:
        lines.append(line)
    lines = [ln.replace("\0", "") for ln in lines] or [""]
    if max_lines is not None and len(lines) > max_lines:
        kept = lines[:max_lines]
        last = kept[-1]
        while last and text_width(last + "...", size, bold) > max_w:
            last = last[:-1]
        kept[-1] = last.rstrip() + "..."
        lines = kept
    return lines


def line_glue(line: str) -> bool:
    return line.endswith("\0")


# --------------------------------------------------------------------------
# The scene: plain drawing primitives
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Line:
    x1: float
    y1: float
    x2: float
    y2: float
    stroke: str = INK
    width: float = 1.0
    dash: tuple[float, ...] = ()
    role: str = "line"
    ref: tuple[int, int, int] | None = None  # (panel, series, point)


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float
    fill: str | None = None
    stroke: str | None = None
    stroke_width: float = 0.8
    role: str = "rect"
    ref: tuple[int, int, int] | None = None


@dataclass(frozen=True)
class Poly:
    points: tuple[tuple[float, float], ...]
    stroke: str | None = INK
    width: float = 1.0
    dash: tuple[float, ...] = ()
    fill: str | None = None
    closed: bool = False
    role: str = "poly"
    ref: tuple[int, int, int] | None = None


@dataclass(frozen=True)
class Circle:
    cx: float
    cy: float
    r: float
    fill: str | None = None
    stroke: str | None = INK
    stroke_width: float = 1.0
    role: str = "marker"
    ref: tuple[int, int, int] | None = None


@dataclass(frozen=True)
class Label:
    x: float
    y: float  # baseline
    s: str
    size: float = 9.0
    anchor: str = "start"  # start | middle | end
    bold: bool = False
    fill: str = INK
    rotate: int = 0  # 0 or -90 (counter-clockwise)
    role: str = "text"


Item = Line | Rect | Poly | Circle | Label


@dataclass(frozen=True)
class AxisMap:
    """Maps data values of one axis to page coordinates."""

    kind: str
    p0: float  # page coordinate of the axis start (left or bottom)
    p1: float  # page coordinate of the axis end (right or top)
    scale: Scale | None = None
    n_categories: int = 0

    def px(self, v: float) -> float:
        if self.kind == "category":
            return self.p0 + (self.p1 - self.p0) * (v + 0.5) / self.n_categories
        assert self.scale is not None
        return self.p0 + (self.p1 - self.p0) * self.scale.frac(v)


@dataclass(frozen=True)
class PanelGeometry:
    left: float
    top: float
    width: float
    height: float
    x: AxisMap
    y: AxisMap


@dataclass(frozen=True)
class Scene:
    width: float
    height: float
    lang: str
    title: str
    desc: str
    items: tuple[Item, ...]
    panels: tuple[PanelGeometry, ...]


class _Resolver:
    def __init__(self, lang: str, latin_only: bool):
        self.lang = lang
        self.latin_only = latin_only

    def __call__(self, t: Text) -> str:
        return t.get(self.lang, self.latin_only)

    def plain(self, s: str) -> str:
        return s.encode("cp1252", "replace").decode("cp1252") if self.latin_only else s


def _dash(name: str) -> tuple[float, ...]:
    return DASH_PATTERNS[name]


def _marker_items(
    shape: str,
    cx: float,
    cy: float,
    size: float,
    color: str,
    ref=None,
    fill: str | None = None,
) -> list[Item]:
    r = size / 2.0
    fill = color if fill is None else fill
    if shape == "circle":
        return [Circle(cx, cy, r, fill=fill, stroke=INK, stroke_width=0.8, ref=ref)]
    if shape == "square":
        pts = ((cx - r, cy - r), (cx + r, cy - r), (cx + r, cy + r), (cx - r, cy + r))
    elif shape == "triangle":
        pts = (
            (cx, cy - r * 1.15),
            (cx + r * 1.05, cy + r * 0.85),
            (cx - r * 1.05, cy + r * 0.85),
        )
    elif shape == "diamond":
        pts = (
            (cx, cy - r * 1.2),
            (cx + r * 1.2, cy),
            (cx, cy + r * 1.2),
            (cx - r * 1.2, cy),
        )
    elif shape == "cross":  # an x
        d = r * 0.95
        return [
            Line(
                cx - d,
                cy - d,
                cx + d,
                cy + d,
                stroke=color,
                width=2.0,
                role="marker",
                ref=ref,
            ),
            Line(
                cx - d,
                cy + d,
                cx + d,
                cy - d,
                stroke=color,
                width=2.0,
                role="marker",
                ref=ref,
            ),
        ]
    else:  # plus
        d = r * 1.1
        return [
            Line(
                cx - d, cy, cx + d, cy, stroke=color, width=2.0, role="marker", ref=ref
            ),
            Line(
                cx, cy - d, cx, cy + d, stroke=color, width=2.0, role="marker", ref=ref
            ),
        ]
    return [
        Poly(pts, stroke=INK, width=0.8, fill=fill, closed=True, role="marker", ref=ref)
    ]


def _clip_segment(c: float, sign: int, x: float, y: float, w: float, h: float):
    """The part of the line ``x_coord + sign * y_coord = c`` inside the
    rectangle, or None (``sign`` +1: runs up to the right; -1: down)."""
    pts = set()
    for xx in (x, x + w):
        yy = (c - xx) * sign
        if y - 1e-9 <= yy <= y + h + 1e-9:
            pts.add((xx, yy))
    for yy in (y, y + h):
        xx = c - sign * yy
        if x - 1e-9 <= xx <= x + w + 1e-9:
            pts.add((xx, yy))
    ordered = sorted(pts)
    if len(ordered) < 2:
        return None
    return ordered[0], ordered[-1]


def _hatch_items(
    x: float, y: float, w: float, h: float, hatch: str, fill: str, ref=None
) -> list[Item]:
    """Hatch lines clipped to a rectangle by geometry (no clip paths)."""
    if not hatch or w <= 0 or h <= 0:
        return []
    ink = "#FFFFFF" if luminance(fill) < 0.3 else "#222222"
    gap = 2.6 if hatch == "//" else 4.0
    out: list[Item] = []

    def add(seg):
        if seg:
            (x1, y1), (x2, y2) = seg
            out.append(
                Line(x1, y1, x2, y2, stroke=ink, width=0.6, role="hatch", ref=ref)
            )

    if hatch in ("/", "//", "x"):  # x + y = c  (rises to the right on the page)
        c = math.ceil((x + y) / gap) * gap
        while c <= x + w + y + h:
            add(_clip_segment(c, 1, x, y, w, h))
            c += gap
    if hatch in ("\\", "x"):  # x - y = c  (falls to the right)
        c = math.ceil((x - y - h) / gap) * gap
        while c <= x + w - y:
            add(_clip_segment(c, -1, x, y, w, h))
            c += gap
    if hatch in ("|", "+"):
        xx = x + gap / 2
        while xx < x + w:
            add(((xx, y), (xx, y + h)))
            xx += gap
    if hatch in ("-", "+"):
        yy = y + gap / 2
        while yy < y + h:
            add(((x, yy), (x + w, yy)))
            yy += gap
    return out


# layout constants (points)
PAD = 16.0
TITLE_SIZE = 14.0
SUMMARY_SIZE = 10.0
LABEL_SIZE = 10.0
TICK_SIZE = 9.0
NOTE_SIZE = 8.0
LINE_H = 1.25


def _extent(panel: Panel, style: str) -> tuple[float, float, float, float]:
    """Data extents (x_lo, x_hi, y_lo, y_hi) including intervals, 0 for bars
    and the reference lines."""
    xs: list[float] = []
    ys: list[float] = []
    if style == "stack":
        n = len(panel.x_axis.categories)
        totals = [0.0] * n
        for s in panel.series:
            for p in s.points:
                totals[int(p.x)] += p.y
        ys += [0.0] + totals
    else:
        for s in panel.series:
            for p in s.points:
                xs.append(p.x)
                ys.append(p.y)
                if p.lo is not None:
                    (xs if style == "forest" else ys).extend((p.lo, p.hi))
    if style in ("bars",):
        ys.append(0.0)
    for r in panel.reflines:
        (xs if r.axis == "x" else ys).append(r.value)
    return (
        min(xs) if xs else 0.0,
        max(xs) if xs else 1.0,
        min(ys) if ys else 0.0,
        max(ys) if ys else 1.0,
    )


def _scale_for(axis: Axis, lo: float, hi: float, target: int) -> Scale:
    return nice_scale(lo, hi, target=target, fixed_lo=axis.min, fixed_hi=axis.max)


def layout(
    spec: FigureSpec,
    lang: str | None = None,
    latin_only: bool = False,
    width: float = 640.0,
) -> Scene:
    """Turn a spec into a scene. ``latin_only`` keeps every string inside
    Windows-1252 (the PDF base-14 fonts); other text falls back to English."""
    validate(spec)
    lang = lang or spec.lang
    R = _Resolver(lang, latin_only)
    style = STYLE[spec.kind]
    items: list[Item] = []
    inner_w = width - 2 * PAD
    y = PAD

    # ---- title and summary
    title = R(spec.title)
    for ln in wrap(title, TITLE_SIZE, inner_w, bold=True):
        y += TITLE_SIZE
        items.append(Label(PAD, y, ln, TITLE_SIZE, bold=True, role="title"))
        y += TITLE_SIZE * (LINE_H - 1)
    summary = R(spec.summary)
    if summary:
        y += 3
        for ln in wrap(summary, SUMMARY_SIZE, inner_w):
            y += SUMMARY_SIZE * LINE_H
            items.append(Label(PAD, y, ln, SUMMARY_SIZE, fill=INK_SOFT, role="summary"))
    y += 8

    # ---- legend (series of the first panel; every panel uses the same order)
    legend_series = spec.panels[0].series
    if len(legend_series) > 1:
        y = _legend(items, legend_series, style, R, y, inner_w)

    # ---- panels
    n = len(spec.panels)
    gap = 14.0
    pw = (inner_w - gap * (n - 1)) / n
    geoms: list[PanelGeometry] = []
    panel_h = 0.0
    for panel in spec.panels:
        panel_h = max(panel_h, _panel_height(panel, style))
    for pi, panel in enumerate(spec.panels):
        left = PAD + pi * (pw + gap)
        geoms.append(
            _draw_panel(items, spec, panel, pi, style, R, left, y, pw, panel_h)
        )
    y += panel_h + 8

    # ---- notes
    for note in spec.notes:
        for ln in wrap(R(note), NOTE_SIZE, inner_w):
            y += NOTE_SIZE * LINE_H
            items.append(Label(PAD, y, ln, NOTE_SIZE, fill=INK_SOFT, role="note"))
    y += PAD
    desc = summary
    return Scene(
        width=width,
        height=math.ceil(y),
        lang=lang,
        title=title,
        desc=desc,
        items=tuple(items),
        panels=tuple(geoms),
    )


def _legend(
    items: list[Item],
    series: Sequence[Series],
    style: str,
    R: _Resolver,
    y: float,
    inner_w: float,
) -> float:
    row_h = 15.0
    x = PAD
    y_row = y
    sample_w = 26.0
    for si, s in enumerate(series):
        st = series_style(si, s)
        name = R(s.name)
        w = sample_w + 6 + text_width(name, TICK_SIZE) + 16
        if x > PAD and x + w > PAD + inner_w:
            x = PAD
            y_row += row_h
        cy = y_row + row_h / 2 - 1
        if style in ("bars", "stack"):
            items.append(
                Rect(
                    x,
                    cy - 5,
                    12,
                    10,
                    fill=st.color,
                    stroke=INK,
                    stroke_width=0.6,
                    role="legend",
                )
            )
            items += [
                replace_role(i, "legend")
                for i in _hatch_items(x, cy - 5, 12, 10, st.hatch, st.color)
            ]
            tx = x + 12 + 5
        else:
            items.append(
                Line(
                    x,
                    cy,
                    x + sample_w,
                    cy,
                    stroke=st.color,
                    width=2.5 if st.heavy else 1.5,
                    dash=_dash(st.dash) if style != "forest" else (),
                    role="legend",
                )
            )
            items += [
                replace_role(i, "legend")
                for i in _marker_items(st.marker, x + sample_w / 2, cy, 7, st.color)
            ]
            tx = x + sample_w + 6
        items.append(Label(tx, cy + 3.2, name, TICK_SIZE, role="legend"))
        x += w
    return y_row + row_h + 6


def replace_role(item: Item, role: str) -> Item:
    from dataclasses import replace

    return replace(item, role=role)


def _panel_height(panel: Panel, style: str) -> float:
    if style == "forest":
        rows = len(panel.y_axis.categories)
        per = 18.0 + 9.0 * len(panel.series)
        return max(150.0, 30.0 + rows * per + 44.0)
    return 250.0


def _draw_panel(
    items: list[Item],
    spec: FigureSpec,
    panel: Panel,
    pi: int,
    style: str,
    R: _Resolver,
    left: float,
    top: float,
    pw: float,
    ph: float,
) -> PanelGeometry:
    x_lo, x_hi, y_lo, y_hi = _extent(panel, style)
    ptitle = R(panel.title)
    if ptitle:
        items.append(Label(left, top + 10, ptitle, 10.5, bold=True, role="panel-title"))
        top += 18
        ph -= 18

    # --- scales
    xs = ys = None
    if panel.x_axis.kind == "linear":
        target = 6 if pw > 280 else 4
        if style == "lines" and x_hi > x_lo:  # keep the end points off the axis
            pad = (x_hi - x_lo) * 0.06
            xs = nice_scale(
                x_lo,
                x_hi,
                target,
                fixed_lo=x_lo - pad if panel.x_axis.min is None else panel.x_axis.min,
                fixed_hi=x_hi + pad if panel.x_axis.max is None else panel.x_axis.max,
            )
        else:
            xs = _scale_for(panel.x_axis, x_lo, x_hi, target)
    if panel.y_axis.kind == "linear":
        ys = _scale_for(panel.y_axis, y_lo, y_hi, 6)

    # --- margins
    ylabel = R(panel.y_axis.label)
    xlabel = R(panel.x_axis.label)
    if style == "forest":
        cat_w = min(
            150.0,
            max(
                (text_width(R(c), TICK_SIZE) for c in panel.y_axis.categories),
                default=0,
            )
            + 2,
        )
        left_lab = cat_w
    else:
        assert ys is not None
        left_lab = max(
            text_width(tick_label(t, ys, panel.y_axis.fmt), TICK_SIZE) for t in ys.ticks
        )
    m_left = (14.0 if ylabel and style != "forest" else 0.0) + left_lab + 8
    annot_w = 0.0
    if style == "forest":
        annot_w = max(
            (
                text_width(p.label, NOTE_SIZE)
                for s in panel.series
                for p in s.points
                if p.label
            ),
            default=0.0,
        )
        annot_w = min(annot_w + 8, pw * 0.35) if annot_w else 0.0
    m_right = 10.0 + annot_w
    plot_l = left + m_left
    plot_r = left + pw - m_right
    plot_w = plot_r - plot_l

    # x tick label lines (category labels wrap into the cell)
    xlines: list[list[str]] = []
    if panel.x_axis.kind == "category":
        cell = plot_w / len(panel.x_axis.categories)
        xlines = [
            wrap(R(c), TICK_SIZE, max(cell - 6, 20), max_lines=3)
            for c in panel.x_axis.categories
        ]
        tick_h = max(len(x) for x in xlines) * TICK_SIZE * LINE_H + 4
    else:
        tick_h = TICK_SIZE * LINE_H + 4
    xlabel_h = (LABEL_SIZE * LINE_H + 2) if xlabel else 0.0
    plot_t = top + 8
    plot_b = top + ph - tick_h - xlabel_h - 6
    plot_h = plot_b - plot_t

    # --- axis maps
    if style == "forest":
        xmap = AxisMap("linear", plot_l, plot_r, xs)
        # row 0 at the top: map index to page y
        ymap = AxisMap("category", plot_t, plot_b, None, len(panel.y_axis.categories))
    else:
        if panel.x_axis.kind == "category":
            xmap = AxisMap(
                "category", plot_l, plot_r, None, len(panel.x_axis.categories)
            )
        else:
            xmap = AxisMap("linear", plot_l, plot_r, xs)
        ymap = AxisMap("linear", plot_b, plot_t, ys)

    # --- grid, axes, ticks
    if style == "forest":
        assert xs is not None
        for t in xs.ticks:
            px = xmap.px(t)
            items.append(
                Line(px, plot_t, px, plot_b, stroke=GRID, width=0.6, role="grid")
            )
            items.append(
                Line(px, plot_b, px, plot_b + 3, stroke=AXIS, width=0.8, role="tick")
            )
            items.append(
                Label(
                    px,
                    plot_b + 3 + TICK_SIZE,
                    tick_label(t, xs, panel.x_axis.fmt),
                    TICK_SIZE,
                    "middle",
                    role="tick-label",
                )
            )
        for ci, c in enumerate(panel.y_axis.categories):
            cy = ymap.px(ci)
            lines = wrap(R(c), TICK_SIZE, left_lab, max_lines=2)
            ty = cy - (len(lines) - 1) * TICK_SIZE * LINE_H / 2 + TICK_SIZE * 0.35
            for ln in lines:
                items.append(
                    Label(plot_l - 6, ty, ln, TICK_SIZE, "end", role="tick-label")
                )
                ty += TICK_SIZE * LINE_H
    else:
        assert ys is not None
        for t in ys.ticks:
            py = ymap.px(t)
            items.append(
                Line(plot_l, py, plot_r, py, stroke=GRID, width=0.6, role="grid")
            )
            items.append(
                Line(plot_l - 3, py, plot_l, py, stroke=AXIS, width=0.8, role="tick")
            )
            items.append(
                Label(
                    plot_l - 5,
                    py + TICK_SIZE * 0.35,
                    tick_label(t, ys, panel.y_axis.fmt),
                    TICK_SIZE,
                    "end",
                    role="tick-label",
                )
            )
        if panel.x_axis.kind == "category":
            for ci, lines in enumerate(xlines):
                cx = xmap.px(ci)
                items.append(
                    Line(
                        cx, plot_b, cx, plot_b + 3, stroke=AXIS, width=0.8, role="tick"
                    )
                )
                ty = plot_b + 3 + TICK_SIZE
                for ln in lines:
                    items.append(
                        Label(cx, ty, ln, TICK_SIZE, "middle", role="tick-label")
                    )
                    ty += TICK_SIZE * LINE_H
        else:
            assert xs is not None
            for t in xs.ticks:
                px = xmap.px(t)
                items.append(
                    Line(
                        px, plot_b, px, plot_b + 3, stroke=AXIS, width=0.8, role="tick"
                    )
                )
                items.append(
                    Label(
                        px,
                        plot_b + 3 + TICK_SIZE,
                        tick_label(t, xs, panel.x_axis.fmt),
                        TICK_SIZE,
                        "middle",
                        role="tick-label",
                    )
                )
    # frame: left and bottom axis lines
    items.append(
        Line(plot_l, plot_t, plot_l, plot_b, stroke=AXIS, width=0.9, role="axis")
    )
    items.append(
        Line(plot_l, plot_b, plot_r, plot_b, stroke=AXIS, width=0.9, role="axis")
    )
    if xlabel:
        items.append(
            Label(
                (plot_l + plot_r) / 2,
                top + ph - 3,
                xlabel,
                LABEL_SIZE,
                "middle",
                role="axis-label",
            )
        )
    if ylabel and style != "forest":
        items.append(
            Label(
                left + 9,
                (plot_t + plot_b) / 2,
                ylabel,
                LABEL_SIZE,
                "middle",
                rotate=-90,
                role="axis-label",
            )
        )
    elif ylabel and style == "forest":
        items.append(
            Label(
                plot_l,
                plot_t - 2,
                ylabel,
                NOTE_SIZE,
                "start",
                fill=INK_SOFT,
                role="axis-label",
            )
        )

    geom = PanelGeometry(
        left=plot_l, top=plot_t, width=plot_w, height=plot_h, x=xmap, y=ymap
    )

    # --- reference lines (under the data)
    for r in panel.reflines:
        if r.axis == "x":
            assert xs is not None
            px = xmap.px(r.value)
            items.append(
                Line(
                    px,
                    plot_t,
                    px,
                    plot_b,
                    stroke=REF,
                    width=1.0,
                    dash=(4.0, 3.0),
                    role="refline",
                )
            )
            lab = R(r.label)
            if lab:
                if (
                    px + 3 + text_width(lab, NOTE_SIZE) > plot_r + 8
                ):  # no room on the right
                    items.append(
                        Label(
                            px - 3,
                            plot_t + NOTE_SIZE + 1,
                            lab,
                            NOTE_SIZE,
                            "end",
                            fill=INK_SOFT,
                            role="refline-label",
                        )
                    )
                else:
                    items.append(
                        Label(
                            px + 3,
                            plot_t + NOTE_SIZE + 1,
                            lab,
                            NOTE_SIZE,
                            fill=INK_SOFT,
                            role="refline-label",
                        )
                    )
        else:
            assert ys is not None
            py = ymap.px(r.value)
            items.append(
                Line(
                    plot_l,
                    py,
                    plot_r,
                    py,
                    stroke=REF,
                    width=1.0,
                    dash=(4.0, 3.0),
                    role="refline",
                )
            )
            lab = R(r.label)
            if lab:
                items.append(
                    Label(
                        plot_r - 3,
                        py - 3,
                        lab,
                        NOTE_SIZE,
                        "end",
                        fill=INK_SOFT,
                        role="refline-label",
                    )
                )

    # --- marks
    draw = {
        "bars": _bars,
        "stack": _stack,
        "forest": _forest,
        "step": _step,
        "lines": _lines,
    }[style]
    draw(items, panel, pi, geom)
    return geom


def _bars(items: list[Item], panel: Panel, pi: int, g: PanelGeometry) -> None:
    ncat = len(panel.x_axis.categories)
    ns = len(panel.series)
    cell = g.width / ncat
    group_w = cell * 0.74
    bar_w = group_w / ns
    base = g.y.px(0.0)
    for si, s in enumerate(panel.series):
        st = series_style(si, s)
        for pj, p in enumerate(s.points):
            ref = (pi, si, pj)
            cx = g.x.px(p.x) - group_w / 2 + bar_w * (si + 0.5)
            top = g.y.px(p.y)
            x0, w = cx - bar_w / 2 + 0.5, bar_w - 1.0
            y0, h = min(top, base), abs(base - top)
            items.append(
                Rect(
                    x0,
                    y0,
                    w,
                    h,
                    fill=st.color,
                    stroke=INK,
                    stroke_width=0.7,
                    role="bar",
                    ref=ref,
                )
            )
            items += _hatch_items(x0, y0, w, h, st.hatch, st.color, ref)
            tip = top
            if p.lo is not None:
                lo_y, hi_y = g.y.px(p.lo), g.y.px(p.hi)
                items.append(
                    Line(
                        cx,
                        lo_y,
                        cx,
                        hi_y,
                        stroke=PAPER,
                        width=3.0,
                        role="ci-halo",
                        ref=ref,
                    )
                )
                for yy in (lo_y, hi_y):
                    items.append(
                        Line(
                            cx - 3.5,
                            yy,
                            cx + 3.5,
                            yy,
                            stroke=PAPER,
                            width=3.0,
                            role="ci-halo",
                            ref=ref,
                        )
                    )
                items.append(
                    Line(cx, lo_y, cx, hi_y, stroke=INK, width=1.2, role="ci", ref=ref)
                )
                for yy in (lo_y, hi_y):
                    items.append(
                        Line(
                            cx - 3.5,
                            yy,
                            cx + 3.5,
                            yy,
                            stroke=INK,
                            width=1.2,
                            role="ci-cap",
                            ref=ref,
                        )
                    )
                tip = min(tip, lo_y, hi_y)
            if p.label:
                items.append(
                    Label(
                        cx,
                        tip - 3,
                        p.label,
                        NOTE_SIZE,
                        "middle",
                        role="point-label",
                    )
                )


def _stack(items: list[Item], panel: Panel, pi: int, g: PanelGeometry) -> None:
    ncat = len(panel.x_axis.categories)
    cell = g.width / ncat
    bar_w = min(cell * 0.6, 70.0)
    cum = [0.0] * ncat
    for si, s in enumerate(panel.series):
        st = series_style(si, s)
        for pj, p in enumerate(s.points):
            ci = int(p.x)
            lo_v, hi_v = cum[ci], cum[ci] + p.y
            cum[ci] = hi_v
            y_top, y_bot = g.y.px(hi_v), g.y.px(lo_v)
            cx = g.x.px(ci)
            ref = (pi, si, pj)
            h = y_bot - y_top
            if h <= 0:
                continue
            items.append(
                Rect(
                    cx - bar_w / 2,
                    y_top,
                    bar_w,
                    h,
                    fill=st.color,
                    stroke=INK,
                    stroke_width=0.7,
                    role="bar",
                    ref=ref,
                )
            )
            items += _hatch_items(
                cx - bar_w / 2, y_top, bar_w, h, st.hatch, st.color, ref
            )
            if h >= 12 and p.y > 0:
                lab = p.label or num(p.y)
                lw = text_width(lab, NOTE_SIZE) + 4
                items.append(
                    Rect(
                        cx - lw / 2,
                        (y_top + y_bot) / 2 - 5.5,
                        lw,
                        10.5,
                        fill=PAPER,
                        role="label-backing",
                        ref=ref,
                    )
                )
                items.append(
                    Label(
                        cx,
                        (y_top + y_bot) / 2 + 2.8,
                        lab,
                        NOTE_SIZE,
                        "middle",
                        fill=INK,
                        role="point-label",
                    )
                )
    for ci in range(ncat):
        if cum[ci] > 0:
            items.append(
                Label(
                    g.x.px(ci),
                    g.y.px(cum[ci]) - 3,
                    num(cum[ci]),
                    NOTE_SIZE,
                    "middle",
                    bold=True,
                    role="total-label",
                )
            )


def _forest(items: list[Item], panel: Panel, pi: int, g: PanelGeometry) -> None:
    ns = len(panel.series)
    row_h = g.height / len(panel.y_axis.categories)
    off_step = min(10.0, row_h * 0.6 / max(ns, 1))
    for si, s in enumerate(panel.series):
        st = series_style(si, s)
        for pj, p in enumerate(s.points):
            ref = (pi, si, pj)
            cy = g.y.px(p.y) + (si - (ns - 1) / 2) * off_step
            cx = g.x.px(p.x)
            right = cx
            if p.lo is not None:
                x1, x2 = g.x.px(p.lo), g.x.px(p.hi)
                items.append(
                    Line(x1, cy, x2, cy, stroke=st.color, width=1.8, role="ci", ref=ref)
                )
                for xx in (x1, x2):
                    items.append(
                        Line(
                            xx,
                            cy - 3,
                            xx,
                            cy + 3,
                            stroke=st.color,
                            width=1.4,
                            role="ci-cap",
                            ref=ref,
                        )
                    )
                right = max(x2, cx)
            items += _marker_items(st.marker, cx, cy, 7, st.color, ref)
            if p.label:
                items.append(
                    Label(
                        right + 6,
                        cy + NOTE_SIZE * 0.35,
                        p.label,
                        NOTE_SIZE,
                        role="point-label",
                    )
                )


def _step_path(pts: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    out = [pts[0]]
    for (_x0, y0), (x1, y1) in pairwise(pts):
        out.append((x1, y0))
        out.append((x1, y1))
    return out


def _step(items: list[Item], panel: Panel, pi: int, g: PanelGeometry) -> None:
    for si, s in enumerate(panel.series):
        st = series_style(si, s)
        pts = [(g.x.px(p.x), g.y.px(p.y)) for p in s.points]
        if all(p.lo is not None for p in s.points) and len(pts) > 1:
            up = _step_path([(g.x.px(p.x), g.y.px(p.hi)) for p in s.points])
            dn = _step_path([(g.x.px(p.x), g.y.px(p.lo)) for p in s.points])
            items.append(
                Poly(
                    tuple(up + dn[::-1]),
                    stroke=None,
                    fill=blend(st.color),
                    closed=True,
                    role="band",
                    ref=(pi, si, 0),
                )
            )
        path = _step_path(pts)
        items.append(
            Poly(
                tuple(path),
                stroke=st.color,
                width=2.6 if st.heavy else 1.6,
                dash=_dash(st.dash),
                role="curve",
                ref=(pi, si, 0),
            )
        )
        last = len(s.points) - 1
        items += _marker_items(
            st.marker, pts[last][0], pts[last][1], 7, st.color, (pi, si, last)
        )
        for pj, p in enumerate(s.points):
            if p.label:
                items.append(
                    Label(
                        pts[pj][0] + 5,
                        pts[pj][1] - 4,
                        p.label,
                        NOTE_SIZE,
                        role="point-label",
                    )
                )


def _lines(items: list[Item], panel: Panel, pi: int, g: PanelGeometry) -> None:
    ns = len(panel.series)
    for si, s in enumerate(panel.series):
        st = series_style(si, s)
        dx = (si - (ns - 1) / 2) * 3.0
        pts = [(g.x.px(p.x) + dx, g.y.px(p.y)) for p in s.points]
        if len(pts) > 1:
            items.append(
                Poly(
                    tuple(pts),
                    stroke=st.color,
                    width=2.6 if st.heavy else 1.4,
                    dash=_dash(st.dash),
                    role="curve",
                    ref=(pi, si, 0),
                )
            )
        for pj, p in enumerate(s.points):
            ref = (pi, si, pj)
            x, yv = pts[pj]
            if p.lo is not None:
                y1, y2 = g.y.px(p.lo), g.y.px(p.hi)
                items.append(
                    Line(x, y1, x, y2, stroke=st.color, width=1.0, role="ci", ref=ref)
                )
                for yy in (y1, y2):
                    items.append(
                        Line(
                            x - 2.5,
                            yy,
                            x + 2.5,
                            yy,
                            stroke=st.color,
                            width=1.0,
                            role="ci-cap",
                            ref=ref,
                        )
                    )
            items += _marker_items(
                st.marker, x, yv, 6.5 if st.heavy else 5.5, st.color, ref
            )
            if p.label:
                items.append(
                    Label(
                        x,
                        (g.y.px(p.hi) if p.hi is not None else yv) - 6,
                        p.label,
                        NOTE_SIZE,
                        "middle",
                        role="point-label",
                    )
                )
