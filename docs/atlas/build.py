#!/usr/bin/env python3
"""Generates the LEVI architecture atlas (docs/assets/atlas/*.svg), English only.

    python3 docs/atlas/build.py            # writes <name>-light.svg and <name>-dark.svg

Each figure is hand-laid on a grid in plain SVG (rect, text, path; no script, no
external image, no foreign object) so it renders the same on GitHub, in the
Report page and in a browser, and diffs as text. Light and dark files exist
because an <img> cannot inherit the page's colours; the README picks one with
<picture>. Every figure states one claim in its <title> and <desc>.
"""

from __future__ import annotations

import html
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "assets" / "atlas"
FONT = "Inter, -apple-system, 'Segoe UI', Roboto, 'Noto Sans', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"

PALETTE = {
    "light": dict(  # noqa: C408
        panel="#ffffff",
        edge="#d0d7de",
        ink="#1f2328",
        soft="#59636e",
        faint="#8c959f",
        box="#f6f8fa",
        boxedge="#d0d7de",
        accent="#24292f",
        on_accent="#ffffff",
        model="#0969da",
        model_bg="#ddf4ff",
        person="#1a7f37",
        person_bg="#dafbe1",
        gate="#9a6700",
        gate_bg="#fff8c5",
        system="#8250df",
        system_bg="#fbefff",
        danger="#cf222e",
        danger_bg="#ffebe9",
    ),
    "dark": dict(  # noqa: C408
        panel="#0d1117",
        edge="#30363d",
        ink="#e6edf3",
        soft="#9198a1",
        faint="#6e7681",
        box="#161b22",
        boxedge="#4b535d",
        accent="#e6edf3",
        on_accent="#0d1117",
        model="#58a6ff",
        model_bg="#10243e",
        person="#3fb950",
        person_bg="#0f2d1a",
        gate="#d29922",
        gate_bg="#2d2308",
        system="#bc8cff",
        system_bg="#271a3d",
        danger="#ff7b72",
        danger_bg="#3d1418",
    ),
}


