"""``levi.live.analysis.summarize`` / ``build``: every number checked against
a hand count of a small constructed cell (12 episodes), warnings for thin
data, the missing-episode counts and the text renderings."""

import json

import pytest
from analysis_helpers import (
    agent,
    operator,
    put_state,
    record,
    row,
    segments_block,
    workspace,
)

from levi.live import analysis

FULL = [
    ("approach", 0, 2),
    ("grasp", 2, 4),
    ("transport", 4, 7),
    ("place", 7, 8),
    ("retreat", 8, 10),
]


def full_episode(n, approach_s):
    triples = [
        ("approach", 0, approach_s, "success"),
        *[(*t, "success") for t in FULL[1:]],
    ]
    return record(
        demo=f"demo_{n:04d}",
        episode_index=n,
        labelled_at=100.0 + n,
        operator=operator("success"),
        agent=agent("success"),
        segments=segments_block(*triples),
    )


def cell_records():
    """12 episodes of one checkpoint on one task, built to be counted by hand
    (see the comments)."""
    rows = [full_episode(n, d) for n, d in enumerate([1, 2, 3, 4, 5, 6])]  # 0-5
    rows += [
        # 6: operator failure, stopped in the grasp
        record(
            demo="demo_0006",
            operator=operator("failure"),
            agent=agent("failure"),
            segments=segments_block(
                ("approach", 0, 2, "success"), ("grasp", 2, 4, "failure")
            ),
        ),
        # 7: operator failure, regrasp, then failed in transport; the
        # background wrongly says success
        record(
            demo="demo_0007",
            operator=operator("failure"),
            agent=agent("success"),
            segments=segments_block(
                ("approach", 0, 2, "success"),
                ("grasp", 2, 4, "failure"),
                ("grasp", 4, 5, "success"),
                ("transport", 5, 8, "failure"),
            ),
        ),
        # 8: operator failure, only an approach; the background is undecided
        record(
            demo="demo_0008",
            operator=operator("failure"),
            agent=agent("failure", undecided=True),
            segments=segments_block(("approach", 0, 4, "success")),
        ),
        # 9: operator failure, no time segments
        record(
            demo="demo_0009",
            operator=operator("failure"),
            agent=agent("failure"),
            segments=segments_block(),
        ),
        # 10: no operator label; the background says failure; the segments
        # skip grasp and transport
        record(
            demo="demo_0010",
            agent=agent("failure"),
            segments=segments_block(
                ("approach", 0, 2, "success"), ("place", 2, 3, "success")
            ),
        ),
        # 11: operator success, nothing from the background
        record(demo="demo_0011", operator=operator("success")),
    ]
    return rows


@pytest.fixture(scope="module")
def cell():
    found = analysis.summarize(cell_records())
    assert len(found["cells"]) == 1
    return found["cells"][0]


def test_the_cell_is_named_and_counted(cell):
    assert (cell["group"], cell["task"], cell["episodes"]) == ("g", "t", 12)


def test_coverage(cell):
    cov = cell["coverage"]
    assert (cov["operator"]["n"], cov["operator"]["of"]) == (11, 12)  # all but 10
    assert cov["background"]["n"] == 11  # all but 11
    assert cov["segments"]["n"] == 10  # 0-8 and 10; 9 has none, 11 is None
    assert cov["segments"]["rate"] == round(10 / 12, 3)


def test_success_rate_is_the_operators_with_a_wilson_interval(cell):
    s = cell["success"]
    assert (s["n"], s["of"], s["rate"]) == (7, 11, 0.636)
    assert s["wilson95"] == [0.354, 0.848]  # hand-computed
    assert s["basis"] == "operator" and s["small_sample"] is False
    b = cell["background_success"]
    assert (b["n"], b["of"]) == (7, 10)  # the undecided one is not judged
    assert b["review"] == "auto"


def test_agreement_reuses_the_statistics_definition(cell):
    a = cell["agreement"]
    assert a["pairs"] == 11 and a["judged"] == 9 and a["agree"] == 8
    assert a["rate"] == round(8 / 9, 3)
    assert (a["false_success"]["n"], a["false_success"]["of"]) == (1, 4)
    assert (a["missed_success"]["n"], a["missed_success"]["of"]) == (0, 6)
    assert (a["undecided"], a["no_agent"]) == (1, 1)
    assert a["review"] == "auto"


