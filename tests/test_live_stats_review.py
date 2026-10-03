"""Regressions from the independent review of the statistics: report churn,
cost on a long history, the exclusion switch, gate arithmetic, unreadable
journals, header and CSV safety, damaged numbers."""

import csv
import io
import json
import threading
from pathlib import Path

import pytest
from test_live_service import cfg
from test_live_stats_aggregate import full

from levi.live import (
    api,
    backfill,
    cli,
    controller,
    mirror,
    report,
    stats,
    statsfmt,
    statsview,
)
from levi.live import config as live_config


def synthetic(tmp_path, keep=20):
    c = live_config.Config()
    c.service.workspace = str(tmp_path / "ws")
    c.service.home = str(tmp_path / "home")
    c.resources.report_keep = keep
    c.live_dir.mkdir(parents=True)
    return c


def finished_history(c, sessions, per=2, dataset="g__t"):
    """``sessions`` ended, all labelled: records and the dataset state."""
    demos = {}
    for n in range(sessions):
        sid = f"s{n:03d}"
        base = 1000.0 + 100 * n
        for k in range(per):
            demo = f"demo_{sid}_{k}"
            row = full(demo, 0, dataset=dataset, session=sid)
            shift = base + 10 * k - row["timeline"]["completed_at"]
            row["timeline"]["completed_at"] += shift
            row["timeline"]["first_request_at"] += shift
            row["at"] += shift
            stats.record(c.live_dir, row)
            demos[demo] = {
                "state": "done",
                "run_id": sid,
                "completed_at": row["timeline"]["completed_at"],
            }
    mirror.jsonio.write(
        mirror.state_path(c, dataset), {"name": dataset, "demos": demos}
    )


# --- H1, H2: reports ------------------------------------------------------------


def test_more_sessions_than_report_keep_do_not_rewrite_and_delete_for_ever(
    tmp_path, monkeypatch
):
    c = synthetic(tmp_path, keep=5)
    finished_history(c, 30)
    first = report.auto(c, limit=100)
    assert len(first) == 5  # only the newest five could survive: only those
    assert [r["session"] for r in report.listing(c)] == [
        f"s{n:03d}" for n in range(29, 24, -1)
    ]
    built = []
    real = report.build
    monkeypatch.setattr(
        report, "build", lambda *a, **k: built.append(a) or real(*a, **k)
    )
    for _ in range(3):
        assert report.auto(c, limit=100) == []  # nothing churns afterwards
    assert built == []
    files = sorted(
        p.name for p in report.reports_dir(c).iterdir() if not p.name.endswith(".lock")
    )
    assert len(files) == 5 * 3 + 1  # three per report and the index


def test_a_report_deleted_to_keep_within_the_limit_is_not_written_again(tmp_path):
    c = synthetic(tmp_path, keep=2)
    finished_history(c, 3)
    report.auto(c, limit=100)
    report.write(c, "g__t", "s000", force=True)  # an explicit request is honoured
    assert [r["session"] for r in report.listing(c)] == ["s002", "s001"]
    assert "g__t__s000" in report.index_of(c)["pruned"]
    assert report.auto(c, limit=100) == []


def test_a_late_record_updates_only_its_session_and_nothing_is_built_otherwise(
    tmp_path, monkeypatch
):
    c = synthetic(tmp_path, keep=10)
    finished_history(c, 4)
    report.auto(c, limit=100)
    row = full("demo_late", 0, session="s001")
    row["at"] = 5000.0
    stats.record(c.live_dir, row)
    built = []
    real = report.build
    monkeypatch.setattr(
        report, "build", lambda *a, **k: built.append(a[2]) or real(*a, **k)
    )
    (done,) = report.auto(c, limit=100)
    assert done["stem"] == "g__t__s001" and built == ["s001"]


