"""The arm adapters (levi/eval/adapters): runs of arms A, B and C to arm_record.v2.

The artifacts are the small synthetic files in tests/fixtures/eval_adapters,
copied and changed here; nothing reads a dataset, calls a model or opens a port.
"""

import json
import shutil
from pathlib import Path

import pytest

from levi.eval import compare
from levi.eval.adapters import cli, common, hub, levi2
from levi.eval.adapters.common import AdapterError
from levi.eval.compare import ArmRecord

FIXTURES = Path(__file__).parent / "fixtures" / "eval_adapters"
BASIS = "excludes_queue"


@pytest.fixture
def work(tmp_path):
    folder = tmp_path / "in"
    shutil.copytree(FIXTURES, folder)
    return folder


def load(folder, name):
    return json.loads((folder / name).read_text())


def save(folder, name, value):
    (folder / name).write_text(json.dumps(value))
    return folder / name


def the_round(folder, **over):
    row = {**load(folder, "round.json"), **over}
    row = {k: v for k, v in row.items() if v is not None}
    return common.load_round(save(folder, "round.json", row))


DIRECT = ("derived_outcome.overall.accuracy",)


def run_direct(folder, **over):
    options = {"wall_basis": BASIS, "quality_path": DIRECT[0]}
    options.update(over)
    return hub.direct(folder / "direct_result.json", the_round(folder), **options)


def run_prior(folder, **over):
    options = {
        "wall_basis": BASIS,
        "quality_path": "derived_outcome.overall.accuracy",
        "ledger_path": folder / "prior_ledger.json",
    }
    options.update(over)
    return hub.prior_levi(folder / "prior_result.json", the_round(folder), **options)


def run_levi2(folder, **over):
    options = {
        "wall_basis": "includes_queue",
        "result_path": folder / "prior_result.json",
        "ledger_path": folder / "prior_ledger.json",
        "external_path": folder / "monitor.json",
        "external_task": "panel",
        "scores": common.load_scores(folder / "scores.json"),
    }
    options.update(over)
    return levi2.levi2(folder / "routes.jsonl", the_round(folder), **options)


def refuses(call, *args, match=None, **kwargs):
    with pytest.raises(AdapterError, match=match) as raised:
        call(*args, **kwargs)
    return str(raised.value)


# ----------------------------------------------------------------- arm A


def test_direct_normal_record_is_valid(work):
    row, notes = run_direct(work)
    record = ArmRecord.from_dict(row)
    assert record.arm == "direct" and record.split == "tune"
    assert record.quality == 0.75 and record.scored_count == record.episode_count == 4
    assert record.wall_s == 120.5 and record.wall_s_basis == BASIS
    assert record.local_tokens == 4400  # the sum of the phases
    assert record.external_tokens == dict.fromkeys(compare.TOKEN_FIELDS, 0)
    assert record.external_tokens_exact is True
    assert record.external_tokens_source == "local_backend.vllm"
    assert record.blocked is None and notes == []
    assert row["schema"] == compare.ARM_SCHEMA


def test_direct_missing_fields_are_null_not_zero(work):
    result = load(work, "direct_result.json")
    for key in ("phases", "run_elapsed_seconds", "backend"):
        del result[key]
    save(work, "direct_result.json", result)
    row, notes = run_direct(work)
    assert row["local_tokens"] is None
    assert row["wall_s"] is None and row["wall_s_basis"] is None
    assert row["external_tokens"] is None and row["external_tokens_source"] is None
    assert len(notes) == 3


def test_direct_one_missing_phase_token_makes_the_total_unknown(work):
    result = load(work, "direct_result.json")
    del result["phases"][2]["tokens"]
    result["phases"][2]["error"] = "failed"
    save(work, "direct_result.json", result)
    row, notes = run_direct(work)
    assert row["local_tokens"] is None  # the file's own total would say 3200
    assert any("no token count" in n for n in notes)