def test_funnel_counts_episodes_that_reached_each_stage(cell):
    f = cell["funnel"]
    assert f["of"] == 10 and f["review"] == "auto"
    got = {s["id"]: (s["reached"]["n"], s["present"]) for s in f["stages"]}
    # grasp: 6 full + 6 + 7 present, plus 10 who reached a later stage
    assert got == {
        "approach": (10, 10),
        "grasp": (9, 8),
        "transport": (8, 7),
        "place": (7, 7),
        "retreat": (6, 6),
    }
    assert [s["id"] for s in f["stages"]] == list(analysis.STAGES)


def test_duration_medians_and_quartiles(cell):
    d = cell["durations"]["by_subtask"]
    # approach: 1 2 3 4 5 6 (eps 0-5), 2, 2, 4, 2 -> sorted 1 2 2 2 2 3 4 4 5 6
    assert d["approach"] == {"n": 10, "median_s": 2.5, "q1_s": 2.0, "q3_s": 4.0}
    # grasp: eight 2 s and the regrasp's 1 s
    assert d["grasp"]["n"] == 9 and d["grasp"]["median_s"] == 2.0
    assert d["retreat"] == {"n": 6, "median_s": 2.0, "q1_s": 2.0, "q3_s": 2.0}


def test_sequences_with_gaps_and_repeats(cell):
    q = cell["sequence"]
    assert q["with_segments"] == 10
    assert q["gaps"] == 1  # episode 10: a place without grasp and transport
    assert q["repeats"] == 1  # episode 7: grasp twice


def test_failure_landing_is_the_last_completed_subtask(cell):
    op = cell["landing"]["operator"]
    # failures 6, 7, 8, 9; episode 9 has no segments
    assert (op["of"], op["with_segments"]) == (4, 3)
    # 6: approach; 7: the regrasp that succeeded; 8: approach
    assert op["counts"] == {"approach": 2, "grasp": 1}
    bg = cell["landing"]["background"]
    assert (bg["of"], bg["with_segments"], bg["review"]) == (1, 1, "auto")
    assert bg["counts"] == {"place": 1}


def test_a_failure_with_nothing_completed_lands_on_none():
    rows = [
        record(
            demo="d1",
            operator=operator("failure"),
            segments=segments_block(("approach", 0, 2, "failure")),
        )
    ]
    (c,) = analysis.summarize(rows)["cells"]
    assert c["landing"]["operator"]["counts"] == {"none": 1}


def test_only_the_few_failures_warning_fires_for_the_cell(cell):
    assert [w["code"] for w in cell["warnings"]] == ["few_failures"]
    assert cell["warnings"][0]["n"] == 3


def test_thin_data_gives_warnings_not_conclusions():
    rows = [
        record(demo=f"d{n}", segments=segments_block(("approach", 0, 1)))
        for n in range(3)
    ]
    (c,) = analysis.summarize(rows)["cells"]
    codes = [w["code"] for w in c["warnings"]]
    assert "small_sample" in codes and "no_operator_labels" in codes
    assert c["success"]["rate"] is None and c["success"]["wilson95"] is None
    assert c["success"]["small_sample"] is True


def test_low_coverage_is_called_out():
    rows = [
        record(demo=f"d{n}", operator=operator("success") if n < 5 else None)
        for n in range(12)
    ]
    (c,) = analysis.summarize(rows)["cells"]
    codes = [w["code"] for w in c["warnings"]]
    assert "low_operator_coverage" in codes
    assert "low_background_coverage" in codes and "low_segment_coverage" in codes


def test_cells_are_per_checkpoint_and_task_and_sorted():
    rows = [
        record(demo="a", group="h", task="t"),
        record(demo="b", group="g", task="u"),
        record(demo="c", group="g", task="t"),
    ]
    found = analysis.summarize(rows)
    assert [(c["group"], c["task"]) for c in found["cells"]] == [
        ("g", "t"),
        ("g", "u"),
        ("h", "t"),
    ]
    assert found["all"]["episodes"] == 3


def test_a_relabelled_episode_counts_once_with_its_newest_record():
    rows = [
        record(demo="a", operator=operator("failure")),
        record(demo="a", operator=operator("success")),
    ]
    (c,) = analysis.summarize(rows)["cells"]
    assert c["episodes"] == 1 and c["success"]["n"] == 1


def test_another_vocabulary_is_ordered_by_first_appearance():
    rows = [
        record(demo="a", segments=segments_block(("open", 0, 1), ("pour", 1, 3))),
        record(
            demo="b",
            segments=segments_block(("open", 0, 2), ("pour", 2, 3), ("wipe", 3, 4)),
        ),
    ]
    (c,) = analysis.summarize(rows)["cells"]
    assert [s["id"] for s in c["funnel"]["stages"]] == ["open", "pour", "wipe"]
    assert c["funnel"]["stages"][2]["reached"]["n"] == 1
    assert c["sequence"]["gaps"] == 0
    stages = ("open", "pour", "wipe")
    (c2,) = analysis.summarize(rows, stages=stages)["cells"]
    assert [s["id"] for s in c2["funnel"]["stages"]] == list(stages)
    # with its own stages, "wipe" reached implies "pour" reached
    assert c2["funnel"]["stages"][1]["reached"]["n"] == 2


