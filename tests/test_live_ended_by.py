"""How a dual-label episode ended (``eval.ended_by``): read from the rollout's
metadata into the operator label, and the agent-vs-operator agreement read
apart by it. An episode the operator's key ended early is shorter than an
unattended one, so only the ``budget`` ones carry over to unattended use."""

import json

import pytest
from test_live_stats_aggregate import full, labelled, verdict

from levi.live import criteria, stats, statsfmt

S, F = "success", "failure"


def meta(**ev):
    return {"eval": {"outcome": "unlabeled", "success_flag_final": 1, **ev}}


# --- reading it ---------------------------------------------------------------


@pytest.mark.parametrize("ended", ["budget", "operator_key"])
def test_a_known_ended_by_is_kept_in_the_operator_label(ended):
    got = criteria.operator_label(
        meta(operator_outcome=S, ended_by=ended, operator_label_timing="during_run")
    )
    assert got == {
        "outcome": S,
        "by": "operator",
        "source": "capture-metadata",
        "ended_by": ended,
    }
    # The same for the key-press source and for an unlabeled episode.
    keyed = criteria.operator_label(meta(outcome=F, verdict_by="key", ended_by=ended))
    assert keyed["ended_by"] == ended and keyed["by"] == "key"


@pytest.mark.parametrize("ended", [None, "timeout", "", 3, ["budget"]])
def test_a_missing_or_unknown_ended_by_leaves_the_label_as_it_was(ended):
    ev = {"operator_outcome": S}
    if ended is not None:
        ev["ended_by"] = ended
    assert criteria.operator_label(meta(**ev)) == {
        "outcome": S,
        "by": "operator",
        "source": "capture-metadata",
    }


def test_the_label_never_reads_the_success_flag_for_it():
    # ended_by does not make a label: without an operator outcome there is none.
    label = criteria.operator_label(meta(ended_by="budget"))
    assert label is None or label["outcome"] == "unlabeled"
    assert label is None or label["outcome"] != "success"
    assert criteria.operator_label({"success_flag_final": 1, "eval": None}) is None


def test_a_record_keeps_only_the_known_ended_by():
    assert stats.operator_brief({"outcome": S, "by": "operator"}) == {
        "outcome": S,
        "by": "operator",
    }
    assert stats.operator_brief(
        {"outcome": S, "by": "operator", "source": "x", "ended_by": "budget"}
    ) == {"outcome": S, "by": "operator", "ended_by": "budget"}
    assert "ended_by" not in stats.operator_brief(
        {"outcome": S, "by": "operator", "ended_by": "?"}
    )


# --- the strata ---------------------------------------------------------------


def pair(op, agent, ended=None):
    label = {"outcome": op, "by": "operator"}
    if ended:
        label["ended_by"] = ended
    return (label, agent)


def test_without_ended_by_the_agreement_is_what_it_was():
    found = stats.agreement([pair(S, verdict(S)), pair(F, verdict(S)), pair(S, None)])
    assert "by_ended_by" not in found
    assert found == stats.agreement([(S, verdict(S)), (F, verdict(S)), (S, None)])


def test_only_budget_episodes():
    found = stats.agreement(
        [pair(S, verdict(S), "budget"), pair(F, verdict(S), "budget")]
    )
    assert list(found["by_ended_by"]) == ["budget"]
    assert found["by_ended_by"]["budget"]["pairs"] == found["pairs"] == 2
    assert found["by_ended_by"]["budget"]["false_success"]["n"] == 1


def test_only_operator_key_episodes():
    found = stats.agreement([pair(S, verdict(F), "operator_key")])
    assert list(found["by_ended_by"]) == ["operator_key"]
    assert found["by_ended_by"]["operator_key"]["missed_success"]["n"] == 1


def test_a_mix_is_split_and_the_total_is_unchanged():
    mixed = [
        pair(S, verdict(S), "budget"),
        pair(F, verdict(F), "budget"),
        pair(F, verdict(S), "budget"),  # false success
        pair(S, verdict(F), "operator_key"),  # missed success
        pair(S, verdict(S), "operator_key"),
        pair(S, verdict(S, undecided=True), "operator_key"),
        pair(S, verdict(S)),  # no record of how it ended
        pair("unlabeled", verdict(S), "budget"),  # no pair at all
    ]
    found = stats.agreement(mixed)
    split = found["by_ended_by"]
    assert list(split) == ["budget", "operator_key", "unknown"]
    assert [split[k]["pairs"] for k in split] == [3, 3, 1]
    assert found["pairs"] == 7  # the total keeps every labelled episode
    b, k, u = split["budget"], split["operator_key"], split["unknown"]
    assert (b["agree"], b["judged"], b["false_success"]["n"]) == (2, 3, 1)
    assert (k["agree"], k["judged"], k["missed_success"]["n"]) == (1, 2, 2)
    assert k["undecided"] == 1 and u["agree"] == 1
    # The total is the same as without the split, in every other key.
    plain = stats.agreement([(a, b_) for a, b_ in mixed], _split=False)
    assert {x: found[x] for x in found if x != "by_ended_by"} == plain
    # A stratum carries no strata of its own.
    assert all("by_ended_by" not in part for part in split.values())


