"""Not a test module: small hand-made FigureSpecs shared by the figure tests (one per kind), plus
a tiny one for the golden snapshots. Numbers are made up; they only exercise
the drawing."""

from levi.automatic.analysis.figspec import (
    Axis,
    FigureSpec,
    Panel,
    Point,
    RefLine,
    Series,
)


def cats(*names):
    return tuple(names)


def _band(rounds):
    return [Point(r, v, max(0.0, v - 0.2), min(1.0, v + 0.2)) for r, v in rounds]


def grouped_bar():
    return FigureSpec(
        id="f1-success",
        kind="grouped_bar",
        title={
            "en": "Success rate by arm (automatic verdict)",
            "zh-CN": "各组成功率（自动判定口径）",
        },
        summary={
            "en": "Arm B succeeds more often than A in this sample; intervals are 95% Wilson.",
            "zh-CN": "本样本中 B 组成功率高于 A 组；区间为 95% Wilson 区间。",
        },
        panels=(
            Panel(
                x_axis=Axis(
                    label={"en": "Task", "zh-CN": "任务"},
                    kind="category",
                    categories=cats(
                        {"en": "Pick cube", "zh-CN": "拿方块"},
                        {"en": "Stack", "zh-CN": "叠放"},
                        {"en": "Insert peg", "zh-CN": "插销"},
                    ),
                ),
                y_axis=Axis(
                    label={"en": "Success rate", "zh-CN": "成功率"},
                    min=0,
                    max=1,
                    fmt="percent",
                ),
                series=(
                    Series(
                        "Arm A",
                        (
                            Point(0, 0.4, 0.22, 0.61, "8/20"),
                            Point(1, 0.25, 0.11, 0.47, "5/20"),
                            Point(2, 0.1, 0.03, 0.30, "2/20"),
                        ),
                    ),
                    Series(
                        "Arm B",
                        (
                            Point(0, 0.7, 0.48, 0.85, "14/20"),
                            Point(1, 0.55, 0.34, 0.74, "11/20"),
                            Point(2, 0.35, 0.18, 0.57, "7/20"),
                        ),
                    ),
                    Series(
                        "Arm C",
                        (
                            Point(0, 0.5, 0.30, 0.70, "10/20"),
                            Point(1, 0.3, 0.14, 0.52, "6/20"),
                            Point(2, 0.0, 0.0, 0.16, "0/20"),
                        ),
                    ),
                ),
            ),
        ),
        notes=(
            {
                "en": "n = 20 per arm and task; exploratory.",
                "zh-CN": "每组每任务 n=20；探索性。",
            },
        ),
    )


def forest():
    return FigureSpec(
        id="f2-forest",
        kind="forest",
        title="Paired differences in success rate",
        summary="B minus A and C minus A on the same layout cards; 0 means no difference.",
        panels=(
            Panel(
                x_axis=Axis(label="Difference in success rate"),
                y_axis=Axis(
                    kind="category",
                    categories=cats("B vs A", "C vs A", "B vs C"),
                ),
                series=(
                    Series(
                        "Newcombe",
                        (
                            Point(0.30, 0, 0.05, 0.52, "p=0.03"),
                            Point(0.10, 1, -0.15, 0.33, "p=0.55"),
                            Point(0.20, 2, -0.04, 0.42, "p=0.21"),
                        ),
                    ),
                    Series(
                        "Bootstrap",
                        (
                            Point(0.30, 0, 0.08, 0.50),
                            Point(0.10, 1, -0.12, 0.31),
                            Point(0.20, 2, -0.02, 0.40),
                        ),
                    ),
                ),
                reflines=(RefLine("x", 0, "no difference"),),
            ),
        ),
    )


def step_curve():
    return FigureSpec(
        id="f3-tts",
        kind="step_curve",
        title="Time to success",
        summary="Share of episodes that succeeded by step; the budget is 300 steps.",
        panels=(
            Panel(
                x_axis=Axis(label="Policy step"),
                y_axis=Axis(label="Cumulative success", min=0, max=1, fmt="percent"),
                series=(
                    Series(
                        "Arm A",
                        (
                            Point(0, 0),
                            Point(60, 0.05),
                            Point(110, 0.2),
                            Point(170, 0.35),
                            Point(260, 0.4),
                        ),
                        emphasis=True,
                    ),
                    Series(
                        "Arm B",
                        (
                            Point(0, 0),
                            Point(50, 0.15),
                            Point(90, 0.45),
                            Point(150, 0.6),
                            Point(240, 0.7),
                        ),
                    ),
                    Series(
                        "Arm C",
                        (
                            Point(0, 0, 0, 0.1),
                            Point(80, 0.1, 0.02, 0.3),
                            Point(130, 0.3, 0.1, 0.5),
                            Point(200, 0.5, 0.25, 0.75),
                        ),
                    ),
                ),
                reflines=(RefLine("x", 300, "step cap"),),
            ),
        ),
    )


