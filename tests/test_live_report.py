"""Session reports (``live/reports/``), ``levi live report`` and the backfill
of ``stats.jsonl`` (``levi live stats backfill``)."""

import contextlib
import io
import json
import time

from test_live_pipeline import NAME, env  # noqa: F401  (fixture)
from test_live_stats_aggregate import full

from levi.live import backfill, cli, report, stats
from levi.live import config as live_config


def synthetic(tmp_path, keep=20):
    c = live_config.Config()
    c.service.workspace = str(tmp_path / "ws")
    c.service.home = str(tmp_path / "home")
    c.resources.report_keep = keep
    c.live_dir.mkdir(parents=True)
    return c


def session_rows(c, session, base, dataset="g__t", n=3):
    """``n`` episodes of one session that ended from ``base`` on."""
    for k in range(n):
        row = full(f"demo_{session}_{k:04d}", 0, dataset=dataset, session=session)
        shift = base + 10 * k - row["timeline"]["completed_at"]
        row["timeline"]["completed_at"] += shift
        row["timeline"]["first_request_at"] += shift
        row["at"] += shift
        row["result"]["spec"] = {
            "guideline": "generic-guideline.v1.md",
            "release_review": "generic-release.v1.json",
            "sha256": {"generic-guideline.v1.md": "abcdef0123456789abcdef"},
        }
        stats.record(c.live_dir, row)


def test_a_report_has_settings_facts_tables_and_no_path(tmp_path):
    c = synthetic(tmp_path)
    session_rows(c, "s1", 1000.0)
    result = report.write(c, "g__t", "s1", now=2000.0, policy={"config": "pi05"})
    assert result["written"] and result["stem"] == "g__t__s1"
    folder = report.reports_dir(c)
    assert sorted(p.name for p in folder.iterdir()) == [
        "g__t__s1.json",
        "g__t__s1.md",
        "g__t__s1.zh-CN.md",
    ]
    md = (folder / "g__t__s1.md").read_text()
    for heading in (
        "# Live annotation statistics: g__t / s1",
        "## Setup",
        "## Facts",
        "## Latency",
        "## Throughput and model cost",
        "## GPU and gate",
        "## Sessions",
        "## Per episode (3/3)",
        "## How to read this",
    ):
        assert heading in md, heading
    assert "provider.name" in md and "vllm.max_model_len" in md
    assert "policy.config | pi05" in md
    assert "## Compared with the previous report" not in md  # there is none
    zh = (folder / "g__t__s1.zh-CN.md").read_text()
    assert "## 口径说明" in zh and "实时倍率" in zh
    for text in (md, zh, (folder / "g__t__s1.json").read_text()):
        assert str(tmp_path) not in text and "/home/" not in text
        assert "api_key" not in text.lower() and 'token"' not in text.lower()
    data = json.loads((folder / "g__t__s1.json").read_text())
    assert data["schema"] == report.SCHEMA and data["episodes"]["total"] == 3
    assert data["settings"]["provider"]["name"] == c.provider.name
    assert data["settings"]["spec"]["files"] == {
        "generic-guideline.v1.md": "abcdef012345"
    }
    assert "spec.sha256 generic-guideline.v1.md | abcdef012345" in md
    assert "spec.release_review | generic-release.v1.json" in md
    assert "script" not in json.dumps(data["settings"])  # no launch script path
    assert not list(folder.glob("*.partial"))


def test_the_report_is_idempotent_and_updates_when_records_arrive(tmp_path):
    c = synthetic(tmp_path)
    session_rows(c, "s1", 1000.0, n=2)
    first = report.write(c, "g__t", "s1", now=2000.0)
    assert first["written"]
    path = report.reports_dir(c) / "g__t__s1.json"
    before = path.read_text()
    again = report.write(c, "g__t", "s1", now=3000.0)
    assert again["written"] is False and path.read_text() == before
    # A missing file is rewritten even when the records are the same.
    (report.reports_dir(c) / "g__t__s1.zh-CN.md").unlink()
    assert report.write(c, "g__t", "s1", now=3000.0)["written"]
    # A late record changes the signature: the report is updated in place.
    stats.record(c.live_dir, full("demo_0009", 5, session="s1"))
    third = report.write(c, "g__t", "s1", now=4000.0)
    assert third["written"]
    assert json.loads(path.read_text())["episodes"]["total"] == 3
    assert len(report.listing(c)) == 1
    assert report.write(c, "g__t", "s1", force=True, now=5000.0)["written"]


