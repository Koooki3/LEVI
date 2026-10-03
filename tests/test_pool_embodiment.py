"""Training pool: robot, gripper, action mode and end-effector frame, read from
metadata only (never from folder or task names), and the export that refuses
to mix grippers."""

import json
from collections import Counter
from pathlib import Path

import pandas as pd
import pytest

from levi.pool import embodiment, export, index, jobs, recipe, rules, scanner
from levi.pool.recipe import Recipe

ROBOTIQ_JOINTS = ["robotiq_85_left_knuckle_joint"]
FRANKA_JOINTS = ["fr3_finger_joint1", "fr3_finger_joint2"]
FR3 = [f"fr3_joint{i}" for i in range(1, 8)]


def read(meta=None, fmt="robot_capture", **more):
    return embodiment.read({"metadata": meta or {}, **more}, fmt)


# ------------------------------------------------------------------ units


def test_robotiq_capture_with_a_policy_frame():
    got = read(
        {
            "gripper_joint_names": ROBOTIQ_JOINTS,
            "robot_joint_names": FR3,
            "policy": {"server_metadata": {"ee_frame": "franka_hand_tcp"}},
        }
    )
    assert got["gripper"] == "robotiq_2f85"
    assert got["robot"] == "franka_fr3"
    assert got["ee_frame"] == "franka_hand_tcp"
    # A raw capture has no action column; the pool export derives the next pose.
    assert got["action_mode"] == "ee_pose_abs_next"
    assert "gripper_joint_names" in got["evidence"]["gripper"]
    assert "policy.server_metadata.ee_frame" in got["evidence"]["ee_frame"]


def test_franka_hand_capture_from_joints_or_the_gripper_driver_topic():
    by_joints = read({"gripper_joint_names": FRANKA_JOINTS})
    assert by_joints["gripper"] == "franka_hand"
    by_topic = read({"gripper_state_topic": "/franka_gripper/joint_states"})
    assert by_topic["gripper"] == "franka_hand"
    # Both say the same thing: still one value.
    both = read(
        {
            "gripper_joint_names": FRANKA_JOINTS,
            "gripper_state_topic": "/franka_gripper/joint_states",
        }
    )
    assert both["gripper"] == "franka_hand"


def test_spacemouse_capture_names_its_gripper_in_other_keys():
    got = read({"gripper": {"joint_name": "robotiq_85_left_knuckle_joint"}})
    assert got["gripper"] == "robotiq_2f85"


def test_nothing_recorded_is_unknown_not_a_guess():
    got = read({"task_description": "pick up the robotiq cube", "task_folder": "x"})
    assert got["gripper"] == got["robot"] == got["ee_frame"] == "unknown"
    assert got["evidence"].get("gripper") is None
    # No metadata and no format evidence at all.
    empty = embodiment.read({}, "lerobot")
    assert {empty[f] for f in embodiment.FIELDS} == {"unknown"}


def test_names_of_folders_and_tasks_are_not_evidence():
    got = embodiment.read(
        {"metadata": {"task_description": "robotiq franka_hand"}},
        "robot_capture",
        path="/data/robotiq_collection/franka_hand_task/demo_0000",
    )
    assert got["gripper"] == "unknown"


def test_conflicting_evidence_is_unknown_and_says_so():
    got = read(
        {
            "gripper_joint_names": ROBOTIQ_JOINTS,
            "gripper_state_topic": "/franka_gripper/joint_states",
        }
    )
    assert got["gripper"] == "unknown"
    assert "conflict" in got["evidence"]["gripper"]
    assert "robotiq_2f85" in got["evidence"]["gripper"]
    assert "franka_hand" in got["evidence"]["gripper"]


