"""The three-arm teacher / harness / direct table (levi/eval/compare.py).

The records are built by the tests; nothing here reads a dataset or calls a model.
"""

import json
import math

import pytest

from levi.automatic import external_report
from levi.eval import compare, tokens
from levi.eval.compare import ArmRecord, CompareError, RecordError

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
            "local_tokens": 0,
            "external_tokens": {
                "input_new": 1000,
                "output": 500,
                "cache_read": 20000,
                "cache_write": 3000,
            },
        },
        "local_harness": {
            "model": "qwen3-vl",
            "quality": 0.82,
            # Paired difference to the teacher, +0.02 as a point estimate.
            "quality_delta_ci_vs_teacher": (-0.03, 0.07),
            "quality_delta_ci_method": "paired_bootstrap_95",
            "agreement_with_teacher": 0.9,
            "wall_s": 200.0,
            "local_tokens": 90000,
            "external_tokens": dict(ZERO),
        },
        "local_direct": {
            "model": "qwen3-vl",
            "quality": 0.60,
            "quality_delta_ci_vs_teacher": (-0.30, -0.10),
            "agreement_with_teacher": 0.7,
            "wall_s": 150.0,
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
    return [arm(n, **over_by_arm.get(n, {})) for n in compare.ARMS]


def every_arm(**over):
    """The same override for all three arms (a round's shared conditions)."""
    return {n: dict(over) for n in compare.ARMS}


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
        for n in compare.ARMS
    ]


def rows(table):
    return {r["arm"]: r for r in table.rows}


def test_the_names_are_the_ones_of_the_external_report():
    assert compare.TOKEN_FIELDS == external_report.TOKEN_FIELDS


def test_three_arms_make_one_table_with_the_derived_columns():
    table = compare.compare(reversed(three()))  # the input order does not matter
    assert [r["arm"] for r in table.rows] == list(compare.ARMS)
    teacher, harness, direct = (rows(table)[a] for a in compare.ARMS)

    assert teacher["external_tokens_total"] == 1000 + 500 + 3000  # cache_read apart
    assert teacher["quality_per_second"] == pytest.approx(0.80 / 600)
    assert teacher["quality_per_external_token"] == pytest.approx(0.80 / 4500)
    assert harness["quality_per_second"] == pytest.approx(0.82 / 200)
    # Zero external tokens: the ratio has no denominator, and it is not "unknown".
    assert harness["quality_per_external_token"] is None
    assert "quality_per_external_token" in harness["undefined"]
    assert harness["unknown"] == []

    assert harness["vs_teacher"] == {
        "quality_delta": pytest.approx(0.02),
        "wall_s_delta": -400.0,
        "external_tokens_delta": -4500,
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
        "fewer_external_tokens": True,
        "all": True,
        "non_inferiority": {
            "higher_is_better": True,
            "margin": 0.05,
            "delta_ci": [-0.03, 0.07],
            "method": "paired_bootstrap_95",
        },
    }
    assert table.conditions["episode_count"] == 12
    assert table.missing_arms == []


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
    for name in compare.ARMS:
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
    assert data["conditions"]["external_token_basis"] == list(compare.FRESH_BASIS)
    assert [r["arm"] for r in data["rows"]] == list(compare.ARMS)
    for key in (
        "quality",
        "agreement_with_teacher",
        "wall_s",
        "local_tokens",
        "external_tokens",
        "quality_per_second",
        "quality_per_external_token",
        "vs_teacher",
        "vs_direct",
    ):
        assert key in data["rows"][0]


