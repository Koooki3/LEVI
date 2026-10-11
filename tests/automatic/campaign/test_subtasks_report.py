"""The subtask section of the campaign report (levi/automatic/campaign/
subtasks.py, T-LQ-2): numbers against a hand count of a constructed
campaign, no change at all without data, nothing in a running or blinded
campaign, the report's naming, wording and privacy rules, and the key that
ties the live service's records to the ledger. Synthetic data only."""

import dataclasses
import json
from pathlib import Path

import pytest
import test_ledger_fixtures as fx
from campaign_guard import aeri_home_fixture, guard_fixture  # noqa: F401

from levi.automatic.campaign import ledger as L
from levi.automatic.campaign import report as R
from levi.automatic.campaign import subtasks as S
from levi.live import analysis

BASIS = "operator_label"
TEXT_SUFFIXES = {".md", ".csv", ".tex", ".json", ".svg"}
FULL = ("approach", "grasp", "transport", "place", "retreat")


def info():
    return R.CampaignInfo(
        campaign_id=fx.CAMPAIGN,
        arms=(
            R.ArmInfo("A", role="reference", checkpoint="ckpt_a", config="cfg_a"),
            R.ArmInfo("B", checkpoint="ckpt_b", config="cfg_b"),
        ),
        task="pick the eggplant",
        seed=7,
        trials_per_arm=10,
        comparison=("B", "A"),
    )


@pytest.fixture(scope="module")
def campaign():
    lay = fx.layout(per_arm=10, segment_trials=5)
    return lay, L.derive(lay, fx.synthetic_facts(lay, rates={"A": 0.5, "B": 0.5}))


def block(ids, outcomes=None, width=2.0):
    """A segments block: ``ids`` in order, each ``width`` s long."""
    items = []
    for n, sid in enumerate(ids):
        outcome = (outcomes or {}).get(n, "success")
        items.append(
            {
                "id": sid,
                "start": n * width,
                "end": (n + 1) * width,
                "duration": width,
                "outcome": outcome,
            }
        )
    return analysis.segment_block(items)


def valid_rows(led, arm):
    return [r for r in led.rows if r.arm == arm and r.status == "valid"]


def outcome_of(led, row):
    return L.label_value(led.labels.get(row.episode_id), BASIS)[0]


def records_for(led, *, drop=()):
    """Arm A: successes run all five stages; failures stop after a failed
    grasp (last completed: approach). Arm B: failures reach transport, which
    fails (last completed: grasp). ``drop``: episode ids with no record."""
    found = {}
    for row in led.rows:
        if row.status != "valid" or row.episode_id in drop:
            continue
        outcome = outcome_of(led, row)
        if outcome == "success":
            seg = block(FULL)
        elif row.arm == "A":
            seg = block(FULL[:2], {1: "failure"})
        else:
            seg = block(FULL[:3], {2: "failure"})
        found[row.episode_id] = {
            "dataset": "g__t",
            "group": "grp",
            "task": "task",
            "demo": row.episode_id.rsplit(".", 1)[-1],
            "segments": seg,
        }
    return found


def write(root, campaign, **kwargs):
    lay, led = campaign
    kwargs.setdefault("campaign_state", "REPORTED")
    return R.write_report(
        root, led, info(), BASIS, layout=lay, now=0, pdf=False, **kwargs
    )


