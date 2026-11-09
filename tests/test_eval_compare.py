"""The three-arm teacher / harness / direct table (levi/eval/compare.py).

The records are built by the tests; nothing here reads a dataset or calls a model.
"""

import json
import math
from pathlib import Path

import pytest

from levi.automatic import external_report
from levi.eval import compare, tokens
from levi.eval.compare import ArmRecord, CompareError, RecordError

THREE = ("teacher", "prior_levi", "direct")  # the v1 round, renamed
EPISODES = list(range(12))
ZERO = dict.fromkeys(compare.TOKEN_FIELDS, 0)
# The direction and the margin delta are declared before the round.
SCORING = {
    "metric": "segment_f1",
    "reference": "gold",
    "higher_is_better": True,
    "non_inferiority_margin": 0.05,
}


def arm(name="teacher", **over):
    """A complete record of one arm; ``over`` replaces fields."""
    row = {
        "teacher": {
            "model": "claude-opus",
            "quality": 0.80,
            "wall_s": 600.0,
            "wall_s_basis": "excludes_queue",
            "local_tokens": 0,
            "external_tokens": {
                "input_new": 1000,
                "output": 500,
                "cache_read": 20000,
                "cache_write": 3000,
            },
        },
        "levi2": {
            "model": "qwen3-vl",
            "quality": 0.85,
            "quality_delta_ci_vs_teacher": (0.0, 0.10),
            "quality_delta_ci_method": "paired_bootstrap_95",
            "agreement_with_teacher": 0.92,
            "wall_s": 220.0,
            "wall_s_basis": "excludes_queue",
            "local_tokens": 120000,
            "external_tokens": {**ZERO, "output": 100},
        },
        "prior_levi": {
            "model": "qwen3-vl",
            "quality": 0.82,
            # Paired difference to the teacher, +0.02 as a point estimate.
            "quality_delta_ci_vs_teacher": (-0.03, 0.07),
            "quality_delta_ci_method": "paired_bootstrap_95",
            "agreement_with_teacher": 0.9,
            "wall_s": 200.0,
            "wall_s_basis": "excludes_queue",
            "local_tokens": 90000,
            "external_tokens": dict(ZERO),
        },
        "direct": {
            "model": "qwen3-vl",
            "quality": 0.60,
            "quality_delta_ci_vs_teacher": (-0.30, -0.10),
            "agreement_with_teacher": 0.7,
            "wall_s": 150.0,
            "wall_s_basis": "excludes_queue",
            "local_tokens": 70000,
            "external_tokens": dict(ZERO),
        },
    }[name]
    row.update(
        arm=name,
        task="plates-subtask",
        decoding={"temperature": 0, "seed": 1},
        scoring=SCORING,
        split="tune",
        episode_set=EPISODES,
    )
    row.update(over)
    if "scored_count" not in over:  # by default every problem was scored
        ids = row.get("episode_set")
        row["scored_count"] = len(set(ids)) if ids else row.get("episode_count")
    return ArmRecord(**row)


def three(**over_by_arm):
    return [arm(n, **over_by_arm.get(n, {})) for n in THREE]


def every_arm(**over):
    """The same override for all three arms (a round's shared conditions)."""
    return {n: dict(over) for n in THREE}


def blind(**over_by_arm):
    """Three arms of a confirm round: split=confirm, problems named by hash and
    count only."""
    digest = compare.episode_set_hash(EPISODES)
    return [
        arm(
            n,
            split="confirm",
            episode_set=None,
            episode_set_hash=digest,
            episode_count=len(EPISODES),
            **over_by_arm.get(n, {}),
        )
        for n in THREE
    ]


def rows(table):
    return {r["arm"]: r for r in table.rows}


def test_the_names_are_the_ones_of_the_external_report():
    assert compare.TOKEN_FIELDS == external_report.TOKEN_FIELDS


def test_three_arms_make_one_table_with_the_derived_columns():
    table = compare.compare(reversed(three()))  # the input order does not matter
    assert [r["arm"] for r in table.rows] == list(THREE)
    teacher, harness, direct = (rows(table)[a] for a in THREE)

    # Two totals side by side: without cache_read, and all four fields.
    assert teacher["external_tokens_total_fresh"] == 1000 + 500 + 3000
    assert teacher["external_tokens_total_all"] == 1000 + 500 + 20000 + 3000
    assert teacher["quality_per_second"] == pytest.approx(0.80 / 600)
    assert teacher["quality_per_external_token_fresh"] == pytest.approx(0.80 / 4500)
    assert teacher["quality_per_external_token_all"] == pytest.approx(0.80 / 24500)
    assert harness["quality_per_second"] == pytest.approx(0.82 / 200)
    # Zero external tokens: the ratio has no denominator, and it is not "unknown".
    for basis in ("fresh", "all"):
        assert harness[f"quality_per_external_token_{basis}"] is None
        assert f"quality_per_external_token_{basis}" in harness["undefined"]
    assert harness["unknown"] == []

    assert harness["vs_teacher"] == {
        "quality_delta": pytest.approx(0.02),
        "wall_s_delta": -400.0,
        "wall_s_problem": None,
        "external_tokens_delta_fresh": -4500,
        "external_tokens_delta_all": -24500,
        "external_tokens_delta_is_bound": False,
        "quality_delta_ci": [-0.03, 0.07],
    }
    assert harness["vs_direct"]["quality_delta"] == pytest.approx(0.22)
    assert harness["vs_direct"]["wall_s_delta"] == 50.0
    assert teacher["vs_teacher"] == {"self": True}
    assert direct["vs_direct"] == {"self": True}
    assert table.main_metric == {
        "quality_non_inferior": True,
        "quality_status": "yes",
        "quality_point_not_worse": True,
        "faster": True,
        "faster_problem": None,
        "complete": True,
        "fewer_external_tokens_fresh": True,
        "fewer_external_tokens_all": True,
        "fewer_external_tokens": True,
        "fewer_external_tokens_basis_dependent": False,
        "fewer_external_tokens_basis": "both",
        "all": True,
        "non_inferiority": {
            "higher_is_better": True,
            "margin": 0.05,
            "delta_ci": [-0.03, 0.07],
            "method": "paired_bootstrap_95",
        },
    }
    assert table.conditions["episode_count"] == 12
    assert table.missing_arms == ["levi2"]  # a v1-shaped round has no LEVI 2.0


def test_the_markdown_has_one_row_per_arm_with_every_column():
    text = compare.compare(three()).to_markdown()
    for head in (
        "质量（对金标准）",
        "与教师一致率",
        "墙钟 (s)",
        "本地 token",
        "外部 input_new",
        "外部 output",
        "外部 cache_read",
        "外部 cache_write",
        "每秒质量",
        "每千外部 token 质量",
        "对教师 Δ质量",
        "对直接调用 Δ质量",
    ):
        assert head in text
    for name in THREE:
        assert text.count(f"| {name} |") == 3  # once in each of the three tables
    assert "| teacher | claude-opus | 0.800 |" in text
    assert "0.178" in text  # 0.80 / 4500 * 1000
    assert "+0.020" in text and "-400.0" in text and "-4,500" in text
    assert "三项全满足：是" in text
    assert "外部 token 来源" in text


def test_the_json_is_plain_and_complete():
    table = compare.compare(three())
    data = json.loads(table.to_json())
    assert data["schema"] == compare.TABLE_SCHEMA
    assert data["conditions"]["external_token_bases"] == {
        "fresh": list(compare.FRESH_BASIS),
        "all": list(compare.TOKEN_FIELDS),
    }
    assert data["conditions"]["verdict_basis"] == "both"
    assert [r["arm"] for r in data["rows"]] == list(THREE)
    for key in (
        "quality",
        "agreement_with_teacher",
        "wall_s",
        "local_tokens",
        "external_tokens",
        "quality_per_second",
        "external_tokens_total_fresh",
        "external_tokens_total_all",
        "quality_per_external_token_fresh",
        "quality_per_external_token_all",
        "vs_teacher",
        "vs_direct",
    ):
        assert key in data["rows"][0]