def test_only_the_newest_reports_are_kept(tmp_path):
    c = synthetic(tmp_path, keep=2)
    for n in range(4):
        session_rows(c, f"s{n}", 1000.0 + 100 * n, n=2)
        report.write(c, "g__t", f"s{n}", now=2000.0 + n)
    assert [r["session"] for r in report.listing(c)] == ["s3", "s2"]
    names = sorted(p.name for p in report.reports_dir(c).iterdir())
    assert len(names) == 6 and not any("s0" in n or "s1" in n for n in names)
    # A file that is not a report is never deleted.
    (report.reports_dir(c) / "notes.json").write_text("{}")
    report.prune(c, keep=1)
    assert (report.reports_dir(c) / "notes.json").exists()
    assert [r["session"] for r in report.listing(c)] == ["s3"]


def test_a_report_compares_with_the_previous_one_of_its_dataset(tmp_path):
    c = synthetic(tmp_path)
    session_rows(c, "s1", 1000.0, n=2)
    session_rows(c, "s9", 1000.0, dataset="g__other", n=2)  # not comparable
    report.write(c, "g__t", "s1", now=1500.0)
    report.write(c, "g__other", "s9", now=1500.0)
    session_rows(c, "s2", 5000.0, n=3)
    report.write(c, "g__t", "s2", now=6000.0)
    data = json.loads((report.reports_dir(c) / "g__t__s2.json").read_text())
    assert data["previous"]["session"] == "s1"
    md = (report.reports_dir(c) / "g__t__s2.md").read_text()
    assert "## Compared with the previous report" in md
    assert "| episodes | 3 | 2 | +1 |" in md
    # The first report of a dataset has nothing to compare with.
    first = json.loads((report.reports_dir(c) / "g__t__s1.json").read_text())
    assert first["previous"] is None


def test_a_session_is_finished_when_it_ended_and_all_its_episodes_are_handled(
    env,  # noqa: F811
):
    e = env()
    for n in (0, 1):
        e.rollouts.write(n, run_id="run-A")
    e.rollouts.session("running", run_id="run-A")
    e.run()
    c = e.config
    from levi.live import sessions

    running = sessions.read_sessions(c.watch.roots)
    assert report.finished_sessions(c, running) == []  # the session goes on
    assert report.auto(c, running) == []
    e.rollouts.session("finished", run_id="run-A")
    ended = sessions.read_sessions(c.watch.roots)
    assert report.finished_sessions(c, ended) == [(NAME, "run-A")]
    assert report.finished_sessions(c, ended, busy={NAME}) == []  # more is queued
    (written,) = report.auto(c, ended, now=time.time())
    assert written["stem"] == f"{NAME}__run-A"
    assert report.auto(c, ended) == []  # nothing changed: nothing written
    data = json.loads((report.reports_dir(c) / f"{written['stem']}.json").read_text())
    assert data["episodes"]["total"] == 2
    assert data["settings"]["policy"] == {
        "config": "pi05_fr3_all_state",
        "checkpoint": "pi05_fr3_all_step49999",
    }
    assert data["summary"]["episodes"]["done"] == 2
    # A new session replaced the old one's file: the old one is over too.
    e.rollouts.session("running", run_id="run-B")
    newer = sessions.read_sessions(c.watch.roots)
    assert report.finished_sessions(c, newer) == [(NAME, "run-A")]
    # A demo of the session that still waits for a retry holds the report back.
    state = e.state()
    state["demos"]["demo_0001"]["state"] = "mirrored"
    mirror_write = __import__("levi.live.mirror", fromlist=["x"])
    mirror_write.jsonio.write(mirror_write.state_path(c, NAME), state)
    assert report.finished_sessions(c, newer) == []