def snapshot(folder: Path):
    return {
        str(p.relative_to(folder)): p.read_bytes()
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


def test_the_constructed_campaign_has_failures_in_both_arms(campaign):
    _, led = campaign
    for arm in "AB":
        outcomes = [outcome_of(led, r) for r in valid_rows(led, arm)]
        assert "success" in outcomes and "failure" in outcomes


# --- no data, no change ---------------------------------------------------------------


@pytest.mark.parametrize(
    "records",
    [
        None,
        {},
        {"nobody/knows/demo_0001": {"segments": block(FULL)}},  # matches no trial
    ],
    ids=["none", "empty", "unmatched"],
)
def test_without_matching_segments_the_report_is_byte_for_byte_unchanged(
    campaign, tmp_path, records
):
    write(tmp_path / "base", campaign)
    write(tmp_path / "with", campaign, subtasks=records)
    assert snapshot(tmp_path / "with" / BASIS) == snapshot(tmp_path / "base" / BASIS)


def test_records_without_time_segments_add_nothing(campaign, tmp_path):
    _, led = campaign
    empty = {
        r.episode_id: {"group": "g", "task": "t", "demo": "d", "segments": block([])}
        for r in led.rows
    }
    write(tmp_path / "base", campaign)
    write(tmp_path / "with", campaign, subtasks=empty)
    assert snapshot(tmp_path / "with" / BASIS) == snapshot(tmp_path / "base" / BASIS)


# --- with data -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def written(campaign, tmp_path_factory):
    _, led = campaign
    root = tmp_path_factory.mktemp("subtasks")
    write(root / "base", campaign)
    write(root / "with", campaign, subtasks=records_for(led))
    return root / "base" / BASIS, root / "with" / BASIS


def test_the_section_and_files_are_added_and_nothing_else_changes(written):
    base, with_ = written
    before, after = snapshot(base), snapshot(with_)
    assert set(after) - set(before) == {
        "tables/subtasks.csv",
        "tables/subtasks.tex",
        "data/subtasks.json",
    }
    changed = {k for k in before if before[k] != after[k]}
    assert changed == {"summary.en.md", "summary.zh-CN.md", "manifest.json"}
    # the numbers of the report itself are the same bytes
    assert before["data/analysis.json"] == after["data/analysis.json"]
    assert before["tables/success.csv"] == after["tables/success.csv"]
    for lang in ("en", "zh-CN"):
        old = before[f"summary.{lang}.md"].decode()
        new = after[f"summary.{lang}.md"].decode()
        head, _, tail = old.rstrip("\n").rpartition("\n\n")
        assert new.startswith(head) and new.rstrip("\n").endswith(tail)
    manifest = json.loads(after["manifest.json"])
    assert {"tables/subtasks.csv", "data/subtasks.json"} <= set(manifest["files"])


def test_the_section_says_it_is_automatic_and_unreviewed(written):
    _, with_ = written
    en = (with_ / "summary.en.md").read_text()
    zh = (with_ / "summary.zh-CN.md").read_text()
    assert "## Subtask analysis (automatic annotation, unreviewed)" in en
    assert "## 子任务分析（自动标注，未复核）" in zh
    assert "none of these numbers is part of the success rates" in en
    # it sits before the closing line
    assert en.index("## Subtask analysis") < en.index("All numbers come from")
    assert zh.index("## 子任务分析") < zh.index("文中所有数字")


def test_the_table_matches_a_hand_count(campaign, written):
    _, led = campaign
    _, with_ = written
    lines = (with_ / "tables/subtasks.csv").read_text().splitlines()
    assert lines[0].split(",") == list(S.HEADER)
    rows = {tuple(line.split(",")[:2]): line.split(",") for line in lines[1:]}
    for arm in "AB":
        trials = valid_rows(led, arm)
        wins = sum(outcome_of(led, r) == "success" for r in trials)
        fails = len(trials) - wins
        assert fails and wins
        reached = {
            "approach": len(trials),
            # the successes reach everything; arm A failures stop at grasp,
            # arm B failures at transport
            "grasp": len(trials),
            "transport": wins if arm == "A" else len(trials),
            "place": wins,
            "retreat": wins,
        }
        for sid, count in reached.items():
            got = rows[(arm, sid)]
            assert got[2] == str(len(trials)), (arm, sid)
            assert got[3] == str(count), (arm, sid)
            assert got[4] == f"{100 * count / len(trials):.1f}%", (arm, sid)
            assert got[6] == "2.000"  # every segment is 2 s long
        landed = {"A": "approach", "B": "grasp"}[arm]
        assert rows[(arm, landed)][9] == str(fails)
        others = [s for s in reached if s != landed]
        assert all(rows[(arm, s)][9] == "0" for s in others)
        assert ("A", "none") not in rows  # every failure completed something


def test_a_failure_that_completed_nothing_gets_a_none_row(campaign, tmp_path):
    _, led = campaign
    found = records_for(led)
    victim = next(
        r for r in valid_rows(led, "A") if outcome_of(led, r) == "failure"
    ).episode_id
    found[victim]["segments"] = block(["approach"], {0: "failure"})
    write(tmp_path, campaign, subtasks=found)
    rows = (tmp_path / BASIS / "tables/subtasks.csv").read_text().splitlines()
    none = [line.split(",") for line in rows if line.startswith("A,none,")]
    assert len(none) == 1 and none[0][9] == "1"


def test_every_basis_still_passes_the_naming_and_wording_checks(campaign, tmp_path):
    _, led = campaign
    for basis in L.LABEL_BASES:
        lay, _ = campaign
        R.write_report(
            tmp_path,
            led,
            info(),
            basis,
            layout=lay,
            campaign_state="REPORTED",
            now=0,
            pdf=False,
            subtasks=records_for(led),
        )
        text = (tmp_path / basis / "summary.en.md").read_text()
        assert "Subtask analysis" in text
        if basis != "adjudicated_ground_truth":
            assert (
                "ground truth" not in text.lower()
                and "ground-truth" not in text.lower()
            )


def test_the_same_inputs_give_the_same_bytes(campaign, tmp_path):
    _, led = campaign
    write(tmp_path / "one", campaign, subtasks=records_for(led))
    write(tmp_path / "two", campaign, subtasks=records_for(led))
    assert snapshot(tmp_path / "one" / BASIS) == snapshot(tmp_path / "two" / BASIS)


# --- gating ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "blinded"),
    [("ARM_RUNNING", False), ("PAUSED", False), ("REPORTED", True)],
)
def test_a_running_or_blinded_campaign_gets_no_subtask_section(
    campaign, tmp_path, state, blinded
):
    _, led = campaign
    write(
        tmp_path,
        campaign,
        subtasks=records_for(led),
        campaign_state=state,
        blinded=blinded,
    )
    folder = tmp_path / BASIS
    names = sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file())
    assert names == [
        "manifest.json",
        "progress.json",
        "summary.en.md",
        "summary.zh-CN.md",
    ]
    for path in folder.rglob("*"):
        if path.is_file():
            text = path.read_text()
            assert "ubtask" not in text and "子任务" not in text