def test_action_mode_from_a_levi_conversion_record():
    nxt = embodiment.read({"conversion": {"action_semantics": "next_state"}}, "lerobot")
    assert nxt["action_mode"] == "ee_pose_abs_next"
    same = embodiment.read({"conversion": {"action_semantics": "state"}}, "lerobot")
    assert same["action_mode"] == "ee_pose_abs_current"
    # A LeRobot dataset with no record of its action says nothing.
    plain = embodiment.read(
        {"info": {"features": {"action": {"names": ["x", "y", "z"]}}}}, "lerobot"
    )
    assert plain["action_mode"] == "unknown"


def test_dict_values_are_not_matched_and_copied_values_are_sanitized():
    # A dict under a matched key is no evidence (only text, numbers, lists of them).
    assert read({"gripper_joint_names": {"a": "robotiq_85"}})["gripper"] == "unknown"
    got = read({"policy": {"server_metadata": {"ee_frame": "  Franka Hand TCP!! "}}})
    assert got["ee_frame"] == "franka_hand_tcp"
    long = read({"policy": {"server_metadata": {"ee_frame": "x" * 200}}})
    assert long["ee_frame"] == "unknown"  # not a frame name


def test_rules_are_data_versioned_and_overridable(tmp_path):
    defaults = rules.load()
    table = defaults["embodiment"]
    assert isinstance(table["version"], int) and table["rules"]
    assert {r["field"] for r in table["rules"]} <= set(embodiment.FIELDS)
    custom = {
        "embodiment": {
            "version": 7,
            "rules": [
                {
                    "field": "gripper",
                    "value": "my_gripper",
                    "in": "metadata",
                    "key": "end_tool",
                    "regex": "^pinch$",
                }
            ],
        }
    }
    (tmp_path / "rules.json").write_text(json.dumps(custom))
    mine = rules.load(tmp_path)
    got = embodiment.read(
        {"metadata": {"end_tool": "pinch"}}, "robot_capture", rules=mine
    )
    assert got["gripper"] == "my_gripper"
    # The default evidence no longer applies.
    other = embodiment.read(
        {"metadata": {"gripper_joint_names": ROBOTIQ_JOINTS}},
        "robot_capture",
        rules=mine,
    )
    assert other["gripper"] == "unknown"
    # The scan signature follows the table, so a changed table is read again.
    assert embodiment.signature(defaults) != embodiment.signature(mine)
    assert embodiment.signature(defaults) == embodiment.signature(rules.load())


def test_a_malformed_rule_is_refused(tmp_path):
    (tmp_path / "rules.json").write_text(
        json.dumps(
            {"embodiment": {"version": 1, "rules": [{"field": "colour", "value": "x"}]}}
        )
    )
    with pytest.raises(ValueError, match="embodiment"):
        embodiment.read({}, "robot_capture", rules=rules.load(tmp_path))


# ---------------------------------------------------- the mix of grippers


def test_gripper_mix_conditions():
    mix = embodiment.gripper_mix
    only = mix(["robotiq_2f85"] * 3)
    assert only["problem"] is None and only["counts"] == {"robotiq_2f85": 3}
    assert mix(["unknown"] * 4)["problem"] is None  # old data keeps working
    assert mix([None, "unknown"])["counts"] == {"unknown": 2}
    two = mix(["robotiq_2f85", "franka_hand", "franka_hand"])
    assert two["problem"] == "mixed_known"
    assert two["known"] == ["franka_hand", "robotiq_2f85"]
    odd = mix(["robotiq_2f85", "unknown"])
    assert odd["problem"] == "known_and_unknown" and odd["unknown"] == 1
    # The recipe names unknown on purpose: that is a choice.
    assert (
        mix(["robotiq_2f85", "unknown"], chosen=["robotiq_2f85", "unknown"])["problem"]
        is None
    )
    # Two known grippers need the explicit permission, naming both is not enough.
    assert (
        mix(["robotiq_2f85", "franka_hand"], chosen=["robotiq_2f85", "franka_hand"])[
            "problem"
        ]
        == "mixed_known"
    )
    assert mix(["robotiq_2f85", "franka_hand"], allow=True)["problem"] is None
    assert mix(["robotiq_2f85", "franka_hand"], allow=True)["mixed"] is True
    assert mix(["robotiq_2f85", "robotiq_2f85"], allow=True)["mixed"] is False


