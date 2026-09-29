"""Training pool: how many episodes a task contributes and which ones."""

import json
import random

import pandas as pd
import pytest

from levi.pool import cli, recipe, select, settings
from levi.pool.recipe import Recipe, TaskEntry

COLUMNS = [
    "key", "source", "source_path", "format", "episode", "episode_index", "task",
    "task_raw", "frames", "category", "robot_flag", "human_label", "label_conflict",
    "outcome", "outcome_source", "policy", "policy_model", "policy_checkpoint",
    "policy_method", "policy_phase", "policy_label", "date", "heldout", "heldout_id",
    "canonical", "nonstandard", "nonstandard_reason", "exportable",
    "not_exportable_reason", "group", "in_levi_workspace", "filtered",
]  # fmt: skip


def ep(key, task="t", frames=100, outcome="success", *, source="s", method="direct",
       checkpoint="c1", date="2026-09-01", by="robot_flag", **extra):  # fmt: skip
    """One index row."""
    row = dict.fromkeys(COLUMNS)
    row.update(
        key=key, source=source, source_path=f"/pool/{source}", format="robot_capture",
        episode=key, episode_index=None, task=task, task_raw=task, frames=frames,
        category="rollout", label_conflict=False, outcome=outcome,
        outcome_source=by if outcome else None, policy_method=method,
        policy_checkpoint=checkpoint, date=date, heldout=False, canonical=True,
        nonstandard=False, exportable=True, group=key, in_levi_workspace=False,
        filtered=False, robot_flag=outcome if by == "robot_flag" else None,
        human_label=outcome if by == "human" else None,
    )  # fmt: skip
    row.update(extra)
    return row


def frame(rows):
    return pd.DataFrame(rows, columns=COLUMNS)


def entry(task="t", **kw):
    return {"task": task, **kw}


def run(rows, tasks, **kw):
    rec = Recipe(name="r", tasks=tasks, **kw)
    return recipe.select_detailed(rec, frame(rows))


def keys(result, task=None):
    return [r["key"] for r in result.chosen if task in (None, r["task"])]


def pool_rows(successes=60, failures=40, **kw):
    rows = [ep(f"s{i:03d}", frames=90 + i, **kw) for i in range(successes)]
    rows += [
        ep(f"f{i:03d}", frames=100 + i, outcome="failure", **kw)
        for i in range(failures)
    ]
    return rows  # fmt: skip


# ------------------------------------------------------------------ quota


def test_natural_ratio_is_kept_and_an_explicit_ratio_is_met():
    rows = pool_rows(60, 40)
    natural = run(rows, [entry(count=20)])
    report = natural.tasks["t"]
    assert (report["selected"], report["selected_successes"]) == (20, 12)
    assert report["selected_failures"] == 8 and report["shortfall"] == 0
    half = run(rows, [entry(count=20, success_ratio=0.5)]).tasks["t"]
    assert (half["selected_successes"], half["selected_failures"]) == (10, 10)
    allfail = run(rows, [entry(count=15, success_ratio=0)]).tasks["t"]
    assert (allfail["selected_successes"], allfail["selected_failures"]) == (0, 15)
    everything = run(rows, [entry()]).tasks["t"]
    assert everything["selected"] == 100 and everything["requested"] is None


def test_a_short_side_is_filled_from_the_other_and_reported():
    rows = pool_rows(50, 6)
    report = run(rows, [entry(count=20, success_ratio=0.5)]).tasks["t"]
    # 10 failures wanted, 6 exist: 4 more successes make up the count.
    assert report["selected"] == 20
    assert (report["selected_successes"], report["selected_failures"]) == (14, 6)
    assert report["shortfall_failures"] == 4 and report["shortfall_successes"] == 0
    assert "failure_short" in report["notes"] and report["note"]
    few = run(pool_rows(3, 2), [entry(count=20)]).tasks["t"]
    assert few["selected"] == 5 and few["shortfall"] == 15
    assert "fewer_available" in few["notes"]