def test_the_summary_json_is_plain_and_deterministic():
    one = analysis.to_json(analysis.summarize(cell_records()))
    two = analysis.to_json(analysis.summarize(list(cell_records())))
    assert one == two
    assert json.loads(one)["schema"] == analysis.SUMMARY_SCHEMA
    assert "NaN" not in one and "Infinity" not in one


def test_every_automatic_figure_is_marked_in_the_text():
    found = analysis.summarize(cell_records())
    for lang, tag in (("en", "automatic, unreviewed"), ("zh", "自动、未复核")):
        text = analysis.to_markdown(found, lang)
        assert tag in text
        assert "7/11" in text and "[35.4%, 84.8%]" in text
        for line in text.splitlines():
            if line.startswith(
                (
                    "**Subtask funnel",
                    "**子任务漏斗",
                    "**Subtask duration",
                    "**子任务时长",
                )
            ):
                assert tag in line
            if (
                "Background verdict against" in line
                or "后台判定与操作员标签对照" in line
            ):
                assert tag in line
        assert "nan" not in text.lower()


def test_an_empty_scope_says_so():
    found = analysis.summarize([])
    assert "No analysis records" in analysis.to_markdown(found, "en")
    assert "没有分析记录" in analysis.to_markdown(found, "zh")


# --- build: the live workspace ---------------------------------------------------


def _write_records(c, rows):
    for r in rows:
        analysis.append(c.live_dir, r)


def test_build_counts_what_the_records_cannot_show(tmp_path):
    c = workspace(tmp_path)
    demos = {
        "demo_0000": row(0),  # done, has a record
        "demo_0001": row(1),  # done, record pending
        "demo_0002": row(2, state="failed"),
        "demo_0003": row(3, state="mirrored"),
        "demo_0004": row(4, state="annotating"),
        "demo_0005": row(5, state="stuck"),
        "demo_0006": row(6, excluded={"at": 1, "by": "p"}),
        "demo_0007": row(7, deleted=True),
    }
    put_state(
        c, "g__t", "g", "t", demos, incomplete={"count": 2, "fr3_fault": 1}, discarded=3
    )
    _write_records(c, [record(demo="demo_0000", operator=operator("success"))])
    found = analysis.build(c)
    (cell_,) = found["cells"]
    assert cell_["missing"] == {
        "incomplete": 2,
        "fr3_fault": 1,
        "discarded": 3,
        "failed": 1,
        "excluded": 1,
        "unlabelled": 3,
        "done_without_record": 1,
    }
    assert "records_pending" in [w["code"] for w in cell_["warnings"]]
    assert "levi live analysis --sync" in analysis.to_markdown(found, "en")


def test_build_leaves_out_removed_episodes_and_filters_the_scope(tmp_path):
    c = workspace(tmp_path)
    put_state(
        c,
        "g__t",
        "g",
        "t",
        {"demo_0000": row(0), "demo_0001": row(1, excluded={"at": 1, "by": "p"})},
    )
    put_state(c, "h__t", "h", "t", {"demo_0000": row(0)})
    _write_records(
        c,
        [
            record(demo="demo_0000", labelled_at=50.0),
            record(demo="demo_0001", labelled_at=60.0),  # removed by a person
            record(dataset="h__t", group="h", demo="demo_0000", labelled_at=70.0),
        ],
    )
    both = analysis.build(c)
    assert {(x["group"], x["episodes"]) for x in both["cells"]} == {("g", 1), ("h", 1)}
    only = analysis.build(c, group="h")
    assert [x["group"] for x in only["cells"]] == ["h"]
    assert analysis.build(c, task="nope")["cells"] == []
    late = analysis.build(c, since=65)
    assert {x["group"] for x in late["cells"] if x["episodes"]} == {"h"}
    iso = analysis.build(c, since="1970-01-01T00:00:55")
    assert iso["scope"]["since"] is not None
    with pytest.raises(ValueError):
        analysis.build(c, since="yesterday")


def test_build_never_writes(tmp_path):
    c = workspace(tmp_path)
    put_state(c, "g__t", "g", "t", {"demo_0000": row(0)})
    analysis.build(c)
    assert not analysis.path(c.live_dir).exists()
