"""Training pool: hold-back lists (episodes set aside for now) and verified
outcomes (an independent check of a rollout's result).

Both are JSON lists named by ``LEVI_POOL_HOLDBACK`` / ``LEVI_POOL_OUTCOMES``.
A held-back episode is indexed and listed, but no recipe picks it and no
export carries it unless the recipe says ``include_holdback``; held-out still
wins. A verified outcome ranks below a human label and above the robot's key
press."""

import json
import shutil
from pathlib import Path

import pandas as pd
import pytest
from test_pool import make_demo

from levi.pool import (
    cli,
    export,
    index,
    jobs,
    journal,
    manifest,
    recipe,
    scanner,
    select,
)
from levi.pool.recipe import Recipe

ROLLOUTS = "rollouts/models/pi05_test/stack_the_plates"
TASK = "stack the plates"
# demo_0000 .. demo_0005: the robot's flag of each rollout.
FLAGS = ["failure", "failure", "success", "success", "failure", "success"]


@pytest.fixture(scope="module")
def root(tmp_path_factory):
    """Six rollouts of one task, two byte copies in an evaluation workspace
    (of demo_0001 and demo_0003) and a human label (failure) on demo_0002."""
    root = tmp_path_factory.mktemp("holdback") / "wenkai"
    task = root / ROLLOUTS
    for i, flag in enumerate(FLAGS):
        make_demo(
            task / f"demo_{i:04d}",
            rollout=flag,
            task=TASK,
            created=f"2026-09-1{i}T10:00:00",
        )
    evalws = root / "evalws"
    shutil.copytree(task / "demo_0001", evalws / "plates_a/demo_0000")
    shutil.copytree(task / "demo_0003", evalws / "plates_b/demo_0000")
    (evalws / "outputs/LEVI/workbench").mkdir(parents=True)
    (evalws / "outputs/LEVI/workbench/datasets.json").write_text("{}")
    levi_ws = root / "LEVI/.state"
    view = levi_ws / "outputs/LEVI/workbench/views/plates"
    (view / "meta").mkdir(parents=True)
    (view / "meta/episodes.jsonl").write_text(
        json.dumps({"episode_index": 0, "source_demo": "stack_the_plates/demo_0002"})
        + "\n"
    )
    (levi_ws / "outputs/LEVI/workbench/datasets.json").write_text(
        json.dumps(
            {
                "plates": {
                    "name": "plates",
                    "kind": "raw",
                    "path": str(root / "rollouts/models/pi05_test"),
                    "view": str(view),
                }
            }
        )
    )
    label = levi_ws / "outputs/LEVI/workbench/annotations/plates/outcomes"
    label.mkdir(parents=True)
    (label / "episode_000000.json").write_text(
        json.dumps({"episode_index": 0, "outcome": "failure", "source": "human"})
    )
    return root


def key(root, n):
    return str(root / ROLLOUTS / f"demo_{n:04d}")


def holdback_value(root):
    return {
        "name": "parked-0610",
        "note": "set aside until the camera is re-checked",
        "episodes": [
            {"path": f"{ROLLOUTS}/demo_0001"},  # relative to the pool root
            {"path": key(root, 4)},  # absolute
            {"path": f"{ROLLOUTS}/demo_9999"},  # not in the pool
        ],
    }


def outcomes_value():
    return {
        "name": "agent-check",
        "source": "agent-verified",
        "note": "independent reading of the videos",
        "episodes": [
            # The flag says failure; the check says success.
            {"path": f"{ROLLOUTS}/demo_0000", "outcome": "success", "basis": "A=B"},
            # A human labelled demo_0002 a failure: the label stands.
            {"path": f"{ROLLOUTS}/demo_0002", "outcome": "success", "basis": "C"},
            # Named through a copy: it applies to the recording.
            {
                "path": "evalws/plates_b/demo_0000",
                "outcome": "failure",
                "basis": "A=B / C",
                "evidence": "the cube slides off",
            },
            {"path": f"{ROLLOUTS}/demo_0004", "outcome": "unknown", "basis": "split"},
            {"path": f"{ROLLOUTS}/demo_9999", "outcome": "success"},
        ],
    }


@pytest.fixture
def pool(root, tmp_path, monkeypatch):
    """``build(holdback=True, outcomes=True, heldout=None)`` scans the pool in
    a fresh workspace with the lists the test asks for."""

    def build(holdback=True, outcomes=True, heldout=None):
        lists = tmp_path / "lists"
        lists.mkdir(exist_ok=True)
        monkeypatch.setenv("LEVI_WORKSPACE", str(tmp_path / "ws"))
        monkeypatch.setenv("LEVI_POOL_ROOTS", str(root))
        monkeypatch.setenv("LEVI_EXPORT_ROOTS", str(tmp_path))
        monkeypatch.setattr(export, "_levi_commit", lambda: "test")
        for env, value, name in (
            ("LEVI_POOL_HOLDBACK", holdback_value(root) if holdback else None, "hb"),
            ("LEVI_POOL_OUTCOMES", outcomes_value() if outcomes else None, "oc"),
            ("LEVI_POOL_HELDOUT", heldout or {"episodes": []}, "ho"),
        ):
            if value is None:
                monkeypatch.delenv(env, raising=False)
                continue
            path = lists / f"{name}.json"
            path.write_text(json.dumps(value))
            monkeypatch.setenv(env, str(path))
        if heldout is None:
            monkeypatch.setenv("LEVI_POOL_HELDOUT", "none")
        summary = scanner.scan()
        return {
            "root": root,
            "out": tmp_path,
            "lists": lists,
            "summary": summary,
        }

    return build