def test_a_missing_teacher_token_count_is_unknown_never_zero():
    table = compare.compare(three(teacher={"external_tokens": None}))
    teacher, harness, _ = (rows(table)[a] for a in THREE)
    assert teacher["external_tokens"] == dict.fromkeys(compare.TOKEN_FIELDS)
    for basis in ("fresh", "all"):
        assert teacher[f"external_tokens_total_{basis}"] is None
        assert teacher[f"quality_per_external_token_{basis}"] is None
        assert f"quality_per_external_token_{basis}" in teacher["unknown"]
        assert f"external_tokens_total_{basis}" in teacher["unknown"]
        # What needs the teacher's tokens is unknown too.
        assert harness["vs_teacher"][f"external_tokens_delta_{basis}"] is None
        assert f"vs_teacher.external_tokens_delta_{basis}" in harness["unknown"]
    # ... and the headline cannot say "yes".
    assert table.main_metric["fewer_external_tokens"] is None
    assert table.main_metric["all"] is None

    text = table.to_markdown()
    teacher_line = next(
        line for line in text.splitlines() if line.startswith("| teacher | 0 |")
    )
    assert teacher_line.count("未知") == 6  # four fields and the two totals
    quality_line = next(
        line for line in text.splitlines() if line.startswith("| teacher | claude")
    )
    assert quality_line.endswith("| 未知 | 未知 | — |")  # per token, on both bases
    assert "外部 token 更少：未知" in text
    assert "三项全满足：未知" in text


def test_unknown_and_zero_are_different_rows():
    zero = rows(compare.compare(three(teacher={"external_tokens": dict(ZERO)})))[
        "teacher"
    ]
    unknown = rows(compare.compare(three(teacher={"external_tokens": None})))["teacher"]
    assert zero["external_tokens"] == ZERO and unknown["external_tokens"] != ZERO
    assert zero["unknown"] == []
    assert zero["undefined"] == [
        "quality_per_external_token_fresh",
        "quality_per_external_token_all",
    ]
    assert "quality_per_external_token_fresh" in unknown["unknown"]
    assert unknown["undefined"] == []


def test_one_missing_field_makes_a_total_unknown_only_if_its_basis_uses_it():
    base = {"input_new": 10, "output": 20, "cache_read": 30, "cache_write": 40}

    def totals(external):
        row = rows(compare.compare([arm(external_tokens=external)]))["teacher"]
        return row["external_tokens_total_fresh"], row["external_tokens_total_all"]

    assert totals(base) == (10 + 20 + 40, 10 + 20 + 30 + 40)
    # cache_read unknown: the fresh total (which leaves it out) is still known.
    assert totals(dict(base, cache_read=None)) == (10 + 20 + 40, None)
    # cache_write unknown: both totals need it.
    assert totals(dict(base, cache_write=None)) == (None, None)
    assert totals(dict(base, input_new=None, output=None)) == (None, None)


@pytest.mark.parametrize(
    ("field", "unknown_columns"),
    [
        ("quality", ["quality_per_second", "vs_teacher.quality_delta"]),
        ("wall_s", ["quality_per_second", "vs_teacher.wall_s_delta"]),
        ("local_tokens", []),
    ],
)
def test_other_missing_measurements_are_unknown_in_every_dependent_column(
    field, unknown_columns
):
    over = {field: None}
    if field == "quality":  # an interval of a difference needs the quality
        over |= {"quality_delta_ci_vs_teacher": None, "quality_delta_ci_method": None}
    if field == "wall_s":  # and a time basis needs the time
        over |= {"wall_s_basis": None}
    table = compare.compare(three(prior_levi=over))
    harness = rows(table)["prior_levi"]
    assert harness[field] is None
    assert {field, *unknown_columns} <= set(harness["unknown"])
    assert "未知" in table.to_markdown()
    assert (table.main_metric["all"] is None) == bool(unknown_columns)


def test_a_slower_or_worse_student_is_reported_as_no():
    table = compare.compare(
        three(
            prior_levi={
                "quality": 0.7,
                # -0.10 against the teacher; even the better end is beyond -0.05
                "quality_delta_ci_vs_teacher": (-0.2, -0.06),
                "wall_s": 900.0,
                "external_tokens": {**ZERO, "output": 100000},
            }
        )
    )
    assert table.main_metric["quality_non_inferior"] is False
    assert table.main_metric["quality_status"] == "no"
    assert table.main_metric["quality_point_not_worse"] is False
    assert table.main_metric["faster"] is False
    assert table.main_metric["fewer_external_tokens"] is False
    assert table.main_metric["all"] is False
    # One "no" decides even when another answer is unknown.
    worse = {"quality": 0.7, "quality_delta_ci_vs_teacher": (-0.2, -0.06)}
    partial = compare.compare(
        three(prior_levi=worse, teacher={"external_tokens": None})
    )
    assert partial.main_metric["all"] is False


def test_a_round_may_lack_arms():
    table = compare.compare([arm("teacher")])
    assert table.missing_arms == ["prior_levi", "levi2", "direct"]
    assert table.main_metric is None
    assert rows(table)["teacher"]["vs_direct"] is None
    assert "本轮缺少：prior_levi, levi2, direct" in table.to_markdown()
    assert "无此臂" in table.to_markdown()


def test_the_same_problems_have_one_hash_however_they_are_written():
    a = compare.episode_set_hash([3, 1, 2])
    assert a == compare.episode_set_hash(["2", "3", "1", 1])
    assert a != compare.episode_set_hash([1, 2, 4])
    by_ids = arm(episode_set=[1, 2, 3])
    by_hash = arm(episode_set=None, episode_set_hash=a, episode_count=3)
    assert by_ids.episode_set_hash == by_hash.episode_set_hash == a
    compare.compare(
        [
            by_ids,
            arm("prior_levi", episode_set=None, episode_set_hash=a, episode_count=3),
        ]
    )


def test_records_over_different_problems_are_refused_and_the_difference_is_named():
    other = arm("prior_levi", episode_set=EPISODES[:-1])
    with pytest.raises(CompareError) as refused:
        compare.compare([arm("teacher"), other, arm("direct")])
    text = str(refused.value)
    assert "episode_set_hash differs" in text
    assert "teacher=" in text and "prior_levi=" in text
    assert "episode_count differs" in text


@pytest.mark.parametrize(
    ("field", "value", "named"),
    [
        ("decoding", {"temperature": 0.7, "seed": 1}, "decoding differs"),
        ("scoring", {**SCORING, "metric": "accuracy"}, "scoring differs"),
        ("task", "screws-subtask", "task differs"),
    ],
)
def test_a_different_decoding_scoring_or_task_is_refused(field, value, named):
    with pytest.raises(CompareError, match=named):
        compare.compare([arm("teacher"), arm("direct", **{field: value})])


def test_every_difference_is_listed_at_once():
    odd = arm(
        "direct",
        decoding={"temperature": 1},
        scoring={**SCORING, "metric": "other"},
        episode_set=[1, 2],
    )
    with pytest.raises(CompareError) as refused:
        compare.compare([arm("teacher"), odd])
    assert len(refused.value.problems) >= 4


def test_two_records_for_one_arm_are_refused():
    with pytest.raises(CompareError, match="two records for arm teacher"):
        compare.compare([arm("teacher"), arm("teacher", quality=0.5)])


