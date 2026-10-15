#!/usr/bin/env python3
"""Generates the LEVI architecture atlas (docs/assets/atlas/*.svg), English only.

    python3 docs/atlas/build.py            # writes <name>-light.svg and <name>-dark.svg

Each figure is hand-laid on a grid in plain SVG (rect, text, path; no script, no
external image, no foreign object) so it renders the same on GitHub, in the
Report page and in a browser, and diffs as text. Light and dark files exist
because an <img> cannot inherit the page's colours; the README picks one with
<picture>. Every figure states one claim in its <title> and <desc>.

Design: flat vector first, in the manner of a paper's system-overview figure.
Depth appears only where it carries meaning (group plates, the emphasised card,
the pyramid and layer slabs) and uses at most a handful of filtered elements per
figure. Motion is CSS only, 3 s or slower, and only where it carries meaning:
the direction of data flow (figures 1, 5, 6), the GPU timeline's "now" marker
(figure 5) and the contract boundary (figure 6). Under prefers-reduced-motion
everything is static, and the static frame is the complete figure.
"""

from __future__ import annotations

import html
import itertools
import sys
from pathlib import Path
from typing import ClassVar

OUT = Path(__file__).resolve().parents[1] / "assets" / "atlas"
FONT = "Inter, -apple-system, 'Segoe UI', Roboto, 'Noto Sans', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"

# Neutrals lean slightly blue so cards, panel and text share one hue.
PALETTE = {
    "light": dict(  # noqa: C408
        panel="#ffffff",
        panel2="#f6f8fc",
        edge="#cfd8e3",
        ink="#1b2430",
        soft="#4f5b6c",
        faint="#5b687a",
        box="#f4f7fb",
        boxedge="#c9d3df",
        accent="#243040",
        on_accent="#ffffff",
        model="#0b64c6",
        model_bg="#e1f0ff",
        person="#1a7a38",
        person_bg="#dff6e5",
        gate="#8f5f00",
        gate_bg="#fff6c9",
        system="#7a45d6",
        system_bg="#f6ecff",
        danger="#c4242f",
        danger_bg="#ffeceb",
        sheen="#ffffff",
        sheen_op=0.85,
        shadow="#1b2a44",
        shadow_op=0.16,
    ),
    "dark": dict(  # noqa: C408
        panel="#0e131b",
        panel2="#0b0f16",
        edge="#2c3644",
        ink="#e6ecf4",
        soft="#a3afc0",
        faint="#8591a3",
        box="#161d28",
        boxedge="#47536a",
        accent="#e6ecf4",
        on_accent="#0e131b",
        model="#62a8ff",
        model_bg="#112640",
        person="#47c15f",
        person_bg="#10301c",
        gate="#dca62c",
        gate_bg="#2e2509",
        system="#c29bff",
        system_bg="#2a1c42",
        danger="#ff8279",
        danger_bg="#40161b",
        sheen="#ffffff",
        sheen_op=0.07,
        shadow="#000000",
        shadow_op=0.55,
    ),
}
TONES = ("model", "person", "gate", "system", "danger")


def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


def mix(a, b, t):
    """Colour a moved fraction t of the way towards b."""
    ra, rb = _rgb(a), _rgb(b)
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ra, rb))


ICONS = {  # 24-unit line icons, drawn with the tone colour
    "doc": '<path d="M6 3h8l4 4v14H6z M14 3v4h4 M9 12h6 M9 16h6"/>',
    "db": '<ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6 M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3"/>',
    "person": '<circle cx="12" cy="8" r="3.5"/><path d="M5 20c0-4 3-6 7-6s7 2 7 6"/>',
    "chip": '<rect x="6" y="6" width="12" height="12" rx="2"/><rect x="10" y="10" width="4" height="4"/>'
    '<path d="M9 3v3 M15 3v3 M9 18v3 M15 18v3 M3 9h3 M3 15h3 M18 9h3 M18 15h3"/>',
    "arm": '<path d="M4 21h9 M8.5 21v-7"/><circle cx="8.5" cy="12" r="2"/><path d="M10 10.5 15 6.5"/>'
    '<circle cx="16.5" cy="5.5" r="1.8"/><path d="M17.6 7 20 10.5 M20 10.5l-1.6 2 M20 10.5l1.8 1.8"/>',
    "term": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M7 10l3 2-3 2 M12 15h5"/>',
    "eye": '<path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6z"/><circle cx="12" cy="12" r="2.8"/>',
}

# Figure 8 (an AERI run) keeps its lane and loop geometry at module level: the
# loop's motion CSS (where the dot is when each step lights up) is computed from
# it, and Fig.CSS_PARTS has to hold that CSS before any figure is drawn.
AF_COL0, AF_COLW, AF_BOXW, AF_BOXH = 52, 186, 168, 54
AF_TOP3, AF_RET_X, AF_ROW1, AF_PITCH = 480, 40, 492, 66
AF_PERIOD = 32  # seconds per round of the dot
# (lane, title, sub lines, status): lanes 0 operator, 1 page, 2 terminal D,
# 3 policy service, 4 vision judge, 5 robot stack
AF_LOOP = [
    (0, "R1 Set the scene", ("reset it, then Enter", "in D; frames follow"), "branch"),
    (0, "R2 Scene ready?", ("y ready · n not ready", "u unsure (n, u: reset)"), "branch"),
    (3, "R3 Policy acts", ("action chunks;", "D sends each step"), "main"),
    (2, "R4 Event candidate", ("e.g. the gripper", "opens or closes"), "main"),
    (4, "R5 Judge", ("frames of the event;", "final verdict at end"), "main"),
    (2, "R6 Stop when done", ("confirmed goal or", "the step limit"), "main"),
    (5, "R7 Home, verify", ("pose and open gripper", "checked after homing"), "main"),
    (2, "R8 Prompt the reset", ("prints [Round n/N]", "and asks for a reset"), "branch"),
]


def af_x(lane):
    return AF_COL0 + lane * AF_COLW + 9


def af_cx(lane):
    return AF_COL0 + lane * AF_COLW + 9 + AF_BOXW // 2


def af_row(i):
    """Top of loop row i (0-based)."""
    return AF_ROW1 + i * AF_PITCH