# ------------------------------------------------------------ index and scan

COUNTER = 0


def demo(path: Path, meta: dict | None = None, task="stack plates") -> Path:
    """A raw capture without videos; every call records other data."""
    global COUNTER
    COUNTER += 1
    path.mkdir(parents=True)
    t = [COUNTER * 1000 + i / 10 for i in range(8)]
    pd.DataFrame({"timestamp_sec": t, "frame_index": range(8)}).to_csv(
        path / "frames.csv", index=False
    )
    pd.DataFrame({"timestamp_sec": t, "success_flag": 1, "px": t}).to_csv(
        path / "end_effector_pose.csv", index=False
    )
    stamp = f"2026-09-{1 + COUNTER % 27:02d}T10:{COUNTER % 60:02d}:00"
    body = {
        "task_description": task,
        "created_at": stamp,
        "stopped_at": stamp[:-2] + "59",
        "frame_count": 8,
        "success_flag_final": 1,
        "data_source": "human_teleop",
        **(meta or {}),
    }
    (path / "metadata.json").write_text(json.dumps(body))
    return path


def lerobot(root: Path, episodes: list[dict], conversion: dict | None = None) -> Path:
    (root / "meta").mkdir(parents=True)
    (root / "meta/info.json").write_text(
        json.dumps(
            {
                "codebase_version": "v2.1",
                "fps": 10,
                "chunks_size": 1000,
                "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
                "features": {
                    "observation.state": {"dtype": "float32", "shape": [7]},
                    "action": {"dtype": "float32", "shape": [7]},
                },
            }
        )
    )
    (root / "meta/tasks.jsonl").write_text(
        json.dumps({"task_index": 0, "task": "stack plates"}) + "\n"
    )
    rows = [
        {"episode_index": i, "tasks": ["stack plates"], "length": 8 + i, **row}
        for i, row in enumerate(episodes)
    ]
    (root / "meta/episodes.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows)
    )
    if conversion is not None:
        (root / "meta/levi_conversion.json").write_text(json.dumps(conversion))
    return root


ROBOTIQ = {"gripper_joint_names": ROBOTIQ_JOINTS, "robot_joint_names": FR3}
FRANKA = {"gripper_joint_names": FRANKA_JOINTS, "robot_joint_names": FR3}
SERVER = {"policy": {"server_metadata": {"ee_frame": "franka_hand_tcp"}}}


@pytest.fixture(scope="module")
def pool_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("embodiment") / "data"
    # Folder names say nothing the metadata does not: the "franka" folder
    # holds a Robotiq capture on purpose.
    for i in range(3):
        demo(root / f"lab_a/stack/demo_{i:04d}", ROBOTIQ)
    for i in range(2):
        demo(root / f"lab_franka/stack/demo_{i:04d}", ROBOTIQ if i else FRANKA)
    demo(
        root / "rollouts/stack/demo_0000",
        {**ROBOTIQ, **SERVER, "data_source": "policy_rollout"},
    )
    demo(root / "mystery/stack/demo_0000")
    demo(root / "mystery/stack/demo_0001", {"gripper_state_topic": "/franka_gripper/x"})
    demo(
        root / "mystery/stack/demo_0002",
        {**ROBOTIQ, "gripper_state_topic": "/franka_gripper/x"},
    )
    # Older captures: nothing in the metadata names a gripper.
    for i in range(2):
        demo(root / f"legacy/stack/demo_{i:04d}")
    # LeRobot: linked to captures, with a conversion record, and with nothing.
    lerobot(
        root / "converted",
        [
            {"rollout_source_demo": str(root / "lab_a/stack/demo_0000")},
            {"rollout_source_demo": str(root / "mystery/stack/demo_0000")},
            {"rollout_source_demo": str(root / "rollouts/stack/demo_0000")},
            {},
        ],
    )
    lerobot(root / "recorded", [{}, {}], conversion={"action_semantics": "state"})
    lerobot(root / "plain", [{}, {}, {}])
    return root