def test_nothing_to_compare_is_refused():
    with pytest.raises(CompareError, match="no records"):
        compare.compare([])
    with pytest.raises(CompareError, match="not an ArmRecord"):
        compare.compare([{"arm": "teacher"}])
    for basis in ("", "fresh,all", "bogus", None):
        with pytest.raises(CompareError, match="verdict_basis"):
            compare.compare([arm()], verdict_basis=basis)
    with pytest.raises(TypeError):  # the single configurable total is gone
        compare.compare([arm()], token_basis=("output",))


def test_confirm_allows_aggregate_scores_only():
    table = compare.compare(blind())
    assert table.conditions["confirm"] is True
    assert table.conditions["split"] == "confirm"
    assert "模式：confirm" in table.to_markdown()
    assert all("per_episode" not in r for r in table.rows)
    assert "per_episode" not in table.to_json()
    # A tune table is not a confirm table and keeps the per-episode column.
    assert compare.compare(three()).conditions["confirm"] is False


def test_there_is_no_flag_to_forget_the_split_alone_decides():
    # The old hole: confirm was a keyword defaulting to False, and a caller who
    # left it out got per-episode scores and the explicit episode list in the
    # table. Now the record says split="confirm" and every table obeys it.
    scores = {"7": 1.0, "9": 0.0}
    with pytest.raises(CompareError, match="per_episode scores"):
        compare.compare(blind(prior_levi={"per_episode": scores}))
    # The explicit episode list is the same hole.
    with pytest.raises(CompareError, match="explicit episode_set"):
        compare.compare(three(**every_arm(split="confirm")))
    # The keyword that could be forgotten is gone.
    with pytest.raises(TypeError):
        compare.compare(blind(), confirm=False)


def test_a_table_holds_one_split():
    mixed = [arm("teacher", split="confirm", **hashed()), *three()[1:]]
    with pytest.raises(CompareError, match="different splits"):
        compare.compare(mixed)
    for other in ("tune", "other"):
        two_blind = blind()[:2]
        with pytest.raises(CompareError, match="different splits"):
            compare.compare([*two_blind, arm("direct", split=other, **hashed())])
    assert compare.compare(three(**every_arm(split="other"))).conditions["split"] == (
        "other"
    )


def test_asserting_confirm_refuses_records_of_another_split():
    # The command line's --confirm: "this round is confirm". A tune record
    # (here with per-episode scores) must not pass for one.
    with pytest.raises(CompareError, match="asserted .* split=tune"):
        compare.compare(three(), assert_confirm=True)
    assert compare.compare(blind(), assert_confirm=True).conditions["confirm"]


def hashed():
    return {
        "episode_set": None,
        "episode_set_hash": compare.episode_set_hash(EPISODES),
        "episode_count": len(EPISODES),
    }


def test_confirm_refuses_per_episode_content():
    scores = {"7": 1.0, "9": 0.0}
    with pytest.raises(CompareError, match="per_episode scores"):
        compare.compare(blind(prior_levi={"per_episode": scores}))
    # In a tune round the same record is fine and the scores are passed through.
    table = compare.compare(three(prior_levi={"per_episode": scores}))
    assert rows(table)["prior_levi"]["per_episode"] == scores


def test_confirm_refuses_an_explicit_episode_list_but_takes_the_hash():
    assert compare.compare(blind()).conditions["episode_count"] == 12
    with pytest.raises(CompareError, match="explicit episode_set"):
        compare.compare(three(**every_arm(split="confirm")))


FREE = "episode 7 failed because the plate was upside down"


def test_confirm_takes_a_reason_code_but_not_free_text():
    with pytest.raises(CompareError, match="blocked text"):
        compare.compare(blind(prior_levi={"blocked": FREE}))
    with pytest.raises(CompareError, match="external_tokens_source text"):
        compare.compare(
            blind(teacher={"external_tokens_source": "see session of episode 7"})
        )
    ok = compare.compare(blind(prior_levi={"blocked": "gpu_busy"}))
    assert rows(ok)["prior_levi"]["blocked"] == "gpu_busy"
    # The free text is fine in a tune round.
    compare.compare(three(prior_levi={"blocked": FREE}))


@pytest.mark.parametrize("field", ["model", "quality_ci_method", "task"])
def test_confirm_checks_every_text_field_not_only_blocked_and_the_source(field):
    # model, task and the interval method were free text of up to 200
    # characters in a confirm table.
    over = {"quality_ci": (0.7, 0.9), "quality_ci_method": "bootstrap_95"}
    with pytest.raises(CompareError, match=f"{field} text"):
        compare.compare(blind(prior_levi={**over, field: FREE}))
    compare.compare(blind(prior_levi=over))  # codes are fine
    same = every_arm(**{field: FREE})  # free text is fine in a tune round
    same["prior_levi"].update(over)
    if field == "model":
        same["teacher"] = {"model": FREE}
    compare.compare(three(**same))


@pytest.mark.parametrize("field", ["decoding", "scoring"])
@pytest.mark.parametrize(
    "smuggled",
    [
        {"metric": "segment_f1", "notes": {"ep7": "plate flipped"}},  # nested
        {"metric": "segment_f1", "per_episode": [1.0, 0.0, 1.0]},  # a list
        {"metric": FREE},  # free text as a value
        {"episode 7 failed": 1},  # free text as a key
        {"metric": "x" * 65},  # a code is at most 64 characters
        {f"k{i}": i for i in range(17)},  # too many entries
        {f"{'k' * 60}{i}": "v" * 60 for i in range(4)},  # few, but 500 characters
        {"metric": None},  # nothing to carry, but also not a number or a code
    ],
)
def test_confirm_refuses_nested_or_free_decoding_and_scoring(field, smuggled):
    # decoding and scoring were any JSON and were printed as written. (The
    # scoring must state its direction and margin, which are flat, so the
    # smuggled keys are added to them.)
    value = {**SCORING, **smuggled} if field == "scoring" else smuggled
    with pytest.raises(CompareError, match=f"{field} that is not a short flat dict"):
        compare.compare(blind(prior_levi={field: value}))
    # A tune round takes the same JSON as written.
    compare.compare(three(**every_arm(**{field: value})))


def test_confirm_takes_flat_codes_numbers_and_booleans_in_decoding_and_scoring():
    decoding = {"temperature": 0, "top_p": 0.95, "seed": 1, "do_sample": False}
    table = compare.compare(blind(**{n: {"decoding": decoding} for n in THREE}))
    assert table.conditions["decoding"] == decoding
    assert "do_sample" in table.to_markdown()
    # The scoring's direction and margin are flat and pass.
    assert table.conditions["scoring"] == SCORING


def test_a_refusal_does_not_repeat_the_content_it_refused():
    canary = "plate-7-was-upside-down but this is longer than a code can be, 0123456789"
    both = {"decoding": {"notes": {"a": canary}}, "task": canary, "blocked": canary}
    with pytest.raises(CompareError) as refused:
        compare.compare(blind(prior_levi=both))
    assert "plate" not in str(refused.value) and "upside" not in str(refused.value)
    # Nor does a malformed record: the message says what kind of value it was.
    for field, value in (("quality", canary), ("wall_s", canary), ("split", canary)):
        with pytest.raises(RecordError) as bad:
            arm(**{field: value})
        assert "upside" not in str(bad.value), field
    row = arm().to_dict()
    row[canary] = 1
    with pytest.raises(RecordError) as unknown:
        ArmRecord.from_dict(row)
    assert "upside" not in str(unknown.value)
    # A short code is still shown: it is the kind of text a confirm table holds.
    with pytest.raises(RecordError, match="'student'"):
        arm(arm="student")
    # Differences between valid confirm records are shown, and they are codes.
    with pytest.raises(CompareError, match="decoding differs"):
        compare.compare(blind(prior_levi={"decoding": {"temperature": 1}}))


def test_a_field_the_record_does_not_know_is_refused_in_both_modes():
    row = arm().to_dict()
    row["answers"] = {"7": "grasp the plate"}
    with pytest.raises(RecordError, match="unknown fields"):
        ArmRecord.from_dict(row)