def test_a_missing_ended_by_is_unknown_and_never_an_error():
    found = stats.agreement(
        [(S, verdict(S)), ({"outcome": F}, verdict(F)), pair(S, verdict(S), "budget")]
    )
    assert found["pairs"] == 3
    assert found["by_ended_by"]["unknown"]["pairs"] == 2


def with_ended(row, ended):
    row["operator_label"] = {**row["operator_label"], "ended_by": ended}
    return row


def test_the_summary_the_episode_rows_and_the_csv_carry_it():
    rows = [
        with_ended(labelled("demo_0000", 0, S, verdict(S)), "budget"),
        with_ended(labelled("demo_0001", 1, S, verdict(F)), "operator_key"),
        labelled("demo_0002", 2, F, verdict(F)),
    ]
    rows = [stats.normalize(r) for r in rows]
    found = stats.summarize(rows)["agreement"]
    assert found["pairs"] == 3
    assert {k: v["pairs"] for k, v in found["by_ended_by"].items()} == {
        "budget": 1,
        "operator_key": 1,
        "unknown": 1,
    }
    episodes = {r["demo"]: r for r in stats.episode_rows(rows)}
    assert episodes["demo_0000"]["ended_by"] == "budget"
    assert episodes["demo_0001"]["ended_by"] == "operator_key"
    assert episodes["demo_0002"]["ended_by"] is None
    # The columns the table always had keep their places; ended_by is appended.
    assert list(episodes["demo_0000"])[-3:] == ["ended_by", "operator", "agreement"]
    assert statsfmt.EPISODE_COLUMNS[-3:] == ("operator", "agreement", "ended_by")
    table = statsfmt.to_csv({"episodes": {"rows": list(episodes.values())}})
    header, first, *_ = table.splitlines()
    cols = header.split(",")
    assert cols[-1] == "ended_by" and first.split(",")[-1] == "budget"


# --- the report ---------------------------------------------------------------


def block(rows, lang="en"):
    found = stats.summarize([stats.normalize(r) for r in rows])["agreement"]
    t = statsfmt.TEXT[lang]
    return "\n".join(statsfmt.agreement_block(found, t))


def test_the_report_adds_the_split_only_when_both_kinds_exist():
    both = [
        with_ended(labelled("demo_0000", 0, S, verdict(S)), "budget"),
        with_ended(labelled("demo_0001", 1, S, verdict(F)), "operator_key"),
    ]
    for lang, heading, early in (
        ("en", "### How the episode ended", "ended early by the operator's key"),
        ("zh", "### 片段怎么结束的", "操作员按键提前结束"),
    ):
        text = block(both, lang)
        assert heading in text and early in text
    text = block(both)
    assert "ran the whole budget (carries over) | 1 | 1/1 (100%)" in text
    assert "| ended early by the operator's key | 1 | 0/1 (0%)" in text
    # Budget alone, or no label saying how it ended: the block is what it was.
    only = [with_ended(labelled("demo_0000", 0, S, verdict(S)), "budget")]
    plain = [labelled("demo_0000", 0, S, verdict(S))]
    for rows in (only, plain):
        assert "How the episode ended" not in block(rows)
    assert block(only) == block(plain)
    # A run in which every episode ended on the operator's key still says so:
    # its agreement does not carry over to unattended use.
    keys = [with_ended(labelled("demo_0000", 0, S, verdict(S)), "operator_key")]
    assert "How the episode ended" in block(keys)


def test_a_report_made_before_the_split_renders_unchanged():
    old = stats.summarize([stats.normalize(full("demo_0000", 0))])
    assert "by_ended_by" not in old["agreement"]
    assert statsfmt.agreement_block(old["agreement"], statsfmt.TEXT["en"]) == []
    assert json.dumps(old["agreement"])
