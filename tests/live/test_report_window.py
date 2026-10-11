"""Private synthetic files only; record time is not operator label time."""

import csv
import io
import json

import pytest

from levi.live import cli, report, report_scope, stats
from levi.live.config import Config


def synthetic(tmp_path):
    config = Config()
    config.service.workspace = str(tmp_path / "ws")
    config.service.home = str(tmp_path / "home")
    config.live_dir.mkdir(parents=True)
    root = tmp_path / "rollouts"
    source = root / "g/t"
    source.mkdir(parents=True)
    config.watch.roots = [str(root)]
    sessions = root / ".eval_sessions"
    sessions.mkdir()
    (sessions / "g-t.json").write_text(
        json.dumps(
            {
                "schema": "levi.eval.session.v1",
                "group": "g",
                "task_folder": "t",
                "run_id": "s1",
                "session_id": "s1",
                "state": "finished",
            }
        )
    )
    states = config.live_dir / "datasets"
    states.mkdir()
    demos = {}
    for n, stamp in enumerate((100.0, 200.0, 300.0)):
        demo = f"demo_{n}"
        (source / demo).mkdir()
        (source / demo / "metadata.json").write_text(
            json.dumps({"eval": {"run_id": "s1"}})
        )
        demos[demo] = {"run_id": "s1", "state": "done"}
        stats.record(
            config.live_dir,
            {
                "schema": stats.SCHEMA,
                "dataset": "g__t",
                "demo": f"demo_{n}",
                "session": "s1",
                "at": stamp,
                "result": {"state": "done"},
            },
        )
    (states / "g__t.json").write_text(
        json.dumps(
            {
                "group": "g",
                "task_folder": "t",
                "source": str(source),
                "demos": demos,
            }
        )
    )
    return config


@pytest.mark.parametrize("fmt", ["json", "csv", "md"])
def test_report_since_reaches_real_statistics_not_backlog(
    tmp_path, monkeypatch, capsys, fmt
):
    config = synthetic(tmp_path)
    config.watch.since = "original backlog cutoff"
    seen = []

    def resolve(args):
        seen.append(args.since)
        return config

    monkeypatch.setattr(cli, "resolve_config", resolve)
    args = cli.build_parser().parse_args(
        [
            "report",
            "--since",
            "1970-01-01T00:03:20+00:00",
            "--format",
            fmt,
        ]
    )
    assert cli.cmd_report(args) == 0
    text = capsys.readouterr().out
    if fmt == "json":
        payload = json.loads(text)
        assert payload["episodes"]["total"] == 2
        assert payload["scope"]["since"] == 200.0
    elif fmt == "csv":
        assert len(list(csv.DictReader(io.StringIO(text)))) == 2
    else:
        assert "demo_0" not in text and "demo_1" in text and "demo_2" in text
    assert seen == [None] and config.watch.since == "original backlog cutoff"


@pytest.mark.parametrize("flag", ["--before", "--until"])
@pytest.mark.parametrize("fmt", ["json", "csv", "md"])
def test_cutoff_is_exclusive_and_timezone_is_explicit(
    tmp_path, monkeypatch, capsys, flag, fmt
):
    config = synthetic(tmp_path)
    monkeypatch.setattr(cli, "resolve_config", lambda args: config)
    args = cli.build_parser().parse_args(
        [
            "report",
            "--since",
            "1970-01-01T08:03:20",
            flag,
            "1970-01-01T08:05:00",
            "--timezone",
            "Asia/Shanghai",
            "--format",
            fmt,
        ]
    )
    assert cli.cmd_report(args) == 0
    text = capsys.readouterr().out
    if fmt == "json":
        payload = json.loads(text)
        assert payload["episodes"]["total"] == 1
        assert payload["scope"] == {
            "dataset": None,
            "session": None,
            "since": 200.0,
            "before": 300.0,
            "timezone": "Asia/Shanghai",
            "time_basis": "stats_record_at",
        }
        assert payload["signature"]["records"] == 1
    elif fmt == "csv":
        assert len(list(csv.DictReader(io.StringIO(text)))) == 1
    else:
        assert "demo_0" not in text and "demo_1" in text and "demo_2" not in text


def test_default_report_keeps_all_rows_and_default_scope(tmp_path):
    got = report.build(synthetic(tmp_path))
    assert got["episodes"]["total"] == 3
    assert got["scope"] == {"dataset": None, "session": None, "since": None}


@pytest.mark.parametrize(
    "extra",
    [
        ["--since", "invalid"],
        ["--before", "invalid"],
        ["--before", "inf"],
        ["--timezone", "not/a-zone"],
        ["--since", "300", "--before", "200"],
        ["--before", "2026-11-01T01:30:00", "--timezone", "America/New_York"],
        ["--before", "2026-03-08T02:30:00", "--timezone", "America/New_York"],
    ],
)
def test_invalid_window_refused_before_config_or_output(tmp_path, monkeypatch, extra):
    monkeypatch.setattr(
        cli,
        "resolve_config",
        lambda args: pytest.fail("configuration read before invalid window rejected"),
    )
    out = tmp_path / "absent" / "report.json"
    args = cli.build_parser().parse_args(["report", "--out", str(out), *extra])
    with pytest.raises(ValueError):
        cli.cmd_report(args)
    assert not out.parent.exists()