def test_unknown_outcomes_only_fill_what_cannot_be_met_otherwise():
    rows = pool_rows(5, 5) + [ep(f"u{i}", outcome=None) for i in range(6)]
    small = run(rows, [entry(count=8, success_ratio=0.5)]).tasks["t"]
    assert small["selected_unknown"] == 0 and small["unknown"] == 6
    big = run(rows, [entry(count=14, success_ratio=0.5)]).tasks["t"]
    assert big["selected"] == 14 and big["selected_unknown"] == 4
    assert "unknown_outcomes_used" in big["notes"]
    # A task with no outcome at all (human demonstrations) is taken as it is.
    demos = run([ep(f"d{i}", outcome=None) for i in range(10)], [entry(count=4)])
    assert (
        demos.tasks["t"]["selected"] == 4 and demos.tasks["t"]["selected_unknown"] == 4
    )


def test_conflicting_labels_are_a_last_resort_for_quality_picks():
    rows = pool_rows(4, 0)
    rows.append(ep("bad", label_conflict=True))
    picked = keys(run(rows, [entry(count=4)]))
    assert "bad" not in picked
    assert "bad" in keys(run(rows, [entry(count=5)]))


# ------------------------------------------------------------------ quality


def test_successes_prefer_efficient_episodes_over_the_longest():
    rows = [ep(f"s{i:02d}", frames=100 + 10 * i) for i in range(20)]
    picked = keys(run(rows, [entry(count=5)]))
    lengths = sorted(int(k[1:]) for k in picked)
    assert max(lengths) < 12  # never the slow upper half
    assert "s19" not in picked and "s18" not in picked


def test_failures_need_length_for_an_attempt_and_odd_lengths_lose():
    rows = [ep(f"s{i}", frames=100) for i in range(10)]
    rows += [ep("f_tiny", frames=3, outcome="failure")]
    rows += [ep("f_huge", frames=900, outcome="failure")]
    rows += [ep(f"f{i}", frames=95 + i, outcome="failure") for i in range(4)]
    picked = keys(run(rows, [entry(count=8, success_ratio=0.5)]))
    failures = [k for k in picked if k.startswith("f")]
    assert len(failures) == 4 and not {"f_tiny", "f_huge"} & set(failures)
    everything = keys(run(rows, [entry(count=len(rows))]))
    assert "f_tiny" in everything  # count == available takes them all


def test_a_human_label_beats_the_robot_flag():
    rows = [ep(f"r{i}", frames=100) for i in range(5)]
    rows += [ep(f"h{i}", frames=100, by="human") for i in range(3)]
    assert set(keys(run(rows, [entry(count=3)]))) == {"h0", "h1", "h2"}
    scores = select.score_rows([r for r in rows])
    assert scores["h0"]["score"] > scores["r0"]["score"]


def test_recap_labels_only_break_ties():
    rows = [ep(f"s{i}", frames=100) for i in range(6)]
    rec = Recipe(name="r", tasks=[entry(count=2)])
    plain = recipe.select_detailed(rec, frame(rows))
    assert keys(plain) == ["s0", "s1"]
    scores = select.score_rows(rows)
    picked, _ = select.choose(
        [dict(r) for r in rows], task="t", count=2, success_ratio=None,
        strategy="quality", seed=0, order_key=lambda r: r["key"],
        recap={"s4": 0.9, "s5": 0.8, "s0": 0.1},
    )  # fmt: skip
    assert [r["key"] for r in picked] == ["s4", "s5"]
    assert scores["s0"]["score"] == scores["s5"]["score"]


# ------------------------------------------------------------------ diversity


def test_picks_spread_over_methods_checkpoints_and_dates():
    rows = []
    for method, cp in (("direct", "a"), ("dsrl", "b"), ("sfe", "c")):
        rows += [ep(f"{method}{i}", method=method, checkpoint=cp, source=method,
                    frames=100 + (i if method == "direct" else 40 + i)) for i in range(30)]  # fmt: skip
    result = run(rows, [entry(count=9, success_ratio=1)])
    counts = {}
    for r in result.chosen:
        counts[r["policy_method"]] = counts.get(r["policy_method"], 0) + 1
    assert counts == {"direct": 3, "dsrl": 3, "sfe": 3}
    assert all(r["sel_stratum"].startswith(r["policy_method"]) for r in result.chosen)
    dated = [ep(f"a{i}", date="2026-08-01") for i in range(10)]
    dated += [ep(f"b{i}", date="2026-09-01", frames=104) for i in range(10)]
    months = {r["date"] for r in run(dated, [entry(count=4)]).chosen}
    assert months == {"2026-08-01", "2026-09-01"}