def test_direct_a_missing_phase_makes_the_total_unknown(work):
    result = load(work, "direct_result.json")
    del result["phases"][0]
    save(work, "direct_result.json", result)
    assert run_direct(work)[0]["local_tokens"] is None


def test_direct_quality_path_naming_nothing_is_null(work):
    row, notes = run_direct(work, quality_path="grades.nope.overall.f1")
    assert row["quality"] is None and row["scored_count"] is None
    assert any("quality" in n for n in notes)


def test_direct_status_not_succeeded_is_a_code_in_blocked(work):
    result = load(work, "direct_result.json")
    result["status"] = "gpu busy"
    save(work, "direct_result.json", result)
    assert run_direct(work)[0]["blocked"] == "status.gpu_busy"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(run_elapsed_seconds="fast"),
        lambda r: r.update(run_elapsed_seconds=0),
        lambda r: r.update(run_elapsed_seconds=-3.0),
        lambda r: r["phases"][0].update(tokens="many"),
        lambda r: r["phases"][0].update(tokens=-1),
        lambda r: r.update(phases={"a": 1}),
        lambda r: r.update(episodes=[]),
        lambda r: r.update(episodes="3,5,8,9"),
        lambda r: r.update(status=7),
        lambda r: r["derived_outcome"]["overall"].update(accuracy="high"),
    ],
)
def test_direct_malformed_input_is_refused_and_nothing_written(work, mutate):
    result = load(work, "direct_result.json")
    mutate(result)
    save(work, "direct_result.json", result)
    out = work / "out.json"
    code = cli.main(
        [
            "direct",
            "--round",
            str(work / "round.json"),
            "--result",
            str(work / "direct_result.json"),
            "--wall-basis",
            BASIS,
            "--quality-path",
            DIRECT[0],
            "--out",
            str(out),
        ]
    )
    assert code == 2
    assert not out.exists() and not Path(f"{out}.partial").exists()


@pytest.mark.parametrize(
    "text", ["{", "[1, 2", "NaN", '{"a": 1, "a": 2}', '{"x": Infinity}', ""]
)
def test_direct_unparseable_files_are_refused(work, text):
    (work / "direct_result.json").write_text(text)
    refuses(run_direct, work, match="not valid JSON")


def test_direct_refuses_a_levi_result_and_prior_refuses_a_direct_one(work):
    shutil.copy(work / "prior_result.json", work / "direct_result.json")
    refuses(run_direct, work, match="not a direct-arm result")
    shutil.copy(FIXTURES / "direct_result.json", work / "prior_result.json")
    refuses(run_prior, work, match="levi flag")


def test_direct_refuses_another_problem_set_without_naming_the_ids(work):
    result = load(work, "direct_result.json")
    result["episodes"] = [3, 5, 8, 12345]
    save(work, "direct_result.json", result)
    message = refuses(run_direct, work, match="different problem set")
    assert "12345" not in message


def test_direct_refuses_a_decoding_that_differs_from_the_declared_one(work):
    result = load(work, "direct_result.json")
    result["config"]["sampling"] = "model"
    save(work, "direct_result.json", result)
    refuses(run_direct, work, match="config.sampling differs")
    result["config"]["sampling"] = "greedy"
    result["config"]["max_tokens"] = 8192.0  # 8192 and 8192.0 are one value
    save(work, "direct_result.json", result)
    run_direct(work)


def test_direct_refuses_another_model_than_declared(work):
    result = load(work, "direct_result.json")
    result["model"] = "other-model"
    save(work, "direct_result.json", result)
    refuses(run_direct, work, match="model differs")


def test_direct_quality_without_a_count_is_refused(work):
    result = load(work, "direct_result.json")
    del result["derived_outcome"]["overall"]["episodes"]
    save(work, "direct_result.json", result)
    refuses(run_direct, work, match="scored_count")


def test_a_short_score_is_written_with_a_note_and_compare_refuses_it(work):
    result = load(work, "direct_result.json")
    result["derived_outcome"]["overall"]["episodes"] = 3
    save(work, "direct_result.json", result)
    row, notes = run_direct(work)
    assert row["scored_count"] == 3
    assert any("below episode_count" in n for n in notes)
    teacher = teacher_record(work)
    with pytest.raises(compare.CompareError, match="scored 3 of 4"):
        compare.compare([teacher, ArmRecord.from_dict(row)])