def listed(**filters):
    return {
        r["key"]: r
        for r in index.episodes(limit=1000, show_copies=True, **filters)["episodes"]
    }


def rollout_recipe(**kw):
    return Recipe(name="r", categories=["rollout"], tasks=[TASK], **kw)


# ------------------------------------------------------------ hold-back: index


def test_holdback_marks_the_whole_group_and_stays_visible(pool):
    built = pool()
    root = built["root"]
    block = built["summary"]["holdback"]
    assert block["entries"] == 3 and block["matched"] == 2
    assert block["unmatched_count"] == 1 and block["unmatched"] == [
        f"{ROLLOUTS}/demo_9999"
    ]
    # Two recordings: demo_0001 with its copy, and demo_0004.
    assert block["groups"] == 2 and block["episodes"] == 3
    assert [d["path"].endswith("hb.json") for d in block["lists"]] == [True]
    assert block["lists"][0]["sha256"]
    rows = listed()
    held = {k for k, r in rows.items() if r["holdback"]}
    assert held == {
        key(root, 1),
        key(root, 4),
        str(root / "evalws/plates_a/demo_0000"),
    }
    assert {r["holdback_set"] for k, r in rows.items() if k in held} == {"parked-0610"}
    assert all(r["holdback_set"] is None for k, r in rows.items() if k not in held)
    # Visible by default; a filter lists only them, or none of them.
    assert key(root, 1) in listed() and len(listed()) == len(rows)
    assert set(listed(holdback="only")) == held
    assert not set(listed(holdback="hide")) & held
    assert index.facets()["holdback"] == 2  # canonical episodes only
    assert index.facets()["holdback_unmatched"] == 1  # demo_9999 holds nothing back


def test_without_a_setting_nothing_is_held_back(pool):
    built = pool(holdback=False, outcomes=False)
    assert built["summary"]["holdback"]["entries"] == 0
    assert not any(r["holdback"] for r in listed().values())
    assert index.facets()["holdback"] == 0
    assert index.facets()["holdback_unmatched"] == 0
    # No warning, and an export is planned as before.
    view = recipe.preview(rollout_recipe())
    assert not [w for w in view["warnings"] if "holdback" in w["code"]]
    assert view["episodes"] == 6 and view["excluded_holdback"] == 0
    assert view["holdback"] == {
        "lists": [],
        "include": False,
        "left_out": 0,
        "included": 0,
    }


# --------------------------------------------------------- hold-back: recipes


def test_a_recipe_skips_held_back_episodes_unless_it_includes_them(pool):
    root = pool()["root"]
    view = recipe.preview(rollout_recipe())
    assert view["episodes"] == 4 and view["excluded_holdback"] == 2
    assert view["excluded"]["held_back"] == 2
    assert view["holdback"]["left_out"] == 2 and view["holdback"]["include"] is False
    result = recipe.select_detailed(rollout_recipe())
    why = {e["key"]: e for e in result.excluded}
    assert why[key(root, 1)]["reason"] == "held_back"
    assert why[key(root, 4)]["holdback_set"] == "parked-0610"
    assert not {key(root, 1), key(root, 4)} & {r["key"] for r in result.chosen}
    # Naming the copy's source does not get around it: the whole group is held.
    copy = recipe.preview(Recipe(name="c", sources=["evalws/plates_a"]))
    assert copy["episodes"] == 0 and copy["excluded"] == {"held_back": 1}
    # Opting in lets them through, with a note.
    opened = recipe.preview(rollout_recipe(include_holdback=True))
    assert opened["episodes"] == 6 and opened["excluded_holdback"] == 0
    assert opened["holdback"]["include"] and opened["holdback"]["included"] == 2
    assert any(w["code"] == "holdback_included" for w in opened["warnings"])
    assert (
        recipe.preview(
            Recipe(name="c", sources=["evalws/plates_a"], include_holdback=True)
        )["episodes"]
        == 1
    )


def test_held_out_wins_over_held_back(pool, root):
    heldout = {"episodes": [{"path": f"{ROLLOUTS}/demo_0001"}]}
    pool(heldout=heldout)
    for include in (False, True):
        result = recipe.select_detailed(rollout_recipe(include_holdback=include))
        why = {e["key"]: e["reason"] for e in result.excluded}
        assert why[key(root, 1)] == "heldout"
        assert key(root, 1) not in {r["key"] for r in result.chosen}
    # The held-back one that is not held out comes back when included.
    assert recipe.preview(rollout_recipe(include_holdback=True))["episodes"] == 5
    # An export of the held-out episode is refused even with include_holdback.
    with pytest.raises(PermissionError, match="held-out"):
        export.refuse_heldout_groups([{"key": key(root, 1)}])


def test_a_recipe_without_the_field_loads_as_before():
    saved = rollout_recipe().model_dump()
    saved.pop("include_holdback")
    assert Recipe.model_validate(saved).include_holdback is False


# ----------------------------------------------------------- hold-back: exports


