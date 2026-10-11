"""Formal report rendering over private synthetic labels, never historical data."""

import json

import pytest

from levi.live import cli, report, stats, statsfmt
from levi.live.config import Config


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
    config = Config()
    config.service.workspace = str(tmp_path / "ws")
    config.service.home = str(tmp_path / "home")
    labels = [
        ("success", "success"),
        ("success", "failure"),
        ("failure", "success"),
        ("failure", "failure"),
        ("success", "undecided"),
        ("failure", None),
    ]
    for n, (person, predicted) in enumerate(labels):
        stats.record(
            config.live_dir,
            {
                "schema": stats.SCHEMA,
                "dataset": "model__task",
                "demo": f"demo_{n}",
                "session": "s1",
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
    monkeypatch.setattr(cli, "resolve_config", lambda args: config)
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
