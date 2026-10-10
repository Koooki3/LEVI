"""The interface between the analysis library and FigureSpec, shown as a
minimal adapter that consumes the library's real results (no hand-written
dicts): ``proportion`` (``rate``, ``wilson.low/high``, ``available``),
``paired_bootstrap`` (``estimate/low/high``), ``newcombe_paired``
(``difference/low/high``) and ``kaplan_meier`` (``steps[]`` with ``time``,
``survival``, ``incidence``, ``low``, ``high`` on S(t), and no row at t = 0).
The production adapter belongs to the report generator (T-CP-06); these
functions are the documented example in docs/AUTOMATIC_CAMPAIGN.md
("Interface with the analysis library")."""

import pytest

from levi.automatic import analysis as an
from levi.automatic.analysis import figspec as fs
from levi.automatic.analysis import pdfplot, svgplot

P = fs.Point


# ------------------------------------------------------------------ the adapter


def success_bars(arms, task="All tasks"):
    """grouped_bar from ``proportion`` results, one series per arm. An arm
    with no trials (``available`` false, ``rate`` None) becomes an
    unavailable series, never a missing one."""
    series = []
    for name, r in arms:
        if not r["available"]:
            series.append(fs.Series(name, (), unavailable=True))
            continue
        w = r["wilson"]
        label = f"{r['k']}/{r['n']}"
        series.append(fs.Series(name, (P(0, r["rate"], w["low"], w["high"], label),)))
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


def paired_value(r):
    """The point estimate of a paired comparison: ``difference`` for
    ``newcombe_paired``, ``estimate`` for the bootstraps."""
    return r["difference"] if "difference" in r else r["estimate"]


def forest(rows):
    """forest from paired results, one row per comparison. A comparison that
    is not available keeps its row and has no point; say so in a note. The
    bootstrap interval may exclude the estimate: keep it as it is."""
    points, missing = [], []
    for i, (name, r) in enumerate(rows):
        if not r["available"]:
            missing.append(name)
            continue
        points.append(P(paired_value(r), i, r["low"], r["high"]))
    return fs.FigureSpec(
        id="pairs",
        kind="forest",
        title="Paired differences",
        summary="B minus A.",
        panels=(
            fs.Panel(
                fs.Axis(label="Difference"),
                fs.Axis(kind="category", categories=tuple(n for n, _ in rows)),
                (fs.Series("Paired", tuple(points)),),
                reflines=(fs.RefLine("x", 0, "no difference"),),
            ),
        ),
        notes=tuple(f"{n}: not available" for n in missing),
    )


def km_curves(arms):
    """step_curve of cumulative success 1 - S(t) from ``kaplan_meier``. The
    library's ``low``/``high`` are on S(t), so the interval flips to
    ``[1 - high, 1 - low]``; ``incidence`` is already 1 - S. The library has
    no row at t = 0: the adapter adds (0, 0) without an interval, which
    FigureSpec reads as "no uncertainty yet". An arm with no episodes
    becomes an unavailable series."""
    series = []
    for name, r in arms:
        if not r["available"]:
            series.append(fs.Series(name, (), unavailable=True))
            continue
        pts = [P(0, 0.0)]
        for s in r["steps"]:
            if s["low"] is None:
                pts.append(P(s["time"], s["incidence"]))
            else:
                pts.append(P(s["time"], s["incidence"], 1 - s["high"], 1 - s["low"]))
        series.append(fs.Series(name, tuple(pts)))
    return fs.FigureSpec(
        id="tts",
        kind="step_curve",
        title="Time to success",
        summary="Kaplan-Meier, log(-log) intervals.",
        panels=(
            fs.Panel(
                fs.Axis(label="Step"),
                fs.Axis(label="Cumulative success", min=0, max=1, fmt="percent"),
                tuple(series),
            ),
        ),
    )


# ------------------------------------------------------------------ the tests