@pytest.fixture
def scanned(pool_root, tmp_path, monkeypatch):
    monkeypatch.setenv("LEVI_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("LEVI_POOL_ROOTS", str(pool_root))
    monkeypatch.setenv("LEVI_POOL_HELDOUT", "none")
    monkeypatch.setenv("LEVI_EXPORT_ROOTS", str(tmp_path))
    monkeypatch.setattr(export, "_levi_commit", lambda: "test")
    return scanner.scan()


def by_episode(source: str | None = None, **filters):
    rows = index.episodes(limit=1000, show_copies=True, **filters)["episodes"]
    return {
        (r["source"], r["episode"]): r
        for r in rows
        if source is None or r["source"] == source
    }


def fields(row):
    return {f: row[f] for f in embodiment.FIELDS}


def test_raw_captures_carry_the_fields(scanned):
    rows = by_episode()
    assert fields(rows[("lab_a", "stack/demo_0000")]) == {
        "robot": "franka_fr3",
        "gripper": "robotiq_2f85",
        "action_mode": "ee_pose_abs_next",
        "ee_frame": "unknown",
    }
    assert rows[("lab_franka", "stack/demo_0000")]["gripper"] == "franka_hand"
    # The folder is called franka, the capture says Robotiq: the capture wins.
    assert rows[("lab_franka", "stack/demo_0001")]["gripper"] == "robotiq_2f85"
    assert rows[("rollouts", "stack/demo_0000")]["ee_frame"] == "franka_hand_tcp"
    assert rows[("mystery", "stack/demo_0000")]["gripper"] == "unknown"
    assert rows[("mystery", "stack/demo_0000")]["robot"] == "unknown"
    assert rows[("mystery", "stack/demo_0001")]["gripper"] == "franka_hand"
    # Contradicting keys: unknown, with the reason in the evidence.
    conflict = rows[("mystery", "stack/demo_0002")]
    assert conflict["gripper"] == "unknown"
    assert "conflict" in conflict["embodiment_evidence"]["gripper"]
    evidence = rows[("lab_a", "stack/demo_0000")]["embodiment_evidence"]
    assert "gripper_joint_names" in evidence["gripper"]


def test_lerobot_rows_inherit_only_from_the_linked_capture(scanned):
    rows = by_episode("converted")
    linked = rows[("converted", "0")]
    assert (linked["robot"], linked["gripper"]) == ("franka_fr3", "robotiq_2f85")
    assert "linked" in linked["embodiment_evidence"]["gripper"]
    # Linked to a capture that says nothing: unknown stays unknown.
    assert rows[("converted", "1")]["gripper"] == "unknown"
    assert rows[("converted", "2")]["ee_frame"] == "franka_hand_tcp"
    # The dataset's action is its own business, not the capture's.
    assert rows[("converted", "0")]["action_mode"] == "unknown"
    assert rows[("converted", "3")]["gripper"] == "unknown"
    recorded = by_episode("recorded")
    assert {r["action_mode"] for r in recorded.values()} == {"ee_pose_abs_current"}
    assert {r["gripper"] for r in recorded.values()} == {"unknown"}
    plain = by_episode("plain")
    assert {tuple(fields(r).values()) for r in plain.values()} == {("unknown",) * 4}


def test_filters_facets_tasks_sources_and_summary(scanned):
    def count(**f):
        return index.episodes(limit=1, show_copies=True, **f)["total"]

    # lab_a 3 + lab_franka demo_0001 + rollout + converted 0 and 2
    assert count(grippers=["robotiq_2f85"]) == 3 + 1 + 1 + 2
    assert count(grippers=["franka_hand"]) == 2
    assert count(grippers=["unknown"]) == count() - 7 - 2
    assert count(grippers=["franka_hand", "unknown"]) == count() - 7
    # seven captures with FR3 joint names, two converted copies of them
    assert count(robots=["franka_fr3"]) == 9
    facets = index.facets(show_copies=True)
    assert facets["grippers"]["franka_hand"] == 2
    assert set(facets["grippers"]) == {"robotiq_2f85", "franka_hand", "unknown"}
    assert "franka_fr3" in facets["robots"]
    task = next(t for t in index.tasks(show_copies=True) if t["task"] == "stack plates")
    assert task["grippers"]["franka_hand"] == 2
    sources = {s["id"]: s for s in index.sources()}
    assert sources["lab_a"]["grippers"] == {"robotiq_2f85": 3}
    assert sources["mystery"]["grippers"] == {"unknown": 2, "franka_hand": 1}
    assert scanned["grippers"]["franka_hand"] == 2


def test_old_index_asks_for_a_rescan_and_is_read_again(scanned):
    frame = index.frame()
    assert {"robot", "gripper", "action_mode", "ee_frame"} <= set(frame.columns)
    old = frame.drop(columns=["gripper"])
    old["stat_sig"] = "signature-of-an-older-scan"
    old.to_parquet(scanner.index_path(), index=False)
    with pytest.raises(ValueError, match="older LEVI"):
        index.frame()
    again = scanner.scan()
    # An older signature never matches: every episode is read again.
    assert again["reused"] == 0
    assert index.frame().gripper.eq("franka_hand").sum() == 2


def test_a_changed_rule_table_reads_the_captures_again(scanned):
    assert {r["gripper"] for r in by_episode("lab_a").values()} == {"robotiq_2f85"}
    assert scanner.scan()["reused"] >= 10  # nothing changed: everything reused
    table = rules.DEFAULTS["embodiment"]
    changed = {
        "version": table["version"] + 1,
        "rules": [r for r in table["rules"] if r.get("value") != "robotiq_2f85"],
    }
    (scanner.settings.pool_dir() / "rules.json").write_text(
        json.dumps({"embodiment": changed})
    )
    scanner.scan()
    assert {r["gripper"] for r in by_episode("lab_a").values()} == {"unknown"}


def test_a_plan_with_an_older_signature_still_counts_as_unchanged(scanned):
    rows = index.episodes(limit=1000, show_copies=True)["episodes"]
    raw = [r for r in rows if r["format"] == "robot_capture"][:2]
    for r in raw:
        r["stat_sig"] = scanner.legacy_raw_sig(Path(r["key"]))
    export._unchanged(raw)  # an interrupted export planned before the upgrade
    raw[0]["stat_sig"] = "somebody changed the files"
    with pytest.raises(ValueError, match="changed since the pool was scanned"):
        export._unchanged(raw)


# ---------------------------------------------------------- recipes


def codes(warnings):
    return {w["code"]: w for w in warnings}


def chosen_grippers(rec):
    chosen, _ = recipe.select(rec)
    return Counter(r["gripper"] for r in chosen)


def test_recipe_filters_by_gripper_and_robot(scanned):
    rec = Recipe(name="r", grippers=["robotiq_2f85"])
    assert set(chosen_grippers(rec)) == {"robotiq_2f85"}
    assert set(chosen_grippers(Recipe(name="r", grippers=["franka_hand"]))) == {
        "franka_hand"
    }
    both = chosen_grippers(Recipe(name="r", grippers=["franka_hand", "unknown"]))
    assert set(both) == {"franka_hand", "unknown"}
    robots = Counter(
        r["robot"] for r in recipe.select(Recipe(name="r", robots=["franka_fr3"]))[0]
    )
    assert set(robots) == {"franka_fr3"}
    with pytest.raises(ValueError):
        Recipe(name="r", grippers=["Robotiq 2F85!"])
    # Recipes saved before the field existed load unchanged.
    old = Recipe.model_validate({"name": "old", "categories": ["human"]})
    assert old.grippers == [] and old.robots == [] and old.allow_mixed_gripper is False


def test_preview_shows_the_gripper_mix_and_blocks_a_mixed_one(scanned):
    preview = recipe.preview(Recipe(name="all"), "raw_capture")
    assert set(preview["grippers"]) == {"robotiq_2f85", "franka_hand", "unknown"}
    assert preview["robots"]["franka_fr3"] >= 1
    mixed = codes(preview["warnings"])["mixed_gripper"]
    assert mixed["blocking"] is True
    assert mixed["counts"] == preview["grippers"]
    assert mixed["problem"] == "mixed_known"
    assert "mixes grippers" in mixed["message"]
    assert mixed["sources"]["franka_hand"]


def test_a_filtered_recipe_is_clean_and_unknown_gets_a_note(scanned):
    one = recipe.preview(Recipe(name="one", grippers=["robotiq_2f85"]), "raw_capture")
    assert "mixed_gripper" not in codes(one["warnings"])
    assert "gripper_unknown" not in codes(one["warnings"])
    # Only older captures: allowed, no warning; the count says what they are.
    old = recipe.preview(Recipe(name="old", sources=["legacy"]), "raw_capture")
    assert old["grippers"] == {"unknown": 2}
    assert old["warnings"] == []
    # Naming unknown on purpose lifts the known-with-unknown refusal.
    chosen = recipe.preview(
        Recipe(name="c", grippers=["robotiq_2f85", "unknown"]), "raw_capture"
    )
    assert "mixed_gripper" not in codes(chosen["warnings"])
    assert codes(chosen["warnings"])["gripper_unknown"]["episodes"] >= 1
    # Two known grippers need the explicit permission.
    two = Recipe(name="t", grippers=["robotiq_2f85", "franka_hand"])
    assert codes(recipe.preview(two, "raw_capture")["warnings"])["mixed_gripper"][
        "blocking"
    ]
    allowed = two.model_copy(update={"allow_mixed_gripper": True})
    got = codes(recipe.preview(allowed, "raw_capture")["warnings"])
    assert got["mixed_gripper"]["blocking"] is False
    assert got["mixed_gripper"]["allowed"] is True


# ---------------------------------------------------------- export


def plan(rec, name="x", fmt="raw_capture"):
    return export.plan(
        rec,
        export.ExportOptions(
            format=fmt,
            name=name,
            output_dir=str(scanner.settings.workspace().parent / "out"),
        ),
    )


def run_export(rec, name):
    out = scanner.settings.workspace().parent / "out"
    options = export.ExportOptions(format="raw_capture", name=name, output_dir=str(out))
    job = jobs.plan_export(rec, options)
    result = jobs.execute(job)
    return job, result, json.loads((out / name / "pool_export.json").read_text())


def test_export_refuses_a_mixed_selection_and_says_what_it_holds(scanned):
    with pytest.raises(ValueError) as caught:
        plan(Recipe(name="all"))
    text = str(caught.value)
    assert "grippers" in text or "gripper" in text
    assert "franka_hand" in text and "robotiq_2f85" in text
    assert "lab_franka" in text  # the sources involved
    # Nothing was written.
    assert not (scanner.settings.workspace().parent / "out").exists()
    with pytest.raises(ValueError, match="allow_mixed_gripper"):
        plan(Recipe(name="two", grippers=["robotiq_2f85", "franka_hand"]))


def test_export_records_a_single_gripper(scanned):
    _, _, record = run_export(
        Recipe(name="r", grippers=["robotiq_2f85"], categories=["human"]), "one"
    )
    info = record["embodiment"]
    assert info["gripper"] == "robotiq_2f85"
    assert info["grippers"] == {"robotiq_2f85": len(record["episodes"])}
    assert info["allow_mixed_gripper"] is False and info["mixed"] is False
    assert info["robots"] == {"franka_fr3": len(record["episodes"])}
    assert info["rules_version"] == rules.DEFAULTS["embodiment"]["version"]
    ep = record["episodes"][0]
    assert ep["gripper"] == "robotiq_2f85" and ep["robot"] == "franka_fr3"
    assert record["recipe"]["grippers"] == ["robotiq_2f85"]


def test_allow_mixed_gripper_is_recorded_with_the_real_mix(scanned):
    rec = Recipe(
        name="m",
        grippers=["robotiq_2f85", "franka_hand"],
        categories=["human"],
        allow_mixed_gripper=True,
    )
    _, _, record = run_export(rec, "mixed")
    info = record["embodiment"]
    assert info["allow_mixed_gripper"] is True and info["mixed"] is True
    assert info["gripper"] == "mixed"
    assert set(info["grippers"]) == {"robotiq_2f85", "franka_hand"}
    assert record["recipe"]["allow_mixed_gripper"] is True


def test_all_unknown_still_exports_and_says_unknown(scanned):
    _, _, record = run_export(Recipe(name="l", sources=["legacy"]), "legacy")
    info = record["embodiment"]
    assert info["gripper"] == "unknown" and info["grippers"] == {"unknown": 2}
    assert info["mixed"] is False


def test_the_run_checks_again_whatever_the_plan_says(scanned):
    job = jobs.plan_export(
        Recipe(
            name="m",
            grippers=["robotiq_2f85", "franka_hand"],
            allow_mixed_gripper=True,
            categories=["human"],
        ),
        export.ExportOptions(
            format="raw_capture",
            name="edited",
            output_dir=str(scanner.settings.workspace().parent / "out"),
        ),
    )
    job["recipe"]["allow_mixed_gripper"] = False  # a plan edited by hand
    with pytest.raises(ValueError, match="gripper"):
        export.run(job)
    assert not (scanner.settings.workspace().parent / "out/edited").exists()


def test_a_plan_from_an_older_level_runs_as_unknown(scanned):
    job = jobs.plan_export(
        Recipe(name="o", sources=["legacy"]),
        export.ExportOptions(
            format="raw_capture",
            name="older",
            output_dir=str(scanner.settings.workspace().parent / "out"),
        ),
    )
    for ep in job["episodes"]:
        for f in embodiment.FIELDS:
            ep.pop(f)
    job["recipe"].pop("allow_mixed_gripper")
    job["recipe"].pop("grippers")
    result = jobs.execute(job)
    assert result["ok"]
    record = json.loads(
        (scanner.settings.workspace().parent / "out/older/pool_export.json").read_text()
    )
    assert record["embodiment"]["gripper"] == "unknown"


# ---------------------------------------------------------- API and CLI


def test_api_filters_facets_and_recipe(scanned, client):
    facets = client.get("/api/levi/pool/facets", params={"show_copies": True}).json()
    assert facets["grippers"]["franka_hand"] == 2
    page = client.get(
        "/api/levi/pool/episodes",
        params={"gripper": ["franka_hand"], "show_copies": True},
    ).json()
    assert page["total"] == 2
    assert {e["gripper"] for e in page["episodes"]} == {"franka_hand"}
    assert page["episodes"][0]["embodiment_evidence"]["gripper"]
    robots = client.get(
        "/api/levi/pool/episodes", params={"robot": ["franka_fr3"], "limit": 1}
    ).json()
    assert robots["total"] >= 1
    tasks = client.get(
        "/api/levi/pool/tasks", params={"gripper": ["franka_hand"], "show_copies": True}
    ).json()["tasks"]
    assert tasks[0]["grippers"] == {"franka_hand": 2}
    body = {"recipe": {"name": "p"}, "format": "raw_capture"}
    got = client.post("/api/levi/pool/preview", json=body).json()
    assert {w["code"] for w in got["warnings"]} >= {"mixed_gripper"}
    ok = client.post(
        "/api/levi/pool/preview",
        json={
            "recipe": {"name": "p", "grippers": ["robotiq_2f85"]},
            "format": "raw_capture",
        },
    ).json()
    assert "mixed_gripper" not in {w["code"] for w in ok["warnings"]}
    saved = client.put(
        "/api/levi/pool/recipes/g",
        json={"name": "g", "grippers": ["robotiq_2f85"], "allow_mixed_gripper": True},
    ).json()
    assert saved["grippers"] == ["robotiq_2f85"] and saved["allow_mixed_gripper"]
    assert client.get("/api/levi/pool/recipes/g").json()["allow_mixed_gripper"] is True


def test_cli_flags(scanned, capsys):
    from levi.pool import cli

    cli.main(["episodes", "--gripper", "franka_hand", "--show-copies"])
    assert json.loads(capsys.readouterr().out)["total"] == 2
    cli.main(
        ["episodes", "--robot", "franka_fr3", "--gripper", "unknown", "--show-copies"]
    )
    assert json.loads(capsys.readouterr().out)["total"] >= 1
    cli.main(
        ["recipe", "save", "c", "--gripper", "robotiq_2f85", "--gripper", "franka_hand",
         "--robot", "franka_fr3", "--allow-mixed-gripper"]
    )  # fmt: skip
    saved = recipe.load("c")
    assert saved.grippers == ["robotiq_2f85", "franka_hand"]
    assert saved.robots == ["franka_fr3"] and saved.allow_mixed_gripper
    capsys.readouterr()
    cli.main(["recipe", "show", "c", "--format", "raw_capture"])
    shown = json.loads(capsys.readouterr().out)
    assert shown["grippers"]


# ------------------------------------------- an older index during the window


def _old_index_with_a_heldout_copy(drop="gripper"):
    """The index an older LEVI wrote (no embodiment columns), with the group of
    one episode marked held out; returns that episode's key."""
    frame = index.frame()
    target = frame[frame.source == "lab_a"].iloc[0]
    frame.loc[frame.group == target.group, "heldout"] = True
    frame.drop(columns=list(embodiment.COLUMNS)).to_parquet(
        scanner.index_path(), index=False
    )
    return target.key, target.group


def test_the_group_check_still_runs_on_an_older_index(scanned):
    key, group = _old_index_with_a_heldout_copy()
    with pytest.raises(ValueError, match="older LEVI"):
        index.frame()
    with pytest.raises(PermissionError, match="held-out"):
        export.refuse_heldout_groups([{"key": key, "group": group}])
    # Unrelated episodes pass.
    export.refuse_heldout_groups([{"key": "/elsewhere/demo_0000", "group": "none"}])


def test_without_an_index_the_group_check_has_nothing_to_compare(scanned):
    scanner.index_path().unlink()
    export.refuse_heldout_groups([{"key": "/x", "group": "g"}])


def test_a_broken_index_is_an_error_not_a_pass(scanned):
    scanner.index_path().write_bytes(b"not a parquet file")
    with pytest.raises(Exception):  # noqa: B017 -- any error, never silence
        export.refuse_heldout_groups([{"key": "/x", "group": "g"}])


def test_a_run_on_an_older_index_still_refuses_a_heldout_copy(scanned):
    rec = Recipe(name="r", sources=["lab_a"])
    out = scanner.settings.workspace().parent / "out"
    job = jobs.plan_export(
        rec, export.ExportOptions(format="raw_capture", name="w", output_dir=str(out))
    )
    key, _ = _old_index_with_a_heldout_copy()
    assert key in {e["key"] for e in job["episodes"]}
    with pytest.raises(PermissionError, match="held-out"):
        export.run(job)  # also what an automatic resume at start-up calls
    with pytest.raises(PermissionError, match="held-out"):
        export.run(job, resume=True)
    assert not (out / "w").exists()