def test_the_service_checks_in_its_own_thread_and_does_not_repeat_itself(
    tmp_path, monkeypatch
):
    c = synthetic(tmp_path, keep=5)
    finished_history(c, 8)
    ctl = controller.Controller(c, log=lambda *a: None)
    started, release = threading.Event(), threading.Event()

    def blocked(*args, **kwargs):
        started.set()
        release.wait(10)
        return []

    monkeypatch.setattr(report, "auto", blocked)
    ctl._reports(1000.0)  # returns at once although the job is blocked
    assert started.wait(5) and not release.is_set()
    assert ctl._report_thread.is_alive()
    ctl._reports(2000.0)  # a second tick does not start a second job
    release.set()
    ctl._report_thread.join(5)
    monkeypatch.undo()
    # Now for real: the event is announced once, then silence.
    ctl._report_at = 0.0
    ctl._reports(3000.0)
    ctl._report_thread.join(20)
    ctl._reports(3001.0)  # harvests
    texts = [e["text"] for e in ctl.events if "session report" in e["text"]]
    assert len(texts) == len(set(texts)) == report.AUTO_LIMIT
    ctl._report_at = 0.0
    for t in (4000.0, 5000.0, 6000.0):
        ctl._reports(t)
        if ctl._report_thread:
            ctl._report_thread.join(20)
    ctl._reports(7000.0)
    # Five sessions are kept: at most five were ever announced, never more.
    total = [e for e in ctl.events if "session report written" in e["text"]]
    assert len(total) <= 5 + 1


def test_a_quiet_history_costs_no_build_and_no_gate_walk(tmp_path, monkeypatch):
    c = synthetic(tmp_path, keep=20)
    finished_history(c, 60)
    rows = stats.read(c.live_dir)
    calls = []
    real = stats.gate_window
    monkeypatch.setattr(stats, "gate_window", lambda *a: calls.append(1) or real(*a))
    table = stats.session_rows(rows)
    assert len(table) == 60 and calls == []  # the per-session table walks no gate
    stats.summarize(rows, gate=[{"at": 1, "to": {"open": False}}])
    assert calls == [1]  # once for the whole scope
    # build() keeps its answer while no input file changes.
    summaries = []
    real_summarize = stats.summarize
    monkeypatch.setattr(
        stats,
        "summarize",
        lambda *a, **k: summaries.append(1) or real_summarize(*a, **k),
    )
    statsview._BUILT.clear()
    statsview.build(c)
    n = len(summaries)
    again = statsview.build(c)
    assert len(summaries) == n and again["summary"]["episodes"]["count"] == 120
    stats.record(c.live_dir, full("demo_new", 0, session="s999"))
    statsview.build(c)
    assert len(summaries) > n  # a changed file is recomputed


# --- M2: restored episodes ---------------------------------------------------------


def test_the_state_file_decides_and_a_restored_episode_counts_again():
    rows = [
        full("demo_a", 0, excluded=True),
        full("demo_b", 1),
        full("demo_c", 2, dataset="other"),
    ]
    rows[2]["excluded"] = True
    marked = stats.mark_excluded(rows, excluded={("g__t", "demo_b")}, known={"g__t"})
    assert [r["excluded"] for r in marked] == [False, True, True]
    # demo_a was removed when written but is back: the state says so.
    assert marked[0]["demo"] == "demo_a" and marked[0]["excluded"] is False
    # A dataset whose state could not be read keeps what the record says.
    assert marked[2]["excluded"] is True


def test_backfill_then_restore_counts_the_episode_again(tmp_path):
    c = synthetic(tmp_path)
    name = "g__t"
    row = {
        "state": "done",
        "run_id": "s1",
        "episode_index": 0,
        "completed_at": 1000.0,
        "mirrored_at": 1004.0,
        "excluded": {"at": 1, "by": "person"},
        "temporal": {"committed_at": 1020.0, "segments": 2, "review": "auto"},
    }
    mirror.jsonio.write(
        mirror.state_path(c, name), {"name": name, "demos": {"demo_0": row}}
    )
    (item,) = backfill.plan(c)
    assert item["record"]["excluded"] is True
    backfill.apply(c, [item])
    hidden = statsview.build(c, dataset=name)
    assert hidden["summary"]["episodes"]["count"] == 0 and hidden["excluded_demos"] == 1
    del row["excluded"]  # restored
    mirror.jsonio.write(
        mirror.state_path(c, name), {"name": name, "demos": {"demo_0": row}}
    )
    back = statsview.build(c, dataset=name)
    assert back["summary"]["episodes"]["count"] == 1 and back["excluded_demos"] == 0