# ----------------------------------------------------------------- arm B


def test_prior_normal_record_is_valid(work):
    row, notes = run_prior(work)
    record = ArmRecord.from_dict(row)
    assert record.arm == "prior_levi"
    assert record.quality == 0.5 and record.scored_count == 4
    # the ledger's wall time, not the model's run_elapsed_seconds (40.0)
    assert record.wall_s == 280.0 and record.wall_s_basis == BASIS
    assert record.local_tokens == 5200
    assert record.external_tokens == dict.fromkeys(compare.TOKEN_FIELDS, 0)
    assert record.external_tokens_source == "ledger.provider_kind.openai-local"
    assert notes == []


def test_prior_without_a_ledger_uses_the_driver_wall_and_leaves_external_unknown(work):
    row, notes = run_prior(work, ledger_path=None)
    assert row["wall_s"] == 300.2
    assert row["external_tokens"] is None and row["external_tokens_exact"] is None
    assert any("external_tokens" in n for n in notes)
    row, _ = run_prior(work, ledger_path=None, no_external_model=True)
    assert row["external_tokens_source"] == "declared.no_external_model"


def test_prior_never_uses_the_model_time_as_wall_time(work):
    result = load(work, "prior_result.json")
    del result["driver_wall_seconds"]
    save(work, "prior_result.json", result)
    row, notes = run_prior(work, ledger_path=None)
    assert row["wall_s"] is None and row["wall_s_basis"] is None
    assert any("model's time" in n for n in notes)


def test_prior_missing_tokens_are_null_not_zero(work):
    result = load(work, "prior_result.json")
    result["tokens"] = None
    save(work, "prior_result.json", result)
    ledger = load(work, "prior_ledger.json")
    ledger["cost"]["tokens"]["metered"] = None
    save(work, "prior_ledger.json", ledger)
    assert run_prior(work)[0]["local_tokens"] is None
    ledger["cost"]["tokens"]["metered"] = 0
    result["tokens"] = None
    save(work, "prior_ledger.json", ledger)
    assert run_prior(work)[0]["local_tokens"] == 0  # a measured zero stays 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r, ledger: r.update(tokens=-5),
        lambda r, ledger: r.update(tokens=2.5),
        lambda r, ledger: r.update(driver_wall_seconds="slow"),
        lambda r, ledger: ledger.update(schema="levi.harness.ledger.v0"),
        lambda r, ledger: ledger.update(run_id="another-run"),
        lambda r, ledger: ledger["cost"]["tokens"].update(metered=1),  # disagrees
        lambda r, ledger: ledger["cost"].update(model="other-model"),
        lambda r, ledger: ledger.update(wall_seconds=0),
    ],
)
def test_prior_malformed_or_inconsistent_input_is_refused(work, mutate):
    result, ledger = load(work, "prior_result.json"), load(work, "prior_ledger.json")
    mutate(result, ledger)
    save(work, "prior_result.json", result)
    save(work, "prior_ledger.json", ledger)
    refuses(run_prior, work)


def test_prior_ledger_status_becomes_a_blocked_code(work):
    ledger = load(work, "prior_ledger.json")
    ledger["status"] = "failed"
    save(work, "prior_ledger.json", ledger)
    assert run_prior(work)[0]["blocked"] == "status.failed"


# ----------------------------------------------------------------- arm C