def test_an_export_refuses_a_held_back_episode_on_every_route(pool):
    built = pool()
    root = built["root"]
    lists = [Path(d["path"]) for d in built["summary"]["holdback"]["lists"]]
    copy = str(root / "evalws/plates_a/demo_0000")
    # By path, independent of the index.
    with pytest.raises(PermissionError, match="held-back"):
        export.refuse_holdback([{"key": key(root, 4)}], [root], lists)
    with pytest.raises(PermissionError, match="held-back"):
        export.refuse_holdback([{"key": key(root, 1)}], [root], lists)
    export.refuse_holdback([{"key": key(root, 0)}], [root], lists)
    export.refuse_holdback([{"key": key(root, 4)}], [root], lists, allow=True)
    # By group, from the index: the copy is held back with its original.
    with pytest.raises(PermissionError, match="held-back"):
        export.refuse_holdback_groups([{"key": copy}])
    with pytest.raises(PermissionError, match="held-back"):
        export.refuse_holdback_groups([{"key": "elsewhere", "group": key(root, 1)}])
    export.refuse_holdback_groups([{"key": copy}], allow=True)
    export.refuse_holdback_groups([{"key": key(root, 0)}])

    options = export.ExportOptions(
        format="raw_capture", name="plain", output_dir=str(built["out"] / "exports")
    )
    job = jobs.plan_export(rollout_recipe(), options)
    assert len(job["episodes"]) == 4
    assert job["holdback_lists"] == [str(p) for p in lists]
    assert not any(e["holdback"] for e in job["episodes"])
    # A plan edited by hand to carry a held-back episode is refused at run.
    row = dict(job["episodes"][0])
    row.update(key=key(root, 4), group=key(root, 4))
    job["episodes"].append(row)
    with pytest.raises(PermissionError, match="held-back"):
        export.run(job)
    assert not (built["out"] / "exports/plain").exists()
    # A recipe that sets include_holdback plans and exports them.
    options = export.ExportOptions(
        format="raw_capture", name="open", output_dir=str(built["out"] / "exports")
    )
    job = jobs.plan_export(rollout_recipe(include_holdback=True), options)
    assert len(job["episodes"]) == 6
    jobs.execute(job)
    record = json.loads((built["out"] / "exports/open/pool_export.json").read_text())
    assert record["holdback"] == {
        "lists": job["holdback_lists"],
        "include": True,
        "left_out": 0,
        "exported": 2,
    }
    by_name = {Path(e["source_path"]).name: e for e in record["episodes"]}
    assert by_name["demo_0004"]["holdback"] and by_name["demo_0004"]["holdback_set"]
    assert not by_name["demo_0000"]["holdback"]


def test_the_default_export_records_what_it_left_out(pool):
    built = pool()
    options = export.ExportOptions(
        format="raw_capture", name="left", output_dir=str(built["out"] / "exports")
    )
    jobs.execute(jobs.plan_export(rollout_recipe(), options))
    record = json.loads((built["out"] / "exports/left/pool_export.json").read_text())
    assert record["holdback"]["include"] is False
    assert record["holdback"]["left_out"] == 2 and record["holdback"]["exported"] == 0
    assert record["counts"]["excluded"]["held_back"] == 2
    assert len(record["episodes"]) == 4
    # Everything the recipe selects is held back: nothing to export.
    with pytest.raises(ValueError, match="no exportable"):
        jobs.plan_export(
            Recipe(name="c", sources=["evalws/plates_a"]),
            export.ExportOptions(
                format="raw_capture", name="none", output_dir=str(built["out"])
            ),
        )


def test_a_resume_refuses_an_episode_held_back_since_the_plan(
    pool, monkeypatch, root, tmp_path
):
    from test_pool_resume import _interrupt_after

    from levi.pool import joblog

    built = pool(holdback=False, outcomes=False)
    monkeypatch.setenv("LEVI_POOL_BATCH_EPISODES", "2")
    options = export.ExportOptions(
        format="lerobot_v21",
        name="cut",
        output_dir=str(built["out"] / "exports"),
        timing="retime",
        filter_static=False,
        workers=1,
    )
    job = jobs.plan_export(rollout_recipe(), options)
    assert job["holdback_lists"] == []
    real = _interrupt_after(monkeypatch, 1)
    jobs.run_worker(jobs._path(job["id"]))
    monkeypatch.setattr(export.pipeline, "run", real)
    joblog.TERMINATING.clear()
    assert (built["out"] / "exports/.cut.partial").is_dir()
    # A person now sets one of its episodes aside and the pool is scanned.
    listing = built["lists"] / "late.json"
    listing.write_text(json.dumps({"episodes": [{"path": key(root, 2)}]}))
    monkeypatch.setenv("LEVI_POOL_HOLDBACK", str(listing))
    scanner.scan()
    with pytest.raises(journal.ResumeRefused, match="held-back"):
        jobs.prepare_resume(job["id"])
    # A plan whose recipe includes held-back episodes resumes: the plan says so.
    joblog.TERMINATING.clear()
    options = options.model_copy(update={"name": "open"})
    open_job = jobs.plan_export(rollout_recipe(include_holdback=True), options)
    assert len(open_job["episodes"]) == 6
    real = _interrupt_after(monkeypatch, 1)
    jobs.run_worker(jobs._path(open_job["id"]))
    monkeypatch.setattr(export.pipeline, "run", real)
    joblog.TERMINATING.clear()
    assert (built["out"] / "exports/.open.partial").is_dir()
    assert jobs.prepare_resume(open_job["id"])


