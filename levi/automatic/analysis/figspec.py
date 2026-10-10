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
from dataclasses import dataclass, field, replace
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
    "confusion_matrix",  # F7: judge agreement, counts and shares per cell
)
# How each kind is drawn.
STYLE = {
    "grouped_bar": "bars",
    "early_stop": "bars",
    "stacked_bar": "stack",
    "forest": "forest",
    "step_curve": "step",
    "drift_lines": "lines",
    "confusion_matrix": "heat",
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
        With ``latin_only`` the text is made printable with the PDF base-14
        fonts (see ``latin``)."""
        if latin_only:
            return self.latin(lang)[0]
        d = dict(self.by_lang)
        for key in [lang, "en", *d]:
            if d.get(key):
                return d[key]
        return ""

    def latin(self, lang: str = "en") -> tuple[str, list[dict]]:
        """The text as Windows-1252 and what had to change to get there: a
        form that already fits (the requested language first, then English,
        then any), else a transliteration (``Δ`` to ``Delta``), else ``?``
        marks (``[n/a]`` when nothing readable is left). Each change is a
        ``{"text", "to", "reason"}`` record: nothing is replaced silently."""
        d = dict(self.by_lang)
        order = [lang, "en", *[k for k in d if k not in (lang, "en")]]
        order = [k for k in order if d.get(k)]
        if not order:
            return "", []
        first = order[0]
        for k in order:
            if _latin(d[k]):
                if k == first:
                    return d[k], []
                return d[k], [{"text": d[first], "to": d[k], "reason": f"fallback_{k}"}]
        for k in order:
            folded, ok = fold_latin(d[k])
            if ok:
                reason = "transliterated" if k == first else f"fallback_{k}"
                return folded, [{"text": d[k], "to": folded, "reason": reason}]
        return _unencodable(d[first])

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


# Symbols that Windows-1252 lacks but statistics figures use, with a readable
# ASCII form (the PDF base-14 fonts cannot show the originals).
TRANSLITERATE = {
    "\u2212": "-",  # minus sign
    "\u2010": "-",
    "\u2011": "-",
    "\u2264": "<=",
    "\u2265": ">=",
    "\u2260": "!=",
    "\u2248": "~=",
    "\u2192": "->",
    "\u2190": "<-",
    "\u221e": "inf",
    "\u221a": "sqrt",
    "\u2032": "'",
    "\u2033": '"',
    "\u0394": "Delta",
    "\u03b1": "alpha",
    "\u03b2": "beta",
    "\u03b3": "gamma",
    "\u03b4": "delta",
    "\u03b5": "epsilon",
    "\u03b7": "eta",
    "\u03b8": "theta",
    "\u03ba": "kappa",
    "\u03bb": "lambda",
    "\u03bc": "mu",
    "\u03bd": "nu",
    "\u03c0": "pi",
    "\u03c1": "rho",
    "\u03c3": "sigma",
    "\u03c4": "tau",
    "\u03c6": "phi",
    "\u03c7": "chi",
    "\u03c9": "omega",
    "\u0393": "Gamma",
    "\u0398": "Theta",
    "\u039b": "Lambda",
    "\u03a0": "Pi",
    "\u03a3": "Sigma",
    "\u03a6": "Phi",
    "\u03a9": "Omega",
}


def fold_latin(s: str) -> tuple[str, bool]:
    """``s`` with the symbols of ``TRANSLITERATE`` spelled out. The flag says
    whether everything could be mapped (otherwise unmapped characters are ``?``)."""
    out: list[str] = []
    ok = True
    for ch in s:
        if _latin(ch):
            out.append(ch)
        elif ch in TRANSLITERATE:
            out.append(TRANSLITERATE[ch])
        else:
            out.append("?")
            ok = False
    return "".join(out), ok


def _unencodable(s: str) -> tuple[str, list[dict]]:
    folded, _ = fold_latin(s)
    if not any(c.isascii() and c.isalnum() for c in folded):
        folded = "[n/a]"
    return folded, [{"text": s, "to": folded, "reason": "unencodable"}]


def latin_plain(s: str) -> tuple[str, list[dict]]:
    """The same for a plain string (a point label)."""
    if _latin(s):
        return s, []
    folded, ok = fold_latin(s)
    if ok:
        return folded, [{"text": s, "to": folded, "reason": "transliterated"}]
    return _unencodable(s)


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
    value: float | None = None  # confusion_matrix only: the cell's count


@dataclass(frozen=True)
class Series:
    name: Text
    points: tuple[Point, ...]
    marker: str | None = None  # default: by index
    dash: str | None = None
    emphasis: bool = False  # the reference arm: drawn heavier
    unavailable: bool = False  # a group with no data: named, drawn as empty

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
    if p.value is not None:
        d["value"] = p.value
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
    if s.unavailable:
        d["unavailable"] = True
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
                unavailable=bool(s.get("unavailable", False)),
            )
            for s in d["series"]
        ),
        reflines=tuple(
            RefLine(axis=r["axis"], value=r["value"], label=Text.of(r.get("label", "")))
            for r in d.get("reflines", ())
        ),
    )


def _all_texts(spec: FigureSpec):
    """Every string a reader will see, with a path for error messages."""
    yield "title", spec.title
    yield "summary", spec.summary
    for i, n in enumerate(spec.notes):
        yield f"note {i}", n
    for pi, panel in enumerate(spec.panels):
        yield f"panel {pi} title", panel.title
        for name, ax in (("x_axis", panel.x_axis), ("y_axis", panel.y_axis)):
            yield f"panel {pi} {name} label", ax.label
            for ci, c in enumerate(ax.categories):
                yield f"panel {pi} {name} category {ci}", c
        for si, s in enumerate(panel.series):
            yield f"panel {pi} series {si} name", s.name
            for pj, p in enumerate(s.points):
                yield f"panel {pi} series {si} point {pj} label", p.label
        for ri, r in enumerate(panel.reflines):
            yield f"panel {pi} refline {ri} label", r.label


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def validate(spec: FigureSpec) -> None:
    """Raise ``ValueError`` (naming the offender) if the spec cannot be drawn.
    Things that are odd but legitimate (an estimate outside its interval, a
    group with no data) are not errors; ``warnings(spec)`` lists them."""

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
    for where, t in _all_texts(spec):
        strings = [v for _, v in t.by_lang] if isinstance(t, Text) else [t]
        if any(ord(c) < 32 or ord(c) == 127 for v in strings for c in v):
            bad(f"{where}: a control character in the text")
    if not 1 <= len(spec.panels) <= MAX_PANELS:
        bad(f"1 to {MAX_PANELS} panels required, got {len(spec.panels)}")
    style = STYLE[spec.kind]
    union: list[Text] = []
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
        if style in ("bars", "stack") and panel.x_axis.kind != "category":
            bad(f"{where}: {spec.kind} needs a category x axis")
        if style in ("bars", "stack", "step") and panel.y_axis.kind != "linear":
            bad(f"{where}: {spec.kind} needs a linear y axis")
        if style == "forest" and (
            panel.y_axis.kind != "category" or panel.x_axis.kind != "linear"
        ):
            bad(f"{where}: forest needs a category y axis and a linear x axis")
        if style == "heat" and (
            panel.y_axis.kind != "category" or panel.x_axis.kind != "category"
        ):
            bad(f"{where}: confusion_matrix needs category x and y axes")
        if style == "step" and panel.x_axis.kind != "linear":
            bad(f"{where}: step_curve needs a linear x axis")
        if style == "lines" and panel.y_axis.kind != "linear":
            bad(f"{where}: drift_lines needs a linear y axis")
        if not 1 <= len(panel.series) <= MAX_SERIES:
            bad(f"{where}: 1 to {MAX_SERIES} series required")
        if style == "heat" and len(panel.series) != 1:
            bad(f"{where}: a confusion matrix takes one series per panel")
        names = [s.name for s in panel.series]
        if len(set(names)) != len(names):
            bad(f"{where}: series names must be unique within a panel")
        for n in names:
            if n not in union:
                union.append(n)
        for si, s in enumerate(panel.series):
            sw = f"{where} series {si}"
            if not s.name:
                bad(f"{sw}: a name is required")
            if s.marker is not None and s.marker not in MARKERS:
                bad(f"{sw}: marker must be one of {MARKERS}")
            if s.dash is not None and s.dash not in DASHES:
                bad(f"{sw}: dash must be one of {DASHES}")
            if s.unavailable and s.points:
                bad(f"{sw}: an unavailable series has no points")
            if not s.points and not s.unavailable:
                bad(
                    f"{sw}: no points (mark the series unavailable if there is no data)"
                )
            prev_x = None
            seen: set = set()
            for pj, p in enumerate(s.points):
                pw = f"{sw} point {pj}"
                for name in ("x", "y"):
                    if not _is_number(getattr(p, name)):
                        bad(f"{pw}: {name} must be a finite number")
                for name in ("lo", "hi"):
                    v = getattr(p, name)
                    if v is not None and not _is_number(v):
                        bad(f"{pw}: {name} must be a finite number")
                if (p.lo is None) != (p.hi is None):
                    bad(f"{pw}: lo and hi come together")
                if p.lo is not None and p.lo > p.hi:
                    bad(f"{pw}: lo must not exceed hi")
                if style == "stack" and p.lo is not None:
                    bad(f"{pw}: stacked_bar does not support intervals")
                if style == "heat":
                    if p.lo is not None:
                        bad(f"{pw}: confusion_matrix does not support intervals")
                    if p.value is None:
                        bad(f"{pw}: a confusion matrix cell needs a value")
                    if not _is_number(p.value):
                        bad(f"{pw}: value must be a finite number")
                    if p.value < 0:
                        bad(f"{pw}: value cannot be negative")
                elif p.value is not None:
                    bad(f"{pw}: value is only for confusion_matrix")
                axes = {
                    "forest": ((panel.y_axis, p.y),),
                    "heat": ((panel.x_axis, p.x), (panel.y_axis, p.y)),
                }.get(
                    style,
                    ((panel.x_axis, p.x),) if panel.x_axis.kind == "category" else (),
                )
                for ax, idx in axes:
                    if idx != int(idx) or not 0 <= idx < len(ax.categories):
                        bad(
                            f"{pw}: category index {idx} is outside 0..{len(ax.categories) - 1}"
                        )
                if axes:
                    slot = tuple(int(i) for _, i in axes)
                    if slot in seen:
                        bad(f"{pw}: the slot {slot} is given twice in this series")
                    seen.add(slot)
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
    if len(union) > MAX_SERIES:
        bad(f"at most {MAX_SERIES} distinct series names across the panels")


def effective_interval(style: str, s: Series, p: Point):
    """What the figure shows for a point's interval: ``(lo, hi, kind)`` with
    kind ``given`` (as supplied), ``zero`` (a step curve at x = 0 that has an
    interval elsewhere: no uncertainty before the first event, so the value
    is its own interval), ``missing`` (the series has intervals but this point
    does not) or ``none`` (the series has no intervals at all)."""
    if p.lo is not None:
        return p.lo, p.hi, "given"
    if not any(q.lo is not None for q in s.points):
        return None, None, "none"
    if style == "step" and p.x == 0:
        return p.y, p.y, "zero"
    return None, None, "missing"


def _estimate(style: str, p: Point) -> float:
    return p.x if style == "forest" else p.y


def warnings(spec: FigureSpec) -> list[str]:
    """Legitimate but notable things, as stable codes with their position:
    ``estimate_outside_interval`` (a bootstrap interval need not contain the
    point estimate), ``interval_unavailable`` and ``series_unavailable``."""
    validate(spec)
    style = STYLE[spec.kind]
    out: list[str] = []
    for pi, panel in enumerate(spec.panels):
        for si, s in enumerate(panel.series):
            if s.unavailable:
                out.append(f"series_unavailable:panel{pi}/series{si}")
            for pj, p in enumerate(s.points):
                at = f"panel{pi}/series{si}/point{pj}"
                lo, hi, kind = effective_interval(style, s, p)
                if kind == "missing":
                    out.append(f"interval_unavailable:{at}")
                elif kind == "given" and not lo <= _estimate(style, p) <= hi:
                    out.append(f"estimate_outside_interval:{at}")
    return out


# --------------------------------------------------------------------------
# Numbers
# --------------------------------------------------------------------------


def coord(v: float) -> str:
    """A coordinate with at most two decimals, never ``-0``."""
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def num(v: float) -> str:
    """A data value for tables: whole numbers exactly, otherwise up to six
    significant digits; never ``-0``."""
    v = float(v)
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    s = f"{v:.6g}"
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
    """A tick's text: fixed decimals matched to the step, a percent sign for
    ``percent``, and compact ``g`` notation for huge values. Never ``-0``."""
    if fmt == "percent":
        s = f"{v * 100:.{max(0, sc.decimals - 2)}f}%"
    elif abs(v) >= 1e9 or sc.decimals > 12:
        sig = 2 if v == 0 else max(2, math.floor(math.log10(abs(v) / sc.step)) + 2)
        s = f"{v:.{sig}g}"
    else:
        s = f"{v:.{sc.decimals}f}"
    if s.startswith("-") and not any(c in "123456789" for c in s):
        s = s[1:]
    return s


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
        "heat": (
            "Panel",
            "Series",
            "Row",
            "Column",
            "Count",
            "Share of row",
            "Note",
        ),
        "reference": "reference",
    },
    "zh-CN": {
        "forest": ("子图", "系列", "行", "估计值", "下限", "上限", "备注"),
        "default": ("子图", "系列", "X", "Y", "下限", "上限", "备注"),
        "heat": ("子图", "系列", "行", "列", "计数", "行占比", "备注"),
        "reference": "参考线",
    },
}

# Strings the figure itself adds (table notes, captions, legend marks).
_WORDS = {
    "en": {
        "interval_unavailable": "interval unavailable",
        "no_uncertainty": "no uncertainty at 0",
        "outside": "estimate outside interval",
        "unavailable": "unavailable",
        "legend_unavailable": "{name} (unavailable)",
        "cap_interval": {
            1: "Interval unavailable for 1 point; see the table.",
            "n": "Interval unavailable for {n} points; see the table.",
        },
        "cap_outside": {
            1: "1 estimate lies outside its interval; see the table.",
            "n": "{n} estimates lie outside their intervals; see the table.",
        },
        "cap_series": {
            1: "1 series unavailable; see the table.",
            "n": "{n} series unavailable; see the table.",
        },
        "row_total": "n = {n}",
    },
    "zh-CN": {
        "interval_unavailable": "区间不可用",
        "no_uncertainty": "起点无不确定性",
        "outside": "估计值在区间之外",
        "unavailable": "无数据",
        "legend_unavailable": "{name}（无数据）",
        "cap_interval": {
            1: "1 个点的区间不可用，见表格。",
            "n": "{n} 个点的区间不可用，见表格。",
        },
        "cap_outside": {
            1: "1 个估计值落在其区间之外，见表格。",
            "n": "{n} 个估计值落在其区间之外，见表格。",
        },
        "cap_series": {
            1: "1 个系列无数据，见表格。",
            "n": "{n} 个系列无数据，见表格。",
        },
        "row_total": "n = {n}",
    },
}


def _caption(lang: str, key: str, n: int) -> str:
    forms = _WORDS[lang][key]
    return forms[1].format(n=n) if n == 1 else forms["n"].format(n=n)


def _point_notes(style: str, s: Series, p: Point, lang: str) -> tuple[str, str, str]:
    """(lower, upper, note) for a table row: the interval as drawn and the
    flags that explain a missing or odd one."""
    w = _WORDS[lang]
    lo, hi, kind = effective_interval(style, s, p)
    flags = [p.label] if p.label else []
    if kind == "missing":
        flags.append(w["interval_unavailable"])
    elif kind == "zero":
        flags.append(w["no_uncertainty"])
    elif kind == "given" and not lo <= _estimate(style, p) <= hi:
        flags.append(w["outside"])
    return (
        "" if lo is None else num(lo),
        "" if hi is None else num(hi),
        "; ".join(flags),
    )


def table(
    spec: FigureSpec, lang: str | None = None
) -> tuple[tuple[str, ...], list[tuple[str, ...]]]:
    """Every number in the figure as a table: one row per point (and per
    reference line, and per unavailable series). This is what a screen reader,
    a text-only report and the consistency tests read; the figure's marks are
    drawn from the same data."""
    validate(spec)
    lang = lang or spec.lang
    h = _HEADERS[lang]
    style = STYLE[spec.kind]
    headers = (
        h["forest"]
        if style == "forest"
        else h["heat" if style == "heat" else "default"]
    )
    rows: list[tuple[str, ...]] = []
    for pi, panel in enumerate(spec.panels):
        ptitle = panel.title.get(lang) or str(pi + 1)
        for s in panel.series:
            sname = s.name.get(lang)
            if s.unavailable:
                rows.append(
                    (ptitle, sname, "", "", "", "", _WORDS[lang]["unavailable"])
                )
            totals: dict[int, float] = {}
            if style == "heat":
                for p in s.points:
                    totals[int(p.y)] = totals.get(int(p.y), 0.0) + p.value
            for p in s.points:
                if style == "heat":
                    total = totals[int(p.y)]
                    share = p.value / total if total else 0.0
                    rows.append(
                        (
                            ptitle,
                            sname,
                            panel.y_axis.categories[int(p.y)].get(lang),
                            panel.x_axis.categories[int(p.x)].get(lang),
                            num(p.value),
                            num(share),
                            p.label,
                        )
                    )
                    continue
                if style == "forest":
                    first = panel.y_axis.categories[int(p.y)].get(lang)
                else:
                    first = (
                        panel.x_axis.categories[int(p.x)].get(lang)
                        if panel.x_axis.kind == "category"
                        else num(p.x)
                    )
                lo, hi, note = _point_notes(style, s, p, lang)
                rows.append(
                    (ptitle, sname, first, num(_estimate(style, p)), lo, hi, note)
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


_NO_LINE_START = set("，。、；：！？）》」』”’,.;:!?)]}%")


def _char_w(ch: str, size: float, bold: bool) -> float:
    return (
        (1000 if _is_wide(ch) else _W.get(ch, 556))
        * size
        / 1000.0
        * (1.06 if bold else 1.0)
    )


def wrap_ex(
    s: str,
    size: float,
    max_w: float,
    bold: bool = False,
    max_lines: int | None = None,
) -> tuple[list[str], bool]:
    """Greedy word wrap in one pass (time linear in the text). Lines break at
    spaces and between any two CJK characters (but not before closing
    punctuation); a word wider than the line is broken by character. Text
    beyond ``max_lines`` is dropped and the last line ends in ``...``; the
    flag says whether that happened."""
    units: list[str] = []
    for ch in s:
        if ch == " ":
            units.append(" ")
        elif _is_wide(ch):
            units.append(ch)
        elif units and units[-1] != " " and not _is_wide(units[-1][0]):
            units[-1] += ch
        else:
            units.append(ch)
    space = _char_w(" ", size, bold)
    lines: list[str] = []
    cur = ""
    cur_w = 0.0
    full = False

    def flush():
        nonlocal cur, cur_w, full
        lines.append(cur.rstrip())
        cur, cur_w = "", 0.0
        if max_lines is not None and len(lines) > max_lines:
            full = True

    for u in units:
        if full:
            break
        if u == " ":
            if cur and not cur.endswith(" "):
                cur += " "
                cur_w += space
            continue
        uw = sum(_char_w(c, size, bold) for c in u)
        if cur.strip() and cur_w + uw > max_w and u[0] not in _NO_LINE_START:
            flush()
            if full:
                break
        if uw <= max_w:
            cur += u
            cur_w += uw
            continue
        for ch in u:  # one unit wider than a line: break it by character
            cw = _char_w(ch, size, bold)
            if cur and cur_w + cw > max_w:
                flush()
                if full:
                    break
            cur += ch
            cur_w += cw
    if not full and (cur.strip() or not lines):
        lines.append(cur.rstrip())
    truncated = False
    if max_lines is not None and len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True
    if truncated:
        last = lines[-1]
        while last and text_width(last + "...", size, bold) > max_w:
            last = last[:-1]
        lines[-1] = last.rstrip() + "..."
    return lines, truncated


def wrap(
    s: str,
    size: float,
    max_w: float,
    bold: bool = False,
    max_lines: int | None = None,
) -> list[str]:
    return wrap_ex(s, size, max_w, bold, max_lines)[0]


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
    refs: tuple[tuple[int, int, int], ...] = ()  # points a band stands for
    # one (x_start, x_end) page span per entry of ``refs``: where the band
    # shows that point's interval (``validate_render`` measures it there)
    spans: tuple[tuple[float, float], ...] = ()


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
    styles: tuple[
        Style, ...
    ] = ()  # one per series of the panel (by name across panels)


@dataclass(frozen=True)
class Scene:
    width: float
    height: float
    lang: str
    title: str
    desc: str
    items: tuple[Item, ...]
    panels: tuple[PanelGeometry, ...]
    substitutions: tuple[
        dict, ...
    ] = ()  # Latin-only layout: every text that was changed
    truncated: tuple[str, ...] = ()  # roles whose text was cut to fit
    lang_used: str = "en"  # the language the title was written in


TEXT_LIMITS = {  # (max characters, max lines); longer text is cut and reported
    "title": (300, 3),
    "summary": (600, 4),
    "note": (400, 6),
}
SHORT_LIMITS = {
    "legend": 60,
    "panel-title": 80,
    "refline": 40,
    "label": 40,
    "axis": 100,
}


class _Resolver:
    """Picks the text for one language, makes it Latin for the PDF and keeps
    the books: what was substituted and what was cut."""

    def __init__(self, lang: str, latin_only: bool):
        self.lang = lang
        self.latin_only = latin_only
        self.subs: list[dict] = []
        self.cut: list[str] = []
        self.title_lang = lang

    def _note(self, recs: list[dict]) -> None:
        for r in recs:
            if r not in self.subs:
                self.subs.append(r)

    def __call__(self, t: Text) -> str:
        if not self.latin_only:
            return t.get(self.lang)
        s, recs = t.latin(self.lang)
        self._note(recs)
        return s

    def plain(self, s: str) -> str:
        if not self.latin_only or not s:
            return s
        out, recs = latin_plain(s)
        self._note(recs)
        return out

    def cap(self, s: str, role: str) -> str:
        """``s`` cut to the short-text limit of ``role`` (marked in ``cut``)."""
        n = SHORT_LIMITS[role]
        if len(s) <= n:
            return s
        if role not in self.cut:
            self.cut.append(role)
        return s[: n - 3].rstrip() + "..."

    def block(
        self, s: str, role: str, size: float, max_w: float, bold: bool = False
    ) -> list[str]:
        """Wrapped lines of a title, summary or note, within its limits."""
        max_chars, max_lines = TEXT_LIMITS[role]
        cut = len(s) > max_chars
        if cut:
            s = s[: max_chars - 3].rstrip() + "..."
        lines, more = wrap_ex(s, size, max_w, bold, max_lines)
        if (cut or more) and role not in self.cut:
            self.cut.append(role)
        return lines


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


def _latin_labels(spec: FigureSpec, R: _Resolver) -> FigureSpec:
    """The spec with every point label made Latin (and the change recorded)."""
    panels = []
    for panel in spec.panels:
        series = tuple(
            replace(
                s,
                points=tuple(
                    replace(p, label=R.plain(p.label)) if p.label else p
                    for p in s.points
                ),
            )
            for s in panel.series
        )
        panels.append(replace(panel, series=series))
    return replace(spec, panels=tuple(panels))


def layout(
    spec: FigureSpec,
    lang: str | None = None,
    latin_only: bool = False,
    width: float = 640.0,
) -> Scene:
    """Turn a spec into a scene. ``latin_only`` keeps every string inside
    Windows-1252 (the PDF base-14 fonts): symbols are transliterated, other
    text falls back to its English form, and every change is listed in
    ``Scene.substitutions``. Over-long titles, summaries and notes are cut and
    named in ``Scene.truncated``. A figure that loses one of its intervals in
    the drawing is an error (``validate_render``), never a silent omission."""
    validate(spec)
    lang = lang or spec.lang
    R = _Resolver(lang, latin_only)
    style = STYLE[spec.kind]
    if latin_only:
        spec = _latin_labels(spec, R)
    items: list[Item] = []
    inner_w = width - 2 * PAD
    y = PAD

    # ---- title and summary
    title = R(spec.title)
    for ln in R.block(title, "title", TITLE_SIZE, inner_w, bold=True):
        y += TITLE_SIZE
        items.append(Label(PAD, y, ln, TITLE_SIZE, bold=True, role="title"))
        y += TITLE_SIZE * (LINE_H - 1)
    summary = R(spec.summary)
    if summary:
        y += 3
        for ln in R.block(summary, "summary", SUMMARY_SIZE, inner_w):
            y += SUMMARY_SIZE * LINE_H
            items.append(Label(PAD, y, ln, SUMMARY_SIZE, fill=INK_SOFT, role="summary"))
    y += 8

    # ---- one legend for all panels; a series keeps its colour by name
    union: dict[Text, list[Series]] = {}
    for panel in spec.panels:
        for s in panel.series:
            union.setdefault(s.name, []).append(s)
    order = {name: i for i, name in enumerate(union)}
    if len(union) > 1 and style != "heat":
        y = _legend(items, union, style, R, y, inner_w)

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
        styles = tuple(series_style(order[s.name], s) for s in panel.series)
        geoms.append(
            _draw_panel(items, spec, panel, pi, style, R, left, y, pw, panel_h, styles)
        )
    y += panel_h + 8

    # ---- what the figure could not show, then the author's notes
    codes = warnings(spec)
    captions = [
        _caption(lang if lang in _WORDS else "en", key, count)
        for key, prefix in (
            ("cap_interval", "interval_unavailable:"),
            ("cap_outside", "estimate_outside_interval:"),
            ("cap_series", "series_unavailable:"),
        )
        if (count := sum(c.startswith(prefix) for c in codes))
    ]
    for cap in captions:
        cap = R.plain(cap)
        for ln in wrap(cap, NOTE_SIZE, inner_w):
            y += NOTE_SIZE * LINE_H
            items.append(Label(PAD, y, ln, NOTE_SIZE, fill=INK_SOFT, role="note"))
    for note in spec.notes:
        for ln in R.block(R(note), "note", NOTE_SIZE, inner_w):
            y += NOTE_SIZE * LINE_H
            items.append(Label(PAD, y, ln, NOTE_SIZE, fill=INK_SOFT, role="note"))
    y += PAD

    used = next(
        (
            k
            for k in (lang, "en", *dict(spec.title.by_lang))
            if dict(spec.title.by_lang).get(k)
        ),
        lang,
    )
    if latin_only:
        for rec in spec.title.latin(lang)[1]:
            if rec["reason"].startswith("fallback_"):
                used = rec["reason"][len("fallback_") :]
    scene = Scene(
        width=width,
        height=math.ceil(y),
        lang=lang,
        title=title,
        desc=summary,
        items=tuple(items),
        panels=tuple(geoms),
        substitutions=tuple(R.subs),
        truncated=tuple(R.cut),
        lang_used=used,
    )
    validate_render(spec, scene)
    return scene


def _cross_section(points: Sequence[tuple[float, float]], x: float) -> float:
    """Vertical extent of a closed polygon on the line ``X = x`` (0 when the
    line misses it)."""
    ys = []
    n = len(points)
    for i in range(n):
        (x1, y1), (x2, y2) = points[i], points[(i + 1) % n]
        if (x1 <= x < x2) or (x2 <= x < x1):
            ys.append(y1 + (y2 - y1) * (x - x1) / (x2 - x1))
    return max(ys) - min(ys) if len(ys) >= 2 else 0.0


RENDER_TOLERANCE = 0.01  # points: a drawn interval may differ this much


def _drawn_extents(scene: Scene, style: str) -> dict[tuple[int, int, int], list]:
    """For each point, the extents (along the value axis, in page points) of
    the interval marks drawn for it. A band counts only where it has width:
    a zero-width span gives 0."""
    out: dict[tuple[int, int, int], list] = {}
    for it in scene.items:
        if it.role == "ci" and isinstance(it, Line) and it.ref:
            ext = abs(it.x2 - it.x1) if style == "forest" else abs(it.y2 - it.y1)
            out.setdefault(it.ref, []).append(ext)
        elif it.role == "band" and isinstance(it, Poly):
            refs = it.refs or ((it.ref,) if it.ref else ())
            for k, ref in enumerate(refs):
                ext = 0.0
                if k < len(it.spans):
                    xa, xb = it.spans[k]
                    if xb - xa > 0:
                        ext = _cross_section(it.points, (xa + xb) / 2)
                out.setdefault(ref, []).append(ext)
    return out


def validate_render(spec: FigureSpec, scene: Scene) -> None:
    """Raise ``ValueError`` if an interval of the spec has no visible mark in
    the scene: the figure must not drop what the table still lists. A mark is
    visible when it spans the interval on the page (within
    ``RENDER_TOLERANCE``); a band of zero width, or an error bar collapsed to
    a point, does not count unless the interval itself is a single value."""
    style = STYLE[spec.kind]
    drawn = _drawn_extents(scene, style)
    lost = []
    for pi, (panel, g) in enumerate(zip(spec.panels, scene.panels, strict=True)):
        axis = g.x if style == "forest" else g.y
        for si, s in enumerate(panel.series):
            for pj, p in enumerate(s.points):
                lo, hi, kind = effective_interval(style, s, p)
                if kind not in ("given", "zero"):
                    continue
                marks = drawn.get((pi, si, pj))
                want = abs(axis.px(hi) - axis.px(lo))
                if not marks:
                    lost.append(f"panel {pi} series {si} point {pj} (no mark)")
                elif want > 1e-9 and not any(
                    abs(m - want) <= RENDER_TOLERANCE for m in marks
                ):
                    lost.append(
                        f"panel {pi} series {si} point {pj} "
                        f"(drawn {max(marks):.2f} pt of {want:.2f} pt)"
                    )
    if lost:
        raise ValueError(
            f"figure {spec.id!r}: the interval of {', '.join(lost[:5])}"
            f"{' and more' if len(lost) > 5 else ''} was not drawn visibly"
        )


def _legend(
    items: list[Item],
    union: Mapping[Text, list[Series]],
    style: str,
    R: _Resolver,
    y: float,
    inner_w: float,
) -> float:
    row_h = 15.0
    x = PAD
    y_row = y
    sample_w = 26.0
    lang = R.lang if R.lang in _WORDS else "en"
    for si, (sname, group) in enumerate(union.items()):
        s = group[0]
        st = series_style(si, s)
        name = R.cap(R(sname), "legend")
        if all(g.unavailable for g in group):
            name = R.plain(_WORDS[lang]["legend_unavailable"].format(name=name))
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
    return replace(item, role=role)


def _panel_height(panel: Panel, style: str) -> float:
    if style == "heat":
        return max(150.0, 70.0 + len(panel.y_axis.categories) * 34.0)
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
    styles: tuple[Style, ...],
) -> PanelGeometry:
    x_lo, x_hi, y_lo, y_hi = _extent(panel, style)
    ycat = style in ("forest", "heat")
    ptitle = R.cap(R(panel.title), "panel-title")
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
    # data must lie inside a fixed axis range: never clip a value silently
    for name, sc, lo, hi in (("x", xs, x_lo, x_hi), ("y", ys, y_lo, y_hi)):
        if sc is not None and (lo < sc.lo - 1e-9 or hi > sc.hi + 1e-9):
            raise ValueError(
                f"figure {spec.id!r}: panel {pi} {name} data [{num(lo)}, {num(hi)}] "
                f"lies outside the axis range [{num(sc.lo)}, {num(sc.hi)}]"
            )

    # --- margins
    ylabel = R.cap(R(panel.y_axis.label), "axis")
    xlabel = R.cap(R(panel.x_axis.label), "axis")
    if ycat:
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
    m_left = (14.0 if ylabel and not ycat else 0.0) + left_lab + 8
    annot_w = 0.0
    if style == "heat":
        annot_w = (
            text_width(R.plain(_WORDS["en"]["row_total"].format(n=99999)), NOTE_SIZE)
            + 8
        )
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
    if panel.x_axis.kind == "category":
        xmap = AxisMap("category", plot_l, plot_r, None, len(panel.x_axis.categories))
    else:
        xmap = AxisMap("linear", plot_l, plot_r, xs)
    if ycat:  # row 0 at the top: map index to page y
        ymap = AxisMap("category", plot_t, plot_b, None, len(panel.y_axis.categories))
    else:
        ymap = AxisMap("linear", plot_b, plot_t, ys)

    # --- grid, axes, ticks
    if ycat:
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
                Line(cx, plot_b, cx, plot_b + 3, stroke=AXIS, width=0.8, role="tick")
            )
            ty = plot_b + 3 + TICK_SIZE
            for ln in lines:
                items.append(Label(cx, ty, ln, TICK_SIZE, "middle", role="tick-label"))
                ty += TICK_SIZE * LINE_H
    else:
        assert xs is not None
        for t in xs.ticks:
            px = xmap.px(t)
            if style == "forest":
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
    if ylabel and not ycat:
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
    elif ylabel and ycat:
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
        left=plot_l,
        top=plot_t,
        width=plot_w,
        height=plot_h,
        x=xmap,
        y=ymap,
        styles=styles,
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
            lab = R.cap(R(r.label), "refline")
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
            lab = R.cap(R(r.label), "refline")
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
        "heat": _heat,
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
        st = g.styles[si]
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
        st = g.styles[si]
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
        st = g.styles[si]
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


def _band_path(xs: Sequence[float], vals: Sequence[float]) -> list[tuple[float, float]]:
    """One edge of a step band: value ``vals[i]`` held from ``xs[i]`` to
    ``xs[i + 1]`` (``len(xs) == len(vals) + 1``)."""
    out: list[tuple[float, float]] = []
    for i, v in enumerate(vals):
        out.append((xs[i], v))
        out.append((xs[i + 1], v))
    return out


def _step(items: list[Item], panel: Panel, pi: int, g: PanelGeometry) -> None:
    """Interval bands first (all series), then the curves, markers and labels,
    so no band ever covers another series' curve.

    A point's value holds from its x to the next point's x, and so does its
    interval: consecutive points that have one share a band, each point's part
    reaching to the next point (whether or not that one has an interval). A
    point with an interval and nothing to its right on the curve (the last
    point, or a next point at the same x) gets an error bar instead, so every
    interval, the curve's end included, is visible."""
    bands: list[Item] = []
    front: list[Item] = []
    for si, s in enumerate(panel.series):
        if not s.points:  # an unavailable series draws nothing
            continue
        st = g.styles[si]
        pts = [(g.x.px(p.x), g.y.px(p.y)) for p in s.points]
        eff = [effective_interval("step", s, p) for p in s.points]
        n = len(pts)
        has = [kind in ("given", "zero") for _lo, _hi, kind in eff]
        banded = [has[j] and j + 1 < n and pts[j + 1][0] > pts[j][0] for j in range(n)]
        runs: list[list[int]] = []
        run: list[int] = []
        for pj in range(n):
            if banded[pj]:
                run.append(pj)
            else:
                if run:
                    runs.append(run)
                    run = []
                if has[pj]:  # an interval with no width to its right
                    x = pts[pj][0]
                    y1, y2 = g.y.px(eff[pj][0]), g.y.px(eff[pj][1])
                    ref = (pi, si, pj)
                    front.append(
                        Line(
                            x, y1, x, y2, stroke=st.color, width=1.4, role="ci", ref=ref
                        )
                    )
                    for yy in (y1, y2):
                        front.append(
                            Line(
                                x - 3,
                                yy,
                                x + 3,
                                yy,
                                stroke=st.color,
                                width=1.4,
                                role="ci-cap",
                                ref=ref,
                            )
                        )
        if run:
            runs.append(run)
        for run in runs:
            xs = [pts[j][0] for j in run] + [pts[run[-1] + 1][0]]
            up = _band_path(xs, [g.y.px(eff[j][1]) for j in run])
            dn = _band_path(xs, [g.y.px(eff[j][0]) for j in run])
            bands.append(
                Poly(
                    tuple(up + dn[::-1]),
                    stroke=None,
                    fill=blend(st.color),
                    closed=True,
                    role="band",
                    ref=(pi, si, run[0]),
                    refs=tuple((pi, si, j) for j in run),
                    spans=tuple((xs[k], xs[k + 1]) for k in range(len(run))),
                )
            )
        front.append(
            Poly(
                tuple(_step_path(pts)),
                stroke=st.color,
                width=2.6 if st.heavy else 1.6,
                dash=_dash(st.dash),
                role="curve",
                ref=(pi, si, 0),
            )
        )
        last = len(s.points) - 1
        front += _marker_items(
            st.marker, pts[last][0], pts[last][1], 7, st.color, (pi, si, last)
        )
        for pj, p in enumerate(s.points):
            if p.label:
                front.append(
                    Label(
                        pts[pj][0] + 5,
                        pts[pj][1] - 4,
                        p.label,
                        NOTE_SIZE,
                        role="point-label",
                    )
                )
    items += bands
    items += front


def _lines(items: list[Item], panel: Panel, pi: int, g: PanelGeometry) -> None:
    ns = len(panel.series)
    for si, s in enumerate(panel.series):
        st = g.styles[si]
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


def _heat(items: list[Item], panel: Panel, pi: int, g: PanelGeometry) -> None:
    """A confusion matrix: each cell shaded by its share of the row, with the
    count and the share written in it, and the row total on the right."""
    s = panel.series[0]
    ncol = len(panel.x_axis.categories)
    nrow = len(panel.y_axis.categories)
    cw, ch = g.width / ncol, g.height / nrow
    totals: dict[int, float] = {}
    for p in s.points:
        totals[int(p.y)] = totals.get(int(p.y), 0.0) + p.value
    for pj, p in enumerate(s.points):
        ref = (pi, 0, pj)
        total = totals[int(p.y)]
        share = p.value / total if total else 0.0
        fill = blend(PALETTE[0], PAPER, 0.92 * (1 - share))
        cx, cy = g.x.px(p.x), g.y.px(p.y)
        items.append(
            Rect(
                cx - cw / 2,
                cy - ch / 2,
                cw,
                ch,
                fill=fill,
                stroke=PAPER,
                stroke_width=2.0,
                role="cell",
                ref=ref,
            )
        )
        ink = PAPER if luminance(fill) < 0.3 else INK
        items.append(
            Label(
                cx,
                cy - 1,
                num(p.value),
                11.0,
                "middle",
                bold=True,
                fill=ink,
                role="cell-count",
            )
        )
        items.append(
            Label(
                cx,
                cy + 10,
                f"{share * 100:.0f}%",
                8.5,
                "middle",
                fill=ink,
                role="cell-share",
            )
        )
    for r in range(nrow):
        if r in totals:
            items.append(
                Label(
                    g.left + g.width + 6,
                    g.y.px(r) + NOTE_SIZE * 0.35,
                    _WORDS["en"]["row_total"].format(n=num(totals[r])),
                    NOTE_SIZE,
                    fill=INK_SOFT,
                    role="row-total",
                )
            )