def test_bounds_exclude_unknown_time_and_count_hidden_in_same_window():
    rows = [
        {"dataset": "g__t", "demo": "unknown", "at": None},
        {"dataset": "g__t", "demo": "outside", "at": 100.0, "excluded": True},
        {"dataset": "g__t", "demo": "inside", "at": 200.0, "excluded": True},
        {"dataset": "g__t", "demo": "boundary", "at": 300.0},
    ]
    assert len(stats.select(rows)) == 4
    assert [r["demo"] for r in stats.select(rows, since=200.0, before=300.0)] == [
        "inside"
    ]
    assert stats.select(rows, since=200.0, before=300.0, include_excluded=False) == []


def test_window_cache_never_reuses_different_cutoff_or_zone(tmp_path):
    from levi.live import statsview

    config = synthetic(tmp_path)
    first = statsview.build(config, before=200.0, timezone="UTC", limit=None)
    second = statsview.build(config, before=300.0, timezone="UTC", limit=None)
    third = statsview.build(config, before=300.0, timezone="Asia/Shanghai", limit=None)
    assert first["episodes"]["total"] == 1
    assert second["episodes"]["total"] == third["episodes"]["total"] == 2
    assert first["scope"]["before"] == 200.0
    assert third["scope"]["timezone"] == "Asia/Shanghai"


def test_explicit_dst_offset_is_absolute_and_naive_default_is_utc():
    from levi.live.report_scope import window

    assert window("1970-01-01T00:00:00", "1970-01-01T00:01:00") == (0.0, 60.0, "UTC")
    start, end, zone = window(
        "2026-11-01T01:30:00-04:00", "2026-11-01T01:30:00-05:00", "America/New_York"
    )
    assert end - start == 3600.0 and zone == "America/New_York"


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_markdown_window_metadata_uses_explicit_zone_not_host_clock(
    tmp_path, monkeypatch, lang
):
    from levi.live import statsfmt

    payload = report.build(
        synthetic(tmp_path),
        since=0.0,
        before=200.0,
        timezone="Asia/Shanghai",
        now=300.0,
    )
    monkeypatch.setattr(
        statsfmt.time,
        "localtime",
        lambda *args: pytest.fail("filtered report used host-local timezone"),
    )
    text = report.render(payload, lang)
    assert "[1970-01-01T08:00:00+08:00, 1970-01-01T08:03:20+08:00)" in text
    assert "Asia/Shanghai" in text and "stats_record_at" in text
    assert "1970-01-01T08:05:00+08:00" in text
    assert ("Time basis" if lang == "en" else "时间口径") in text


def test_cutoff_only_markdown_does_not_claim_an_unfiltered_scope(tmp_path):
    text = report.render(
        report.build(synthetic(tmp_path), before=200.0, timezone="UTC")
    )
    assert "[-∞, 1970-01-01T00:03:20+00:00)" in text
    assert "Timezone: UTC" in text and "stats_record_at" in text


def test_unrepresentable_epoch_refused_before_config(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cli,
        "resolve_config",
        lambda args: pytest.fail("invalid range reached configuration"),
    )
    args = cli.build_parser().parse_args(["report", "--before", "1e300"])
    with pytest.raises(ValueError):
        cli.cmd_report(args)


def test_fractional_markdown_window_and_membership_match(tmp_path):
    built = report.build(
        synthetic(tmp_path), since=100.5, before=200.5, timezone="UTC", now=300
    )
    assert built["episodes"]["rows"][0]["demo"] == "demo_1"
    assert built["episodes"]["total"] == 1
    for lang in ("en", "zh-CN"):
        md = report.render(built, lang=lang)
        assert (
            "[1970-01-01T00:01:40.500000+00:00, 1970-01-01T00:03:20.500000+00:00)" in md
        )
        assert "[100.5, 200.5)" in md


def test_submicrosecond_markdown_window_keeps_exact_epoch(tmp_path):
    start, end = 100.00000001, 200.00000001
    built = report.build(
        synthetic(tmp_path), since=start, before=end, timezone="UTC", now=300
    )
    assert built["episodes"]["rows"][0]["demo"] == "demo_1"
    md = report.render(built)
    assert f"[{start}, {end})" in md


@pytest.mark.parametrize(
    "boundary,zone",
    [
        ("9999-12-31T23:30:00+00:00", "Pacific/Kiritimati"),
        ("0001-01-01T00:00:00+00:00", "America/New_York"),
    ],
)
def test_requested_zone_range_refused_before_config(
    tmp_path, monkeypatch, boundary, zone
):
    reached = []
    monkeypatch.setattr(cli, "resolve_config", lambda args: reached.append(True))
    args = cli.build_parser().parse_args(
        ["report", "--before", boundary, "--timezone", zone]
    )
    with pytest.raises(ValueError, match="supported date range"):
        cli.cmd_report(args)
    assert reached == []


def test_renderable_maximum_boundary_remains_supported(tmp_path):
    before = "9999-12-30T23:30:00+00:00"
    start, end, zone = report_scope.window(None, before, "Pacific/Kiritimati")
    built = report.build(
        synthetic(tmp_path), since=start, before=end, timezone=zone, now=300
    )
    assert "9999-12-31T13:30:00+14:00" in report.render(built)