def test_a_record_is_validated():
    bad = [
        {"arm": "student"},
        {"quality": math.nan},
        {"quality": True},
        {"quality": "0.8"},
        {"wall_s": 0},
        {"wall_s": -3},
        {"local_tokens": True},
        {"local_tokens": -1},
        {"local_tokens": 1.5},
        {"external_tokens": {"input_new": True}},
        {"external_tokens": {"input_new": -1}},
        {"external_tokens": {"input": 1}},
        {"agreement_with_teacher": 1.2, "arm": "direct"},
        {"agreement_with_teacher": 0.5},  # the teacher arm has none
        {"quality_ci": (0.9, 0.7)},
        {"quality_ci": (0.5,)},
        {"blocked": "two\nlines"},
        {"blocked": ""},
        {"decoding": "greedy"},
        {"scoring": ""},
        {"episode_set": []},
        {"episode_set": [1, True]},
        {"episode_set": [1, 2], "episode_count": 5},
        {"episode_set": [1, 2], "episode_set_hash": "0" * 64},
        {"episode_set": None},
        {"model": None},
        {"per_episode": [1, 2]},
        {"split": "dev"},
        {"split": None},
        {"decoding": {}},  # nothing declared would match anything
        {"scored_count": 13},  # more than the 12 problems
        {"scored_count": -1},
        {"scored_count": True},
        {"scored_count": 1.5},
    ]
    for over in bad:
        with pytest.raises(RecordError):
            arm(**over)


def test_a_record_survives_json_and_is_not_changed_by_its_source():
    source = {"temperature": 0, "seed": 1}
    tokens_in = {"input_new": 1, "output": 2}
    record = arm(
        decoding=source,
        external_tokens=tokens_in,
        quality_ci=(0.7, 0.9),
        quality_ci_method="wilson95",
        per_episode={"1": 1.0},
    )
    source["temperature"] = 9
    tokens_in["output"] = 99
    assert record.decoding["temperature"] == 0
    assert record.external_tokens == {
        "input_new": 1,
        "output": 2,
        "cache_read": None,
        "cache_write": None,
    }
    again = ArmRecord.from_dict(json.loads(json.dumps(record.to_dict())))
    assert again == record
    assert record.to_dict()["schema"] == compare.ARM_SCHEMA
    with pytest.raises(RecordError, match="schema"):
        ArmRecord.from_dict({**record.to_dict(), "schema": "other.v9"})
    with pytest.raises(RecordError, match="missing fields"):
        ArmRecord.from_dict({"arm": "teacher"})
    # The split has no default: a record that forgets it is refused, so it can
    # never be taken for a tune record by accident.
    no_split = record.to_dict()
    del no_split["split"]
    with pytest.raises(RecordError, match=r"missing fields \['split'\]"):
        ArmRecord.from_dict(no_split)


def test_every_field_of_every_arm_survives_json():
    # A scorer writes records as JSON files: the interval, the bases, the
    # exactness flag and the counts must come back equal, whichever arm.
    for name in THREE:
        record = arm(
            name,
            quality_ci=(0.7, 0.9),
            quality_ci_method="wilson95",
            external_tokens_exact=False,
            external_tokens_source="claude_jsonl.last_usage:lower_bound",
        )
        again = ArmRecord.from_dict(json.loads(json.dumps(record.to_dict())))
        assert again == record, name
    harness = arm("prior_levi")
    assert harness.quality_delta_ci_vs_teacher == (-0.03, 0.07)
    assert harness.to_dict()["quality_delta_ci_vs_teacher"] == [-0.03, 0.07]
    assert harness.to_dict()["wall_s_basis"] == "excludes_queue"
    assert harness.to_dict()["scored_count"] == 12
    assert harness.to_dict()["split"] == "tune"


def test_a_pipe_in_a_name_does_not_break_the_table():
    text = compare.compare([arm(model="a|b")]).to_markdown()
    assert "a\\|b" in text


def test_a_lower_bound_student_is_never_called_fewer_even_when_its_number_is_low():
    bound = {"external_tokens_exact": False, "external_tokens": {**ZERO, "output": 10}}
    table = compare.compare(three(prior_levi=bound))
    assert rows(table)["prior_levi"]["external_tokens_total_fresh"] == 10
    assert table.main_metric["fewer_external_tokens"] is None
    assert table.main_metric["all"] is None
    # An exact student at the same number is "fewer", and exact=None (not
    # stated) is not treated as a bound.
    exact = {"external_tokens_exact": True, "external_tokens": {**ZERO, "output": 10}}
    assert compare.compare(three(prior_levi=exact)).main_metric["fewer_external_tokens"]
    assert compare.compare(
        three(prior_levi={"external_tokens": {**ZERO, "output": 10}})
    ).main_metric["fewer_external_tokens"]
    # Above the teacher's total a bound is certain: still not "yes", and it
    # does not turn into "no" either.
    big = {"external_tokens_exact": False, "external_tokens": {**ZERO, "output": 9000}}
    assert (
        compare.compare(three(prior_levi=big)).main_metric["fewer_external_tokens"]
        is None
    )


def test_a_lower_bound_teacher_is_marked_and_only_a_student_below_it_is_fewer():
    teacher = {"external_tokens_exact": False}  # the teacher's total: 4500, a bound
    table = compare.compare(three(teacher=teacher))
    t = rows(table)["teacher"]
    assert t["external_tokens_exact"] is False
    # The student (0 tokens, exact) is below the bound, hence below the truth.
    assert table.main_metric["fewer_external_tokens"] is True
    # A student above the bound cannot be called "not fewer": the teacher may
    # have used more than what could be counted.
    above = {
        "external_tokens": {**ZERO, "output": 100000},
        "external_tokens_exact": True,
    }
    over = compare.compare(three(teacher=teacher, prior_levi=above))
    assert over.main_metric["fewer_external_tokens"] is None
    # With the teacher exact, the same numbers are a plain "no".
    exact = compare.compare(
        three(teacher={"external_tokens_exact": True}, prior_levi=above)
    )
    assert exact.main_metric["fewer_external_tokens"] is False

    text = table.to_markdown()
    teacher_line = next(l for l in text.splitlines() if l.startswith("| teacher | 0 |"))
    assert "≥1,000" in teacher_line and "≥4,500" in teacher_line
    assert "下限" in teacher_line
    quality_line = next(
        l for l in text.splitlines() if l.startswith("| teacher | claude")
    )
    assert "≤0.178" in quality_line  # 0.80 / 4500 * 1000, an upper bound
    assert "≤0.0327" in quality_line  # and the same on the all-four basis
    assert "-4,500（含下限）" in text  # the difference to the teacher is a bound too
    plain = compare.compare(three()).to_markdown()
    assert "≥" not in plain.split("注：")[0] and "（含下限）" not in plain
    assert "精确" not in plain and "未声明" in plain


def test_the_exactness_flag_is_validated_and_needs_token_counts():
    for bad in (1, "yes", 0):
        with pytest.raises(RecordError, match="external_tokens_exact"):
            arm(external_tokens_exact=bad)
    with pytest.raises(RecordError, match="needs external_tokens"):
        arm(external_tokens=None, external_tokens_exact=False)
    assert arm(external_tokens_exact=True).external_tokens_exact is True