def af_loop_path():
    """The dot's closed route: box centre to box centre along the arrows, then
    back up the left margin to R1. It starts on the arrow into R3 (the policy
    acting), so the still frame shows the dot inside a round, about to run it."""
    centres = [(af_cx(k), af_row(i) + AF_BOXH // 2) for i, (k, *_) in enumerate(AF_LOOP)]
    start = (af_x(AF_LOOP[2][0]) - 34, centres[2][1])
    pts = [start, centres[2]]
    for a, b in itertools.pairwise(centres[2:]):
        pts += [b] if a[0] == b[0] else [(a[0], b[1]), b]
    pts += [(AF_RET_X, centres[-1][1]), (AF_RET_X, AF_TOP3), (centres[0][0], AF_TOP3)]
    for a, b in itertools.pairwise(centres[:3]):
        pts += [a] if not pts or pts[-1] != a else []
        pts += [b] if a[0] == b[0] else [(a[0], b[1])]
    pts += [start]
    return pts


def af_step_delays():
    """Seconds after the dot sets off at which it enters each step's box."""
    pts = af_loop_path()
    total = sum(abs(x1 - x0) + abs(y1 - y0) for (x0, y0), (x1, y1) in itertools.pairwise(pts))
    order = list(range(2, len(AF_LOOP))) + [0, 1]  # the route enters R3 first
    boxes = [(af_x(AF_LOOP[i][0]), af_row(i)) for i in order]
    delays, s, nxt = [], 0, 0
    for (x0, y0), (x1, y1) in itertools.pairwise(pts):
        n = abs(x1 - x0) + abs(y1 - y0)
        for j in range(n):
            x, y = x0 + (x1 - x0) * j / n, y0 + (y1 - y0) * j / n
            bx, by = boxes[nxt] if nxt < len(boxes) else (None, None)
            if bx is not None and bx <= x <= bx + AF_BOXW and by <= y <= by + AF_BOXH:
                delays.append(AF_PERIOD * (s + j) / total)
                nxt += 1
        s += n
    assert len(delays) == len(AF_LOOP), delays
    return [delays[order.index(i)] for i in range(len(AF_LOOP))]


AF_STEP_CSS = (
    "@keyframes step{0%,9%{opacity:1}14%{opacity:.2}}"
    f".st{{opacity:.2;animation:step {AF_PERIOD}s linear infinite}}"
    + "".join(f".s{i}{{animation-delay:{d:.2f}s}}" for i, d in enumerate(af_step_delays()))
)


class Fig:
    def __init__(self, name: str, w: int, h: int, theme: str, title: str, desc: str):
        self.name, self.w, self.h, self.theme = name, w, h, theme
        self.c = PALETTE[theme]
        self.title, self.desc = title, desc
        self.parts: list[str] = []
        self.markers: set[str] = set()
        self.grads: set[str] = set()
        self.filters: set[str] = set()
        self.rects: list[
            tuple
        ] = []  # (x, y, w, h, colours, opacity) for the contrast audit
        self.texts: list[tuple] = []  # (x, y, size, fill, anchor)
        self.flows = 0
        self.uses: set[str] = set()

    # -- helpers -----------------------------------------------------------
    def _paint(self, fill):
        """Cards get a very soft vertical gradient; anything else stays flat."""
        c = self.c
        for tone in (*TONES, "box"):
            key = c[tone + "_bg"] if tone != "box" else c["box"]
            if fill == key:
                self.grads.add(tone)
                return f"url(#g_{tone})"
        return fill

    def _gradient_ends(self, tone):
        c = self.c
        base = c[tone + "_bg"] if tone != "box" else c["box"]
        amt = 0.55 if self.theme == "light" else 0.045
        return mix(base, "#ffffff", amt), base

    def icon(self, name, x, y, tone, size=22):
        col = self.c[tone] if tone else self.c["soft"]
        s = size / 24
        self.parts.append(
            f'<g transform="translate({x} {y}) scale({s:.3f})" fill="none" stroke="{col}" '
            f'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">{ICONS[name]}</g>'
        )

    # -- primitives ------------------------------------------------------
    def rect(
        self,
        x,
        y,
        w,
        h,
        fill=None,
        stroke=None,
        r=10,
        dash=False,
        sw=1.2,
        op=None,
        elev=0,
        plain=False,
        cls=None,
    ):
        """elev 0 flat, 1 soft group shadow, 2 emphasis shadow. Few per figure on purpose."""
        c = self.c
        fill = fill or c["box"]
        stroke = stroke or c["boxedge"]
        self.rects.append((x, y, w, h, fill, op))
        paint = fill if plain else self._paint(fill)
        d = ' stroke-dasharray="5 4"' if dash else ""
        o = f' fill-opacity="{op}"' if op is not None else ""
        flt = ""
        if elev:
            self.filters.add(f"sh{elev}")
            flt = f' filter="url(#sh{elev})"'
        k = f' class="{cls}"' if cls else ""
        self.parts.append(
            f'<rect{k} x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{paint}"{o} '
            f'stroke="{stroke}" stroke-width="{sw}"{d}{flt}/>'
        )
        if not dash and not plain and w > 24 and h > 24:
            self.parts.append(
                f'<rect x="{x + 1.5}" y="{y + 1.5}" width="{w - 3}" height="{h - 3}" rx="{max(r - 1.5, 1)}" '
                f'fill="none" stroke="{c["sheen"]}" stroke-opacity="{c["sheen_op"] * 0.5:.3f}" stroke-width="1"/>'
            )

    def slab(self, x, y, w, h, r=14, depth=5):
        """A pale group plate with a visible thickness: layering, not a heavy shadow."""
        c = self.c
        side = mix(c["boxedge"], c["panel"], 0.2)
        self.parts.append(
            f'<rect x="{x}" y="{y + depth}" width="{w}" height="{h}" rx="{r}" fill="{side}"/>'
        )
        top = (
            mix(c["panel"], c["edge"], 0.38)
            if self.theme == "light"
            else mix(c["panel"], "#ffffff", 0.035)
        )
        self.rects.append((x, y, w, h, top, None))
        self.parts.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{top}" '
            f'stroke="{c["edge"]}" stroke-width="1"/>'
        )

    def text(
        self,
        x,
        y,
        s,
        size=13,
        weight=400,
        anchor="start",
        fill=None,
        mono=False,
        op=None,
        ls=None,
    ):
        fill = fill or self.c["ink"]
        fam = MONO if mono else FONT
        o = f' opacity="{op}"' if op is not None else ""
        spacing = f' letter-spacing="{ls}"' if ls else ""
        self.texts.append((x, y, size, fill, anchor, s))
        self.parts.append(
            f'<text x="{x}" y="{y}" font-family="{fam}" font-size="{size}" font-weight="{weight}" '
            f'text-anchor="{anchor}" fill="{fill}"{o}{spacing}>{html.escape(s)}</text>'
        )

    def lines(self, x, y, items, size=13, gap=18, **kw):
        for i, s in enumerate(items):
            self.text(x, y + i * gap, s, size=size, **kw)

    def box(
        self,
        x,
        y,
        w,
        h,
        title,
        sub=(),
        tone=None,
        size=15,
        subsize=13,
        dash=False,
        align="start",
        icon=None,
        elev=0,
    ):
        c = self.c
        fill, stroke = (c["box"], c["boxedge"])
        if tone:
            fill, stroke = c[tone + "_bg"], c[tone]
        self.rect(
            x,
            y,
            w,
            h,
            fill=fill,
            stroke=stroke,
            dash=dash,
            sw=1.4 if tone else 1.2,
            elev=elev,
        )
        tx = x + 14 if align == "start" else x + w / 2
        self.text(
            tx,
            y + 24,
            title,
            size=size,
            weight=600,
            anchor=align,
            fill=c[tone] if tone else c["ink"],
        )
        self.lines(
            tx, y + 45, list(sub), size=subsize, gap=18, anchor=align, fill=c["soft"]
        )
        if icon:
            self.icon(icon, x + w - 34, y + 9, tone)

    def pill(self, x, y, label, tone="model", w=None):
        c = self.c
        w = w or (len(label) * 6.9 + 22)
        self.rect(x, y, w, 24, fill=c[tone + "_bg"], stroke=c[tone], r=12, sw=1.2)
        self.text(
            x + w / 2,
            y + 16.5,
            label,
            size=12,
            weight=600,
            anchor="middle",
            fill=c[tone],
        )
        return w

    def arrow(
        self,
        pts,
        label=None,
        tone=None,
        dash=False,
        lx=None,
        ly=None,
        both=False,
        anchor="middle",
        lsize=12,
        flow=False,
        cls=None,
        head=True,
    ):
        c = self.c
        col = c[tone] if tone else c["soft"]
        mid = "a_" + (tone or "soft")
        if head:
            self.markers.add(mid)
        if both:
            self.markers.add(mid + "_rev")
        d = "M" + " L".join(f"{x} {y}" for x, y in pts)
        da = ' stroke-dasharray="6 4"' if dash else ""
        start = f' marker-start="url(#{mid}_rev)"' if both else ""
        k = f'class="{cls}" ' if cls else ""
        end = f' marker-end="url(#{mid})"' if head else ""
        self.parts.append(
            f'<path {k}d="{d}" fill="none" stroke="{col}" stroke-width="1.8" stroke-linejoin="round"{da}'
            f"{end}{start}/>"
        )
        if flow:
            # Direction of travel: small bright dots drift along the line, start to end.
            self.flows += 1
            self.parts.append(
                f'<path class="flow" d="{d}" fill="none" stroke="{col}" stroke-width="3.4" '
                f'stroke-linecap="round" stroke-linejoin="round" stroke-dasharray="0.1 18"/>'
            )
        if label:
            x0, y0 = pts[len(pts) // 2 - 1] if len(pts) > 2 else pts[0]
            x1, y1 = pts[len(pts) // 2]
            lx = lx if lx is not None else (x0 + x1) / 2
            ly = ly if ly is not None else (y0 + y1) / 2 - 7
            self.text(lx, ly, label, size=lsize, anchor=anchor, fill=col, weight=500)

    TAGS: ClassVar[dict[str, tuple[str, str]]] = {
        "main": ("main", "person"),
        "branch": ("branch", "system"),
        "planned": ("planned", "gate"),
    }

    def tag(self, right, y, kind):
        """A status badge, right-aligned at `right`, centred on the line y."""
        label, tone = self.TAGS[kind]
        w = len(label) * 6.6 + 14
        self.rect(
            right - w, y - 8, w, 16, fill=self.c[tone + "_bg"], stroke=self.c[tone],
            r=8, sw=1.1, plain=True,
        )
        self.text(right - w / 2, y + 4, label, size=11.5, weight=700, anchor="middle", fill=self.c[tone])
        return w

    def heading(self, label, sub=None):
        c = self.c
        self.parts.append(
            f'<rect x="32" y="14" width="30" height="3.5" rx="1.75" fill="{c["model"]}"/>'
            f'<rect x="64" y="14" width="14" height="3.5" rx="1.75" fill="{c["system"]}"/>'
        )
        self.text(32, 40, label, size=22, weight=700)
        if sub:
            self.text(32, 64, sub, size=13.5, fill=self.c["soft"])

    def note(self, y, s, tone="gate"):
        c = self.c
        self.rect(32, y, self.w - 64, 32, fill=c[tone + "_bg"], stroke=c[tone], r=8)
        self.text(48, y + 21, s, size=13.5, weight=500, fill=c[tone])

    # -- output ----------------------------------------------------------
    # One block per motion; a figure carries only the blocks it uses, then the
    # reduced-motion rule. tests/test_atlas.py compares <style> with these constants.
    CSS_PARTS: ClassVar[dict[str, str]] = {
        "flow": "@keyframes flow{to{stroke-dashoffset:-18}}.flow{animation:flow 3s linear infinite}",
        "now": "@keyframes now{from{transform:translateX(0)}to{transform:translateX(1120px)}}"
        ".now{animation:now 12s linear infinite;animation-delay:-5.571s}",
        "guard": "@keyframes guard{0%,100%{opacity:.3}50%{opacity:.9}}"
        ".guard{opacity:.6;animation:guard 5s ease-in-out infinite}",
        # figure 8: one dot travels the round loop, the step it is in lights up,
        # and the exception branches crawl slowly
        "token": "@keyframes token{to{stroke-dashoffset:-100}}"
        f".token{{animation:token {AF_PERIOD}s linear infinite}}",
        "step": AF_STEP_CSS,
        "drift": "@keyframes drift{to{stroke-dashoffset:-20}}.drift{animation:drift 6s linear infinite}",
    }
    CSS_REDUCED = (
        "@media (prefers-reduced-motion: reduce) { * { animation: none !important; } }"
    )

    @property
    def css(self) -> str:
        used = [
            k for k in self.CSS_PARTS if k in self.uses or (k == "flow" and self.flows)
        ]
        return "".join(self.CSS_PARTS[k] for k in used) + self.CSS_REDUCED

    def svg(self) -> str:
        c = self.c
        defs = []
        for mid in sorted(self.markers):
            reverse = mid.endswith("_rev")
            tone = mid[2:-4] if reverse else mid[2:]
            col = c.get(tone, c["soft"])
            orient = "auto-start-reverse" if reverse else "auto"
            defs.append(
                f'<marker id="{mid}" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" '
                f'orient="{orient}"><path d="M1 1.5 L9 5 L1 8.5 Z" fill="{col}"/></marker>'
            )
        for tone in sorted(self.grads):
            top, bot = self._gradient_ends(tone)
            defs.append(
                f'<linearGradient id="g_{tone}" x1="0" y1="0" x2="0" y2="1">'
                f'<stop offset="0" stop-color="{top}"/><stop offset="1" stop-color="{bot}"/></linearGradient>'
            )
        defs.append(
            f'<linearGradient id="g_panel" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset="0" stop-color="{c["panel"]}"/><stop offset="1" stop-color="{c["panel2"]}"/></linearGradient>'
        )
        sh = {"sh1": (1.5, 3, 0.55), "sh2": (3, 7, 0.9)}
        for fid in sorted(self.filters):
            dy, sd, k = sh[fid]
            defs.append(
                f'<filter id="{fid}" x="-8%" y="-20%" width="116%" height="150%">'
                f'<feDropShadow dx="0" dy="{dy}" stdDeviation="{sd}" flood-color="{c["shadow"]}" '
                f'flood-opacity="{c["shadow_op"] * k:.3f}"/></filter>'
            )
        head = (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" width="{self.w}" '
            f'height="{self.h}" role="img" aria-labelledby="t d">'
            f'<title id="t">{html.escape(self.title)}</title><desc id="d">{html.escape(self.desc)}</desc>'
            f"<defs><style>{self.css}</style>{''.join(defs)}</defs>"
            f'<rect x="0.6" y="0.6" width="{self.w - 1.2}" height="{self.h - 1.2}" rx="16" fill="url(#g_panel)" stroke="{c["edge"]}" stroke-width="1.2"/>'
        )
        return head + "".join(self.parts) + "</svg>\n"


# ------------------------------------------------------------------ figures
def fig_context(t):
    f = Fig(
        "01-system-context",
        1200,
        716,
        t,
        "LEVI in context: data in, reviewed labels and training data out, a person in charge",
        "Raw captures, LeRobot datasets and robot rollouts enter LEVI; models propose; a person approves; "
        "LeRobot v2.1, RECAP value data, training manifests and pool exports leave. Source data is never modified.",
    )
    c = f.c
    f.heading(
        "LEVI in context",
        "Data goes in, models propose, a person decides, curated training data comes out.",
    )
    # centre
    f.rect(400, 150, 400, 400, fill=c["box"], stroke=c["accent"], r=16, sw=2, elev=2)
    f.text(600, 184, "LEVI workbench", size=18, weight=700, anchor="middle")
    f.text(600, 205, "runs on your machine", size=12.5, anchor="middle", fill=c["soft"])
    f.box(
        424,
        222,
        352,
        56,
        "Web UI · REST · MCP · CLI",
        ["four ways in; agent work goes through one layer"],
        size=13.5,
    )
    f.box(
        424,
        292,
        352,
        56,
        "Agent capability layer",
        ["permissions · revisions · undo · audit trail"],
        tone="gate",
        size=13.5,
    )
    f.box(
        424,
        362,
        352,
        70,
        "Domains",
        [
            "convert · check · annotate · live labelling",
            "training pool · RECAP · harness · AERI",
        ],
        size=13.5,
    )
    f.box(
        424,
        446,
        352,
        80,
        "Stores and workers",
        ["files + SQLite · FFmpeg", "model processes in their own environments"],
        size=13.5,
    )
    # inputs
    f.text(32, 120, "IN", size=12, weight=700, fill=c["faint"], ls=1)
    ins = [
        ("Raw captures", "CSV + video or image folders"),
        ("LeRobot datasets", "v2.0 – v3.1, local or Hub"),
        ("Robot rollouts", "written by a running evaluation"),
        ("Hugging Face Hub", "streamed on demand"),
    ]
    for i, (a, b) in enumerate(ins):
        y = 138 + i * 100
        f.box(32, y, 280, 76, a, [b], icon=("doc", "db", "arm", "db")[i])
        f.arrow([(312, y + 38), (400, 382 + i * 12)], tone=None, flow=True)
    # outputs
    f.text(888, 120, "OUT", size=12, weight=700, fill=c["faint"], ls=1)
    outs = [
        ("LeRobot v2.1", "converted, source untouched"),
        ("RECAP value dataset", "per-frame value + advantage"),
        ("Training manifests", "which frames enter a loss, at what weight"),
        ("Pool exports", "merged tasks with provenance"),
    ]
    for i, (a, b) in enumerate(outs):
        y = 138 + i * 100
        f.box(888, y, 280, 76, a, [b], icon=("db", "doc", "doc", "db")[i])
        f.arrow([(800, 382 + i * 12), (888, y + 38)], tone=None, flow=True)
    # models (top) and person (bottom)
    f.box(
        400,
        78,
        400,
        56,
        "Models propose",
        ["MCP agents · API models · local Ollama / vLLM · SAM3"],
        tone="model",
        size=13.5,
        icon="chip",
    )
    f.arrow(
        [(600, 134), (600, 150)],
        "suggestions cite frames",
        tone="model",
        lx=720,
        ly=146,
        anchor="start",
    )
    f.box(
        400,
        574,
        400,
        100,
        "A person decides",
        [
            "approves plans, accepts pilots, commits, reviews labels;",
            "a commit can be undone. The live service's audited",
            "auto-approver is the one exception.",
        ],
        tone="person",
        size=13.5,
        icon="person",
    )
    f.arrow([(600, 574), (600, 550)], tone="person", dash=False, flow=True)
    f.text(
        1168,
        698,
        "Nothing leaves the machine unless you connect a remote model or run an explicit upload or push.",
        size=12,
        anchor="end",
        fill=c["soft"],
    )
    return f


def fig_pyramid(t):
    f = Fig(
        "02-pyramid",
        1200,
        720,
        t,
        "What LEVI does with robot data, from reading it to a better policy",
        "A five-tier pyramid: read and convert, check, label and judge, curate for training, and a better policy at the top, "
        "which is not established. The measured result sits at the label-and-judge tier.",
    )
    c = f.c
    f.heading(
        "From raw data to a better policy",
        "Each tier rests on the one below it. The top tier is the goal, and it is not proven yet.",
    )
    tiers = [
        (
            "5",
            "A better policy",
            "Downstream training and real-robot evaluation (run by the user's team).",
            "Not established",
            "danger",
            True,
        ),
        (
            "4",
            "Curate for training",
            "Training pool · RECAP value and advantage labels · training manifests · reset export.",
            "Built · fixture-tested",
            None,
            False,
        ),
        (
            "3",
            "Label and judge",
            "Agents and local vision-language models propose; a person reviews. Release-anchored review judges success.",
            "Measured on two tasks; bar met on one",
            "gate",
            False,
        ),
        (
            "2",
            "Check",
            "Structural quality checks · outcome labels · review flags · agent content review.",
            "Built · fixture-tested",
            None,
            False,
        ),
        (
            "1",
            "Read and convert",
            "Browse LeRobot v2.0–v3.1 and raw captures; convert to LeRobot v2.1 or a RECAP value dataset.",
            "Built · fixture and real-data checked",
            None,
            False,
        ),
    ]
    top, bottom = 96, 676
    th = (bottom - top) / len(tiers)
    cx = 386
    for i, (n, name, body, tag, tone, dash) in enumerate(tiers):
        y = top + i * th
        # trapezoid widths: narrow at top
        w_top = 120 + i * 116
        w_bot = 120 + (i + 1) * 116
        x0t, x1t = cx - w_top / 2, cx + w_top / 2
        x0b, x1b = cx - w_bot / 2, cx + w_bot / 2
        fill = c[tone + "_bg"] if tone else c["box"]
        stroke = c[tone] if tone else c["boxedge"]
        d = f"M{x0t:.0f} {y:.0f} L{x1t:.0f} {y:.0f} L{x1b:.0f} {y + th - 6:.0f} L{x0b:.0f} {y + th - 6:.0f} Z"
        da = ' stroke-dasharray="6 4"' if dash else ""
        # thickness: the same slab pushed down and right, in a darker tone
        side = mix(stroke, c["panel"], 0.35)
        f.parts.append(f'<path d="{d}" transform="translate(7 6)" fill="{side}"/>')
        paint = f._paint(fill)
        f.parts.append(
            f'<path d="{d}" fill="{paint}" stroke="{stroke}" stroke-width="1.5"{da}/>'
        )
        f.text(
            cx,
            y + th / 2 - 4,
            f"{n}  {name}",
            size=15 if i else 14,
            weight=700,
            anchor="middle",
            fill=c[tone] if tone else c["ink"],
        )
        # right-hand explanation
        f.text(
            780, y + 28, tag, size=12, weight=700, fill=c[tone] if tone else c["soft"]
        )
        for j, line in enumerate(_wrap(body, 52)):
            f.text(780, y + 50 + j * 17, line, size=12.5, fill=c["soft"])
        f.arrow(
            [(cx + (w_top + w_bot) / 4 + 8, y + th / 2 - 8), (768, y + th / 2 - 8)],
            tone=None,
            dash=True,
        )
    f.rect(780, 676, 388, 28, fill=c["box"], stroke=c["boxedge"], r=8)
    f.text(
        792,
        695,
        "Project's own experiments: plates stacking, blind 60-episode test.",
        size=11.5,
        fill=c["soft"],
    )
    return f


def fig_layers(t):
    f = Fig(
        "03-layers",
        1200,
        820,
        t,
        "LEVI's layers: one entry layer, one capability boundary, domain modules, adapters",
        "Agent work from the web UI, REST, MCP and CLI goes through one capability layer that enforces permissions, revisions and "
        "undo; domain modules sit below it; stores, FFmpeg and model workers at the bottom, with heavy models in isolated environments.",
    )
    c = f.c
    f.heading(
        "Layers of the workbench",
        "Four interfaces, one capability layer for agent work, ten main modules, and the adapters beneath them.",
    )
    # layer bands
    f.slab(20, 84, 1160, 94)
    f.text(32, 98, "INTERFACES", size=11.5, weight=700, fill=c["faint"], ls=1)
    for i, (a, b) in enumerate(
        [
            ("Web UI", "Next.js · React · light and dark"),
            ("REST", "loopback API, token-gated"),
            ("MCP", "stdio, scoped grants"),
            ("CLI", "levi …  one command per group"),
        ]
    ):
        f.box(32 + i * 290, 108, 276, 62, a, [b], size=14)
    f.arrow(
        [(600, 170), (600, 196)],
        "agent work",
        tone=None,
        lx=612,
        ly=188,
        anchor="start",
    )
    f.text(
        32, 220, "BOUNDARY FOR AGENT WORK", size=11.5, weight=700, fill=c["faint"], ls=1
    )
    f.rect(32, 230, 1136, 96, fill=c["gate_bg"], stroke=c["gate"], r=12, sw=1.6, elev=2)
    f.text(52, 256, "Agent capability layer", size=16, weight=700, fill=c["gate"])
    f.text(
        52,
        277,
        "Permissions (read · draft · execute · approve · commit) · revision checks · idempotency keys · undo · audit trail",
        size=12.5,
        fill=c["ink"],
    )
    f.text(
        52,
        296,
        "Used by the Agent Workbench, the agent REST routes, MCP and `levi agent`: a scoped agent connection cannot reach a person-only action by another route.",
        size=12,
        fill=c["soft"],
    )
    f.text(
        52,
        314,
        "Direct routes (convert, pool, annotate, live, automatic) have their own checks: plan before run, same-origin writes, person-only routes refuse agent credentials.",
        size=12,
        fill=c["soft"],
    )
    f.arrow([(600, 326), (600, 346)], tone="gate")
    f.slab(20, 356, 1160, 180)
    f.text(32, 370, "DOMAIN MODULES", size=11.5, weight=700, fill=c["faint"], ls=1)
    mods = [
        ("Conversion", "raw → v2.1 / RECAP"),
        ("Annotations", "sidecars · outcomes · undo"),
        ("Agents + harness", "plans · pilots · cost · memory"),
        ("Training pool", "index · recipes · exports"),
        ("RECAP value", "checkpoints · advantage"),
        ("Segmentation", "SAM3 student · masks"),
        ("Live annotation", "rollouts · GPU gate"),
        ("Events (A)", "opt-in: candidates, judgements"),
        ("Performance (B)", "admission kernel, not wired"),
        ("Automatic eval (C)", "AERI: journal, authority"),
    ]
    for i, (a, b) in enumerate(mods):
        col, row = i % 5, i // 5
        tone = (
            "system" if a.startswith(("Events", "Performance", "Automatic")) else None
        )
        f.box(
            32 + col * 228,
            380 + row * 78,
            216,
            66,
            a,
            [b],
            tone=tone,
            size=13.5,
            subsize=12.5,
        )
    f.arrow([(600, 538), (600, 564)], tone=None)
    f.slab(20, 576, 1160, 94)
    f.text(32, 588, "ADAPTERS AND STORES", size=11.5, weight=700, fill=c["faint"], ls=1)
    ad = [
        ("Files + SQLite", "datasets are never rewritten"),
        ("FFmpeg", "one encode per camera; remux to retime"),
        ("Model servers", "Ollama · vLLM, loopback only"),
        ("Worker environments", "SAM3 · segmentation · RECAP"),
    ]
    for i, (a, b) in enumerate(ad):
        f.box(32 + i * 290, 598, 276, 62, a, [b], size=13.5, subsize=12.5)
    f.rect(32, 682, 1136, 110, fill=c["box"], stroke=c["boxedge"], r=12, elev=1)
    f.text(52, 704, "Rules that hold in every layer", size=13.5, weight=700)
    f.lines(
        52,
        726,
        [
            "Source data is read-only: conversion and export write to new folders.   ·   The core never imports Torch: heavy models run in their own environments.",
            "Unknown is never success: an unavailable judge is reported as unavailable.   ·   Every long-running child process is registered and reclaimed on stop or restart.",
            "A model's suggestion becomes a label only through a person's commit; the live service's audited auto-approver is the one exception, and marks its labels `auto`.",
        ],
        size=12,
        gap=17,
        fill=c["soft"],
    )
    return f


def fig_workflow(t):
    f = Fig(
        "04-human-in-charge",
        1200,
        600,
        t,
        "One task, three actors: the agent proposes and works, LEVI checks and records, the person holds every gate",
        "A swim-lane workflow in nine steps: plan, check, approve, pilot, accept, remaining episodes, review, commit, record. "
        "Only the person approves, accepts and commits; the harness records cost and verified facts around the task.",
    )
    c = f.c
    f.heading(
        "A task, step by step",
        "Agent work is cheap to start and cheap to undo, because only a person can approve, accept and commit.",
    )
    lanes = {"Agent": ("model", 96), "LEVI": ("system", 236), "Person": ("person", 376)}
    for name, (tone, y) in lanes.items():
        f.rect(
            32,
            y,
            1136,
            124,
            fill=c[tone + "_bg"],
            stroke=c[tone],
            r=12,
            sw=1.0,
            op=0.55,
        )
        f.text(48, y + 22, name, size=13, weight=700, fill=c[tone])
        f.icon(
            {"Agent": "chip", "LEVI": "db", "Person": "person"}[name],
            100,
            y + 8,
            tone,
            size=18,
        )
    steps = [
        ("1  Plan", "a task spec", "Agent", "model"),
        ("2  Check", "vs the catalog", "LEVI", "system"),
        ("3  Approve", "plan, person-only", "Person", "gate"),
        ("4  Pilot", "a small sample", "Agent", "model"),
        ("5  Accept", "pilot (default)", "Person", "gate"),
        ("6  Remaining", "cites its frames", "Agent", "model"),
        ("7  Review", "each suggestion", "Person", "person"),
        ("8  Commit", "undoable", "Person", "gate"),
        ("9  Record", "closes the task", "LEVI", "system"),
    ]
    W, STEP, X0 = 118, 125, 44
    pos = []
    for i, (title, sub, lane, tone) in enumerate(steps):
        x, y = X0 + i * STEP, lanes[lane][1] + 40
        f.box(x, y, W, 62, title, [sub], tone=tone, size=12.5, subsize=11.5)
        pos.append((x, y))
    for i in range(len(steps) - 1):
        (x0, y0), (x1, y1) = pos[i], pos[i + 1]
        mid = x0 + W + (STEP - W) / 2
        f.arrow(
            [(x0 + W, y0 + 31), (mid, y0 + 31), (mid, y1 + 31), (x1, y1 + 31)],
            tone=steps[i][3],
        )
    f.note(
        512,
        "Uncertainty is a valid answer. Each suggestion cites the frames it was read from, and a commit can be undone through the same review.",
        tone="person",
    )
    f.note(
        552,
        "Around every task the harness records cost per agent and per dataset and keeps what a person verified for the next task.",
        tone="system",
    )
    return f


def fig_live(t):
    f = Fig(
        "05-live-gpu",
        1200,
        680,
        t,
        "Live annotation beside a robot evaluation: one GPU, shared by turns",
        "While a robot evaluation writes rollouts, the live service labels finished episodes with a local model, but only "
        "while the policy server is not inferring; its verdicts are automatic and unreviewed until a person reviews them.",
    )
    c = f.c
    f.heading(
        "Live annotation beside a robot",
        "A gate keeps the vision model out of the way while the policy is inferring.",
    )
    # components
    f.box(
        32,
        96,
        270,
        92,
        "Robot evaluation",
        ["policy server infers on the GPU", "client writes rollout folders"],
        tone="gate",
        size=14,
        icon="arm",
    )
    f.box(
        466,
        96,
        268,
        92,
        "LEVI live service",
        ["its own workspace", "watcher · queue · GPU gate"],
        tone="system",
        size=14,
        icon="db",
    )
    f.box(
        898,
        96,
        270,
        92,
        "Local vision model",
        ["vLLM · loopback · sleeps/stops idle", "Qwen3.8 judges and segments"],
        tone="model",
        size=14,
        icon="chip",
    )
    f.arrow(
        [(302, 130), (466, 130)],
        "finished episode",
        tone="gate",
        lx=384,
        ly=122,
        flow=True,
    )
    f.arrow(
        [(734, 130), (898, 130)],
        "frames + question",
        tone="system",
        lx=816,
        ly=122,
        flow=True,
    )
    f.arrow(
        [(898, 164), (734, 164)],
        "time segments · outcome",
        tone="model",
        lx=816,
        ly=184,
        flow=True,
    )
    f.box(
        466,
        232,
        268,
        64,
        "Automatic, unreviewed labels",
        ["never written as human labels"],
        tone="system",
        size=13.5,
        subsize=12.5,
    )
    f.arrow(
        [(600, 188), (600, 232)],
        "writes",
        tone="system",
        lx=612,
        ly=216,
        anchor="start",
        flow=True,
    )
    f.box(
        466,
        330,
        268,
        64,
        "A person reviews",
        ["normal viewer; auto-approve opt-in"],
        tone="person",
        size=13.5,
        subsize=12.5,
    )
    f.arrow(
        [(600, 296), (600, 330)],
        "to review",
        tone="person",
        lx=612,
        ly=318,
        anchor="start",
        flow=True,
    )
    # GPU timeline
    f.text(32, 432, "ONE GPU OVER TIME", size=11.5, weight=700, fill=c["faint"], ls=1)
    f.rect(32, 444, 1136, 56, fill=c["box"], stroke=c["boxedge"], r=10)
    segs = [
        ("policy infers", 32, 250, "gate"),
        ("idle: gate opens", 282, 130, None),
        ("model labels the episode", 412, 330, "model"),
        ("policy infers", 742, 230, "gate"),
        ("model sleeps", 972, 196, None),
    ]
    for label, x, w, tone in segs:
        fill = c[tone + "_bg"] if tone else c["panel"]
        stroke = c[tone] if tone else c["boxedge"]
        f.rect(
            x + 3, 450, w - 6, 44, fill=fill, stroke=stroke, r=8, dash=(tone is None)
        )
    f.uses.add("now")
    # "now": a marker sweeping the strip, behind the labels, so reading order is unchanged
    f.parts.append(
        f'<g class="now" transform="translate(520 0)"><rect x="39" y="446" width="2" height="52" rx="1" fill="{c["accent"]}" fill-opacity="0.55"/>'
        f'<path d="M33 511 L47 511 L40 500 Z" fill="{c["accent"]}"/></g>'
    )
    for label, x, w, tone in segs:
        f.text(
            x + w / 2,
            477,
            label,
            size=12.5,
            weight=600,
            anchor="middle",
            fill=c[tone] if tone else c["soft"],
        )
    f.note(
        520,
        "The service yields the GPU when another process computes; with a shared lock file configured it holds it while its model server is up. Start it before the policy server.",
        tone="gate",
    )
    f.note(
        560,
        "It never writes the rollout folders, never connects to the robot, never writes a human outcome label.",
        tone="danger",
    )
    f.note(
        600,
        "Status: the gate is verified with fakes; sleep and wake beside a real policy server are not yet verified with the real model.",
        tone="system",
    )
    return f


def fig_aeri(t):
    f = Fig(
        "06-aeri-authority",
        1200,
        640,
        t,
        "Automatic evaluation (AERI): three parts joined by versioned messages, one part with motion authority",
        "A proposes events and judges goals and scenes, B grants resources and serves policy chunks, C alone may command "
        "motion and write the run journal. Messages from A or B carrying a robot or reset command are refused. "
        "Status: software with fake-I/O validation; hardware acceptance is pending.",
    )
    c = f.c
    f.heading(
        "Automatic evaluation: three parts, one authority",
        "The judge can say what happened; only one part can move the robot, and it writes the only journal.",
    )
    f.box(
        32,
        130,
        330,
        130,
        "A  Event intelligence",
        [
            "proposes events from recorded signals",
            "judges goals and scenes",
            "may answer 'unknown' or 'unavailable'",
        ],
        tone="model",
        size=15,
        icon="doc",
    )
    f.box(
        435,
        130,
        330,
        130,
        "B  Performance runtime",
        [
            "grants resources (leases, admission)",
            "serves policy chunks",
            "reports pressure, acknowledges quiesce",
        ],
        tone="system",
        size=15,
        icon="chip",
    )
    f.box(
        838,
        130,
        330,
        130,
        "C  Automatic pipeline",
        [
            "designed as the only part with motion authority",
            "the only writer of the run journal",
            "pauses, resets, resumes, stops",
        ],
        tone="danger",
        size=15,
        icon="arm",
        elev=2,
    )
    f.arrow(
        [(197, 130), (197, 104), (1003, 104), (1003, 130)],
        None,
        tone="model",
        flow=True,
    )
    f.text(
        600,
        94,
        "event · judgement · scene  (A → C)",
        size=12,
        anchor="middle",
        fill=c["model"],
        weight=500,
    )
    f.arrow(
        [(600, 260), (600, 290), (1003, 290), (1003, 260)],
        None,
        tone="system",
        both=True,
    )
    f.text(
        800,
        282,
        "runtime messages (B ↔ C)",
        size=12,
        anchor="middle",
        fill=c["system"],
        weight=500,
    )
    f.arrow(
        [(197, 260), (197, 290), (500, 290), (500, 260)],
        None,
        tone="system",
        dash=True,
        both=True,
    )
    f.text(348, 282, "B ↔ A", size=12, anchor="middle", fill=c["system"], weight=500)
    f.text(
        30,
        318,
        "versioned contracts: levi.aeri.event · judgement · scene · runtime · run_event  (v1)",
        size=12.5,
        fill=c["soft"],
        mono=True,
    )
    f.rect(
        32, 340, 1136, 92, fill=c["danger_bg"], stroke=c["danger"], r=12, sw=1.4, elev=1
    )
    f.uses.add("guard")
    f.parts.append(
        f'<rect class="guard" x="32" y="340" width="1136" height="92" rx="12" fill="none" '
        f'stroke="{c["danger"]}" stroke-width="3"/>'
    )
    f.text(
        52, 366, "What the contract refuses", size=13.5, weight=700, fill=c["danger"]
    )
    f.lines(
        52,
        388,
        [
            "A robot or reset command key anywhere in a message from A or B (after Unicode normalisation)  →  E_CONTROL_FIELD.",
            "A newer schema minor than the reader knows → refused (fail closed).   An unknown field, a duplicate key, NaN → refused.",
            "An unknown or unavailable verdict is never turned into success; an unknown physical command result is never re-sent.",
        ],
        size=12,
        gap=17,
        fill=c["ink"],
    )
    f.box(
        32,
        452,
        552,
        92,
        "Status today",
        [
            "software contracts and execution bridge, validated with fake I/O",
            "B is an admission kernel not installed in any service; A is opt-in",
            "hardware acceptance pending: no claim of a real-robot result",
        ],
        tone="gate",
        size=13.5,
        subsize=13,
    )
    f.box(
        616,
        452,
        552,
        92,
        "Who may do what",
        [
            "A and B: read, judge, serve.   C: command, write the journal.",
            "A person: approve runs, take over, accept results.",
        ],
        tone="person",
        size=13.5,
        subsize=13,
    )
    f.note(
        562,
        "AERI is this pipeline's code name. The launch wizard and run window configure and show; they cannot give A or B motion authority.",
        tone="gate",
    )
    return f


def fig_evidence(t):
    f = Fig(
        "07-evidence",
        1200,
        700,
        t,
        "Judging success: one task meets the bar, one does not",
        "Dot plots on blind 60-episode test sets with the scale-up quality bar drawn in. Plates: release-anchored review "
        "reaches balanced accuracy 0.936 (LEVI from segments 0.808, a direct call 0.782) and judges 12.8% of failures as "
        "successes (38.5%, 43.6%); both meet the bar of 0.90 and 15%. Screws: 0.756 and 48.7%, both miss it. Time-segment "
        "labelling is at parity with a direct call; whether curated data improves a policy is not established.",
    )
    c = f.c
    f.heading(
        "Judging success: one task meets the bar, one does not",
        "Same local model, blind gold labels, 60 episodes per task. The project's own experiments; the record is not shipped.",
    )
    # legend: hollow = direct call, grey = LEVI from segments, solid = anchored review
    lx, ly = 32, 98
    f.parts.append(
        f'<circle cx="{lx + 8}" cy="{ly}" r="7" fill="{c["panel"]}" stroke="{c["faint"]}" stroke-width="2.5"/>'
    )
    f.text(lx + 22, ly + 5, "direct call", size=14, fill=c["soft"])
    f.parts.append(f'<circle cx="{lx + 128}" cy="{ly}" r="7" fill="{c["faint"]}"/>')
    f.text(lx + 142, ly + 5, "LEVI, from segments", size=14, fill=c["soft"])
    f.parts.append(f'<circle cx="{lx + 308}" cy="{ly}" r="9" fill="{c["system"]}"/>')
    f.text(lx + 324, ly + 5, "release-anchored review", size=14, fill=c["soft"])
    f.text(
        1168,
        ly + 5,
        "change vs LEVI, from segments",
        size=13,
        fill=c["faint"],
        anchor="end",
    )

    ax0, ax1 = 230, 1030

    def chart(
        y0,
        title,
        sub,
        rows,
        lo,
        hi,
        ticks,
        bar,
        bar_label,
        lower_better,
        fmt,
        delta_fmt,
    ):
        def X(v):
            return ax0 + (ax1 - ax0) * (v - lo) / (hi - lo)

        f.text(32, y0, title, size=19, weight=700)
        f.text(32 + 11 * len(title) + 24, y0, sub, size=14, fill=c["soft"])
        top, rowh = y0 + 30, 62
        bottom = top + rowh * len(rows)
        for v in ticks:
            f.parts.append(
                f'<line x1="{X(v):.1f}" y1="{top}" x2="{X(v):.1f}" y2="{bottom}" stroke="{c["edge"]}" stroke-width="1.2"/>'
            )
            f.text(
                round(X(v), 1),
                bottom + 20,
                fmt(v),
                size=13.5,
                fill=c["faint"],
                anchor="middle",
            )
        gx = round(X(bar), 1)
        f.parts.append(
            f'<line x1="{gx}" y1="{top - 6}" x2="{gx}" y2="{bottom}" stroke="{c["gate"]}" '
            f'stroke-width="2.5" stroke-dasharray="7 5"/>'
        )
        f.text(gx + 7, top + 6, bar_label, size=13.5, weight=700, fill=c["gate"])
        for i, (name, vals) in enumerate(rows):
            cy = top + rowh * i + rowh / 2 + 4
            f.text(32, cy + 6, name, size=17, weight=700)
            a = vals["anchored"]
            ok = a <= bar if lower_better else a >= bar
            col = c["system"] if ok else c["danger"]
            base = [vals[k] for k in ("direct", "levi") if vals.get(k) is not None]
            if base:
                lo_v, hi_v = min([*base, a]), max([*base, a])
                f.parts.append(
                    f'<line x1="{X(lo_v):.1f}" y1="{cy}" x2="{X(hi_v):.1f}" y2="{cy}" stroke="{c["boxedge"]}" '
                    f'stroke-width="7" stroke-linecap="round"/>'
                )
                f.parts.append(
                    f'<circle cx="{X(vals["direct"]):.1f}" cy="{cy}" r="8" fill="{c["panel"]}" '
                    f'stroke="{c["faint"]}" stroke-width="2.5"/>'
                )
                f.parts.append(
                    f'<circle cx="{X(vals["levi"]):.1f}" cy="{cy}" r="8" fill="{c["faint"]}"/>'
                )
                d = a - vals["levi"]
                f.text(
                    1168,
                    cy + 7,
                    ("+" if d > 0 else "\u2212") + delta_fmt(abs(d)),
                    size=19,
                    weight=700,
                    fill=col,
                    anchor="end",
                )
            else:
                f.text(
                    1168,
                    cy + 6,
                    "other arms did not finish",
                    size=13,
                    fill=c["faint"],
                    anchor="end",
                )
            f.parts.append(f'<circle cx="{X(a):.1f}" cy="{cy}" r="10.5" fill="{col}"/>')
            f.text(
                round(X(a), 1),
                cy - 15,
                fmt(a),
                size=15,
                weight=700,
                fill=col,
                anchor="middle",
            )
            f.text(
                round(X(a), 1),
                cy + 27,
                "meets the bar" if ok else "misses the bar",
                size=13,
                weight=600,
                fill=col,
                anchor="middle",
            )
        return bottom + 20

    def acc(v):
        return f"{v:.2f}"

    def pct(v):
        x = round(v * 100, 1)
        return f"{x:.0f}%" if x == int(x) else f"{x}%"

    y = chart(
        150,
        "Balanced accuracy",
        "higher is better; the axis starts at 0.5, the chance level",
        [
            ("Plates", {"direct": 0.782, "levi": 0.808, "anchored": 0.936}),
            ("Screws", {"anchored": 0.756}),
        ],
        0.5,
        1.0,
        [0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
        0.90,
        "scale-up bar 0.90",
        False,
        acc,
        acc,
    )
    y = chart(
        y + 50,
        "Failures judged as success",
        "lower is better",
        [
            ("Plates", {"direct": 0.436, "levi": 0.385, "anchored": 0.128}),
            ("Screws", {"anchored": 0.487}),
        ],
        0.0,
        0.6,
        [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6],
        0.15,
        "bar 15%",
        True,
        pct,
        lambda d: f"{d * 100:.1f} pts",
    )
    f.box(
        32,
        y + 24,
        552,
        78,
        "Time-segment labelling: parity",
        [
            "LEVI and a direct call of the same model score alike (segment F1",
            "about 0.34 on plates); LEVI takes about 1.7x the direct call's time.",
        ],
        tone="model",
        size=15,
        subsize=13.5,
    )
    f.box(
        616,
        y + 24,
        552,
        78,
        "Not established",
        [
            "Whether training on LEVI-curated data improves a robot policy.",
            "Automatic live verdicts are unreviewed and over-call success.",
        ],
        tone="gate",
        size=15,
        subsize=13.5,
        dash=True,
    )
    f.text(
        32,
        y + 132,
        "Plates re-run: docs/VALIDATION.md. Some screws outcomes reached the coordinator through annotator replies; "
        "not used for tuning. No figure here is a real-robot policy result.",
        size=12.5,
        fill=c["faint"],
    )
    f.h = int(y + 156)
    return f


def fig_aeri_flow(t):
    f = Fig(
        "08-aeri-flow",
        1200,
        1424,
        t,
        "An AERI run, start to finish: one terminal drives the robot, the page and the vision model watch, answer and label",
        "A swim-lane flow of one real-robot evaluation with the automatic pipeline (AERI). Prepare: the robot stack, the job, "
        "binding and contract, a read-only check. Start: the typed execute phrase, a start summary, the start-pose check and the "
        "policy warm-up. Every round: set the scene, answer the scene question, the policy acts, an event candidate, the vision "
        "judgement, stop when done, home and verify, the reset prompt. Exceptions: an unknown verdict is never a success, event "
        "contract violations turn events off for the round, a fault locks the run, a stop is possible at any time. End: summary, "
        "metrics, labels and the run journal. Only terminal D sends motion. Status: the round loop, judging, the home check and "
        "page tracking are in main; the one-terminal console, the start checks, the policy unit D starts itself and terminal-only "
        "resume are on the integration branch, not yet in main; pausing the policy to judge and the handling of other event "
        "adapter errors are planned; hardware acceptance pending.",
    )
    c = f.c
    f.heading(
        "An AERI run, start to finish",
        "Terminal D runs the whole session and alone moves the robot; the page and the vision model watch, answer and label.",
    )
    # legend: what the badges mean
    x = 32
    for kind, words in (
        ("main", "in the main branch"),
        ("branch", "on the integration branch, not yet in main"),
        ("planned", "designed, not built (dashed)"),
    ):
        w = f.tag(x + len(f.TAGS[kind][0]) * 6.6 + 14, 86, kind)
        f.text(x + w + 8, 90.5, words, size=12, fill=c["soft"])
        x += w + 8 + len(words) * 6.3 + 22
    f.text(1168, 90.5, "No badge: outside LEVI.  The dot marks the current step.", size=12, anchor="end", fill=c["soft"])
    f.text(
        32, 111, "Keys in D:  Enter = scene reset  ·  y / n / u = scene ready?  ·  Ctrl+C (q on the branch) = stop",
        size=12, fill=c["soft"],
    )

    lane_top, lane_bottom = 124, 1118
    lanes = [
        ("Operator", "you, at the robot", "person", "person"),
        ("Web page", "watch, answer, label", None, "doc"),
        ("Terminal D", "scripts/aeri.py", "danger", "term"),
        ("Policy service", "the policy under test", "system", "chip"),
        ("Vision judge", "local vision model", "model", "eye"),
        ("Robot stack", "started by you", None, "arm"),
    ]
    tones = [tone for _, _, tone, _ in lanes]
    for k, (name, sub, tone, icon) in enumerate(lanes):
        x0 = AF_COL0 + k * AF_COLW
        fill = c[tone + "_bg"] if tone else c["box"]
        stroke = c[tone] if tone else c["boxedge"]
        f.rect(x0 + 3, lane_top, AF_COLW - 6, lane_bottom - lane_top, fill=fill, stroke=stroke, r=12, sw=1.0, op=0.45)
        f.icon(icon, x0 + 12, lane_top + 9, tone, size=20)
        f.text(x0 + 38, lane_top + 24, name, size=13, weight=700, fill=c[tone] if tone else c["ink"])
        f.text(x0 + 14, lane_top + 42, sub, size=11.5, fill=c["soft"])

    def phase(y, label):
        w = f.pill(60, y, label, tone="system" if label.startswith("3") else "model")
        f.parts.append(
            f'<path d="M{60 + w + 8} {y + 12} L1160 {y + 12}" stroke="{c["edge"]}" stroke-width="1" stroke-dasharray="2 4"/>'
        )

    def box(lane, y, title, sub, status=None, tone="lane", badge=True):
        """A step card; the badge sits on its top edge, right of centre, clear of an arrow into the top."""
        tone = tones[lane] if tone == "lane" else tone
        x, w = af_x(lane), AF_BOXW
        assert len(title) * 12.5 * 0.6 <= w - 24, title  # rough Inter widths: keep text inside the card
        for line in sub:
            assert len(line) * 11.5 * 0.56 <= w - 24, line
        f.rect(
            x, y, w, AF_BOXH,
            fill=c[tone + "_bg"] if tone else c["box"],
            stroke=c[tone] if tone else c["boxedge"],
            dash=status == "planned", sw=1.4 if tone else 1.2,
        )
        f.text(x + 12, y + 19, title, size=12.5, weight=600, fill=c[tone] if tone else c["ink"])
        for j, line in enumerate(sub):
            f.text(x + 12, y + 35 + j * 14, line, size=11.5, fill=c["soft"])
        if status and badge:
            f.tag(x + w - 8, y, status)

    def mid(y):
        return y + AF_BOXH // 2

    def bottom(y):
        return y + AF_BOXH

    # 1 Prepare
    phase(178, "1  Prepare")
    r = 212
    box(0, r, "Pick the job", ("plan, binding and the", "scene contract"), "main")
    box(1, r, "Job wizard", ("writes the job file;", "dry runs only here"), "main")
    box(2, r, "aeri.py check", ("read-only preflight,", "prints the digests"), "main")
    box(4, r, "Live service up", ("serves the vision", "model; you start it"), "main")
    box(5, r, "Stack running", ("arm, gripper, robot", "server, health monitor"))
    f.arrow([(af_x(1) + AF_BOXW, mid(r)), (af_x(2), mid(r))])
    ra, rb = 310, 378
    f.arrow([(af_cx(2), bottom(r)), (af_cx(2), ra)])

    # 2 Start
    phase(278, "2  Start")
    box(0, ra, "Type execute <id>", ("confirms the plan and", "binding digests"), "main")
    box(2, ra, "Start checks", ("summary, start pose,", "policy warm-up"), "branch")
    box(3, rb, "Own unit, warmed", ("started by D, or the", "service you run"), "branch")
    box(1, rb, "Run appears", ("registered by D;", "reads its journal"), "main")
    f.arrow([(af_x(0) + AF_BOXW, mid(ra)), (af_x(2), mid(ra))], tone="person")
    f.arrow([(af_x(2) + 142, bottom(ra)), (af_x(2) + 142, mid(rb)), (af_x(3), mid(rb))], tone="danger")
    f.arrow([(af_x(2) + 26, bottom(ra)), (af_x(2) + 26, mid(rb)), (af_x(1) + AF_BOXW, mid(rb))], tone="danger")
    # into round 1: joins the loop's return line above step 1
    f.arrow([(af_cx(3), bottom(rb)), (af_cx(3), AF_TOP3), (af_cx(0), AF_TOP3)], head=False)
    f.text(af_cx(2), AF_TOP3 - 6, "round 1", size=12, anchor="middle", fill=c["soft"], weight=500)

    # 3 Every round
    phase(444, "3  Every round")
    rows = [af_row(i) for i in range(len(AF_LOOP))]
    for i, (k, title, sub, status) in enumerate(AF_LOOP):
        if i:  # arrow from the previous step, along the dot's route
            pk, py = AF_LOOP[i - 1][0], rows[i - 1]
            if pk == k:
                f.arrow([(af_cx(k), bottom(py)), (af_cx(k), rows[i])])
            else:
                side = af_x(k) if af_cx(k) > af_cx(pk) else af_x(k) + AF_BOXW
                f.arrow([(af_cx(pk), bottom(py)), (af_cx(pk), mid(rows[i])), (side, mid(rows[i]))])
    k8 = AF_LOOP[-1][0]
    f.arrow(
        [(af_x(k8), mid(rows[-1])), (AF_RET_X, mid(rows[-1])), (AF_RET_X, AF_TOP3), (af_cx(0), AF_TOP3), (af_cx(0), rows[0])]
    )
    f.text(AF_RET_X + 22, mid(rows[-1]) - 8, "next round", size=12, fill=c["soft"], weight=500)
    # the dot, under the cards: it disappears into the step it is in, and that step lights up.
    # pathLength scales the dash in browsers; the gap is longer than the path so a renderer
    # without pathLength still draws one dot
    d = "M" + " L".join(f"{x} {y}" for x, y in af_loop_path())
    f.uses.update(("token", "step"))
    f.parts.append(
        f'<path class="token" d="{d}" pathLength="100" fill="none" stroke="{c["accent"]}" stroke-width="9" '
        f'stroke-linecap="round" stroke-linejoin="round" stroke-dasharray="0.1 9999"/>'
    )
    for i, (k, title, sub, status) in enumerate(AF_LOOP):
        box(k, rows[i], title, sub, status, badge=False)
    for i, (k, *_) in enumerate(AF_LOOP):
        f.parts.append(
            f'<rect class="st s{i}" x="{af_x(k) - 2.5}" y="{rows[i] - 2.5}" width="{AF_BOXW + 5}" height="{AF_BOXH + 5}" '
            f'rx="11" fill="none" stroke="{c["accent"]}" stroke-width="2.5"/>'
        )
    for i, (k, *_, status) in enumerate(AF_LOOP):  # badges above the highlight rings
        f.tag(af_x(k) + AF_BOXW - 8, rows[i], status)
    # what happens beside the loop
    box(1, rows[1], "Or answer here", ("the scene question;", "first answer counts"), "main")
    f.arrow([(af_x(1), mid(rows[1])), (af_x(0) + AF_BOXW, mid(rows[1]))])
    box(5, rows[2], "Arm moves", ("only on D's guarded,", "acknowledged steps"), "main")
    f.arrow([(af_x(3) + AF_BOXW, mid(rows[2])), (af_x(5), mid(rows[2]))], "via D", lx=af_cx(4), ly=mid(rows[2]) - 7)
    box(5, rows[4], "Pause to judge", ("the policy holds still", "while the model judges"), "planned")
    f.arrow([(af_x(4) + AF_BOXW, mid(rows[4])), (af_x(5), mid(rows[4]))], dash=True)

    # 5 End
    phase(1020, "4  End")
    re_ = 1052
    box(2, re_, "Run ends", ("summary line and a", "clear exit status"), "branch")
    box(1, re_, "Results", ("metrics, labels and", "the run journal"), "main")
    box(0, re_, "Label the rounds", ("blind, on the page;", "keys in D (opt-in)"), "main")
    f.arrow(
        [(af_cx(5), bottom(rows[6])), (af_cx(5), mid(re_)), (af_x(2) + AF_BOXW, mid(re_))],
        "after the last round", lx=af_cx(4), ly=mid(re_) - 7,
    )
    f.arrow([(af_x(2), mid(re_)), (af_x(1) + AF_BOXW, mid(re_))], tone="danger")
    f.arrow([(af_x(0) + AF_BOXW, mid(re_)), (af_x(1), mid(re_))], tone="person")

    # 4 When something goes wrong
    f.pill(196, 1130, "Any time: when something goes wrong", tone="danger")
    by, bw = 1168, 267
    branches = [
        (
            "Event source fails (R4)", "model", "main",
            ["Contract violations: events off for", "this round, noted; the round goes",
             "on. Other adapter errors: the same", "handling is planned."],
        ),
        (
            "Verdict unknown (R5)", "model", "main",
            ["Never counted as success: noted, the", "round runs to its step limit, your",
             "blind label decides. Pause-to-judge", "mode locks instead (planned)."],
        ),
        (
            "FAULT_LOCKED", "danger", "main",
            ["Home not verified, a policy error or", "a gripper reply unconfirmed (branch)",
             "holds the arm; D prints a fault", "summary (branch). New run, new id."],
        ),
        (
            "Stop at any time", "person", "main",
            ["Ctrl+C in D (q on the branch), or", "Stop on the page: no more motion;",
             "the run waits or ends. Resume by a", "key in D (branch); never the page."],
        ),
    ]
    f.uses.add("drift")
    for i, (title, tone, status, body) in enumerate(branches):
        bx = 52 + i * (bw + 16)
        for line in body:
            assert len(line) * 12 * 0.55 <= bw - 24, line
        f.rect(bx, by, bw, 102, fill=c[tone + "_bg"], stroke=c[tone], sw=1.4)
        f.text(bx + 14, by + 22, title, size=13, weight=700, fill=c[tone])
        f.lines(bx + 14, by + 42, body, size=12, gap=16, fill=c["soft"])
        f.tag(bx + bw - 10, by, status)
        ax = bx + bw // 2
        f.arrow([(ax, lane_bottom + 2), (ax, by - 9)], dash=True, cls="drift")

    # who may move the robot
    ay = 1286
    f.rect(32, ay, 1136, 84, fill=c["danger_bg"], stroke=c["danger"], r=12, sw=1.4, elev=1)
    f.text(52, ay + 23, "Who may move the robot", size=13.5, weight=700, fill=c["danger"])
    rules = [
        ("Only D's process sends motion, and only after its typed execute phrase; a real launch over HTTP is refused.", "main"),
        ("A page or model answer only reports what it sees: it never sends motion, starts a run or clears a fault.", "main"),
        ("A stopped real run resumes only by a key in D; the page's resume button is refused for it.", "branch"),
    ]
    for j, (line, status) in enumerate(rules):
        f.tag(120, ay + 39 + j * 17, status)
        f.text(132, ay + 43 + j * 17, line, size=12.5, fill=c["ink"])
    f.note(
        1382,
        "Status: software, tested with fakes and offline runs. Pausing the policy to judge stays refused for real runs. "
        "Hardware acceptance pending.",
        tone="system",
    )
    return f


def _wrap(s, n):
    words, out, cur = s.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > n:
            out.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        out.append(cur)
    return out


FIGURES = [
    fig_context,
    fig_pyramid,
    fig_layers,
    fig_workflow,
    fig_live,
    fig_aeri,
    fig_evidence,
    fig_aeri_flow,
]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for make in FIGURES:
        for theme in ("light", "dark"):
            fig = make(theme)
            (OUT / f"{fig.name}-{theme}.svg").write_text(fig.svg(), encoding="utf8")
    print(f"wrote {len(FIGURES) * 2} files to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