def test_a_removed_episode_does_not_move_the_end_of_its_session(tmp_path):
    c = synthetic(tmp_path)
    mirror.jsonio.write(
        mirror.state_path(c, "g__t"),
        {
            "name": "g__t",
            "demos": {
                "a": {"state": "done", "run_id": "s", "completed_at": 100.0},
                "b": {
                    "state": "done",
                    "run_id": "s",
                    "completed_at": 900.0,
                    "excluded": {"at": 1},
                },
            },
        },
    )
    assert statsview.session_ends(c) == {("g__t", "s"): 100.0}
    assert statsview.session_ends(c, include_excluded=True) == {("g__t", "s"): 900.0}


# --- M3, L4, L5 -----------------------------------------------------------------------


def test_tokens_are_null_when_nothing_reported_usage():
    journal = [
        {"type": "model_step", "episode": 1, "phase": "coarse",
         "usage": {"tokens": 800, "elapsed_seconds": 1.0}},
    ]  # fmt: skip
    got = stats.usage_of([("temporal", journal)], 1)
    assert got["total_tokens"] is None and got["prompt_tokens"] is None
    assert got["reserved_tokens"] == 800 and got["unreported_steps"] == 1
    assert (
        stats.usage_of([("temporal", journal)], 99)["total_tokens"] is None
    )  # no request
    row = full("demo_0", 0, model__total_tokens=None, model__prompt_tokens=None,
               model__completion_tokens=None)  # fmt: skip
    s = stats.summarize([row])
    assert s["model"]["tokens_total"] is None
    assert s["model"]["tokens_per_episode"]["n"] == 0
    table = stats.episode_rows([row])
    assert table[0]["total_tokens"] is None
    text = statsfmt.to_csv({"episodes": {"rows": table}})
    cells = dict(
        zip(*[line.split(",") for line in text.splitlines()[:2]], strict=False)
    )
    assert cells["total_tokens"] == ""


def test_retried_follows_what_attempts_counts():
    ok_first = full("a", 0, attempts=0)
    ok_after_retry = full("b", 1, attempts=1)
    failed_after_two = full("c", 2, attempts=2, result__state="failed")
    failed_once_only = full("d", 3, attempts=1, result__state="failed")
    s = stats.summarize([ok_first, ok_after_retry, failed_after_two, failed_once_only])
    assert s["episodes"]["retried"] == 2  # b (done after 1 failure) and c


def test_shares_and_per_episode_figures_leave_the_calibration_out():
    row = full("a", 0, model__requests__probe=2, model__tokens__probe=700,
               model__probe_tokens=700)  # fmt: skip
    m = stats.summarize([row])["model"]
    assert m["requests_per_episode"]["median"] == 3  # not 5
    assert m["by_kind"]["probe"]["requests"] == 2
    assert m["by_kind"]["probe"]["tokens_share"] is None
    shares = sum(
        m["by_kind"][k]["tokens_share"] for k in ("coarse", "refine", "review")
    )
    assert abs(shares - 1.0) < 0.01
    assert stats.episode_rows([row])[0]["requests"] == 3


def test_model_seconds_and_the_real_time_factor_use_the_same_attempts():
    first = full("a", 0, attempts=1, result__state="mirrored")
    first["at"] = 10.0
    second = full("a", 0, attempts=2)
    second["at"] = 20.0
    t = stats.summarize([first, second])["throughput"]
    assert t["model_seconds"] == 7.0 and t["realtime_factor"] == round(6 / 7, 3)


# --- M4: the gate ---------------------------------------------------------------------------


def test_a_change_of_reason_while_closed_is_not_another_closure():
    gate = [
        {"at": 90.0, "to": {"open": True, "code": "ok"}},
        {"at": 100.0, "to": {"open": False, "code": "episode_running"}},
        {"at": 105.0, "to": {"open": False, "code": "episode_imminent"}},
        {"at": 110.0, "to": {"open": True, "code": "ok"}},
    ]
    w = stats.gate_window(gate, 90.0, 120.0)
    assert w["closures"] == 1 and w["closed_s"] == 10.0