def test_confirm_takes_the_exactness_flag_and_the_source_code_but_not_the_sentence(
    tmp_path,
):
    session = tmp_path / "s.jsonl"
    session.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "id": "m",
                    "usage": {"input_tokens": 5, "output_tokens": 6},
                },
            }
        )
        + "\n"
    )
    usage = tokens.usage_from_files([session])  # two fields missing: a lower bound
    assert not usage.exact
    # The sentence has spaces and a semicolon: not a code, so a confirm table
    # refuses it. This is why the code exists.
    sentence = blind(teacher={"external_tokens_source": usage.source})
    with pytest.raises(CompareError, match="external_tokens_source text"):
        compare.compare(sentence)
    coded = blind(teacher=usage.record_fields(confirm=True))
    table = compare.compare(coded)
    t = rows(table)["teacher"]
    assert t["external_tokens_exact"] is False
    assert t["external_tokens_source"] == usage.source_code
    assert compare.CODE.fullmatch(usage.source_code)
    # Outside confirm the full sentence is the default.
    assert usage.record_fields()["external_tokens_source"] == usage.source


def test_the_teacher_tokens_come_from_the_session_reader(tmp_path):
    session = tmp_path / "s.jsonl"
    session.write_text(
        "\n".join(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": "2026-11-06T09:00:00Z",
                    "message": {
                        "id": "msg_a",
                        "model": "claude-opus",
                        "usage": {
                            "input_tokens": 5,
                            "output_tokens": out,
                            "cache_read_input_tokens": 70,
                            "cache_creation_input_tokens": 9,
                        },
                    },
                }
            )
            for out in (16, 415)
        )
    )
    usage = tokens.usage_from_files([session])
    record = arm(
        model=usage.models[0],
        external_tokens=usage.external_tokens(),
        external_tokens_source=usage.source,
    )
    row = rows(compare.compare([record]))["teacher"]
    assert row["external_tokens"] == {
        "input_new": 5,
        "output": 415,
        "cache_read": 70,
        "cache_write": 9,
    }
    assert row["external_tokens_total_fresh"] == 5 + 415 + 9
    assert row["external_tokens_total_all"] == 5 + 415 + 70 + 9
    assert "last usage record per message id" in row["external_tokens_source"]


def write_records(tmp_path, name, value):
    path = tmp_path / name
    path.write_text(json.dumps(value))
    return str(path)


def test_command_line_reads_files_in_three_shapes(tmp_path, capsys):
    a, b, c = (r.to_dict() for r in three())
    files = [
        write_records(tmp_path, "teacher.json", a),  # one record
        write_records(tmp_path, "rest.json", {"records": [b, c]}),  # an envelope
    ]
    assert compare.main(files) == 0
    text = capsys.readouterr().out
    assert "教师–学生三组对比" in text and "direct" in text
    all_three = write_records(tmp_path, "all.json", [a, b, c])  # a list
    assert compare.main([all_three, "--format", "json"]) == 0
    assert [r["arm"] for r in json.loads(capsys.readouterr().out)["rows"]] == list(
        THREE
    )


def test_command_line_refuses_and_says_why(tmp_path, capsys):
    a = arm("teacher").to_dict()
    b = arm("prior_levi", episode_set=EPISODES[:3]).to_dict()
    both = write_records(tmp_path, "both.json", [a, b])
    assert compare.main([both]) == 2
    assert "episode_set_hash differs" in capsys.readouterr().err
    assert compare.main([str(tmp_path / "missing.json")]) == 2
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert compare.main([str(broken)]) == 2
    leaky = write_records(
        tmp_path, "leaky.json", [arm(per_episode={"1": 1.0}).to_dict()]
    )
    assert compare.main([leaky]) == 0  # a tune record
    # --confirm asserts "this is a confirm round": a tune record is refused.
    assert compare.main([leaky, "--confirm"]) == 2
    assert "split=tune" in capsys.readouterr().err
    # And without the flag a confirm record is still held to the confirm rules.
    forgot = write_records(
        tmp_path,
        "forgot.json",
        [arm(split="confirm", per_episode={"1": 1.0}).to_dict()],
    )
    assert compare.main([forgot]) == 2
    assert "per_episode scores" in capsys.readouterr().err
    with pytest.raises(SystemExit):  # argparse refuses an unknown choice
        compare.main([leaky, "--verdict-basis", "bogus"])


def test_whole_floats_and_ints_are_one_value_in_decoding_and_scoring():
    # 0 and 0.0 are the same declared temperature; the arms must not be
    # refused for the way a number was typed.
    a = {"temperature": 0, "top_k": 1, "nested": {"p": 1.0}}
    b = {"temperature": 0.0, "top_k": 1.0, "nested": {"p": 1}}
    table = compare.compare([arm("teacher", decoding=a), arm("direct", decoding=b)])
    assert table.conditions["decoding"] == a
    assert arm(decoding=b).decoding == a and arm(decoding=b) == arm(decoding=a)
    # A real difference is still refused: 0.5 is not 0.
    with pytest.raises(CompareError, match="decoding differs"):
        compare.compare(
            [
                arm("teacher", decoding=a),
                arm("direct", decoding={**a, "temperature": 0.5}),
            ]
        )
    # nan and infinity are not JSON and are refused, not normalised.
    with pytest.raises(RecordError, match="plain JSON"):
        arm(decoding={"temperature": math.nan})


def test_a_difference_names_the_first_key_that_differs():
    base = {"temperature": 0, "seed": 1, "max_tokens": 512}
    other = {"temperature": 0.7, "seed": 2, "max_tokens": 512}
    with pytest.raises(CompareError) as refused:
        compare.compare([arm("teacher", decoding=base), arm("direct", decoding=other)])
    text = str(refused.value)
    # Keys are visited in sorted order: max_tokens agrees, seed is first to differ.
    assert "decoding differs at 'seed' (teacher=1, direct=2)" in text
    assert "temperature" not in text
    # Nested and one-sided keys are named by path.
    with pytest.raises(CompareError, match=r"at 'a.b' \(teacher=1, direct=<absent>"):
        compare.compare(
            [
                arm("teacher", decoding={"a": {"b": 1}}),
                arm("direct", decoding={"a": {"c": 1}}),
            ]
        )
    # Each other arm is named against the first.
    with pytest.raises(CompareError) as three_way:
        compare.compare(
            [
                arm("teacher", scoring={**SCORING, "metric": "f1"}),
                arm(
                    "prior_levi",
                    scoring={**SCORING, "metric": "f1", "reference": "panel"},
                ),
                arm("direct", scoring={**SCORING, "metric": "iou"}),
            ]
        )
    assert "at 'reference' (teacher=" in str(three_way.value)
    assert "at 'metric' (teacher=" in str(three_way.value)


def test_an_empty_decoding_is_refused_not_matched_with_everything():
    with pytest.raises(RecordError, match="decoding must be a non-empty dict"):
        arm(decoding={})


def test_the_direct_arm_must_be_the_same_model_as_the_harness_arm():
    with pytest.raises(CompareError, match="different models") as refused:
        compare.compare(three(direct={"model": "qwen3-vl-small"}))
    assert "qwen3-vl" in str(refused.value) and "without LEVI" in str(refused.value)
    # The teacher is another model by definition, and a table without the
    # direct arm has nothing to compare it with.
    compare.compare(three())
    compare.compare([arm("teacher"), arm("prior_levi")])
    # In a confirm table the model names are codes, so naming them is safe.
    with pytest.raises(CompareError, match="different models"):
        compare.compare(blind(direct={"model": "qwen3-vl-small"}))


def test_every_arm_must_have_scored_all_the_problems():
    # The hash and the count agree, but the harness arm was blocked on 2 of
    # the 12 and its quality is over the 10 it answered: not comparable.
    with pytest.raises(CompareError, match="prior_levi scored 10 of 12") as refused:
        compare.compare(three(prior_levi={"scored_count": 10}))
    assert "denominator" in str(refused.value)
    with pytest.raises(CompareError, match="teacher has a quality but no scored_count"):
        compare.compare(three(teacher={"scored_count": None}))
    # An arm with no quality (blocked, not measured) has nothing to be compared.
    blocked = three(
        prior_levi={
            "quality": None,
            "quality_delta_ci_vs_teacher": None,
            "quality_delta_ci_method": None,
            "scored_count": None,
            "blocked": "gpu_busy",
        }
    )
    assert rows(compare.compare(blocked))["prior_levi"]["quality"] is None
    assert arm(scored_count=12).to_dict()["scored_count"] == 12