class Fig:
    def __init__(self, name: str, w: int, h: int, theme: str, title: str, desc: str):
        self.name, self.w, self.h, self.theme = name, w, h, theme
        self.c = PALETTE[theme]
        self.title, self.desc = title, desc
        self.parts: list[str] = []
        self.markers: set[str] = set()

    # -- primitives ------------------------------------------------------
    def rect(
        self, x, y, w, h, fill=None, stroke=None, r=10, dash=False, sw=1.2, op=None
    ):
        c = self.c
        fill = fill or c["box"]
        stroke = stroke or c["boxedge"]
        d = ' stroke-dasharray="5 4"' if dash else ""
        o = f' fill-opacity="{op}"' if op is not None else ""
        self.parts.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}"{o} '
            f'stroke="{stroke}" stroke-width="{sw}"{d}/>'
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
    ):
        fill = fill or self.c["ink"]
        fam = MONO if mono else FONT
        o = f' opacity="{op}"' if op is not None else ""
        self.parts.append(
            f'<text x="{x}" y="{y}" font-family="{fam}" font-size="{size}" font-weight="{weight}" '
            f'text-anchor="{anchor}" fill="{fill}"{o}>{html.escape(s)}</text>'
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
    ):
        c = self.c
        fill, stroke = (c["box"], c["boxedge"])
        if tone:
            fill, stroke = c[tone + "_bg"], c[tone]
        self.rect(
            x, y, w, h, fill=fill, stroke=stroke, dash=dash, sw=1.4 if tone else 1.2
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
    ):
        c = self.c
        col = c[tone] if tone else c["soft"]
        mid = "a_" + (tone or "soft")
        self.markers.add(mid)
        if both:
            self.markers.add(mid + "_rev")
        d = "M" + " L".join(f"{x} {y}" for x, y in pts)
        da = ' stroke-dasharray="6 4"' if dash else ""
        start = f' marker-start="url(#{mid}_rev)"' if both else ""
        self.parts.append(
            f'<path d="{d}" fill="none" stroke="{col}" stroke-width="1.8" stroke-linejoin="round"{da} '
            f'marker-end="url(#{mid})"{start}/>'
        )
        if label:
            x0, y0 = pts[len(pts) // 2 - 1] if len(pts) > 2 else pts[0]
            x1, y1 = pts[len(pts) // 2]
            lx = lx if lx is not None else (x0 + x1) / 2
            ly = ly if ly is not None else (y0 + y1) / 2 - 7
            self.text(lx, ly, label, size=lsize, anchor=anchor, fill=col, weight=500)

    def heading(self, label, sub=None):
        self.text(32, 40, label, size=22, weight=700)
        if sub:
            self.text(32, 64, sub, size=13.5, fill=self.c["soft"])

    def note(self, y, s, tone="gate"):
        c = self.c
        self.rect(32, y, self.w - 64, 32, fill=c[tone + "_bg"], stroke=c[tone], r=8)
        self.text(48, y + 21, s, size=13.5, weight=500, fill=c[tone])

    # -- output ----------------------------------------------------------
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
        head = (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" width="{self.w}" '
            f'height="{self.h}" role="img" aria-labelledby="t d">'
            f'<title id="t">{html.escape(self.title)}</title><desc id="d">{html.escape(self.desc)}</desc>'
            f"<defs>{''.join(defs)}</defs>"
            f'<rect x="0.6" y="0.6" width="{self.w - 1.2}" height="{self.h - 1.2}" rx="16" fill="{c["panel"]}" stroke="{c["edge"]}" stroke-width="1.2"/>'
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
    f.rect(400, 150, 400, 400, fill=c["box"], stroke=c["accent"], r=16, sw=2)
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
    f.text(32, 120, "IN", size=12, weight=700, fill=c["faint"])
    ins = [
        ("Raw captures", "CSV + video or image folders"),
        ("LeRobot datasets", "v2.0 – v3.1, local or Hub"),
        ("Robot rollouts", "written by a running evaluation"),
        ("Hugging Face Hub", "streamed on demand"),
    ]
    for i, (a, b) in enumerate(ins):
        y = 138 + i * 100
        f.box(32, y, 280, 76, a, [b])
        f.arrow([(312, y + 38), (400, 382 + i * 12)], tone=None)
    # outputs
    f.text(888, 120, "OUT", size=12, weight=700, fill=c["faint"])
    outs = [
        ("LeRobot v2.1", "converted, source untouched"),
        ("RECAP value dataset", "per-frame value + advantage"),
        ("Training manifests", "which frames enter a loss, at what weight"),
        ("Pool exports", "merged tasks with provenance"),
    ]
    for i, (a, b) in enumerate(outs):
        y = 138 + i * 100
        f.box(888, y, 280, 76, a, [b])
        f.arrow([(800, 382 + i * 12), (888, y + 38)], tone=None)
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
    )
    f.arrow([(600, 574), (600, 550)], tone="person", dash=False)
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
        f.parts.append(
            f'<path d="{d}" fill="{fill}" stroke="{stroke}" stroke-width="1.5"{da}/>'
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
    f.text(32, 98, "INTERFACES", size=11.5, weight=700, fill=c["faint"])
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
    f.text(32, 220, "BOUNDARY FOR AGENT WORK", size=11.5, weight=700, fill=c["faint"])
    f.rect(32, 230, 1136, 96, fill=c["gate_bg"], stroke=c["gate"], r=12, sw=1.6)
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
    f.text(32, 370, "DOMAIN MODULES", size=11.5, weight=700, fill=c["faint"])
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
    f.text(32, 588, "ADAPTERS AND STORES", size=11.5, weight=700, fill=c["faint"])
    ad = [
        ("Files + SQLite", "datasets are never rewritten"),
        ("FFmpeg", "one encode per camera; remux to retime"),
        ("Model servers", "Ollama · vLLM, loopback only"),
        ("Worker environments", "SAM3 · segmentation · RECAP"),
    ]
    for i, (a, b) in enumerate(ad):
        f.box(32 + i * 290, 598, 276, 62, a, [b], size=13.5, subsize=12.5)
    f.rect(32, 680, 1136, 112, fill=c["box"], stroke=c["boxedge"], r=12)
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
    )
    f.arrow([(302, 130), (466, 130)], "finished episode", tone="gate", lx=384, ly=122)
    f.arrow(
        [(734, 130), (898, 130)], "frames + question", tone="system", lx=816, ly=122
    )
    f.arrow(
        [(898, 164), (734, 164)],
        "time segments · outcome",
        tone="model",
        lx=816,
        ly=184,
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
    )
    # GPU timeline
    f.text(32, 432, "ONE GPU OVER TIME", size=11.5, weight=700, fill=c["faint"])
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
    )
    f.arrow([(197, 130), (197, 104), (1003, 104), (1003, 130)], None, tone="model")
    f.text(
        600,
        98,
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
    f.rect(32, 340, 1136, 92, fill=c["danger_bg"], stroke=c["danger"], r=12, sw=1.4)
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
        640,
        t,
        "What has been measured, and what has not",
        "On the plates task, release-anchored review raised balanced accuracy from 0.81 to 0.94 and cut failures judged as success from "
        "38.5% to 12.8%; on the screws task it reached 0.76 with 49% of failures judged successes; time-segment labelling is at parity "
        "with a direct call; whether curated data improves a policy is not established.",
    )
    c = f.c
    f.heading(
        "Evidence, with its limits",
        "The project's own experiments on blind test sets, one task family unless stated. The experiment record is not shipped.",
    )

    def bars(x, y, title, rows):
        f.text(x, y, title, size=13.5, weight=700)
        for i, (label, v, shown, tone) in enumerate(rows):
            yy = y + 18 + i * 44
            f.text(x, yy + 15, label, size=12, fill=c["soft"])
            f.rect(x + 150, yy, 300, 22, fill=c["box"], stroke=c["boxedge"], r=6)
            f.rect(
                x + 150,
                yy,
                max(6, 300 * v),
                22,
                fill=c[tone + "_bg"],
                stroke=c[tone],
                r=6,
            )
            f.text(
                x + 150 + 300 + 10, yy + 16, shown, size=12.5, weight=700, fill=c[tone]
            )

    bars(
        32,
        112,
        "Balanced accuracy of judging success",
        [
            ("Plates: from segments", 0.81, "0.81", "gate"),
            ("Plates: anchored", 0.94, "0.94", "person"),
            ("Screws: anchored", 0.76, "0.76", "danger"),
        ],
    )
    bars(
        620,
        112,
        "Failures judged as success (lower is better)",
        [
            ("Plates: from segments", 0.385, "38.5%", "gate"),
            ("Plates: anchored", 0.128, "12.8%", "person"),
            ("Screws: anchored", 0.49, "49%", "danger"),
        ],
    )
    f.box(
        32,
        292,
        552,
        112,
        "Time-segment labelling: parity",
        [
            "LEVI and a direct call of the same local model score alike",
            "on blind gold labels (segment F1 about 0.34). Wall time is",
            "about 1.7x a direct call (development set, candidate",
            "configuration; 3.6x before the speed-up).",
        ],
        tone="model",
        size=14,
        subsize=13,
    )
    f.box(
        616,
        292,
        552,
        112,
        "The second task did not meet the bar",
        [
            "Screws: balanced accuracy 0.76, 49% of failures judged successes.",
            "The quality gate for scaling up is not met, so scale-up has not started.",
        ],
        tone="danger",
        size=13.5,
        subsize=13,
    )
    f.box(
        32,
        422,
        1136,
        84,
        "Not established",
        [
            "Whether training on LEVI-curated data improves a robot policy. Training manifests are delivered; training and robot evaluation are the user's team's next step.",
            "Automatic live verdicts are unreviewed and over-call success on tasks that place several objects.",
        ],
        tone="gate",
        size=14,
        subsize=12.5,
        dash=True,
    )
    f.note(
        518,
        "Plates: 60-episode blind test. Project's own numbers; docs/VALIDATION.md records the 0.94 / 12.8% re-run.",
        tone="system",
    )
    f.note(
        556,
        "Some screws outcomes were seen by the coordinator through annotator replies; they were not used for tuning.",
        tone="system",
    )
    f.note(
        594,
        "Automated tests use fixtures and stub models; no figure here is a real-robot policy result.",
        tone="gate",
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