def test_an_episode_an_earlier_task_took_is_not_taken_again():
    rows = [ep(f"x{i}", task="a") for i in range(6)]
    picked, report = select.choose(
        rows, task="b", count=6, success_ratio=None, strategy="quality", seed=0,
        order_key=lambda r: r["key"], taken={"x0", "x1"},
    )  # fmt: skip
    assert {r["key"] for r in picked} == {"x2", "x3", "x4", "x5"}
    assert report["available"] == 4 and report["already_used"] == 2
    # A copy (same group) is as good as the episode itself.
    grouped = [ep("y0", group="g"), ep("y1", group="g2")]
    picked, _ = select.choose(
        grouped, task="b", count=2, success_ratio=None, strategy="quality", seed=0,
        order_key=lambda r: r["key"], taken={"g"},
    )  # fmt: skip
    assert [r["key"] for r in picked] == ["y1"]


def test_tasks_of_a_recipe_never_share_an_episode():
    rows = [ep(f"a{i}", task="a") for i in range(5)]
    rows += [ep(f"b{i}", task="b", frames=100 + i) for i in range(5)]
    result = run(rows, [entry("a", count=3), entry("b", count=3)])
    assert len(set(keys(result))) == 6
    assert {r["task"] for r in result.chosen} == {"a", "b"}
    assert [r["task"] for r in result.chosen] == ["a"] * 3 + ["b"] * 3


# ------------------------------------------------------------------ strategies


def test_selection_is_deterministic_and_the_seed_matters_for_random():
    rows = pool_rows(50, 50)
    for strategy in ("quality", "random", "first"):
        spec = [entry(count=20, success_ratio=0.5, strategy=strategy)]
        first = keys(run(rows, spec, seed=3))
        assert first == keys(run(list(reversed(rows)), spec, seed=3))
    spec = [entry(count=20, strategy="random")]
    assert keys(run(rows, spec, seed=1)) != keys(run(rows, spec, seed=2))
    assert keys(run(rows, [entry(count=5, strategy="first")])) == [
        "f000", "f001", "f002", "f003", "f004",
    ]  # fmt: skip
    firsts = run(rows, [entry(count=6, success_ratio=0.5, strategy="first")])
    assert sorted(keys(firsts)) == ["f000", "f001", "f002", "s000", "s001", "s002"]


def test_the_old_form_means_what_it_meant():
    rows = pool_rows(30, 30)
    assert Recipe(name="r", tasks=["Some Task"]).tasks[0].task == "some task"
    old = run(rows, ["t"], per_task_cap=10, seed=7)
    ordered = sorted(r["key"] for r in rows)
    expected = random.Random("7:t").sample(ordered, 10)
    assert sorted(keys(old)) == sorted(expected)
    assert old.tasks["t"]["strategy"] == "random"
    # An entry's own count wins; without one the global cap is the fallback.
    rec = Recipe(name="r", tasks=[entry(count=4), "u"], per_task_cap=9)
    assert rec.task_count("t") == 4 and rec.task_count("u") == 9
    with pytest.raises(ValueError):
        Recipe(name="r", tasks=["a", entry("A")])
    with pytest.raises(ValueError):
        Recipe(name="r", tasks=[entry(count=0)])
    with pytest.raises(ValueError):
        Recipe(name="r", tasks=[entry(success_ratio=1.5)])
    with pytest.raises(ValueError):
        Recipe(name="r", tasks=[entry(strategy="best")])