def test_levi2_normal_record_is_valid(work):
    row, notes = run_levi2(work)
    record = ArmRecord.from_dict(row)
    assert record.arm == "levi2"
    assert record.graded == {
        "local_answered_count": 2,
        "escalated_count": 1,
        "pending_count": 1,
        "with_human_count": 0,
        "coverage": 0.5,
        "escalation_rate": 0.5,
    }
    assert record.external_tokens == {
        "input_new": 1200,
        "output": 300,
        "cache_read": 40000,
        "cache_write": 5000,
    }
    assert record.external_tokens_exact is True
    assert record.external_tokens_source == "token_monitor.v1:exact"
    assert record.wall_s == 280.0 and record.wall_s_basis == "includes_queue"
    assert record.local_tokens == 5200
    assert record.quality == 0.9 and record.scored_count == 4
    assert record.quality_delta_ci_vs_teacher == (-0.1, 0.1)
    assert notes == []


def test_levi2_does_not_take_zero_external_tokens_from_the_local_run(work):
    # the ledger says a local provider; for arm C that is the local part only
    row, notes = run_levi2(work, external_path=None, external_task=None)
    assert row["external_tokens"] is None and row["external_tokens_exact"] is None
    assert any("external_tokens" in n for n in notes)


def test_levi2_only_the_route_log_leaves_everything_else_null(work):
    row, notes = levi2.levi2(work / "routes.jsonl", the_round(work), wall_basis=BASIS)
    for name in (
        "wall_s",
        "wall_s_basis",
        "local_tokens",
        "quality",
        "scored_count",
        "external_tokens",
    ):
        assert row[name] is None
    assert row["graded"]["coverage"] == 0.5
    assert len(notes) >= 2


def test_levi2_monitor_field_null_and_lower_bound_stay_as_they_are(work):
    report = load(work, "monitor.json")
    report["tasks"]["panel"]["tokens"]["cache_read"] = None
    report["tasks"]["panel"]["exact"] = False
    save(work, "monitor.json", report)
    row, notes = run_levi2(work)
    assert row["external_tokens"]["cache_read"] is None
    assert row["external_tokens_exact"] is False
    assert row["external_tokens_source"] == "token_monitor.v1:lower_bound"
    assert any("null in the report" in n for n in notes)


def write_routes(folder, rows):
    (folder / "routes.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def good_rows(folder):
    return [json.loads(x) for x in (folder / "routes.jsonl").read_text().splitlines()]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda rows: rows.pop(),  # a problem not covered
        lambda rows: rows.append({**rows[0]}),  # a problem twice
        lambda rows: rows[0].update(episode=99),  # undeclared problem
        lambda rows: rows[0].update(
            state="answered_external"
        ),  # route and state disagree
        lambda rows: rows[2].update(state="answered_local"),
        lambda rows: rows[0].update(route="elsewhere"),
        lambda rows: rows[0].update(reason="because"),
        lambda rows: rows[0].update(label_tier="human"),
        lambda rows: rows[0].update(p_cal=1.5),
        lambda rows: rows[0].update(answer="success"),  # a key that is not on the row
        lambda rows: rows[0].pop("state"),
        lambda rows: rows[0].update(episode=True),
        lambda rows: rows.__setitem__(0, [1, 2]),
        lambda rows: rows[0].update(contract="levi.decision.route.v0"),
    ],
)
def test_levi2_bad_route_logs_are_refused(work, mutate):
    rows = good_rows(work)
    mutate(rows)
    write_routes(work, rows)
    refuses(run_levi2, work)


def test_levi2_a_human_route_is_counted_but_is_neither_local_nor_escalated(work):
    rows = good_rows(work)
    rows[3] = {
        "episode": 9,
        "route": "human",
        "reason": "physical_safety",
        "state": "with_human",
    }
    write_routes(work, rows)
    graded = run_levi2(work)[0]["graded"]
    assert graded["with_human_count"] == 1 and graded["pending_count"] == 0
    assert graded["escalation_rate"] == 0.25


def test_levi2_external_arguments_go_together_and_the_label_must_exist(work):
    refuses(run_levi2, work, external_task=None, match="--external-task")
    refuses(run_levi2, work, external_path=None, match="--external-tokens")
    refuses(run_levi2, work, external_task="other", match="no task with that label")
    report = load(work, "monitor.json")
    report["schema"] = "levi.eval.token_monitor.v0"
    save(work, "monitor.json", report)
    refuses(run_levi2, work, match="token_monitor")