def test_success_bars_from_proportion_keep_an_empty_arm_visible():
    arms = [
        ("A", an.proportion(8, 20)),
        ("B", an.proportion(14, 20)),
        ("C", an.proportion(0, 0)),
    ]
    assert arms[2][1]["rate"] is None and arms[2][1]["wilson"] is None
    spec = success_bars(arms)
    assert fs.warnings(spec) == ["series_unavailable:panel0/series2"]
    a = spec.panels[0].series[0].points[0]
    assert (a.y, a.lo, a.hi, a.label) == (0.4, 0.219, 0.613, "8/20")
    scene = fs.layout(spec)
    legend = [
        i.s for i in scene.items if isinstance(i, fs.Label) and i.role == "legend"
    ]
    assert legend == ["A", "B", "C (unavailable)"]
    pdfplot.verify_pdf(pdfplot.render_pdf(spec))
    svgplot.render_svg(spec)


def test_forest_from_bootstrap_and_newcombe_results():
    pairs = [(1, 1), (0, 1), (1, 1), (1, 1), (0, 0), (1, 1), (0, 1), (1, 0)] * 3
    boot = an.paired_bootstrap(pairs, seed=7, resamples=2000)
    newc = an.newcombe_paired(pairs)
    empty = an.paired_bootstrap([], seed=7, resamples=2000)
    assert "estimate" in boot and "difference" in newc and not empty["available"]
    spec = forest(
        [("B vs A (bootstrap)", boot), ("B vs A (Newcombe)", newc), ("C vs A", empty)]
    )
    pts = spec.panels[0].series[0].points
    assert [(p.x, p.lo, p.hi) for p in pts] == [
        (boot["estimate"], boot["low"], boot["high"]),
        (newc["difference"], newc["low"], newc["high"]),
    ]
    fs.layout(spec)
    _, rows = fs.table(spec)
    assert [r[2] for r in rows if r[1] != "reference"] == [
        "B vs A (bootstrap)",
        "B vs A (Newcombe)",
    ]
    assert "C vs A: not available" in [
        i.s for i in fs.layout(spec).items if i.role == "note"
    ]


def test_a_median_bootstrap_interval_that_excludes_the_estimate_is_kept():
    # a skewed sample where the median's percentile interval need not hold it
    r = an.paired_bootstrap(
        [(0.0, d) for d in (0, 0, 0, 0, 5, 5, 5, 9, 9, 9, 9)],
        seed=3,
        resamples=2000,
        statistic="median",
        binary=False,
    )
    spec = forest([("B vs A", r)])
    fs.layout(spec)  # never refused: outside is a warning, not an error
    inside = r["low"] <= r["estimate"] <= r["high"]
    assert fs.warnings(spec) == (
        [] if inside else ["estimate_outside_interval:panel0/series0/point0"]
    )


def test_kaplan_meier_flips_to_cumulative_success_with_t0_added():
    km = an.kaplan_meier([10, 20, 20, 30, 40, 50], [1, 1, 0, 1, 1, 0])
    assert km["steps"][0]["time"] == 10.0  # the library has no t = 0 row
    first = km["steps"][0]
    spec = km_curves([("A", km), ("B", an.kaplan_meier([], []))])
    pts = spec.panels[0].series[0].points
    assert (pts[0].x, pts[0].y, pts[0].lo) == (0, 0.0, None)
    assert (pts[1].x, pts[1].y, pts[1].lo, pts[1].hi) == (
        10.0,
        pytest.approx(1 - first["survival"]),
        pytest.approx(1 - first["high"]),
        pytest.approx(1 - first["low"]),
    )
    assert fs.warnings(spec) == ["series_unavailable:panel0/series1"]
    scene = fs.layout(spec)  # validate_render: every interval visible
    assert [i for i in scene.items if i.role == "band"]
    _, rows = fs.table(spec)
    assert rows[0][6] == "no uncertainty at 0"


def test_kaplan_meier_reaching_zero_survival_gives_a_single_value_interval():
    km = an.kaplan_meier([5, 10, 15], [1, 1, 1])
    last = km["steps"][-1]
    assert (last["survival"], last["low"], last["high"]) == (0.0, 0.0, 0.0)
    spec = km_curves([("A", km)])
    end = spec.panels[0].series[0].points[-1]
    assert (end.y, end.lo, end.hi) == (1.0, 1.0, 1.0)
    fs.layout(spec)