# --- coverage and warnings -----------------------------------------------------------------


def test_thin_coverage_and_few_failures_are_warned_about(campaign, tmp_path):
    _, led = campaign
    drop = {r.episode_id for r in valid_rows(led, "B")[:5]}
    write(tmp_path, campaign, subtasks=records_for(led, drop=drop))
    data = json.loads((tmp_path / BASIS / "data/subtasks.json").read_text())
    by_arm = {a["arm"]: a for a in data["arms"]}
    assert by_arm["A"]["with_segments"] == len(valid_rows(led, "A"))
    assert by_arm["B"]["with_segments"] == len(valid_rows(led, "B")) - 5
    codes = {a["arm"]: [w["code"] for w in a["warnings"]] for a in data["arms"]}
    assert "low_coverage" in codes["B"] and "low_coverage" not in codes["A"]
    for arm in "AB":
        seen = [
            r
            for r in valid_rows(led, arm)
            if outcome_of(led, r) == "failure" and r.episode_id not in drop
        ]
        assert ("few_failures" in codes[arm]) == (len(seen) < 5), arm
    assert "few_failures" in codes["A"]  # four failures in all
    en = (tmp_path / BASIS / "summary.en.md").read_text()
    assert "Arm B: time segments cover" in en
    zh = (tmp_path / BASIS / "summary.zh-CN.md").read_text()
    assert "组 B：时间片段只覆盖" in zh


def test_a_trial_without_a_basis_outcome_stays_out_of_the_landing_counts(campaign):
    _, led = campaign
    found = records_for(led)
    data = S.analyse(led, "autonomous_verdict", found)
    arm_a = next(a for a in data["arms"] if a["arm"] == "A")
    fails = sum(
        L.label_value(led.labels.get(r.episode_id), "autonomous_verdict")[0]
        == "failure"
        for r in valid_rows(led, "A")
    )
    assert arm_a["landing"]["of"] == fails


# --- privacy -------------------------------------------------------------------------------------


def test_subtask_ids_with_paths_addresses_or_serials_are_replaced(campaign, tmp_path):
    _, led = campaign
    found = records_for(led)
    odd = ["/home/someone/secret", "10.1.2.3", "123456789012", "mail me@example.org"]
    first = valid_rows(led, "A")[0].episode_id
    found[first]["segments"] = block(["approach", *odd])
    write(tmp_path, campaign, subtasks=found)
    blob = "\n".join(
        p.read_text()
        for p in (tmp_path / BASIS).rglob("*")
        if p.suffix in TEXT_SUFFIXES
    )
    for bad in ("/home/someone", "10.1.2.3", "123456789012", "example.org"):
        assert bad not in blob
    assert "other" in (tmp_path / BASIS / "tables/subtasks.csv").read_text()
    # the live group and task folder names are keys only, never printed
    assert "grp/task" not in blob


# --- the key ----------------------------------------------------------------------------------------