def test_levi2_a_ledger_or_quality_path_needs_a_result(work):
    refuses(
        levi2.levi2,
        work / "routes.jsonl",
        the_round(work),
        wall_basis=BASIS,
        ledger_path=work / "prior_ledger.json",
        match="--ledger needs --result",
    )
    refuses(
        levi2.levi2,
        work / "routes.jsonl",
        the_round(work),
        wall_basis=BASIS,
        quality_path="a.b",
        match="needs --result",
    )


# ------------------------------------------------ the rules all three share


@pytest.mark.parametrize("bad", ["dev", "Tune", "", "frozen", 1, None, ["tune"]])
def test_split_must_be_one_of_the_three_and_is_never_inferred(work, bad):
    row = load(work, "round.json")
    row["split"] = bad
    save(work, "round.json", row)
    refuses(common.load_round, work / "round.json", match="split must be one of")


def test_split_is_required_in_the_declaration(work):
    row = load(work, "round.json")
    del row["split"]
    save(work, "round.json", row)
    refuses(common.load_round, work / "round.json", match="lacks")


@pytest.mark.parametrize("name", ["round.json"])
def test_round_declaration_refuses_unknown_fields_and_bad_problem_sets(work, name):
    row = load(work, name)
    save(work, name, {**row, "notes": "x"})
    refuses(common.load_round, work / name, match="unknown fields")
    save(work, name, {**row, "episode_set_hash": "0" * 64})
    refuses(common.load_round, work / name, match="does not match")
    row.pop("episodes")
    save(work, name, row)
    refuses(common.load_round, work / name, match="needs episodes")


def test_wall_basis_is_required_and_checked(work):
    refuses(run_direct, work, wall_basis=None, match="wall_s_basis must be one of")
    refuses(run_prior, work, wall_basis="whenever", match="wall_s_basis must be one of")
    refuses(run_levi2, work, wall_basis="", match="wall_s_basis must be one of")
    with pytest.raises(SystemExit):
        cli.main(["direct", "--round", "r", "--result", "x", "--out", "o"])


def test_the_output_is_new_and_never_replaces_a_file(work):
    out = work / "record.json"
    out.write_text("keep me")
    args = [
        "direct",
        "--round",
        str(work / "round.json"),
        "--result",
        str(work / "direct_result.json"),
        "--wall-basis",
        BASIS,
        "--quality-path",
        DIRECT[0],
        "--out",
        str(out),
    ]
    assert cli.main(args) == 2
    assert out.read_text() == "keep me"
    assert not Path(f"{out}.partial").exists()
    # a leftover .partial from another run is not overwritten either
    out.unlink()
    Path(f"{out}.partial").write_text("other run")
    assert cli.main(args) == 2
    assert Path(f"{out}.partial").read_text() == "other run"
    Path(f"{out}.partial").unlink()
    assert cli.main(args) == 0
    assert json.loads(out.read_text())["arm"] == "direct"
    assert not Path(f"{out}.partial").exists()


def test_output_in_a_missing_directory_or_a_symlink_to_a_file_is_refused(work):
    refuses(common.write, {"a": 1}, work / "nope" / "x.json", match="does not exist")
    target = work / "t.json"
    target.write_text("t")
    link = work / "l.json"
    link.symlink_to(target)
    refuses(common.write, {"a": 1}, link, match="already exists")
    assert target.read_text() == "t"
    dangling = work / "dangling.json"
    dangling.symlink_to(work / "missing-target")
    refuses(common.write, {"a": 1}, dangling, match="already exists")
    assert not (work / "missing-target").exists()