def test_a_stopped_service_ends_the_closed_interval_instead_of_extending_it():
    gate = [
        {"at": 90.0, "to": {"open": True, "code": "ok"}},
        {"at": 100.0, "to": {"open": False, "code": "episode_running"}},
        {"at": 110.0, "to": {"open": None, "code": "service_stopped"}},
        {"at": 1100.0, "to": {"open": True, "code": "ok"}},
    ]
    w = stats.gate_window(gate, 90.0, 1200.0)
    assert w["closed_s"] == 10.0 and w["closures"] == 1  # not 1000
    assert w["unknown_s"] == 990.0
    # Closed again after the restart is a new closure.
    gate += [{"at": 1150.0, "to": {"open": False, "code": "x"}}]
    again = stats.gate_window(gate, 90.0, 1200.0)
    assert again["closures"] == 2 and again["closed_s"] == 10.0 + 50.0
    # Before the first transition nothing is known.
    early = stats.gate_window(gate, 0.0, 200.0)
    assert early["unknown_s"] >= 90.0


# --- M5: an unreadable journal ------------------------------------------------------------------


def test_a_store_that_cannot_be_read_is_a_failure_not_an_empty_journal(tmp_path):
    c = synthetic(tmp_path)
    row = {
        "state": "done",
        "run_id": "s1",
        "episode_index": 0,
        "completed_at": 1000.0,
        "temporal": {"run_id": "r1", "committed_at": 1020.0, "segments": 2},
    }
    mirror.jsonio.write(
        mirror.state_path(c, "g__t"), {"name": "g__t", "demos": {"d0": row}}
    )
    store = c.workspace / backfill.STORE
    store.parent.mkdir(parents=True)
    store.write_bytes(b"this is not a database" * 100)
    (item,) = backfill.plan(c)
    assert item["record"] is None and "cannot be read" in item["error"]
    assert backfill.failures([item]) == [item]
    assert backfill.apply(c, [item]) == 0
    assert stats.read(c.live_dir) == []  # nothing written: it can be tried again
    # The CLI says so and fails; a dry run shows the failure.
    base = ["--workspace", str(c.workspace), "--root", str(tmp_path / "none")]
    assert cli.main(["stats", "backfill", *base, "--dry-run"]) == 1
    assert cli.main(["stats", "backfill", *base]) == 1
    assert stats.read(c.live_dir) == []
    # Once the store is readable (here: gone, no journal) the demo is filled.
    store.unlink()
    (ok,) = backfill.plan(c)
    assert ok["record"]["result"]["segments"] == 2 and "error" not in ok
    assert backfill.apply(c, [ok]) == 1


def test_an_injected_reader_that_fails_is_a_failure_too(tmp_path):
    c = synthetic(tmp_path)
    row = {
        "state": "done",
        "run_id": "s",
        "episode_index": 0,
        "temporal": {"run_id": "r"},
    }
    mirror.jsonio.write(
        mirror.state_path(c, "g__t"), {"name": "g__t", "demos": {"d0": row}}
    )

    def locked(*args):
        raise backfill.Unreadable("database is locked")

    (item,) = backfill.plan(c, events=locked)
    assert item["error"] == "database is locked" and backfill.apply(c, [item]) == 0


# --- L1, L2, L3 -------------------------------------------------------------------------------------


@pytest.fixture
def served(client, tmp_path, monkeypatch):
    c = cfg(tmp_path)
    cli.prepare(c)
    monkeypatch.setattr(api, "_workspace", lambda: Path(c.service.workspace))
    return client, c


def test_exports_survive_unusual_names_and_refuse_a_trailing_newline(served):
    client, c = served
    for name in ("数据集__任务", 'a"b c', "é"):
        stats.record(c.live_dir, full("demo_0", 0, dataset=name, session="s1"))
    for name in ("数据集__任务", 'a"b c', "é"):
        answer = client.get(
            "/api/levi/live/stats/export", params={"dataset": name, "format": "csv"}
        )
        assert answer.status_code == 200, name
        header = answer.headers["content-disposition"]
        header.encode("latin-1")  # a valid header
        plain = header.split(";")[1].strip()
        assert plain.startswith('filename="') and '"' not in plain[10:-1]
        assert "filename*=UTF-8''" in header
    assert client.get("/api/levi/live/stats?session=abc%0A").status_code == 400
    assert client.get("/api/levi/live/stats/export?session=abc%0A").status_code == 400
    assert client.get("/api/levi/live/stats?session=abc").status_code == 200