def harness_vs_teacher(
    interval, *, quality=None, margin=0.05, higher=True, teacher=0.80
):
    """A round where only the paired interval of prior_levi changes."""
    scoring = {**SCORING, "higher_is_better": higher, "non_inferiority_margin": margin}
    if quality is None:
        quality = teacher + sum(interval) / 2  # the point estimate in the middle
    harness = {
        "scoring": scoring,
        "quality": quality,
        "quality_delta_ci_vs_teacher": interval,
    }
    if interval is None:
        harness["quality_delta_ci_method"] = None
    return compare.compare(
        three(
            teacher={"scoring": scoring, "quality": teacher},
            prior_levi=harness,
            direct={"scoring": scoring, "quality_delta_ci_vs_teacher": None},
        )
    )


@pytest.mark.parametrize(
    ("interval", "status", "answer"),
    [
        ((-0.03, 0.07), "yes", True),  # the worse end is within the margin
        ((-0.05, 0.02), "yes", True),  # exactly at the margin counts
        ((-0.06, 0.10), "undetermined", None),  # covers the margin
        ((-0.20, -0.0501), "no", False),  # even the better end is beyond it
        ((-0.20, -0.05), "undetermined", None),  # the better end is AT the margin
        (None, "unknown", None),  # no interval, no call
    ],
)
def test_quality_is_judged_on_the_paired_interval_against_the_margin(
    interval, status, answer
):
    table = harness_vs_teacher(interval, quality=0.82 if interval is None else None)
    assert table.main_metric["quality_status"] == status
    assert table.main_metric["quality_non_inferior"] is answer
    # What the table says in words.
    text = table.to_markdown()
    word = {"yes": "是", "no": "否", "undetermined": "未定", "unknown": "未知"}[status]
    line = next(l for l in text.splitlines() if l.startswith("- 质量不低于教师"))
    assert line.split("：")[-1].startswith(word)
    assert "δ = 0.05" in line


def test_a_point_estimate_that_looks_fine_is_not_enough_when_the_interval_is_wide():
    # About six problems: one flipped problem moves a rate by 0.14-0.17. The
    # harness is +0.02 above the teacher, but the paired interval reaches -0.17.
    table = harness_vs_teacher((-0.17, 0.21), quality=0.82)
    assert table.main_metric["quality_point_not_worse"] is True  # the side line
    assert table.main_metric["quality_status"] == "undetermined"
    assert table.main_metric["all"] is None  # an undetermined quality never makes "yes"
    assert "质量点估计不劣于教师（辅助，不进结论）：是" in table.to_markdown()
    # A point estimate below the teacher is no refutation either.
    low = harness_vs_teacher((-0.10, 0.05), quality=0.77)
    assert low.main_metric["quality_point_not_worse"] is False
    assert low.main_metric["quality_status"] == "undetermined"


def test_a_missing_interval_leaves_the_headline_unknown_whatever_the_points_say():
    table = compare.compare(
        three(
            prior_levi={
                "quality_delta_ci_vs_teacher": None,
                "quality_delta_ci_method": None,
            }
        )
    )
    assert table.main_metric["quality_status"] == "unknown"
    assert table.main_metric["quality_point_not_worse"] is True
    assert table.main_metric["all"] is None
    assert "未知（没有配对差区间）" in table.to_markdown()
    # An unknown teacher quality also leaves the quality call open.
    no_teacher = compare.compare(three(teacher={"quality": None}))
    assert no_teacher.main_metric["quality_status"] == "unknown"


def test_a_metric_where_smaller_is_better_is_read_the_other_way_round():
    # An error (MAE, P90): the student is better when its value is LOWER.
    # Interval of (student - teacher) of an error: positive = worse.
    margin = 0.5
    ok = harness_vs_teacher((-0.1, 0.3), higher=False, margin=margin, teacher=0.8)
    assert ok.main_metric["quality_status"] == "yes"
    assert (
        ok.main_metric["quality_point_not_worse"] is False
    )  # 0.9 > 0.8: a point estimate
    assert ok.conditions["quality_direction"] == "lower_is_better"
    worse = harness_vs_teacher((0.7, 1.7), higher=False, margin=margin, teacher=0.8)
    assert worse.main_metric["quality_status"] == "no"
    mid = harness_vs_teacher((0.2, 1.0), higher=False, margin=margin, teacher=0.8)
    assert mid.main_metric["quality_status"] == "undetermined"
    better = harness_vs_teacher((-0.9, -0.2), higher=False, margin=margin, teacher=0.8)
    assert better.main_metric["quality_status"] == "yes"
    assert better.main_metric["quality_point_not_worse"] is True  # 0.25 <= 0.8
    # The same interval means the opposite for a metric where larger is better.
    flipped = harness_vs_teacher((0.7, 1.7), higher=True, margin=margin, teacher=0.8)
    assert flipped.main_metric["quality_status"] == "yes"
    # An error divided by seconds or tokens is not a rate of merit.
    row = rows(ok)["prior_levi"]
    assert row["quality_per_second"] is None
    assert "quality_per_second" in row["undefined"]
    assert "越小越好" in ok.to_markdown()


def test_the_scoring_must_state_its_direction_and_margin():
    for bad in (
        {"metric": "f1"},  # nothing stated
        {**SCORING, "higher_is_better": "yes"},
        {**SCORING, "higher_is_better": 1},
        {**SCORING, "non_inferiority_margin": -0.01},
        {**SCORING, "non_inferiority_margin": True},
        {**SCORING, "non_inferiority_margin": "0.05"},
        {k: v for k, v in SCORING.items() if k != "non_inferiority_margin"},
        {k: v for k, v in SCORING.items() if k != "higher_is_better"},
        "segment_f1",  # a bare name states neither
    ):
        with pytest.raises(RecordError, match="scoring"):
            arm(scoring=bad)
    assert (
        arm(scoring={**SCORING, "non_inferiority_margin": 0}).scoring[
            "non_inferiority_margin"
        ]
        == 0
    )  # zero margin = strict non-inferiority, allowed


def test_the_paired_interval_is_validated_and_belongs_to_a_student():
    for bad in ((0.1, -0.1), (0.1,), ("a", 1), (math.nan, 1), True):
        with pytest.raises(RecordError, match="quality_delta_ci_vs_teacher"):
            arm("prior_levi", quality_delta_ci_vs_teacher=bad)
    with pytest.raises(RecordError, match="teacher arm has no"):
        arm("teacher", quality_delta_ci_vs_teacher=(-0.1, 0.1))
    with pytest.raises(RecordError, match="needs this arm's quality"):
        arm("prior_levi", quality=None, quality_delta_ci_vs_teacher=(-0.1, 0.1))
    with pytest.raises(RecordError, match="quality_delta_ci_method needs"):
        arm(
            "prior_levi",
            quality_delta_ci_vs_teacher=None,
            quality_delta_ci_method="x",
        )
    # Confirm: the method is a code, the interval is two numbers.
    compare.compare(blind())
    with pytest.raises(CompareError, match="quality_delta_ci_method text"):
        compare.compare(blind(prior_levi={"quality_delta_ci_method": FREE}))


def test_an_interval_that_excludes_its_own_point_difference_is_refused():
    # The classic slip is a reversed sign (teacher minus student). The harness
    # is +0.02 above the teacher; an interval of the opposite sign cannot be
    # that difference's own interval.
    with pytest.raises(CompareError, match="does not contain its own point difference"):
        compare.compare(
            three(
                prior_levi={
                    "quality_delta_ci_vs_teacher": (-0.07, 0.03)
                    if False
                    else (-0.30, -0.05)
                }
            )
        )


