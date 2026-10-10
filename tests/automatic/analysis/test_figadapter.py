"""The interface between the analysis library (T-CP-01) and FigureSpec, shown
as a minimal adapter. The input dicts are the *assumed* shape of the library's
results (estimate/low/high, None when unavailable), inferred from its task
brief and the design note section 2; nothing from the analysis package is
imported. The real adapter belongs to the report generator (T-CP-06) and must
be checked against the library's actual output."""

import pytest

from levi.automatic.analysis import figspec as fs
from levi.automatic.analysis import pdfplot, svgplot

P = fs.Point


def success_bars(groups, task="All tasks"):
    """grouped_bar: one series per group, one category. A group with no data
    (``estimate`` is None) becomes an unavailable series, never a missing one."""
    series = []
    for g in groups:
        if g["estimate"] is None:
            series.append(fs.Series(g["name"], (), unavailable=True))
            continue
        label = f"{g['k']}/{g['n']}"
        series.append(
            fs.Series(g["name"], (P(0, g["estimate"], g["low"], g["high"], label),))
        )
    return fs.FigureSpec(
        id="success",
        kind="grouped_bar",
        title="Success rate by arm",
        summary="Wilson intervals.",
        panels=(
            fs.Panel(
                fs.Axis(kind="category", categories=(task,)),
                fs.Axis(label="Success rate", min=0, max=1, fmt="percent"),
                tuple(series),
            ),
        ),
    )


def forest(rows):
    """forest: one row per comparison. The bootstrap interval may exclude the
    point estimate; the figure keeps it and flags it (``fs.warnings``)."""
    return fs.FigureSpec(
        id="pairs",
        kind="forest",
        title="Paired differences",
        summary="B minus A.",
        panels=(
            fs.Panel(
                fs.Axis(label="Difference"),
                fs.Axis(kind="category", categories=tuple(r["name"] for r in rows)),
                (
                    fs.Series(
                        "Bootstrap",
                        tuple(
                            P(r["estimate"], i, r["low"], r["high"])
                            for i, r in enumerate(rows)
                        ),
                    ),
                ),
                reflines=(fs.RefLine("x", 0, "no difference"),),
            ),
        ),
    )


def km_curves(arms):
    """step_curve of 1 - S(t). The library's interval is on S(t), so it flips:
    [1 - high, 1 - low]. At t = 0 (before the first event) the library has no
    interval; FigureSpec treats a missing interval at x = 0 as no uncertainty."""
    series = []
    for a in arms:
        pts = []
        for r in a["points"]:
            est = 1.0 - r["estimate"]
            if r["low"] is None:
                pts.append(P(r["t"], est))
            else:
                pts.append(P(r["t"], est, 1.0 - r["high"], 1.0 - r["low"]))
        series.append(fs.Series(a["name"], tuple(pts)))
    return fs.FigureSpec(
        id="tts",
        kind="step_curve",
        title="Time to success",
        summary="",
        panels=(
            fs.Panel(
                fs.Axis(label="Step"),
                fs.Axis(label="Cumulative success", min=0, max=1, fmt="percent"),
                tuple(series),
            ),
        ),
    )


GROUPS = [
    {"name": "A", "k": 8, "n": 20, "estimate": 0.4, "low": 0.22, "high": 0.61},
    {"name": "B", "k": 14, "n": 20, "estimate": 0.7, "low": 0.48, "high": 0.85},
    {"name": "C", "k": 0, "n": 0, "estimate": None, "low": None, "high": None},
]


def test_success_bars_keep_an_unavailable_group_visible():
    spec = success_bars(GROUPS)
    fs.validate(spec)
    assert fs.warnings(spec) == ["series_unavailable:panel0/series2"]
    scene = fs.layout(spec)
    legend = [
        i.s for i in scene.items if isinstance(i, fs.Label) and i.role == "legend"
    ]
    assert legend == ["A", "B", "C (unavailable)"]
    pdfplot.verify_pdf(pdfplot.render_pdf(spec))
    svgplot.render_svg(spec)


def test_a_bootstrap_interval_that_excludes_the_estimate_is_kept_and_flagged():
    rows = [
        {"name": "B vs A", "estimate": 0.30, "low": 0.05, "high": 0.52},
        {
            "name": "C vs A",
            "estimate": 0.55,
            "low": 0.08,
            "high": 0.50,
        },  # median-type interval
    ]
    spec = forest(rows)
    assert fs.warnings(spec) == ["estimate_outside_interval:panel0/series0/point1"]
    fs.layout(spec)


def test_kaplan_meier_flips_to_cumulative_success_and_t0_has_no_uncertainty():
    arms = [
        {
            "name": "A",
            "points": [
                {"t": 0, "estimate": 1.0, "low": None, "high": None},
                {"t": 60, "estimate": 0.9, "low": 0.7, "high": 0.97},
                {"t": 120, "estimate": 0.6, "low": 0.35, "high": 0.8},
            ],
        }
    ]
    spec = km_curves(arms)
    pts = spec.panels[0].series[0].points
    assert (pts[0].y, pts[0].lo) == (0.0, None)
    assert (pts[1].y, pts[1].lo, pts[1].hi) == (
        pytest.approx(0.1),
        pytest.approx(0.03),
        pytest.approx(0.3),
    )
    assert fs.warnings(spec) == []  # t = 0 is not "unavailable"
    scene = fs.layout(spec)
    assert [i for i in scene.items if i.role == "band"]