def test_saved_old_recipes_load_and_listing_migrates_them(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVI_WORKSPACE", str(tmp_path))
    folder = settings.pool_dir() / "recipes"
    folder.mkdir(parents=True)
    (folder / "old.json").write_text(
        json.dumps(
            {
                "name": "old",
                "tasks": ["a task", "b task"],
                "per_task_cap": 5,
                "saved_at": "2026-09-01T00:00:00+0800",
            }
        )
    )
    loaded = recipe.load("old")
    assert [t.task for t in loaded.tasks] == ["a task", "b task"]
    assert loaded.tasks[0] == TaskEntry(task="a task", strategy="random")
    listed = recipe.listing()[0]
    assert listed["tasks"][0]["task"] == "a task" and listed["saved_at"]


# ------------------------------------------------------------------ preview


def test_preview_reports_per_task_numbers_and_the_mix():
    rows = pool_rows(60, 40) + [
        ep(f"o{i}", task="u", category="human", outcome=None) for i in range(30)
    ]
    rec = Recipe(
        name="r", tasks=[entry(count=20, success_ratio=0.5), entry("u", count=10)]
    )
    view = recipe.preview(rec, df=frame(rows))
    first, second = view["tasks"]
    assert (first["available"], first["successes"], first["failures"]) == (100, 60, 40)
    assert (
        first["selected"],
        first["selected_successes"],
        first["selected_failures"],
    ) == (20, 10, 10)
    assert second["selected_unknown"] == 10 and second["unknown"] == 30
    assert view["episodes"] == 30 and view["mix"]["successes"] == 10
    assert view["mix"]["success_share"] == 0.5 and view["mix"]["categories"] == {"rollout": 20, "human": 10}  # fmt: skip
    assert view["excluded"]["not_selected"] == 100
    assert view["task_reports"][0]["task"] == "t"


def test_suggested_count_keeps_the_composition_balanced():
    assert select.suggest_count([], 500) == 100
    assert select.suggest_count([], 30) == 30
    assert select.suggest_count([40, 60, 80], 500) == 60
    assert select.suggest_count([40, 60, 80], 25) == 25
    rows = pool_rows(120, 80) + [ep(f"n{i}", task="new") for i in range(300)]
    rec = Recipe(name="r", tasks=[entry(count=50)])
    tip = recipe.suggest(rec, "New", df=frame(rows))
    assert tip["suggested_count"] == 50 and tip["available"] == 300
    assert tip["earlier_counts"] == [50] and tip["successes"] == 300
    fresh = recipe.suggest(Recipe(name="r"), "t", df=frame(rows))
    assert fresh["suggested_count"] == 100


def test_selected_episodes_list_score_stratum_and_reason():
    rows = pool_rows(20, 20)
    rec = Recipe(name="r", tasks=[entry(count=6, success_ratio=0.5)])
    view = recipe.selected_episodes(rec, "t", df=frame(rows))
    assert len(view["episodes"]) == 6 and view["report"]["selected"] == 6
    first = view["episodes"][0]
    assert 0 < first["quality_score"] <= 1 and first["sel_stratum"].startswith("direct")
    assert "robot_flag" in first["selection_reason"]
    assert set(first["selection_parts"]) == {"trust", "completeness", "fit"}
    with pytest.raises(KeyError):
        recipe.selected_episodes(rec, "missing", df=frame(rows))


# ------------------------------------------------------------------ CLI


def test_task_spec_syntax():
    assert recipe.parse_task_spec("stack the plates") == "stack the plates"
    assert recipe.parse_task_spec("put x: then y") == "put x: then y"
    assert recipe.parse_task_spec("a, b:count=50,success=0.6,strategy=quality") == {
        "task": "a, b", "count": 50, "success_ratio": 0.6, "strategy": "quality",
    }  # fmt: skip
    got = recipe.parse_task_spec("a:count=all,success=60%,strategy=first")
    assert got == {
        "task": "a",
        "count": None,
        "success_ratio": 0.6,
        "strategy": "first",
    }
    assert recipe.parse_task_spec("a:success=natural") == {"task": "a", "success_ratio": None}  # fmt: skip
    # Unknown keys are text, not options.
    assert recipe.parse_task_spec("a:size=3") == "a:size=3"
    rec = Recipe(
        name="r",
        tasks=[recipe.parse_task_spec("A:count=5"), recipe.parse_task_spec("b")],
    )
    assert rec.tasks[0].strategy == "quality" and rec.tasks[1].strategy == "random"
    args = cli.build_parser().parse_args(
        ["recipe", "save", "r", "--task", "a:count=5,success=0.5"]
    )
    assert args.task == ["a:count=5,success=0.5"]