def test_a_missing_teacher_token_count_is_unknown_never_zero():
    table = compare.compare(three(teacher={"external_tokens": None}))
    teacher, harness, _ = (rows(table)[a] for a in compare.ARMS)
    assert teacher["external_tokens"] == dict.fromkeys(compare.TOKEN_FIELDS)
    assert teacher["external_tokens_total"] is None
    assert teacher["quality_per_external_token"] is None
    assert "quality_per_external_token" in teacher["unknown"]
    assert "external_tokens_total" in teacher["unknown"]
    # What needs the teacher's tokens is unknown too, and the headline cannot say "yes".
    assert harness["vs_teacher"]["external_tokens_delta"] is None
    assert "vs_teacher.external_tokens_delta" in harness["unknown"]
    assert table.main_metric["fewer_external_tokens"] is None
    assert table.main_metric["all"] is None

    text = table.to_markdown()
    teacher_line = next(
        line for line in text.splitlines() if line.startswith("| teacher | 0 |")
    )
    assert teacher_line.count("未知") == 5  # four fields and the total
    quality_line = next(
        line for line in text.splitlines() if line.startswith("| teacher | claude")
    )
    assert quality_line.endswith("| 未知 | — |")  # quality per external token
    assert "外部 token 更少：未知" in text
    assert "三项全满足：未知" in text


def test_unknown_and_zero_are_different_rows():
    zero = rows(compare.compare(three(teacher={"external_tokens": dict(ZERO)})))[
        "teacher"
    ]
    unknown = rows(compare.compare(three(teacher={"external_tokens": None})))["teacher"]
    assert zero["external_tokens"] == ZERO and unknown["external_tokens"] != ZERO
    assert zero["unknown"] == [] and zero["undefined"] == ["quality_per_external_token"]
    assert "quality_per_external_token" in unknown["unknown"]
    assert unknown["undefined"] == []


def test_one_missing_field_makes_the_total_unknown_only_if_the_basis_uses_it():
    base = {"input_new": 10, "output": 20, "cache_read": 30, "cache_write": 40}
    no_read = dict(base, cache_read=None)
    no_write = dict(base, cache_write=None)
    assert (
        rows(compare.compare([arm(external_tokens=no_read)]))["teacher"][
            "external_tokens_total"
        ]
        == 10 + 20 + 40
    )
    assert (
        rows(compare.compare([arm(external_tokens=no_write)]))["teacher"][
            "external_tokens_total"
        ]
        is None
    )
    wide = compare.compare(
        [arm(external_tokens=no_read)], token_basis=compare.TOKEN_FIELDS
    )
    assert rows(wide)["teacher"]["external_tokens_total"] is None
    only = compare.compare([arm(external_tokens=base)], token_basis=("output",))
    assert rows(only)["teacher"]["external_tokens_total"] == 20
    assert only.conditions["external_token_basis"] == ["output"]


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
    table = compare.compare(three(local_harness=over))
    harness = rows(table)["local_harness"]
    assert harness[field] is None
    assert {field, *unknown_columns} <= set(harness["unknown"])
    assert "未知" in table.to_markdown()
    assert (table.main_metric["all"] is None) == bool(unknown_columns)


def test_a_slower_or_worse_student_is_reported_as_no():
    table = compare.compare(
        three(
            local_harness={
                "quality": 0.7,
                # -0.10 against the teacher; even the better end is beyond -0.05
                "quality_delta_ci_vs_teacher": (-0.2, -0.06),
                "wall_s": 900.0,
                "external_tokens": {**ZERO, "output": 10000},
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
        three(local_harness=worse, teacher={"external_tokens": None})
    )
    assert partial.main_metric["all"] is False


def test_a_round_may_lack_arms():
    table = compare.compare([arm("teacher")])
    assert table.missing_arms == ["local_harness", "local_direct"]
    assert table.main_metric is None
    assert rows(table)["teacher"]["vs_direct"] is None
    assert "本轮缺少：local_harness, local_direct" in table.to_markdown()
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
            arm("local_harness", episode_set=None, episode_set_hash=a, episode_count=3),
        ]
    )


def test_records_over_different_problems_are_refused_and_the_difference_is_named():
    other = arm("local_harness", episode_set=EPISODES[:-1])
    with pytest.raises(CompareError) as refused:
        compare.compare([arm("teacher"), other, arm("local_direct")])
    text = str(refused.value)
    assert "episode_set_hash differs" in text
    assert "teacher=" in text and "local_harness=" in text
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
        compare.compare([arm("teacher"), arm("local_direct", **{field: value})])