def test_a_list_edited_after_the_plan_stops_the_run_and_the_resume_until_scanned(
    pool, monkeypatch, root
):
    """A copy of a planned episode is added to the list after the plan was frozen
    and the export interrupted. The path check cannot see it (the plan names the
    original) and the index does not know it until a scan: the run and the
    resume must refuse rather than export the whole group."""
    from test_pool_resume import _interrupt_after

    from levi.pool import joblog

    built = pool(holdback=False, outcomes=False)
    monkeypatch.setenv("LEVI_POOL_BATCH_EPISODES", "2")
    listing = built["lists"] / "parked.json"
    listing.write_text(json.dumps({"episodes": [{"path": key(root, 4)}]}))
    monkeypatch.setenv("LEVI_POOL_HOLDBACK", str(listing))
    scanner.scan()
    options = export.ExportOptions(
        format="lerobot_v21",
        name="cut",
        output_dir=str(built["out"] / "exports"),
        timing="retime",
        filter_static=False,
        workers=1,
    )
    job = jobs.plan_export(rollout_recipe(), options)
    planned = {Path(e["key"]).name for e in job["episodes"]}
    assert planned == {f"demo_000{n}" for n in (0, 1, 2, 3, 5)}
    real = _interrupt_after(monkeypatch, 1)
    jobs.run_worker(jobs._path(job["id"]))
    monkeypatch.setattr(export.pipeline, "run", real)
    joblog.TERMINATING.clear()
    assert (built["out"] / "exports/.cut.partial").is_dir()
    # As planned, the unfinished export may continue.
    assert jobs.prepare_resume(job["id"])

    # demo_0001's copy is now named; nobody scanned.
    value = json.loads(listing.read_text())
    value["episodes"].append({"path": "evalws/plates_a/demo_0000"})
    listing.write_text(json.dumps(value))
    with pytest.raises(journal.ResumeRefused, match="changed since the last scan"):
        jobs.prepare_resume(job["id"])
    with pytest.raises(PermissionError, match="changed since the last scan"):
        export.run(jobs.read_job(job["id"]), resume=True)
    # A fresh run of the same plan is refused the same way, not only a resume.
    with pytest.raises(PermissionError, match="changed since the last scan"):
        export.guard_holdback(job["episodes"], [root], [listing], allow=False)
    # Included on purpose, the edit does not matter.
    export.guard_holdback(job["episodes"], [root], [listing], allow=True)

    # Once scanned, the index knows the group and says so.
    scanner.scan()
    with pytest.raises(journal.ResumeRefused, match="held-back"):
        jobs.prepare_resume(job["id"])


def test_the_plan_hash_of_an_older_plan_does_not_change():
    base = {"format": "lerobot_v21", "episodes": [], "heldout_lists": []}
    assert journal.plan_hash(base) == journal.plan_hash({**base, "holdback_lists": []})
    assert journal.plan_hash(base) != journal.plan_hash(
        {**base, "holdback_lists": ["/a.json"]}
    )


def test_a_worker_is_started_with_the_lists_it_was_planned_with(pool, monkeypatch):
    pool()
    job = {
        "pool_roots": ["/pool"],
        "heldout_lists": [],
        "holdback_lists": ["/a/hb.json", "/b/hb.json"],
        "outcome_lists": ["/a/oc.json"],
    }
    env = jobs._environment(job)
    assert env["LEVI_POOL_HOLDBACK"] == "/a/hb.json,/b/hb.json"
    assert env["LEVI_POOL_OUTCOMES"] == "/a/oc.json"
    # A plan from before has none: the worker does not inherit the service's.
    old = jobs._environment({"pool_roots": ["/pool"], "heldout_lists": []})
    assert old["LEVI_POOL_HOLDBACK"] == "" and old["LEVI_POOL_OUTCOMES"] == ""
    assert jobs.plan_scan()["holdback_lists"]


# ------------------------------------------------- lists that cannot be trusted


def test_a_list_edited_after_the_scan_blocks_exports_until_scanned_again(pool, root):
    built = pool()
    assert not [
        w for w in recipe.preview(rollout_recipe())["warnings"] if w["blocking"]
    ]
    path = Path(built["summary"]["holdback"]["lists"][0]["path"])
    value = json.loads(path.read_text())
    value["episodes"].append({"path": f"{ROLLOUTS}/demo_0005"})
    path.write_text(json.dumps(value))
    warnings = {w["code"]: w for w in recipe.preview(rollout_recipe())["warnings"]}
    assert warnings["holdback_lists_changed"]["blocking"]
    with pytest.raises(ValueError, match="hold-back lists changed"):
        jobs.plan_export(
            rollout_recipe(),
            export.ExportOptions(
                format="raw_capture", name="x", output_dir=str(built["out"])
            ),
        )
    scanner.scan()
    assert "holdback_lists_changed" not in {
        w["code"] for w in recipe.preview(rollout_recipe())["warnings"]
    }
    assert recipe.preview(rollout_recipe())["episodes"] == 3  # demo_0005 too


def test_a_list_added_or_removed_after_the_scan_is_noticed(pool, monkeypatch):
    built = pool(holdback=False, outcomes=False)
    path = built["lists"] / "new.json"
    path.write_text(json.dumps(holdback_value(built["root"])))
    monkeypatch.setenv("LEVI_POOL_HOLDBACK", str(path))
    codes = {w["code"] for w in recipe.preview(rollout_recipe())["warnings"]}
    assert "holdback_lists_changed" in codes
    monkeypatch.delenv("LEVI_POOL_HOLDBACK")
    scanner.scan()
    monkeypatch.setenv("LEVI_POOL_HOLDBACK", str(path))
    scanner.scan()
    monkeypatch.delenv("LEVI_POOL_HOLDBACK")
    codes = {w["code"] for w in recipe.preview(rollout_recipe())["warnings"]}
    assert "holdback_lists_changed" in codes