def student_with(**tokens_by_field):
    return {"external_tokens": {**ZERO, **tokens_by_field}}


def test_fewer_external_tokens_can_flip_with_the_basis_and_the_table_says_so():
    # The teacher: fresh 4,500 and cache_read 20,000 (all 24,500). A student
    # that writes 3,000 fresh tokens but re-reads a 60,000-token cache is
    # fewer on the fresh basis and more on the full one.
    flip = student_with(output=3000, cache_read=60000)
    table = compare.compare(three(prior_levi=flip))
    m = table.main_metric
    assert (m["fewer_external_tokens_fresh"], m["fewer_external_tokens_all"]) == (
        True,
        False,
    )
    assert m["fewer_external_tokens"] is None  # a claim one basis can reverse
    assert m["fewer_external_tokens_basis_dependent"] is True
    assert m["fewer_external_tokens_basis"] == "both"
    assert m["all"] is None
    text = table.to_markdown()
    assert "外部 token 更少：未定" not in text  # unknown is for what is not measured
    assert "新增口径 是，全量口径 否" in text and "结论随口径翻转" in text
    # One basis registered before the round decides, and the table names it.
    fresh = compare.compare(three(prior_levi=flip), verdict_basis="fresh")
    assert fresh.main_metric["fewer_external_tokens"] is True
    assert fresh.main_metric["fewer_external_tokens_basis"] == "fresh"
    assert fresh.main_metric["fewer_external_tokens_basis_dependent"] is False
    assert "按事先登记的口径：新增" in fresh.to_markdown()
    full = compare.compare(three(prior_levi=flip), verdict_basis="all")
    assert full.main_metric["fewer_external_tokens"] is False
    assert full.main_metric["all"] is False
    assert full.conditions["verdict_basis"] == "all"


def test_both_bases_agreeing_give_a_plain_answer():
    fewer = compare.compare(three(prior_levi=student_with(output=3000)))
    assert fewer.main_metric["fewer_external_tokens"] is True
    more = compare.compare(three(prior_levi=student_with(output=100000)))
    assert more.main_metric["fewer_external_tokens"] is False
    assert more.main_metric["fewer_external_tokens_basis_dependent"] is False


def test_both_totals_have_their_own_quality_per_thousand_tokens_in_the_table():
    row = next(
        line
        for line in compare.compare(three()).to_markdown().splitlines()
        if line.startswith("| teacher | claude")
    )
    assert "0.178" in row  # 0.80 / 4,500 x 1000, without cache_read
    assert "0.0327" in row  # 0.80 / 24,500 x 1000, all four fields
    text = compare.compare(three()).to_markdown()
    assert "新增口径 = input_new + output + cache_write" in text
    assert "全量口径 = input_new + output + cache_read + cache_write" in text
    assert "-4,500" in text and "-24,500" in text  # the difference on each basis


def test_a_missing_cache_read_leaves_only_the_full_basis_unknown():
    no_read = {
        "external_tokens": {
            "input_new": 1000,
            "output": 500,
            "cache_read": None,
            "cache_write": 3000,
        }
    }
    table = compare.compare(three(teacher=no_read))
    m = table.main_metric
    assert m["fewer_external_tokens_fresh"] is True
    assert m["fewer_external_tokens_all"] is None
    assert m["fewer_external_tokens"] is None  # both must say yes
    assert m["fewer_external_tokens_basis_dependent"] is False  # unknown, not flipped
    assert (
        compare.compare(three(teacher=no_read), verdict_basis="fresh").main_metric[
            "fewer_external_tokens"
        ]
        is True
    )


def test_a_time_with_queueing_is_not_compared_with_one_without():
    # A sub-agent's duration includes its queueing (measured in external-agent rounds); a
    # local call's time need not. The difference between them is not "the
    # speed difference".
    queued = {"wall_s_basis": "includes_queue"}
    table = compare.compare(three(teacher=queued))
    m = table.main_metric
    assert m["faster"] is None and m["faster_problem"] == "different"
    assert m["all"] is None
    harness = rows(table)["prior_levi"]
    assert harness["vs_teacher"]["wall_s_delta"] is None
    assert harness["vs_teacher"]["wall_s_problem"] == "different"
    assert "vs_teacher.wall_s_delta" in harness["unknown"]
    # The two local arms were measured the same way and are still compared.
    assert harness["vs_direct"]["wall_s_delta"] == 50.0
    text = table.to_markdown()
    assert "600.0（含排队）" in text and "200.0（不含排队）" in text
    assert "耗时更低：未知（两臂耗时口径不同，不比较）" in text
    assert "-400.0" not in text
    # Both with queueing: compared.
    same = compare.compare(
        three(
            teacher=queued,
            prior_levi=queued,
            direct=queued,
        )
    )
    assert (
        same.main_metric["faster"] is True
        and same.main_metric["faster_problem"] is None
    )


def test_an_unstated_time_basis_is_not_guessed():
    table = compare.compare(three(teacher={"wall_s_basis": None}))
    assert table.main_metric["faster"] is None
    assert table.main_metric["faster_problem"] == "unstated"
    assert "耗时更低：未知（两臂耗时口径未说明，不比较）" in table.to_markdown()
    assert "600.0（口径未说明）" in table.to_markdown()
    # An unmeasured time is plain unknown: there is no basis to dispute.
    unmeasured = compare.compare(three(teacher={"wall_s": None, "wall_s_basis": None}))
    assert unmeasured.main_metric["faster"] is None
    assert unmeasured.main_metric["faster_problem"] is None


def test_the_time_basis_is_validated():
    for bad in ("with_queue", "", 1, True):
        with pytest.raises(RecordError, match="wall_s_basis"):
            arm(wall_s_basis=bad)
    with pytest.raises(RecordError, match="wall_s_basis needs wall_s"):
        arm(wall_s=None, wall_s_basis="includes_queue")
    # A basis is a code, allowed in a confirm record.
    compare.compare(blind())


def test_a_round_without_the_direct_arm_is_marked_incomplete_and_never_yes():
    table = compare.compare(three()[:2])  # teacher and harness only
    m = table.main_metric
    assert m["complete"] is False
    # The three measured answers are all yes, yet "all" does not say yes.
    assert (m["quality_non_inferior"], m["faster"], m["fewer_external_tokens"]) == (
        True,
        True,
        True,
    )
    assert m["all"] is None
    text = table.to_markdown()
    assert "不完整：缺 direct" in text
    assert "三项全满足：不完整" in text and "三项全满足：是" not in text
    # With the direct arm present the same numbers are a plain yes.
    assert compare.compare(three()).main_metric["complete"] is True
    # A "no" is a no with or without the direct arm.
    worse = compare.compare([arm("teacher"), arm("prior_levi", wall_s=900.0)])
    assert worse.main_metric["all"] is False and worse.main_metric["complete"] is False
    # No teacher, no headline at all (unchanged).
    assert compare.compare([arm("prior_levi")]).main_metric is None


DOCS = Path(__file__).resolve().parent.parent / "docs"


@pytest.mark.parametrize("page", ["EVALUATION.md", "EVALUATION.zh-CN.md"])
def test_each_edition_of_the_page_names_every_field_and_option_of_the_record(page):
    # The field table is the contract a scorer writes records against: a field
    # added to ArmRecord without a row (in both languages) fails here.
    text = (DOCS / page).read_text()
    # The first cell of a table row: where a field is described, not merely
    # mentioned in some other sentence.
    cells = " ".join(
        line.split("|")[1] for line in text.splitlines() if line.startswith("| `")
    )
    missing = [
        name for name in ArmRecord.__dataclass_fields__ if f"`{name}`" not in cells
    ]
    assert not missing, f"{page} does not describe {missing}"
    for word in (
        "levi.eval.arm_record.v1",
        "--confirm",
        "--verdict-basis",
        "source_code",
        "record_fields",
        "subagents_uncovered",
        "truncated_tail",
        "uuid_fallback",
        "non_inferiority_margin",
        "higher_is_better",
        "wall_s_basis",
    ):
        assert word in text, f"{page} does not mention {word}"