def test_csv_cells_that_start_like_a_formula_are_quoted():
    rows = [
        {"demo": "=HYPERLINK(1)", "session": "+1", "dataset": "-2", "verdict": "@x"},
        {"demo": "\tx", "session": "\rx", "dataset": "ok", "verdict": "success"},
        {"demo": "demo_0", "episode_index": -3, "to_commit_s": -1.5},
    ]
    out = statsfmt.to_csv({"episodes": {"rows": rows}})
    parsed = list(csv.DictReader(io.StringIO(out)))
    assert parsed[0]["demo"] == "'=HYPERLINK(1)" and parsed[0]["session"] == "'+1"
    assert parsed[0]["dataset"] == "'-2" and parsed[0]["verdict"] == "'@x"
    assert parsed[1]["demo"] == "'\tx" and parsed[1]["session"] == "'\rx"
    assert parsed[1]["dataset"] == "ok" and parsed[1]["verdict"] == "success"
    # Numbers are not text: a negative one stays a number.
    assert parsed[2]["episode_index"] == "-3" and parsed[2]["to_commit_s"] == "-1.5"


def test_a_record_with_nan_or_infinity_is_damaged_and_skipped(tmp_path, served):
    client, c = served
    stats.record(c.live_dir, full("demo_ok", 0, session="s1"))
    path = c.live_dir / "stats.jsonl"
    line = json.dumps(full("demo_nan", 1, session="s1")).replace(
        '"attempts": 0', '"attempts": NaN'
    )
    assert "NaN" in line
    inf = json.dumps(full("demo_inf", 2, session="s1")).replace(
        '"images": 10', '"images": Infinity'
    )
    assert "Infinity" in inf
    path.write_text(path.read_text() + line + "\n" + inf + "\n")
    assert [r["demo"] for r in stats.read(c.live_dir)] == ["demo_ok"]
    body = client.get("/api/levi/live/stats")
    assert body.status_code == 200 and body.json()["summary"]["episodes"]["count"] == 1
    assert client.get("/api/levi/live/stats/export?format=json").status_code == 200


def test_a_report_that_fails_to_render_leaves_the_old_one_whole(tmp_path, monkeypatch):
    c = synthetic(tmp_path)
    finished_history(c, 1)
    report.write(c, "g__t", "s000", now=1.0)
    folder = report.reports_dir(c)
    before = {p.name: p.read_text() for p in folder.iterdir()}
    stats.record(c.live_dir, full("demo_more", 0, session="s000"))
    real = report.render

    def broken(rep, lang="en", max_rows=None):
        if lang == "zh":
            raise ValueError("cannot render")
        return real(rep, lang, max_rows)

    monkeypatch.setattr(report, "render", broken)
    with pytest.raises(ValueError):
        report.write(c, "g__t", "s000", now=2.0)
    assert {p.name: p.read_text() for p in folder.iterdir()} == before
    assert report.auto(c) == []  # and the service just logs and goes on


# --- L5 root-marked names in the service's session lookup ------------------------------------------


def test_a_running_session_under_a_root_mark_is_not_reported_as_finished(tmp_path):
    from levi.live.sessions import Session

    c = synthetic(tmp_path)
    c.watch.roots = [str(tmp_path / "models")]
    name = "g__t__at__models"
    mirror.jsonio.write(
        mirror.state_path(c, name),
        {
            "name": name,
            "root": str(tmp_path / "models"),
            "demos": {"d": {"state": "done", "run_id": "run-A"}},
        },
    )
    live = Session(
        path="p", group="g", task_folder="t", state="running", raw_state="running",
        updated_at=1.0, started_at=1.0, pid=1, age_s=0.0, run_id="run-A",
    )  # fmt: skip
    sessions = {(str(tmp_path / "models"), "g", "t"): live}
    assert mirror.resolve_name(c, str(tmp_path / "models"), "g", "t") == name
    assert report.finished_sessions(c, sessions) == []
    live.state = "finished"
    assert report.finished_sessions(c, sessions) == [(name, "run-A")]