@pytest.mark.parametrize(
    ("name", "text", "message"),
    [
        ("missing", None, "Cannot read hold-back list"),
        ("broken", "{not json", "Cannot read hold-back list"),
        ("array", "[1, 2]", 'expected a JSON object with an "episodes" list'),
        ("noeps", '{"name": "x"}', 'an "episodes" list'),
        (
            "nopath",
            '{"episodes": [{"path": "a"}, {"note": "b"}]}',
            "episode 1 has no path",
        ),
        ("emptypath", '{"episodes": [{"path": " "}]}', "episode 0 has no path"),
    ],
)
def test_a_hold_back_list_that_cannot_be_read_stops_the_scan(
    pool, monkeypatch, name, text, message
):
    built = pool(holdback=False, outcomes=False)
    path = built["lists"] / f"{name}.json"
    if text is not None:
        path.write_text(text)
    monkeypatch.setenv("LEVI_POOL_HOLDBACK", str(path))
    with pytest.raises(ValueError, match=message) as caught:
        scanner.scan()
    assert str(path) in str(caught.value)
    # The last index stays; the page is told, and an export would be refused.
    warnings = {w["code"]: w for w in recipe.preview(rollout_recipe())["warnings"]}
    if text is None:
        assert warnings["holdback_list_unreadable"]["blocking"]
        with pytest.raises(ValueError, match="Refusing to plan"):
            jobs.plan_export(
                rollout_recipe(),
                export.ExportOptions(
                    format="raw_capture", name="x", output_dir=str(built["out"])
                ),
            )
        with pytest.raises(ValueError, match="Cannot read hold-back list"):
            export.refuse_holdback([{"key": "a"}], [], [path])