def test_every_difference_is_listed_at_once():
    odd = arm(
        "local_direct",
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
    for basis in ((), ("output", "output"), ("bogus",)):
        with pytest.raises(CompareError, match="token_basis"):
            compare.compare([arm()], token_basis=basis)


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
        compare.compare(blind(local_harness={"per_episode": scores}))
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
            compare.compare([*two_blind, arm("local_direct", split=other, **hashed())])
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
        compare.compare(blind(local_harness={"per_episode": scores}))
    # In a tune round the same record is fine and the scores are passed through.
    table = compare.compare(three(local_harness={"per_episode": scores}))
    assert rows(table)["local_harness"]["per_episode"] == scores


def test_confirm_refuses_an_explicit_episode_list_but_takes_the_hash():
    assert compare.compare(blind()).conditions["episode_count"] == 12
    with pytest.raises(CompareError, match="explicit episode_set"):
        compare.compare(three(**every_arm(split="confirm")))


FREE = "episode 7 failed because the plate was upside down"


def test_confirm_takes_a_reason_code_but_not_free_text():
    with pytest.raises(CompareError, match="blocked text"):
        compare.compare(blind(local_harness={"blocked": FREE}))
    with pytest.raises(CompareError, match="external_tokens_source text"):
        compare.compare(
            blind(teacher={"external_tokens_source": "see session of episode 7"})
        )
    ok = compare.compare(blind(local_harness={"blocked": "gpu_busy"}))
    assert rows(ok)["local_harness"]["blocked"] == "gpu_busy"
    # The free text is fine in a tune round.
    compare.compare(three(local_harness={"blocked": FREE}))


@pytest.mark.parametrize("field", ["model", "quality_ci_method", "task"])
def test_confirm_checks_every_text_field_not_only_blocked_and_the_source(field):
    # model, task and the interval method were free text of up to 200
    # characters in a confirm table.
    over = {"quality_ci": (0.7, 0.9), "quality_ci_method": "bootstrap_95"}
    with pytest.raises(CompareError, match=f"{field} text"):
        compare.compare(blind(local_harness={**over, field: FREE}))
    compare.compare(blind(local_harness=over))  # codes are fine
    same = every_arm(**{field: FREE})  # free text is fine in a tune round
    same["local_harness"].update(over)
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
        compare.compare(blind(local_harness={field: value}))
    # A tune round takes the same JSON as written.
    compare.compare(three(**every_arm(**{field: value})))


def test_confirm_takes_flat_codes_numbers_and_booleans_in_decoding_and_scoring():
    decoding = {"temperature": 0, "top_p": 0.95, "seed": 1, "do_sample": False}
    table = compare.compare(blind(**{n: {"decoding": decoding} for n in compare.ARMS}))
    assert table.conditions["decoding"] == decoding
    assert "do_sample" in table.to_markdown()
    # The scoring's direction and margin are flat and pass.
    assert table.conditions["scoring"] == SCORING


def test_a_refusal_does_not_repeat_the_content_it_refused():
    canary = "plate-7-was-upside-down but this is longer than a code can be, 0123456789"
    both = {"decoding": {"notes": {"a": canary}}, "task": canary, "blocked": canary}
    with pytest.raises(CompareError) as refused:
        compare.compare(blind(local_harness=both))
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
        compare.compare(blind(local_harness={"decoding": {"temperature": 1}}))


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
        {"agreement_with_teacher": 1.2, "arm": "local_direct"},
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


def test_a_pipe_in_a_name_does_not_break_the_table():
    text = compare.compare([arm(model="a|b")]).to_markdown()
    assert "a\\|b" in text


def test_a_lower_bound_student_is_never_called_fewer_even_when_its_number_is_low():
    bound = {"external_tokens_exact": False, "external_tokens": {**ZERO, "output": 10}}
    table = compare.compare(three(local_harness=bound))
    assert rows(table)["local_harness"]["external_tokens_total"] == 10
    assert table.main_metric["fewer_external_tokens"] is None
    assert table.main_metric["all"] is None
    # An exact student at the same number is "fewer", and exact=None (not
    # stated) is not treated as a bound.
    exact = {"external_tokens_exact": True, "external_tokens": {**ZERO, "output": 10}}
    assert compare.compare(three(local_harness=exact)).main_metric[
        "fewer_external_tokens"
    ]
    assert compare.compare(
        three(local_harness={"external_tokens": {**ZERO, "output": 10}})
    ).main_metric["fewer_external_tokens"]
    # Above the teacher's total a bound is certain: still not "yes", and it
    # does not turn into "no" either.
    big = {"external_tokens_exact": False, "external_tokens": {**ZERO, "output": 9000}}
    assert (
        compare.compare(three(local_harness=big)).main_metric["fewer_external_tokens"]
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
    above = {"external_tokens": {**ZERO, "output": 6000}, "external_tokens_exact": True}
    over = compare.compare(three(teacher=teacher, local_harness=above))
    assert over.main_metric["fewer_external_tokens"] is None
    # With the teacher exact, the same numbers are a plain "no".
    exact = compare.compare(
        three(teacher={"external_tokens_exact": True}, local_harness=above)
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
    assert row["external_tokens_total"] == 5 + 415 + 9
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
    assert "教师–学生三组对比" in text and "local_direct" in text
    all_three = write_records(tmp_path, "all.json", [a, b, c])  # a list
    assert compare.main([all_three, "--format", "json"]) == 0
    assert [r["arm"] for r in json.loads(capsys.readouterr().out)["rows"]] == list(
        compare.ARMS
    )


def test_command_line_refuses_and_says_why(tmp_path, capsys):
    a = arm("teacher").to_dict()
    b = arm("local_harness", episode_set=EPISODES[:3]).to_dict()
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
    assert compare.main([leaky, "--basis", "bogus"]) == 2


def test_whole_floats_and_ints_are_one_value_in_decoding_and_scoring():
    # 0 and 0.0 are the same declared temperature; the arms must not be
    # refused for the way a number was typed.
    a = {"temperature": 0, "top_k": 1, "nested": {"p": 1.0}}
    b = {"temperature": 0.0, "top_k": 1.0, "nested": {"p": 1}}
    table = compare.compare(
        [arm("teacher", decoding=a), arm("local_direct", decoding=b)]
    )
    assert table.conditions["decoding"] == a
    assert arm(decoding=b).decoding == a and arm(decoding=b) == arm(decoding=a)
    # A real difference is still refused: 0.5 is not 0.
    with pytest.raises(CompareError, match="decoding differs"):
        compare.compare(
            [
                arm("teacher", decoding=a),
                arm("local_direct", decoding={**a, "temperature": 0.5}),
            ]
        )
    # nan and infinity are not JSON and are refused, not normalised.
    with pytest.raises(RecordError, match="plain JSON"):
        arm(decoding={"temperature": math.nan})


def test_a_difference_names_the_first_key_that_differs():
    base = {"temperature": 0, "seed": 1, "max_tokens": 512}
    other = {"temperature": 0.7, "seed": 2, "max_tokens": 512}
    with pytest.raises(CompareError) as refused:
        compare.compare(
            [arm("teacher", decoding=base), arm("local_direct", decoding=other)]
        )
    text = str(refused.value)
    # Keys are visited in sorted order: max_tokens agrees, seed is first to differ.
    assert "decoding differs at 'seed' (teacher=1, local_direct=2)" in text
    assert "temperature" not in text
    # Nested and one-sided keys are named by path.
    with pytest.raises(
        CompareError, match=r"at 'a.b' \(teacher=1, local_direct=<absent>"
    ):
        compare.compare(
            [
                arm("teacher", decoding={"a": {"b": 1}}),
                arm("local_direct", decoding={"a": {"c": 1}}),
            ]
        )
    # Each other arm is named against the first.
    with pytest.raises(CompareError) as three_way:
        compare.compare(
            [
                arm("teacher", scoring={**SCORING, "metric": "f1"}),
                arm(
                    "local_harness",
                    scoring={**SCORING, "metric": "f1", "reference": "panel"},
                ),
                arm("local_direct", scoring={**SCORING, "metric": "iou"}),
            ]
        )
    assert "at 'reference' (teacher=" in str(three_way.value)
    assert "at 'metric' (teacher=" in str(three_way.value)


def test_an_empty_decoding_is_refused_not_matched_with_everything():
    with pytest.raises(RecordError, match="decoding must be a non-empty dict"):
        arm(decoding={})


def test_the_direct_arm_must_be_the_same_model_as_the_harness_arm():
    with pytest.raises(CompareError, match="different models") as refused:
        compare.compare(three(local_direct={"model": "qwen3-vl-small"}))
    assert "qwen3-vl" in str(refused.value) and "R13" in str(refused.value)
    # The teacher is another model by definition, and a table without the
    # direct arm has nothing to compare it with.
    compare.compare(three())
    compare.compare([arm("teacher"), arm("local_harness")])
    # In a confirm table the model names are codes, so naming them is safe.
    with pytest.raises(CompareError, match="different models"):
        compare.compare(blind(local_direct={"model": "qwen3-vl-small"}))


def test_every_arm_must_have_scored_all_the_problems():
    # The hash and the count agree, but the harness arm was blocked on 2 of
    # the 12 and its quality is over the 10 it answered: not comparable.
    with pytest.raises(CompareError, match="local_harness scored 10 of 12") as refused:
        compare.compare(three(local_harness={"scored_count": 10}))
    assert "denominator" in str(refused.value)
    with pytest.raises(CompareError, match="teacher has a quality but no scored_count"):
        compare.compare(three(teacher={"scored_count": None}))
    # An arm with no quality (blocked, not measured) has nothing to be compared.
    blocked = three(
        local_harness={
            "quality": None,
            "quality_delta_ci_vs_teacher": None,
            "quality_delta_ci_method": None,
            "scored_count": None,
            "blocked": "gpu_busy",
        }
    )
    assert rows(compare.compare(blocked))["local_harness"]["quality"] is None
    assert arm(scored_count=12).to_dict()["scored_count"] == 12


def harness_vs_teacher(
    interval, *, quality=None, margin=0.05, higher=True, teacher=0.80
):
    """A round where only the paired interval of local_harness changes."""
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
            local_harness=harness,
            local_direct={"scoring": scoring, "quality_delta_ci_vs_teacher": None},
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
            local_harness={
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
    row = rows(ok)["local_harness"]
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
            arm("local_harness", quality_delta_ci_vs_teacher=bad)
    with pytest.raises(RecordError, match="teacher arm has no"):
        arm("teacher", quality_delta_ci_vs_teacher=(-0.1, 0.1))
    with pytest.raises(RecordError, match="needs this arm's quality"):
        arm("local_harness", quality=None, quality_delta_ci_vs_teacher=(-0.1, 0.1))
    with pytest.raises(RecordError, match="quality_delta_ci_method needs"):
        arm(
            "local_harness",
            quality_delta_ci_vs_teacher=None,
            quality_delta_ci_method="x",
        )
    # Confirm: the method is a code, the interval is two numbers.
    compare.compare(blind())
    with pytest.raises(CompareError, match="quality_delta_ci_method text"):
        compare.compare(blind(local_harness={"quality_delta_ci_method": FREE}))


def test_an_interval_that_excludes_its_own_point_difference_is_refused():
    # The classic slip is a reversed sign (teacher minus student). The harness
    # is +0.02 above the teacher; an interval of the opposite sign cannot be
    # that difference's own interval.
    with pytest.raises(CompareError, match="does not contain its own point difference"):
        compare.compare(
            three(
                local_harness={
                    "quality_delta_ci_vs_teacher": (-0.07, 0.03)
                    if False
                    else (-0.30, -0.05)
                }
            )
        )