def test_a_failed_write_leaves_no_partial_file(work, monkeypatch):
    out = work / "record.json"

    def boom(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(common.os, "link", boom)
    with pytest.raises(OSError):
        common.write({"a": 1}, out)
    assert not out.exists() and not Path(f"{out}.partial").exists()


def test_paths_in_a_gold_working_folder_are_refused_as_input_and_output(work):
    gold = work / "levi-gold-work" / "x"
    gold.mkdir(parents=True)
    shutil.copy(work / "direct_result.json", gold / "direct_result.json")
    shutil.copy(work / "round.json", gold / "round.json")
    refuses(
        hub.direct,
        gold / "direct_result.json",
        the_round(work),
        wall_basis=BASIS,
        match="gold working folder",
    )
    refuses(common.load_round, gold / "round.json", match="gold working folder")
    refuses(common.write, {"a": 1}, gold / "out.json", match="gold working folder")
    for part in ("screws-frozen", "generic-frozen", "generic-heldout"):
        refuses(
            common.read_text, work / part / "f.json", "x", match="gold working folder"
        )


def test_scores_file_takes_only_scores(work):
    scores = load(work, "scores.json")
    save(work, "scores.json", {**scores, "wall_s": 1.0})
    refuses(common.load_scores, work / "scores.json", match="not scores")
    save(work, "scores.json", {**scores, "schema": "other"})
    refuses(common.load_scores, work / "scores.json", match="schema")


def test_quality_from_both_a_path_and_a_scores_file_is_refused(work):
    refuses(
        run_direct,
        work,
        scores={"quality": 0.5, "scored_count": 4},
        match="quality is given",
    )


def test_a_scores_file_can_carry_quality_and_the_interval_for_arm_a(work):
    row, _ = run_direct(
        work, quality_path=None, scores=common.load_scores(work / "scores.json")
    )
    assert row["quality"] == 0.9 and row["quality_ci"] == [0.6, 1.0]
    assert row["agreement_with_teacher"] == 0.9


# ------------------------------------------------------------- confirm


def confirm_round(folder, **over):
    row = load(folder, "round.json")
    row.pop("episodes", None)
    row.update(
        split="confirm",
        episode_set_hash=common.episode_set_hash([3, 5, 8, 9]),
        episode_count=4,
        decoding={"sampling": "greedy", "max_tokens": 8192},
        model="qwen3.8-27b",
    )
    row.update(over)
    return save(folder, "round.json", row)


def test_confirm_records_hold_aggregates_and_codes_only(work):
    confirm_round(work)
    for build in (run_direct, run_prior, run_levi2):
        row, _ = build(work)
        assert row["split"] == "confirm"
        assert row["per_episode"] is None and row["episode_set"] is None
        assert ArmRecord.from_dict(row).confirm_violations() == []


def test_confirm_refuses_per_episode_scores_and_free_text_without_repeating_them(work):
    confirm_round(work)
    scores = {"quality": 0.5, "scored_count": 4, "per_episode": {"3": 1.0}}
    message = refuses(
        run_direct, work, quality_path=None, scores=scores, match="per_episode"
    )
    assert '"3"' not in message and "1.0" not in message
    confirm_round(work, model="Qwen 3.8 27B secret-text")
    result = load(work, "direct_result.json")
    result["model"] = None
    save(work, "direct_result.json", result)
    message = refuses(run_direct, work, match="model text")
    assert "secret-text" not in message


def test_confirm_refuses_free_text_in_the_decoding(work):
    confirm_round(work, decoding={"note": "this is a sentence with spaces"})
    result = load(work, "direct_result.json")
    del result["config"]
    save(work, "direct_result.json", result)
    message = refuses(run_direct, work, match="decoding")
    assert "sentence" not in message


def test_confirm_with_an_explicit_episode_list_is_refused(work):
    row = load(work, "round.json")
    row["split"] = "confirm"
    save(work, "round.json", row)
    refuses(run_direct, work, match="explicit episode_set")


# ------------------------------------------------------- compare round trip


def teacher_record(folder):
    return ArmRecord(
        arm="teacher",
        task="t2_outcome",
        model="claude-x",
        decoding=load(folder, "round.json")["decoding"],
        scoring=load(folder, "round.json")["scoring"],
        split=load(folder, "round.json")["split"],
        episode_set_hash=common.episode_set_hash([3, 5, 8, 9]),
        episode_count=4,
        scored_count=4,
        quality=1.0,
        wall_s=900.0,
        wall_s_basis="includes_queue",
        local_tokens=0,
        external_tokens={
            "input_new": 9000,
            "output": 2000,
            "cache_read": 90000,
            "cache_write": 8000,
        },
        external_tokens_exact=True,
    )


@pytest.mark.parametrize("split", ["tune", "confirm"])
def test_the_four_records_go_into_one_table(work, split):
    if split == "confirm":
        confirm_round(work)
    records = [teacher_record(work)]
    for build in (run_direct, run_prior, run_levi2):
        records.append(ArmRecord.from_dict(build(work)[0]))
    table = compare.compare(records, assert_confirm=split == "confirm")
    assert table.missing_arms == []
    assert {row["arm"] for row in table.rows} == {
        "teacher",
        "direct",
        "prior_levi",
        "levi2",
    }
    assert table.conditions["split"] == split
    levi_row = next(r for r in table.rows if r["arm"] == "levi2")
    assert levi_row["quality"] == 0.9


def test_a_record_written_by_the_cli_is_read_by_the_compare_command(work, capsys):
    outs = []
    for name, extra in (
        ("direct", ["--result", "direct_result.json", "--quality-path", DIRECT[0]]),
        (
            "prior-levi",
            [
                "--result",
                "prior_result.json",
                "--ledger",
                "prior_ledger.json",
                "--quality-path",
                DIRECT[0],
            ],
        ),
        (
            "levi2",
            [
                "--route-log",
                "routes.jsonl",
                "--result",
                "prior_result.json",
                "--ledger",
                "prior_ledger.json",
                "--external-tokens",
                "monitor.json",
                "--external-task",
                "panel",
                "--scores",
                "scores.json",
            ],
        ),
    ):
        out = work / f"{name}.rec.json"
        argv = [name, "--round", "round.json", "--wall-basis", BASIS, "--out", str(out)]
        flags = []
        it = iter(extra)
        for flag in it:
            flags += [
                flag,
                str(work / next(it))
                if flag != "--quality-path" and flag != "--external-task"
                else next(it),
            ]
        argv[argv.index("round.json")] = str(work / "round.json")
        assert cli.main(argv + flags) == 0, capsys.readouterr().err
        outs.append(str(out))
    save(work, "teacher.rec.json", teacher_record(work).to_dict())
    assert compare.main([str(work / "teacher.rec.json"), *outs]) == 0
    assert "levi2" in capsys.readouterr().out


def test_the_three_cli_commands_print_their_notes_and_the_path(work, capsys):
    result = load(work, "direct_result.json")
    del result["phases"]
    save(work, "direct_result.json", result)
    out = work / "o.json"
    assert (
        cli.main(
            [
                "direct",
                "--round",
                str(work / "round.json"),
                "--result",
                str(work / "direct_result.json"),
                "--wall-basis",
                BASIS,
                "--out",
                str(out),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "wrote" in captured.out and "note: local_tokens" in captured.err
    assert json.loads(out.read_text())["local_tokens"] is None


def test_refusal_goes_to_stderr_with_exit_code_two(work, capsys):
    out = work / "o.json"
    row = load(work, "round.json")
    row["split"] = "dev"
    save(work, "round.json", row)
    assert (
        cli.main(
            [
                "direct",
                "--round",
                str(work / "round.json"),
                "--result",
                str(work / "direct_result.json"),
                "--wall-basis",
                BASIS,
                "--out",
                str(out),
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.err.startswith("refused:") and captured.out == ""
    assert not out.exists()


def test_a_file_that_appears_between_the_check_and_the_link_is_not_replaced(
    work, monkeypatch
):
    out = work / "record.json"
    out.write_text("keep me")
    monkeypatch.setattr(common.os.path, "lexists", lambda p: False)  # lost the race
    refuses(common.write, {"a": 1}, out, match="already exists")
    assert out.read_text() == "keep me"
    assert not Path(f"{out}.partial").exists()
