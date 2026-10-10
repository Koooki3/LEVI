"""SVG writer for a ``FigureSpec`` (pure standard library, deterministic).

The output depends only on the spec, the language and the options: no clock,
no random ids, no dict-order surprises, so two runs give the same bytes and a
snapshot test or a ``diff`` between two campaign reports is meaningful.

Text stays live ``<text>`` (searchable, restyleable) in a generic font family;
the browser or the paper's toolchain picks the face. ``<title>`` and
``<desc>`` carry the figure's title and one-sentence summary for assistive
technology. With ``embed_spec`` the spec's JSON is stored in ``<metadata>``, so
the figure can be rebuilt from the file alone.
"""

from __future__ import annotations

from xml.sax.saxutils import escape

from .figspec import (
    INK,
    PAPER,
    Circle,
    FigureSpec,
    Label,
    Line,
    Poly,
    Rect,
    Scene,
    coord,
    layout,
)

FONT_FAMILY = "Helvetica, Arial, sans-serif"
CJK_FONT_FAMILY = (
    "Helvetica, Arial, 'Noto Sans CJK SC', 'PingFang SC', 'Microsoft YaHei', sans-serif"
)
NS = "http://www.w3.org/2000/svg"


def _dash_attr(dash: tuple[float, ...]) -> str:
    return f' stroke-dasharray="{" ".join(coord(d) for d in dash)}"' if dash else ""


def _fill(v: str | None) -> str:
    return v if v else "none"


def _item(it) -> str:
    if isinstance(it, Line):
        return (
            f'<line x1="{coord(it.x1)}" y1="{coord(it.y1)}" x2="{coord(it.x2)}" y2="{coord(it.y2)}" '
            f'stroke="{it.stroke}" stroke-width="{coord(it.width)}"{_dash_attr(it.dash)}/>'
        )
    if isinstance(it, Rect):
        stroke = (
            f' stroke="{it.stroke}" stroke-width="{coord(it.stroke_width)}"'
            if it.stroke
            else ""
        )
        return (
            f'<rect x="{coord(it.x)}" y="{coord(it.y)}" width="{coord(it.w)}" '
            f'height="{coord(it.h)}" fill="{_fill(it.fill)}"{stroke}/>'
        )
    if isinstance(it, Poly):
        pts = " ".join(f"{coord(x)},{coord(y)}" for x, y in it.points)
        tag = "polygon" if it.closed else "polyline"
        stroke = (
            f' stroke="{it.stroke}" stroke-width="{coord(it.width)}" stroke-linejoin="round"'
            f"{_dash_attr(it.dash)}"
            if it.stroke
            else ""
        )
        return f'<{tag} points="{pts}" fill="{_fill(it.fill)}"{stroke}/>'
    if isinstance(it, Circle):
        stroke = (
            f' stroke="{it.stroke}" stroke-width="{coord(it.stroke_width)}"'
            if it.stroke
            else ""
        )
        return (
            f'<circle cx="{coord(it.cx)}" cy="{coord(it.cy)}" r="{coord(it.r)}" '
            f'fill="{_fill(it.fill)}"{stroke}/>'
        )
    if isinstance(it, Label):
        extra = ""
        if it.anchor != "start":
            extra += f' text-anchor="{it.anchor}"'
        if it.bold:
            extra += ' font-weight="bold"'
        if it.fill != INK:
            extra += f' fill="{it.fill}"'
        if it.rotate:
            extra += f' transform="rotate({it.rotate} {coord(it.x)} {coord(it.y)})"'
        return (
            f'<text x="{coord(it.x)}" y="{coord(it.y)}" font-size="{coord(it.size)}"{extra}>'
            f"{escape(it.s)}</text>"
        )
    raise TypeError(f"unknown scene item {type(it).__name__}")


def scene_to_svg(
    scene: Scene, spec: FigureSpec | None = None, embed_spec: bool = False
) -> str:
    cjk = any(
        ord(c) > 0x2E7F
        for c in scene.title
        + scene.desc
        + "".join(i.s for i in scene.items if isinstance(i, Label))
    )
    family = CJK_FONT_FAMILY if cjk else FONT_FAMILY
    w, h = coord(scene.width), coord(scene.height)
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<svg xmlns="{NS}" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" '
            f'aria-labelledby="fig-title fig-desc" lang="{scene.lang}" font-family="{family}" '
            f'fill="{INK}">'
        ),
        f'<title id="fig-title">{escape(scene.title)}</title>',
        f'<desc id="fig-desc">{escape(scene.desc)}</desc>',
    ]
    if embed_spec and spec is not None:
        out.append(
            f'<metadata id="fig-spec" data-schema="levi.aeri.figure_spec.v1">{escape(spec.to_json())}</metadata>'
        )
    out.append(f'<rect x="0" y="0" width="{w}" height="{h}" fill="{PAPER}"/>')
    out.extend(_item(i) for i in scene.items)
    out.append("</svg>")
    return "\n".join(out) + "\n"


def render_svg(
    spec: FigureSpec,
    lang: str | None = None,
    embed_spec: bool = False,
    width: float = 640.0,
) -> str:
    """The figure as an SVG document (a ``str``; write it as UTF-8)."""
    scene = layout(spec, lang=lang, latin_only=False, width=width)
    return scene_to_svg(scene, spec, embed_spec)