def test_the_service_writes_the_report_itself_once_the_session_is_over(env):  # noqa: F811
    e = env()
    e.rollouts.write(0, run_id="run-A")
    e.rollouts.session("finished", run_id="run-A")
    ctl = e.run()
    ctl._report_at = 0.0
    ctl._reports(time.time())
    assert (e.ws / "live/reports" / f"{NAME}__run-A.json").is_file()
    assert any(
        "session report written" in m
        for m in e.messages + [x["text"] for x in ctl.events]
    )
    before = ctl._report_at
    ctl._reports(before + 1)  # throttled
    assert ctl._report_at == before


def test_levi_live_report_prints_or_writes_the_same_report(tmp_path, capsys):
    c = synthetic(tmp_path)
    session_rows(c, "s1", 1000.0, n=2)
    base = ["--workspace", str(c.workspace), "--root", str(tmp_path / "none")]
    assert cli.main(["report", *base, "--dataset", "g__t", "--session", "s1"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Live annotation statistics: g__t / s1")
    assert cli.main(["report", *base, "--lang", "zh"]) == 0
    assert "后台实时标注统计" in capsys.readouterr().out
    target = tmp_path / "out" / "r.json"
    assert cli.main(["report", *base, "--format", "json", "--out", str(target)]) == 0
    assert json.loads(target.read_text())["summary"]["episodes"]["count"] == 2
    assert not list(target.parent.glob("*.partial"))
    assert capsys.readouterr().out.startswith("wrote ")
    assert cli.main(["report", *base, "--format", "csv"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("dataset,demo") and len(lines) == 3
    # An empty scope is a report of nothing, not an error.
    assert cli.main(["report", *base, "--dataset", "nope"]) == 0
    assert "| Episodes | 0 |" in capsys.readouterr().out


# --- backfill ------------------------------------------------------------------


def test_backfill_rebuilds_what_survives_and_nulls_the_rest(env):  # noqa: F811
    e = env()
    for n in (0, 1):
        e.rollouts.write(n, run_id="run-A")
    e.run()
    original = {r["demo"]: r for r in stats.read(e.ws / "live")}
    assert set(original) == {"demo_0000", "demo_0001"}
    # The service started without statistics: remove one record, keep the other.
    path = e.ws / "live/stats.jsonl"
    keep = [json.loads(x) for x in path.read_text().splitlines()]
    path.write_text(json.dumps(keep[1]) + "\n")
    items = backfill.plan(e.config)
    assert [i["demo"] for i in items] == [keep[0]["demo"]]
    (item,) = items
    got, was = item["record"], original[item["demo"]]
    assert got["backfilled"] is True and got["session"] == "run-A"
    assert got["dataset"] == NAME and got["episode_index"] == was["episode_index"]
    assert got["result"]["state"] == "done"
    assert got["result"]["segments"] == was["result"]["segments"]
    assert got["result"]["segment_labels"] == was["result"]["segment_labels"]
    assert got["result"]["verdict"] == was["result"]["verdict"]
    assert got["timeline"]["completed_at"] == was["timeline"]["completed_at"]
    for key in ("to_mirror_s", "to_commit_s", "to_verdict_s", "to_first_request_s"):
        assert got["timeline"][key] == was["timeline"][key], key
    assert got["model"]["requests"]["coarse"] == was["model"]["requests"]["coarse"]
    # The batch's calibration requests were carried by its first demo and are
    # not recoverable: left out, and said to be unknown.
    probe = (was["model"]["tokens"]["probe"] or 0) if was["model"]["tokens"] else 0
    assert got["model"]["total_tokens"] == was["model"]["total_tokens"] - probe
    assert got["model"]["requests"]["probe"] is None
    # Not recorded anywhere: null, not today's configuration.
    assert got["gate"] == {k: None for k in got["gate"]}
    assert got["batch"] == {"id": None, "size": None}
    assert got["episode"] == {"frames": None, "episode_seconds": None}
    assert got["result"]["provider"] is None and got["result"]["model"] is None
    assert got["result"]["spec"]["guideline"] is None
    assert (
        got["result"]["spec"]["release_review"]
        == was["result"]["spec"]["release_review"]
    )
    # Every field of the schema has a stated source.
    assert sorted(item["sources"]) == sorted(stats.leaves(stats.TEMPLATE))
    assert item["sources"]["gate.closed_wait_s"].startswith("null")
    assert "committed_at" in item["sources"]["timeline.to_commit_s"]
    assert item["sources"]["model.total_tokens"].startswith("run journal")
    # A dry run writes nothing; applying writes once; a second run adds nothing.
    assert len(path.read_text().splitlines()) == 1
    assert backfill.apply(e.config, items) == 1
    assert backfill.apply(e.config, items) == 0
    assert backfill.plan(e.config) == []
    rows = stats.read(e.ws / "live")
    assert len(rows) == 2 and {r["demo"] for r in rows} == set(original)
    old = next(r for r in rows if not r["backfilled"])
    assert old == stats.normalize(keep[1])  # the line that was there is untouched
    # The aggregate counts it like any other record.
    assert stats.summarize(rows)["episodes"]["done"] == 2


def test_backfill_cli_dry_run_lists_sources_and_apply_is_idempotent(env, capsys):  # noqa: F811
    e = env()
    e.rollouts.write(0, run_id="run-A")
    e.run()
    (e.ws / "live/stats.jsonl").unlink()
    base = ["--workspace", str(e.ws), "--root", str(e.tmp / "none")]
    assert cli.main(["stats", "backfill", *base, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "demo_0000" in out and "gate.closed_wait_s" in out
    assert "dry run: 1 demo(s) would be backfilled; nothing written" in out
    assert not (e.ws / "live/stats.jsonl").exists()
    assert cli.main(["stats", "backfill", *base, "--dry-run", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["record"]["backfilled"] is True
    assert cli.main(["stats", "backfill", *base, "--dataset", "other"]) == 0
    assert "backfilled 0" in capsys.readouterr().out
    assert cli.main(["stats", "backfill", *base]) == 0
    assert "backfilled 1" in capsys.readouterr().out
    assert cli.main(["stats", "backfill", *base]) == 0
    assert "backfilled 0" in capsys.readouterr().out


def test_backfill_survives_missing_journals_and_damaged_state(env):  # noqa: F811
    e = env()
    e.rollouts.write(0, run_id="run-A")
    e.run()
    (e.ws / "live/stats.jsonl").unlink()
    state = e.state()
    row = state["demos"]["demo_0000"]
    row["temporal"]["run_id"] = "no-such-run"  # journal gone
    row["verdict"] = None
    state["demos"]["junk"] = "not a row"
    state["demos"]["demo_failed"] = {"state": "failed", "reason": "coarse: boom"}
    from levi.live import mirror

    mirror.jsonio.write(mirror.state_path(e.config, NAME), state)
    items = {i["demo"]: i["record"] for i in backfill.plan(e.config)}
    assert set(items) == {"demo_0000", "demo_failed"}
    assert items["demo_0000"]["model"]["total_tokens"] is None  # nothing to count
    assert items["demo_0000"]["result"]["verdict"]["outcome"] is None
    failed = items["demo_failed"]
    assert (
        failed["result"]["state"] == "failed"
        and failed["result"]["reason"] == "coarse: boom"
    )
    assert failed["session"] is None and failed["result"]["segments"] is None
    # A store that cannot be read at all gives the same answer, not a crash.
    assert backfill.run_events(e.tmp / "nowhere", "x") == []
    assert backfill.change_labels(e.tmp / "nowhere", "c", 0) is None
    with contextlib.redirect_stdout(io.StringIO()):
        assert backfill.apply(e.config, list(backfill.plan(e.config))) == 2