def test_a_list_is_read_as_utf8_whatever_the_locale(pool, monkeypatch):
    built = pool(holdback=False, outcomes=False)
    path = built["lists"] / "parked-utf8.json"
    path.write_text(
        json.dumps(
            {"name": "暂留-0610", "episodes": [{"path": key(built["root"], 4)}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    real = Path.read_text
    seen = []

    def spy(self, *args, **kwargs):
        if self.name == path.name:
            seen.append(kwargs.get("encoding"))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy)
    monkeypatch.setenv("LEVI_POOL_HOLDBACK", str(path))
    scanner.scan()
    assert seen and set(seen) == {"utf-8"}
    assert listed()[key(built["root"], 4)]["holdback_set"] == "暂留-0610"
    # Bytes that are not UTF-8 are an unreadable list, named, not a crash.
    path.write_bytes(b'{"episodes": [], "name": "\xff\xfe"}')
    with pytest.raises(ValueError, match="Cannot read hold-back list") as caught:
        scanner.scan()
    assert str(path) in str(caught.value)


GHOST = "~no_such_user_for_levi_tests"


def test_a_home_of_a_user_that_does_not_exist_is_a_missing_file_not_a_crash(
    pool, monkeypatch
):
    built = pool(holdback=False, outcomes=False)
    # As the place of a list: the file is not there.
    for env, what in (
        ("LEVI_POOL_HOLDBACK", "hold-back"),
        ("LEVI_POOL_OUTCOMES", "verified-outcome"),
    ):
        monkeypatch.setenv(env, f"{GHOST}/list.json")
        with pytest.raises(ValueError, match=f"Cannot read {what} list"):
            scanner.scan()
        monkeypatch.delenv(env)
    # As an entry's path: it names no episode (an unmatched entry), everywhere
    # a path is resolved.
    path = built["lists"] / "ghost.json"
    path.write_text(json.dumps({"episodes": [{"path": f"{GHOST}/demo_0000"}]}))
    monkeypatch.setenv("LEVI_POOL_HOLDBACK", str(path))
    block = scanner.scan()["holdback"]
    assert block["matched"] == 0 and block["unmatched_count"] == 1
    assert block["unmatched"] == [f"{GHOST}/demo_0000"]
    assert manifest.locate(f"{GHOST}/x", [built["root"]], {}) is None
    assert manifest.candidates(f"{GHOST}/demo_0000", [built["root"]]) == {
        str(built["root"] / GHOST / "demo_0000")
    }
    export.refuse_holdback([{"key": key(built["root"], 1)}], [built["root"]], [path])


def test_a_verified_outcome_list_with_a_bad_shape_stops_the_scan(pool, monkeypatch):
    built = pool(holdback=False, outcomes=False)
    path = built["lists"] / "bad.json"
    path.write_text(json.dumps({"episodes": "all"}))
    monkeypatch.setenv("LEVI_POOL_OUTCOMES", str(path))
    with pytest.raises(ValueError, match="verified-outcome list"):
        scanner.scan()


def test_an_index_without_the_hold_back_column_asks_for_a_scan(pool, monkeypatch):
    pool()
    frame = pd.read_parquet(scanner.index_path()).drop(columns=["holdback"])
    frame.to_parquet(scanner.index_path(), index=False)
    with pytest.raises(ValueError, match="older LEVI"):
        index.frame()
    # The export's own check cannot pass by silence while a list is set...
    with pytest.raises(ValueError, match="older LEVI"):
        export.refuse_holdback_groups([{"key": "a"}])
    # ...and has nothing to check when none is.
    monkeypatch.delenv("LEVI_POOL_HOLDBACK")
    assert index.holdback_view() is None
    export.refuse_holdback_groups([{"key": "a"}])


# ------------------------------------------------------------ verified outcomes


def test_outcome_priority_is_human_then_verified_then_the_robot_flag(pool):
    built = pool()
    root = built["root"]
    rows = listed()
    seen = {
        n: (rows[key(root, n)]["outcome"], rows[key(root, n)]["outcome_source"])
        for n in range(6)
    }
    assert seen == {
        0: ("success", "verified"),  # flag failure, check success
        1: ("failure", "robot_flag"),  # no entry
        2: ("failure", "human"),  # human failure beats a verified success
        3: ("failure", "verified"),  # named through a copy; flag success
        4: ("failure", "robot_flag"),  # "unknown" says nothing
        5: ("success", "robot_flag"),
    }
    first = rows[key(root, 0)]
    assert (
        first["verified_outcome"] == "success" and first["verified_by"] == "agent-check"
    )
    assert first["verified_basis"] == "A=B" and first["robot_flag"] == "failure"
    assert not first["verified_conflict"]
    # The recording named through its copy: both copies carry it.
    for path in (key(root, 3), str(root / "evalws/plates_b/demo_0000")):
        assert rows[path]["verified_outcome"] == "failure"
        assert rows[path]["verified_basis"] == "A=B / C"
    assert rows[key(root, 4)]["verified_outcome"] is None
    assert rows[key(root, 5)]["verified_by"] is None


def test_a_conflict_with_a_human_label_is_recorded_and_the_label_stands(pool):
    built = pool()
    root = built["root"]
    rows = listed()
    human = rows[key(root, 2)]
    assert human["human_label"] == "failure" and human["verified_outcome"] == "success"
    assert human["verified_conflict"] and not human["label_conflict"]
    assert human["outcome"] == "failure" and human["outcome_source"] == "human"
    block = built["summary"]["outcomes"]
    assert block["conflicts_with_human"] == 1 and block["disagreeing"] == 0
    assert sum(1 for r in rows.values() if r["verified_conflict"]) == 1
    warnings = {w["code"]: w for w in recipe.preview(rollout_recipe())["warnings"]}
    assert not warnings["outcomes_conflict"]["blocking"]
    assert warnings["outcomes_conflict"]["with_human"] == 1


def test_unknown_entries_are_skipped_and_counted_not_an_error(pool):
    block = pool()["summary"]["outcomes"]
    assert block["entries"] == 4  # five in the list, one "unknown"
    assert block["skipped"] == 1 and block["skipped_values"] == {"unknown": 1}
    assert block["matched"] == 3 and block["unmatched_count"] == 1
    assert block["unmatched"] == [f"{ROLLOUTS}/demo_9999"]
    assert block["episodes"] == 4  # demo_0000, 0002 and the two copies of 0003
    assert block["used"] == 3  # all but the human-labelled one
    warnings = {w["code"]: w for w in recipe.preview(rollout_recipe())["warnings"]}
    assert not warnings["outcomes_unmatched"]["blocking"]
    assert warnings["outcomes_unmatched"]["paths"] == [f"{ROLLOUTS}/demo_9999"]


def test_a_missing_value_counts_as_skipped(pool, monkeypatch):
    built = pool(holdback=False, outcomes=False)
    path = built["lists"] / "oc2.json"
    path.write_text(
        json.dumps(
            {
                "episodes": [
                    {"path": f"{ROLLOUTS}/demo_0000"},
                    {"path": f"{ROLLOUTS}/demo_0001", "outcome": 3},
                    {"path": f"{ROLLOUTS}/demo_0005", "outcome": " Success "},
                ]
            }
        )
    )
    monkeypatch.setenv("LEVI_POOL_OUTCOMES", str(path))
    block = scanner.scan()["outcomes"]
    assert block["skipped_values"] == {"missing": 2} and block["entries"] == 1
    assert listed()[key(built["root"], 5)]["outcome_source"] == "verified"


def test_two_disagreeing_entries_for_one_recording_count_as_none(pool, monkeypatch):
    built = pool()
    root = built["root"]
    other = built["lists"] / "second.json"
    other.write_text(
        json.dumps(
            {
                "name": "second-reader",
                "episodes": [
                    # demo_0000 is "success" in the first list.
                    {"path": key(root, 0), "outcome": "failure"},
                    # Agrees with the first list for the copy of demo_0003.
                    {"path": key(root, 3), "outcome": "failure"},
                ],
            }
        )
    )
    monkeypatch.setenv("LEVI_POOL_OUTCOMES", f"{built['lists'] / 'oc.json'},{other}")
    block = scanner.scan()["outcomes"]
    rows = listed()
    clash = rows[key(root, 0)]
    assert clash["verified_outcome"] is None and clash["verified_conflict"]
    assert clash["outcome"] == "failure" and clash["outcome_source"] == "robot_flag"
    assert block["disagreeing"] == 1
    agreed = rows[key(root, 3)]
    assert agreed["verified_outcome"] == "failure" and not agreed["verified_conflict"]
    assert agreed["verified_by"] == "agent-check"  # the first list that said so


def test_a_changed_outcome_list_blocks_until_scanned_again(pool):
    built = pool()
    path = Path(built["summary"]["outcomes"]["lists"][0]["path"])
    value = json.loads(path.read_text())
    value["episodes"][0]["outcome"] = "failure"
    path.write_text(json.dumps(value))
    warnings = {w["code"]: w for w in recipe.preview(rollout_recipe())["warnings"]}
    assert warnings["outcomes_lists_changed"]["blocking"]
    scanner.scan()
    assert listed()[key(built["root"], 0)]["outcome"] == "failure"
    assert "outcomes_lists_changed" not in {
        w["code"] for w in recipe.preview(rollout_recipe())["warnings"]
    }


def test_without_a_setting_the_outcome_is_the_label_or_the_flag(pool):
    built = pool(holdback=False, outcomes=False)
    rows = listed()
    assert {r["outcome_source"] for r in rows.values()} == {"human", "robot_flag"}
    assert all(r["verified_outcome"] is None for r in rows.values())
    assert built["summary"]["outcomes"]["entries"] == 0
    # 0000 is a failure by its flag now.
    assert rows[key(built["root"], 0)]["outcome"] == "failure"


# -------------------------------------------------- recipes: the outcome filters


def counts(outcome, **kw):
    view = recipe.preview(rollout_recipe(outcome=outcome, **kw))
    return view["episodes"], view["outcome_sources"]


def picked(outcome, **kw):
    """The episodes (by folder name) the selection takes for an outcome option."""
    chosen = recipe.select_detailed(rollout_recipe(outcome=outcome, **kw)).chosen
    return {Path(r["key"]).name for r in chosen}


def test_the_outcome_options_rank_verified_between_the_label_and_the_flag(pool):
    pool()
    # Not held back: demo_0000 (flag failure, verified success), 0002 (flag
    # success, human failure), 0003 (flag success, verified failure), 0005
    # (flag success). Counts alone cannot tell a rule that ignores the verified
    # outcome from the right one (two episodes either way): name the episodes.
    assert picked("all") == {"demo_0000", "demo_0002", "demo_0003", "demo_0005"}
    assert counts("all") == (4, {"verified": 2, "human": 1, "robot_flag": 1})
    # A human label, else a verified outcome, else the flag: 0000 and 0005.
    assert picked("verified_success") == {"demo_0000", "demo_0005"}
    assert counts("verified_success") == (2, {"verified": 1, "robot_flag": 1})
    # A human label or a verified outcome; the flag alone does not count.
    assert picked("checked_success") == {"demo_0000"}
    assert counts("checked_success") == (1, {"verified": 1})
    # Only a human label: the one in the pool says failure.
    assert picked("human_verified_success") == set()
    assert counts("human_verified_success") == (0, {})
    # The flag as it is: 0002 (human failure), 0003 and 0005.
    assert picked("robot_flag_success") == {"demo_0002", "demo_0003", "demo_0005"}
    # Who is left out, and what outcome the filter saw.
    left = {
        Path(e["key"]).name: e.get("outcome")
        for e in recipe.select_detailed(
            rollout_recipe(outcome="verified_success")
        ).excluded
        if e["reason"] == "outcome_filter"
    }
    assert left == {"demo_0002": "failure", "demo_0003": "failure"}
    # The warning about the key press counts only what rests on it.
    view = recipe.preview(rollout_recipe(outcome="verified_success"))
    note = next(w for w in view["warnings"] if w["code"] == "outcome_from_robot_flag")
    assert note["episodes"] == 1
    assert not any(
        w["code"] == "outcome_from_robot_flag"
        for w in recipe.preview(rollout_recipe(outcome="checked_success"))["warnings"]
    )


def test_the_outcome_filters_of_the_listings_agree(pool):
    root = pool()["root"]

    def names(outcome):
        rows = index.episodes(limit=100, outcome=outcome)["episodes"]
        return {Path(r["key"]).name for r in rows}

    assert names("verified_success") == {"demo_0000", "demo_0005"}
    assert names("checked_success") == {"demo_0000"}
    assert names("human_verified_success") == set()
    assert names("robot_flag_success") == {"demo_0002", "demo_0003", "demo_0005"}
    facets = index.facets()["outcomes"]
    assert facets["verified_success"] == 2 and facets["checked_success"] == 1
    assert facets["human_verified_success"] == 0
    assert facets["success"] == 2 and facets["failure"] == 4  # canonical rows
    assert root


def test_a_label_conflict_still_leaves_the_checked_selection(pool):
    root = pool()["root"]
    df = index.frame()
    df.loc[df.key == key(root, 0), ["label_conflict", "human_label"]] = [True, None]
    chosen, excluded = recipe.select(rollout_recipe(outcome="checked_success"), df=df)
    assert {"key": key(root, 0), "reason": "label_conflict"}.items() <= next(
        e for e in excluded if e["key"] == key(root, 0)
    ).items()
    assert not chosen


def test_the_quality_score_trusts_a_verified_outcome_a_little_less_than_a_label():
    assert (
        select.TRUST["human"]
        > select.TRUST["verified"]
        > select.TRUST["sft_demonstration"]
        > select.TRUST["robot_flag"]
    )
    assert select.TRUST["verified"] == 0.9
    assert select.trust_of({"outcome_source": "verified"}) == (0.9, "verified")
    assert select.trust_of({"outcome_source": "human"}) == (1.0, "human")
    assert select.trust_of({"outcome_source": "robot_flag"}) == (0.6, "robot_flag")
    # A contested label stays untrusted whatever the source says.
    assert select.trust_of({"outcome_source": "verified", "label_conflict": True}) == (
        0.0,
        "conflicting_labels",
    )

    def row(name, by):
        return {
            "key": name,
            "outcome": "success",
            "outcome_source": by,
            "frames": 100,
            "label_conflict": False,
        }

    scores = select.score_rows(
        [row("a", "human"), row("b", "verified"), row("c", "robot_flag")]
    )
    assert scores["a"]["score"] > scores["b"]["score"] > scores["c"]["score"]
    assert scores["b"]["reason"][0] == "verified"


# ----------------------------------------------------------- RECAP and exports


def test_a_recap_export_treats_a_verified_outcome_like_a_label_and_keeps_its_name(
    pool,
):
    built = pool()
    rec = rollout_recipe()
    options = export.ExportOptions(
        format="recap_value", name="value", output_dir=str(built["out"] / "exports")
    )
    job = jobs.plan_export(rec, options)
    jobs.execute(job)
    out = built["out"] / "exports/value"
    record = json.loads((out / "pool_export.json").read_text())
    by_name = {Path(e["source_path"]).name: e for e in record["episodes"]}
    assert {n: e["outcome_source"] for n, e in by_name.items()} == {
        "demo_0000": "verified",
        "demo_0002": "human",
        "demo_0003": "verified",
        "demo_0005": "robot_flag",
    }
    assert by_name["demo_0000"]["verified_by"] == "agent-check"
    assert by_name["demo_0000"]["verified_basis"] == "A=B"
    assert by_name["demo_0003"]["verified_outcome"] == "failure"
    assert by_name["demo_0005"]["verified_outcome"] is None
    assert record["outcome_lists"] == job["outcome_lists"]
    episodes = [
        json.loads(line)
        for line in (out / "meta/episodes.jsonl").read_text().splitlines()
    ]
    sources = {
        Path(e["pool_key"]).name: (e["levi_outcome"], e["levi_outcome_source"])
        for e in episodes
    }
    assert sources == {
        "demo_0000": ("success", "verified"),
        "demo_0002": ("failure", "human"),
        "demo_0003": ("failure", "verified"),
        "demo_0005": ("success", "robot_flag"),
    }
    recap = json.loads((out / "meta/levi_recap.json").read_text())
    assert recap["outcomes"] == {"success": 2, "failure": 2}
    assert recap["label_sources"] == {"verified": 2, "human": 1, "robot_flag": 1}
    # A verified outcome is enough for the value export: an episode with no
    # label and a verified outcome is not "no_outcome".
    result = recipe.select_detailed(rec, target="recap_value")
    assert not [e for e in result.excluded if e["reason"] == "no_outcome"]


# ------------------------------------------------------------------ API and CLI


def test_the_api_lists_filters_and_counts_held_back_episodes(pool, client):
    built = pool()
    status = client.get("/api/levi/pool/status").json()
    assert len(status["holdback_lists"]) == 1 and len(status["outcome_lists"]) == 1
    assert status["last_scan"]["holdback"]["episodes"] == 3
    assert status["last_scan"]["outcomes"]["used"] == 3
    only = client.get("/api/levi/pool/episodes", params={"holdback": "only"}).json()
    assert only["total"] == 2 and all(r["holdback"] for r in only["episodes"])
    hidden = client.get("/api/levi/pool/episodes", params={"holdback": "hide"}).json()
    assert hidden["total"] == 4 and not any(r["holdback"] for r in hidden["episodes"])
    assert client.get("/api/levi/pool/episodes").json()["total"] == 6
    assert (
        client.get("/api/levi/pool/episodes", params={"holdback": "x"}).status_code
        == 422
    )
    checked = client.get(
        "/api/levi/pool/episodes", params={"outcome": "checked_success"}
    ).json()
    assert [Path(r["key"]).name for r in checked["episodes"]] == ["demo_0000"]
    shown = client.get("/api/levi/pool/facets").json()
    assert shown["holdback"] == 2 and shown["holdback_unmatched"] == 1
    tasks = client.get("/api/levi/pool/tasks", params={"holdback": "hide"}).json()[
        "tasks"
    ]
    assert tasks[0]["episodes"] == 4
    # The preview endpoint carries the block and the new recipe fields.
    view = client.post(
        "/api/levi/pool/preview",
        json={"recipe": rollout_recipe(outcome="checked_success").model_dump()},
    ).json()
    assert view["holdback"]["left_out"] == 2 and view["episodes"] == 1
    assert view["excluded_holdback"] == 2
    assert built["root"]


def test_the_cli_saves_the_new_recipe_options_and_filters_the_listing(pool, capsys):
    pool()
    code = cli.main(
        [
            "recipe",
            "save",
            "chk",
            "--category",
            "rollout",
            "--task",
            TASK,
            "--outcome",
            "checked_success",
            "--include-holdback",
        ]
    )
    assert code == 0
    saved = recipe.load("chk")
    assert saved.outcome == "checked_success" and saved.include_holdback
    capsys.readouterr()
    assert cli.main(["recipe", "show", "chk"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["holdback"]["include"] and shown["episodes"] == 1
    assert cli.main(["episodes", "--holdback", "only", "--limit", "10"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["total"] == 2
    assert cli.main(["status"]) == 0
    assert len(json.loads(capsys.readouterr().out)["holdback_lists"]) == 1
    with pytest.raises(SystemExit):
        cli.main(["recipe", "save", "bad", "--outcome", "nonsense"])