def test_load_keys_records_like_a_legacy_client_ledger_row(tmp_path):
    one = {
        "dataset": "grp__task",
        "group": "grp",
        "task": "task",
        "demo": "demo_0001",
        "segments": block(FULL),
    }
    analysis.append(tmp_path, analysis.normalize({"schema": analysis.SCHEMA, **one}))
    newer = {**one, "segments": block(FULL[:2])}
    analysis.append(tmp_path, analysis.normalize({"schema": analysis.SCHEMA, **newer}))
    analysis.append(tmp_path, {"schema": analysis.SCHEMA, "dataset": "x", "demo": "d"})
    found = S.load(tmp_path)
    assert list(found) == ["grp/task/demo_0001"]  # the third names no group
    assert found["grp/task/demo_0001"]["segments"]["count"] == 2  # newest wins
    assert S.load(tmp_path / "missing") == {}


def test_a_legacy_client_ledger_is_matched_through_that_key(tmp_path):
    lay = fx.layout(per_arm=5, segment_trials=5)
    facts = fx.synthetic_facts(lay)
    counter = 0
    legacy = {}
    for run, items in facts.items():
        legacy[run] = []
        for item in items:
            counter += 1
            legacy[run].append(
                dataclasses.replace(item, episode_id=f"grp/task/demo_{counter:04d}")
            )
    led = L.derive(lay, legacy)
    for n, row in enumerate(led.rows, start=1):
        analysis.append(
            tmp_path,
            {
                "schema": analysis.SCHEMA,
                "dataset": "grp__task",
                "group": "grp",
                "task": "task",
                "demo": row.episode_id.rsplit("/", 1)[-1],
                "segments": block(FULL if n % 2 else FULL[:2]),
            },
        )
    data = S.analyse(led, BASIS, S.load(tmp_path))
    total = sum(a["with_segments"] for a in data["arms"])
    assert total == len([r for r in led.rows if r.status == "valid"])


@pytest.mark.parametrize("configured", [True, False])
def test_controller_loads_safely_located_live_records_into_real_report(
    tmp_path, monkeypatch, campaign, configured
):
    from levi import paths
    from levi.automatic.campaign import controller as C
    from levi.live import auto, jsonio, locate

    lay, original = campaign
    keys = {
        r.episode_id: "grp/task/" + r.episode_id.replace(".", "_")
        for r in original.rows
    }
    led = dataclasses.replace(
        original,
        rows=[
            dataclasses.replace(r, episode_id=keys[r.episode_id]) for r in original.rows
        ],
        labels={keys[k]: v for k, v in original.labels.items()},
    )
    live = tmp_path / "live-workspace"
    jsonio.write(live / "live" / auto.MARKER, {"schema": "levi.live.workspace.v1"})
    for row_ in led.rows:
        if row_.status == "valid":
            analysis.append(
                live / "live",
                {
                    "schema": analysis.SCHEMA,
                    "dataset": "grp__task",
                    "group": "grp",
                    "task": "task",
                    "demo": row_.episode_id.rsplit("/", 1)[-1],
                    "segments": block(FULL),
                },
            )
    product = tmp_path / "product"
    monkeypatch.setattr(paths, "ROOT", product)
    monkeypatch.setenv(locate.ENV_WORKSPACE, str(live if configured else product))
    assert bool(locate.find(product).workspace) is configured
    monkeypatch.setattr(C.A, "build_ledger", lambda *a, **k: (led, lay, {}))
    monkeypatch.setattr(C, "shared_task", lambda *a: "pick the eggplant")
    monkeypatch.setattr(C, "campaign_info", lambda *a, **k: info())
    monkeypatch.setattr(C, "REPORT_PDF", False)
    monkeypatch.setattr(C, "REPORT_RESAMPLES", 100)
    folder = tmp_path / "campaigns" / fx.CAMPAIGN
    folder.mkdir(parents=True)
    C.write_reports(
        folder,
        {"campaign_id": fx.CAMPAIGN, "campaign": {}, "schedule": {"seed": 7}},
        "guided",
        "REPORTED",
        [],
        rollout_root=tmp_path / "rollouts",
    )
    output = C.report_root(folder) / BASIS
    assert (output / "data/subtasks.json").is_file() is configured
    if configured:
        data = json.loads((output / "data/subtasks.json").read_text())
        assert [a["with_segments"] for a in data["arms"]] == [10, 10]
        assert "Subtask analysis" in (output / "summary.en.md").read_text()