def test_the_docs_index_links_both_editions_of_the_evaluation_page():
    for index, targets in (
        ("README.md", ("EVALUATION.md", "EVALUATION.zh-CN.md")),
        ("README.zh-CN.md", ("EVALUATION.md", "EVALUATION.zh-CN.md")),
    ):
        text = (DOCS / index).read_text()
        for target in targets:
            assert f"]({target})" in text, f"{index} does not link {target}"
    english = (DOCS / "EVALUATION.md").read_text()
    chinese = (DOCS / "EVALUATION.zh-CN.md").read_text()
    assert "](EVALUATION.zh-CN.md)" in english and "](EVALUATION.md)" in chinese


# ---------------------------------------------------------- arm_record.v2 arms


def four():
    return [arm(n) for n in compare.ARMS]


def test_the_arm_set_is_the_four_arms_of_the_three_arm_experiment():
    assert compare.ARMS == ("teacher", "prior_levi", "levi2", "direct")
    assert compare.ARM_SCHEMA == "levi.eval.arm_record.v2"
    table = compare.compare(reversed(four()))
    assert [r["arm"] for r in table.rows] == list(compare.ARMS)
    assert table.missing_arms == []


def test_v1_records_are_read_with_their_arm_names_mapped():
    # local_direct is the direct arm; local_harness predates LEVI 2.0, so it is
    # the prior LEVI and never LEVI 2.0.
    for old, new in (("local_harness", "prior_levi"), ("local_direct", "direct")):
        row = {**arm(new).to_dict(), "schema": "levi.eval.arm_record.v1", "arm": old}
        record = ArmRecord.from_dict(row)
        assert record.arm == new
        assert record.to_dict()["schema"] == compare.ARM_SCHEMA
    assert compare.LEGACY_ARMS == {
        "local_harness": "prior_levi",
        "local_direct": "direct",
    }
    # A file with no schema key is a v2 file: it must use the new names.
    bare = arm("direct").to_dict()
    del bare["schema"]
    assert ArmRecord.from_dict(bare).arm == "direct"
    with pytest.raises(RecordError, match="v1 name"):
        ArmRecord.from_dict({**bare, "arm": "local_direct"})


def test_the_two_schemas_do_not_borrow_each_others_arm_names():
    v2 = arm("prior_levi").to_dict()
    with pytest.raises(RecordError, match="v1 name"):
        ArmRecord.from_dict({**v2, "arm": "local_harness"})
    v1 = {**v2, "schema": "levi.eval.arm_record.v1"}
    with pytest.raises(RecordError, match="does not exist"):
        ArmRecord.from_dict({**v1, "arm": "levi2"})
    with pytest.raises(RecordError, match="does not exist"):
        ArmRecord.from_dict({**v1, "arm": "prior_levi"})


def test_unknown_fields_are_named_against_the_records_own_schema():
    row = {
        **arm("direct").to_dict(),
        "schema": "levi.eval.arm_record.v1",
        "arm": "local_direct",
        "x": 1,
    }
    with pytest.raises(RecordError, match="fields of levi.eval.arm_record.v1"):
        ArmRecord.from_dict(row)


def test_all_local_arms_must_be_one_model():
    for name in ("prior_levi", "levi2", "direct"):
        with pytest.raises(CompareError, match="different models") as refused:
            compare.compare(four_with(**{name: {"model": "qwen3-vl-small"}}))
        assert name + "=qwen3-vl-small" in str(refused.value)
    compare.compare(four())  # the teacher is another model by definition


def four_with(**over):
    return [arm(n, **over.get(n, {})) for n in compare.ARMS]


def test_the_headline_is_about_levi2_when_the_round_has_it():
    table = compare.compare(four())
    assert table.main_metric_arm == "levi2"
    assert table.main_metric["quality_status"] == "yes"  # [0.0, 0.10] vs margin 0.05
    old = compare.compare(three())
    assert old.main_metric_arm == "prior_levi"
    assert compare.compare([arm("teacher")]).main_metric_arm is None


def test_rows_carry_the_difference_to_the_prior_levi():
    table = compare.compare(four())
    levi2 = rows(table)["levi2"]
    assert levi2["vs_prior_levi"]["quality_delta"] == pytest.approx(0.85 - 0.82)
    assert levi2["vs_prior_levi"]["wall_s_delta"] == 20.0
    assert rows(table)["prior_levi"]["vs_prior_levi"] == {"self": True}
    assert rows(compare.compare([arm("teacher")]))["teacher"]["vs_prior_levi"] is None


# ------------------------------------------------------------------- graded

GRADED = {
    "escalation_rate": 0.25,
    "coverage": 0.75,
    "pending_count": 1,
    "local_answered_count": 9,
    "escalated_count": 3,
}


def test_graded_statistics_ride_on_the_levi2_arm_as_a_flat_dict_of_numbers():
    record = arm("levi2", graded=GRADED)
    assert record.graded == GRADED
    assert ArmRecord.from_dict(record.to_dict()).graded == GRADED
    table = compare.compare(four_with(levi2={"graded": GRADED}))
    assert rows(table)["levi2"]["graded"] == GRADED
    assert rows(table)["direct"]["graded"] is None
    assert "escalation_rate" in table.to_markdown()
    assert "分级作答" in table.to_markdown()


@pytest.mark.parametrize("name", ["teacher", "prior_levi", "direct"])
def test_graded_belongs_to_the_levi2_arm_only(name):
    with pytest.raises(RecordError, match="levi2 arm"):
        arm(name, graded={"coverage": 0.5})


@pytest.mark.parametrize(
    "graded",
    [
        {},
        {"coverage": "high"},
        {"coverage": True},
        {"coverage": None},
        {"coverage": math.nan},
        {"coverage": 1.5},
        {"escalation_rate": -0.1},
        {"escalated_count": 2.5},
        {"escalated_count": 13},
        {"pending_count": -1},
        {"local_answered_count": 8, "escalated_count": 5},
        {"bad key": 1},
        {"x" * 65: 1},
        "coverage",
    ],
)
def test_bad_graded_values_are_refused(graded):
    with pytest.raises(RecordError):
        arm("levi2", graded=graded)


def test_graded_keys_and_values_follow_the_confirm_rules():
    ok = {k: GRADED[k] for k in ("coverage", "escalated_count")}
    compare.compare(blind_four(levi2={"graded": ok}))
    # Too many entries or too long in a confirm record (a score list could ride along).
    many = {f"k{i}": 1 for i in range(compare.MAX_FLAT_ITEMS + 1)}
    with pytest.raises(CompareError, match="graded that is not a short flat dict"):
        compare.compare(blind_four(levi2={"graded": many}))
    # The refusal does not repeat the content.
    with pytest.raises(CompareError) as refused:
        compare.compare(blind_four(levi2={"graded": many}))
    assert "k3" not in str(refused.value)
    # A tune table is not held to the length rule.
    compare.compare(four_with(levi2={"graded": many}))


def blind_four(**over):
    digest = compare.episode_set_hash(EPISODES)
    return [
        arm(
            n,
            split="confirm",
            episode_set=None,
            episode_set_hash=digest,
            episode_count=len(EPISODES),
            **over.get(n, {}),
        )
        for n in compare.ARMS
    ]


def test_a_v1_record_cannot_carry_graded():
    row = {
        **arm("direct").to_dict(),
        "schema": "levi.eval.arm_record.v1",
        "arm": "local_direct",
        "graded": {"coverage": 0.5},
    }
    with pytest.raises(RecordError, match="graded does not exist"):
        ArmRecord.from_dict(row)