def stacked_bar():
    return FigureSpec(
        id="f4-fail",
        kind="stacked_bar",
        title="Failure modes",
        summary="Count of failed episodes by category.",
        panels=(
            Panel(
                x_axis=Axis(
                    label="Arm",
                    kind="category",
                    categories=cats("Arm A", "Arm B", "Arm C"),
                ),
                y_axis=Axis(label="Failed episodes"),
                series=(
                    Series("Missed grasp", (Point(0, 6), Point(1, 2), Point(2, 4))),
                    Series("Dropped object", (Point(0, 4), Point(1, 3), Point(2, 1))),
                    Series("Timeout", (Point(0, 2), Point(1, 1), Point(2, 5))),
                    Series("Other", (Point(0, 0), Point(1, 0), Point(2, 1))),
                ),
            ),
        ),
    )


def early_stop():
    arms = cats("A", "B", "C")
    return FigureSpec(
        id="f5-early",
        kind="early_stop",
        title="Early termination",
        summary="Steps saved and wrongly terminated episodes per arm.",
        panels=(
            Panel(
                title="Steps saved",
                x_axis=Axis(label="Arm", kind="category", categories=arms),
                y_axis=Axis(label="Steps saved per episode"),
                series=(
                    Series(
                        "Saved",
                        (
                            Point(0, 40, 30, 52),
                            Point(1, 65, 50, 81),
                            Point(2, 22, 10, 35),
                        ),
                    ),
                ),
            ),
            Panel(
                title="Error rate",
                x_axis=Axis(label="Arm", kind="category", categories=arms),
                y_axis=Axis(label="Wrongly terminated", min=0, max=0.3, fmt="percent"),
                series=(
                    Series(
                        "False stop",
                        (
                            Point(0, 0.05, 0.01, 0.15),
                            Point(1, 0.08, 0.02, 0.2),
                            Point(2, 0.0, 0.0, 0.1),
                        ),
                    ),
                ),
            ),
        ),
    )


def drift_lines():
    return FigureSpec(
        id="f6-drift",
        kind="drift_lines",
        title="Success by round",
        summary="Per-round rate for each arm; A is the reference.",
        panels=(
            Panel(
                x_axis=Axis(label="Round"),
                y_axis=Axis(label="Success rate", min=0, max=1, fmt="percent"),
                series=(
                    Series(
                        "Arm A",
                        tuple(_band([(1, 0.4), (2, 0.35), (3, 0.45), (4, 0.3)])),
                        emphasis=True,
                    ),
                    Series(
                        "Arm B",
                        tuple(_band([(1, 0.6), (2, 0.7), (3, 0.65), (4, 0.75)])),
                    ),
                ),
            ),
        ),
    )


def all_specs():
    return [
        grouped_bar(),
        forest(),
        step_curve(),
        stacked_bar(),
        early_stop(),
        drift_lines(),
    ]


def tiny_bars():
    """The golden snapshot: two categories, two arms, bilingual text."""
    return FigureSpec(
        id="golden-bars",
        kind="grouped_bar",
        title={"en": "Success rate by arm", "zh-CN": "各组成功率"},
        summary={"en": "B is higher than A.", "zh-CN": "B 组高于 A 组。"},
        panels=(
            Panel(
                x_axis=Axis(
                    label={"en": "Task", "zh-CN": "任务"},
                    kind="category",
                    categories=(
                        {"en": "Pick", "zh-CN": "拿取"},
                        {"en": "Stack", "zh-CN": "叠放"},
                    ),
                ),
                y_axis=Axis(
                    label={"en": "Rate", "zh-CN": "比例"}, min=0, max=1, fmt="percent"
                ),
                series=(
                    Series(
                        "A",
                        (
                            Point(0, 0.4, 0.2, 0.6, "8/20"),
                            Point(1, 0.2, 0.1, 0.4, "4/20"),
                        ),
                    ),
                    Series(
                        "B",
                        (
                            Point(0, 0.7, 0.5, 0.85, "14/20"),
                            Point(1, 0.5, 0.3, 0.7, "10/20"),
                        ),
                    ),
                ),
            ),
        ),
    )


def tiny_forest():
    return FigureSpec(
        id="golden-forest",
        kind="forest",
        title={"en": "Difference B minus A", "zh-CN": "差值 B 减 A"},
        summary={"en": "The interval spans zero.", "zh-CN": "区间跨过零。"},
        panels=(
            Panel(
                x_axis=Axis(label={"en": "Difference", "zh-CN": "差值"}),
                y_axis=Axis(
                    kind="category", categories=({"en": "Overall", "zh-CN": "总体"},)
                ),
                series=(Series("Newcombe", (Point(0.2, 0, -0.05, 0.4, "p=0.2"),)),),
                reflines=(RefLine("x", 0, {"en": "none", "zh-CN": "无差"}),),
            ),
        ),
    )
