"""Formal report rendering over private synthetic labels, never historical data."""

import json

import pytest
from test_result_index import change_state, configured, write_header

from levi.live import cli, report, stats, statsfmt
from levi.live.config import Config


def classification_workspace(tmp_path, labels):
    previous, args = configured(tmp_path)
    config = Config()
    config.service.workspace = str(previous.workspace)
    config.service.home = str(tmp_path / "home")
    config.watch.roots = previous.watch.roots
    demos = {}
    source = args["rollout_root"] / "model-a/task-a"
    for n, (person, _) in enumerate(labels):
        demo = f"demo_{n:04d}"
        write_header(source, demo, "run-a")
        demos[demo] = {
            "state": "done",
            "run_id": "run-a",
            "episode_index": n,
            "operator_label": {"outcome": person},
        }
    change_state(args, lambda state: state.update(demos=demos))
    return config


@pytest.mark.parametrize(
    "lang,heading,precision,success,failure,pairs",
    [
        (
            "en",
            "Binary classification",
            "Precision",
            "success",
            "failure",
            "binary success/failure pairs",
        ),
        ("zh", "成败分类指标", "精确率", "成功", "失败", "成功/失败可靠配对数"),
    ],
)
def test_formal_cli_displays_existing_binary_metrics(
    tmp_path, monkeypatch, capsys, lang, heading, precision, success, failure, pairs
):
    labels = [
        ("success", "success"),
        ("success", "failure"),
        ("failure", "success"),
        ("failure", "failure"),
        ("success", "undecided"),
        ("failure", None),
    ]
    config = classification_workspace(tmp_path, labels)
    for n, (person, predicted) in enumerate(labels):
        stats.record(
            config.live_dir,
            {
                "schema": stats.SCHEMA,
                "dataset": "dataset-a",
                "demo": f"demo_{n:04d}",
                "session": "run-a",
                "at": 100.0 + n,
                "operator_label": {"outcome": person},
                "result": {
                    "state": "done",
                    "verdict": None
                    if predicted is None
                    else {"outcome": None, "undecided": True}
                    if predicted == "undecided"
                    else {"outcome": predicted},
                },
            },
        )
    monkeypatch.setattr(cli, "resolve_config", lambda args, **_kwargs: config)
    args = cli.build_parser().parse_args(["report", "--format", "md", "--lang", lang])
    assert cli.cmd_report(args) == 0
    md = capsys.readouterr().out
    built = report.build(config, now=300)
    classification = built["summary"]["agreement"]["classification"]
    assert classification == stats.binary_classification(1, 1, 1, 1)
    assert built["summary"]["agreement"]["pairs"] == 6
    assert f"### {heading}" in md and precision in md
    assert f"| {pairs} | 4 |" in md
    assert f"| {success} | 0.5 | 0.5 | 0.5 | 2 |" in md
    assert f"| {failure} | 0.5 | 0.5 | 0.5 | 2 |" in md
    assert json.loads(json.dumps(classification))["macro_f1"] == 0.5


def test_empty_or_legacy_classification_preserves_previous_report():
    found = stats.agreement([("failure", {"outcome": "failure"})])
    old = dict(found)
    old.pop("classification")
    for lang in ("en", "zh"):
        t = statsfmt.TEXT[lang]
        legacy = statsfmt.agreement_block(old, t)
        assert t["binary_classification"] not in "\n".join(legacy)
        md = "\n".join(statsfmt.agreement_block(found, t))
        assert f"| {t['class_success']} | - | - | - | 0 |" in md
        assert f"| {t['macro_f1']} | - |" in md
        assert f"| {t['balanced_accuracy']} | - |" in md
        assert statsfmt.agreement_block(stats.agreement([]), t) == []


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_formal_cli_keeps_three_digit_noninteger_ratios(
    tmp_path, monkeypatch, capsys, lang
):
    labels = (
        [("success", "success")]
        + [("success", "failure")] * 2
        + [("failure", "success")] * 2
        + [("failure", "failure")] * 4
    )
    config = classification_workspace(tmp_path, labels)
    for n, (person, predicted) in enumerate(labels):
        stats.record(
            config.live_dir,
            {
                "schema": stats.SCHEMA,
                "dataset": "dataset-a",
                "demo": f"demo_{n:04d}",
                "session": "run-a",
                "at": 100.0 + n,
                "operator_label": {"outcome": person},
                "result": {"state": "done", "verdict": {"outcome": predicted}},
            },
        )
    monkeypatch.setattr(cli, "resolve_config", lambda args, **_kwargs: config)
    args = cli.build_parser().parse_args(["report", "--format", "md", "--lang", lang])
    assert cli.cmd_report(args) == 0
    md = capsys.readouterr().out
    t = statsfmt.TEXT[lang]
    assert f"| {t['class_success']} | 0.333 | 0.333 | 0.333 | 3 |" in md
    assert f"| {t['class_failure']} | 0.667 | 0.667 | 0.667 | 6 |" in md
    assert f"| {t['classification_accuracy']} | 0.556 |" in md
    assert f"| {t['binary_pairs']} | 9 |" in md
    assert (
        report.build(config)["summary"]["agreement"]["classification"]["accuracy"]
        == 5 / 9
    )
