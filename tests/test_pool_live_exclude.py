"""The training pool and an episode a person removed on the live page.

A live workspace under a pool root has its mirror (``captures/<dataset>/
demo_NNNN``, hard links of the rollout) indexed beside the rollout folder it
was linked from. Removing the episode must keep the recording out of the
index listings, the recipes and the exports, whichever copy a recipe would
pick, and without a new scan."""

import json
import os
import shutil
from pathlib import Path

import pytest
from test_pool import make_demo

from levi.live import config as live_config
from levi.live import exclusion, mirror
from levi.pool import exclusions, export, index, jobs, recipe, scanner
from levi.pool.recipe import Recipe

DATASET = "pi05_test__stack_the_plates"


@pytest.fixture(scope="module")
def rollout_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("pool-live") / "wenkai"
    task = root / "rollouts/models/pi05_test/stack_the_plates"
    for i in range(3):
        make_demo(
            task / f"demo_{i:04d}",
            rollout="success",
            task="stack the plates",
            created=f"2026-09-1{i}T10:00:00",
        )
    return root


@pytest.fixture
def pool(rollout_root, tmp_path, monkeypatch):
    """A scanned pool whose root holds a live workspace that mirrored demos 0
    and 1 of the rollout folder."""
    live_ws = rollout_root / "liveworkspace"
    shutil.rmtree(live_ws, ignore_errors=True)
    (live_ws / "outputs/LEVI/workbench").mkdir(parents=True)
    (live_ws / "outputs/LEVI/workbench/datasets.json").write_text("{}")
    config = live_config.Config()
    config.service.workspace = str(live_ws)
    config.service.home = str(tmp_path / "home")
    (live_ws / "live").mkdir()
    (live_ws / "live/workspace.json").write_text("{}")
    task = rollout_root / "rollouts/models/pi05_test/stack_the_plates"
    capture = config.captures_dir / DATASET
    for n in (0, 1):
        mirror.link_tree(task / f"demo_{n:04d}", capture / f"demo_{n:04d}")
    state = mirror.empty_state(
        config,
        (str(rollout_root / "rollouts/models"), "pi05_test", "stack_the_plates"),
        0,
    )
    state["name"] = DATASET
    state["capture"] = str(capture)
    state["demos"] = {
        f"demo_{n:04d}": {"state": "done", "episode_index": n} for n in (0, 1)
    }
    mirror.jsonio.write(mirror.state_path(config, DATASET), state)

    monkeypatch.setenv("LEVI_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("LEVI_POOL_ROOTS", str(rollout_root))
    monkeypatch.setenv("LEVI_POOL_HELDOUT", "none")
    monkeypatch.setenv("LEVI_EXPORT_ROOTS", str(tmp_path))
    monkeypatch.setattr(export, "_levi_commit", lambda: "test")
    (tmp_path / "ws/pool").mkdir(parents=True)
    (tmp_path / "ws/pool/rules.json").write_text(
        json.dumps(
            {
                "embodiment_declared": [
                    {
                        "field": "gripper",
                        "value": "robotiq_2f85",
                        "source": "rollouts",
                        "evidence": "declared",
                    }
                ]
            }
        )
    )
    summary = scanner.scan()
    yield {
        "root": rollout_root,
        "config": config,
        "summary": summary,
        "out": tmp_path,
        "task": task,
    }
    shutil.rmtree(live_ws, ignore_errors=True)


def episodes(**filters):
    return {
        r["key"]: r
        for r in index.episodes(limit=1000, show_copies=True, **filters)["episodes"]
    }


def test_the_mirror_is_indexed_as_a_copy_of_the_rollout(pool):
    shown = episodes()
    mirror_keys = [k for k in shown if "liveworkspace" in k]
    assert len(shown) == 5 and len(mirror_keys) == 2
    original = pool["task"] / "demo_0000"
    copy = next(
        r for k, r in shown.items() if k.endswith("captures/" + DATASET + "/demo_0000")
    )
    assert copy["group"] == str(original) and not copy["canonical"]
    assert not any(r["excluded"] for r in shown.values())
    assert recipe.preview(Recipe(name="r", categories=["rollout"]))["episodes"] == 3


def test_a_removed_episode_leaves_the_listings_and_recipes_with_every_copy(pool):
    exclusion.exclude(pool["config"], DATASET, ["demo_0000"], "reflex")
    # No new scan: the click counts at once.
    shown = episodes()
    assert str(pool["task"] / "demo_0000") not in shown
    assert not any(k.endswith("/demo_0000") and "liveworkspace" in k for k in shown)
    assert len(shown) == 3  # demo_0001 and 0002, and the mirror of 0001
    preview = recipe.preview(Recipe(name="r", categories=["rollout"]))
    assert preview["episodes"] == 2 and preview["excluded_in_live"] == 1
    assert preview["excluded"]["excluded_in_live"] == 1
    # Asking for the mirror itself changes nothing: the recording is out.
    (source,) = {r["source"] for k, r in episodes().items() if "liveworkspace" in k}
    mirrored = recipe.preview(Recipe(name="r", sources=[source]))
    assert mirrored["episodes"] == 1 and mirrored["excluded_in_live"] == 1
    assert index.facets()["removed_in_live"] == 1  # one recording, two copies
    # Restoring brings both copies back.
    exclusion.restore(pool["config"], DATASET, ["demo_0000"])
    assert len(episodes()) == 5
    assert recipe.preview(Recipe(name="r", categories=["rollout"]))["episodes"] == 3


def test_the_selection_never_picks_a_removed_recording_by_any_route(pool):
    exclusion.exclude(pool["config"], DATASET, ["demo_0001"])
    removed = {
        str(pool["task"] / "demo_0001"),
        str(pool["config"].captures_dir / DATASET / "demo_0001"),
    }
    for rec in (
        Recipe(name="a"),
        Recipe(name="b", categories=["rollout"]),
        Recipe(name="c", categories=["levi"]),
        Recipe(name="d", tasks=["stack the plates"], per_task_cap=10),
    ):
        chosen = {r["key"] for r in recipe.select_detailed(rec).chosen}
        assert not chosen & removed, rec.name
        assert str(pool["task"] / "demo_0000") in chosen or rec.categories == ["levi"]
    reasons = {
        e["key"]: e["reason"] for e in recipe.select_detailed(Recipe(name="a")).excluded
    }
    assert reasons[str(pool["task"] / "demo_0001")] == "excluded_in_live"


def test_an_export_leaves_the_removed_recording_out_and_a_stale_plan_is_refused(pool):
    options = export.ExportOptions(
        format="raw_capture", name="late", output_dir=str(pool["out"] / "exports")
    )
    job = jobs.plan_export(Recipe(name="r", categories=["rollout"]), options)
    assert len(job["episodes"]) == 3
    export.refuse_removed(job["episodes"])  # nothing removed yet
    exclusion.exclude(pool["config"], DATASET, ["demo_0001"])
    with pytest.raises(PermissionError, match="removed on the live page"):
        export.refuse_removed(job["episodes"])
    with pytest.raises(PermissionError, match="removed on the live page"):
        export.run(job)
    assert not (pool["out"] / "exports/late").exists()
    # A plan made now leaves it out.
    again = jobs.plan_export(
        Recipe(name="r", categories=["rollout"]),
        export.ExportOptions(
            format="raw_capture", name="after", output_dir=str(pool["out"] / "exports")
        ),
    )
    assert len(again["episodes"]) == 2
    jobs.execute(again)
    record = json.loads((pool["out"] / "exports/after/pool_export.json").read_text())
    assert len(record["episodes"]) == 2
    assert not any("demo_0001" in e["source_path"] for e in record["episodes"])
    copied = (pool["out"] / "exports/after/stack_the_plates").glob("demo_*")
    assert len(list(copied)) == 2


def test_a_removal_names_the_mirror_and_the_rollout_it_was_linked_from(pool):
    exclusion.exclude(pool["config"], DATASET, ["demo_0001"])
    found = exclusions.workspace_exclusions(pool["config"].workspace)
    assert set(found) == {
        str(pool["config"].captures_dir / DATASET / "demo_0001"),
        str(pool["task"] / "demo_0001"),
    }
    assert found[next(iter(found))]["reason"] == ""
    # Another workspace, or a folder with no live marker, has none.
    assert exclusions.workspace_exclusions(pool["task"]) == {}


def test_a_rollout_is_kept_out_when_the_scan_ran_before_the_demo_was_mirrored(
    pool,
):
    """The live workspace was in the index, its mirror of demo_0001 was not:
    the rollout original is the only row, and the removal must still find it."""
    shutil.rmtree(pool["config"].captures_dir / DATASET / "demo_0001")
    scanner.scan()
    assert not any(k.endswith(f"captures/{DATASET}/demo_0001") for k in episodes())
    exclusion.exclude(pool["config"], DATASET, ["demo_0001"])
    original = str(pool["task"] / "demo_0001")
    assert original not in episodes()
    assert original not in {
        r["key"] for r in recipe.select_detailed(Recipe(name="a")).chosen
    }
    reasons = {
        e["key"]: e["reason"] for e in recipe.select_detailed(Recipe(name="a")).excluded
    }
    assert reasons[original] == "excluded_in_live"
    assert index.facets()["removed_in_live"] == 1
    job = jobs.plan_export(
        Recipe(name="r", categories=["rollout"]),
        export.ExportOptions(
            format="raw_capture", name="early", output_dir=str(pool["out"] / "exports")
        ),
    )
    assert original not in {e["key"] for e in job["episodes"]}
    with pytest.raises(PermissionError, match="removed on the live page"):
        export.refuse_removed([{"key": original}])


def test_a_rollout_is_kept_out_when_the_source_was_replaced_after_mirroring(pool):
    """The mirror no longer matches the rollout (different fingerprint and
    recording, so another group): removing the mirror's episode still keeps
    the original out."""
    mirrored = pool["config"].captures_dir / DATASET / "demo_0001"
    for name in ("frames.csv", "metadata.json"):  # break the hard links
        text = (mirrored / name).read_text()
        (mirrored / name).unlink()
        (mirrored / name).write_text(
            text.replace("2026-09-11", "2026-09-27")
            + ("\n" if name == "frames.csv" else "")
        )
    scanner.scan()
    shown = episodes()
    original = str(pool["task"] / "demo_0001")
    copy = str(mirrored)
    assert original in shown and copy in shown
    assert shown[original]["group"] != shown[copy]["group"]
    exclusion.exclude(pool["config"], DATASET, ["demo_0001"])
    shown = episodes()
    assert original not in shown and copy not in shown
    assert original not in {
        r["key"] for r in recipe.select_detailed(Recipe(name="a")).chosen
    }
    export.refuse_removed([{"key": str(pool["task"] / "demo_0002")}])
    with pytest.raises(PermissionError):
        export.refuse_removed(
            [{"key": original, "group": shown.get(original, {}).get("group")}]
        )
    job = jobs.plan_export(
        Recipe(name="r", categories=["rollout"]),
        export.ExportOptions(
            format="raw_capture",
            name="replaced",
            output_dir=str(pool["out"] / "exports"),
        ),
    )
    assert original not in {e["key"] for e in job["episodes"]}


def test_a_state_file_can_only_make_the_pool_leave_out_more(pool):
    """Whatever the file says, the paths are only compared with the index's
    keys: an odd one matches nothing and nothing is opened."""
    jsonio_update = mirror.jsonio.update
    jsonio_update(
        mirror.state_path(pool["config"], DATASET),
        lambda v: v.update(source="/definitely/not/here"),
    )
    exclusion.exclude(pool["config"], DATASET, ["demo_0001"])
    found = exclusions.workspace_exclusions(pool["config"].workspace)
    assert str(Path("/definitely/not/here/demo_0001")) in found
    assert len(episodes()) == 3


def test_an_index_without_the_live_workspace_still_refuses_the_mirror_by_key(pool):
    exclusion.exclude(pool["config"], DATASET, ["demo_0001"])
    mirror_key = str(pool["config"].captures_dir / DATASET / "demo_0001")
    # Plans made by hand, whatever the index knows: the key alone is enough.
    with pytest.raises(PermissionError):
        export.refuse_removed([{"key": mirror_key}])
    export.refuse_removed([{"key": str(pool["task"] / "demo_0000")}])


def test_a_removal_on_the_product_page_reaches_the_pool_outside_the_pool_roots(
    pool, tmp_path
):
    """The product LEVI's live page removes episodes in a live workspace that
    may lie outside the pool roots (the scan never sees it): the pool still
    consults the live workspace that page shows (``levi/live/locate.py``)."""
    outside = tmp_path / "live-elsewhere"
    config = live_config.Config()
    config.service.workspace = str(outside)
    (outside / "live").mkdir(parents=True)
    (outside / "live/workspace.json").write_text("{}")
    state = mirror.empty_state(
        config,
        (str(pool["root"] / "rollouts/models"), "pi05_test", "stack_the_plates"),
        0,
    )
    state["demos"] = {"demo_0002": {"state": "done", "episode_index": 2}}
    mirror.jsonio.write(mirror.state_path(config, DATASET), state)
    exclusion.exclude(config, DATASET, ["demo_0002"], via="product")
    original = str(pool["task"] / "demo_0002")
    assert original in episodes()  # nothing names that live workspace yet
    home = Path(os.environ["LEVI_LIVE_HOME"])
    mirror.jsonio.write(home / "status.json", {"workspace": str(outside)})
    assert original not in episodes()
    assert recipe.preview(Recipe(name="r", categories=["rollout"]))["episodes"] == 2
