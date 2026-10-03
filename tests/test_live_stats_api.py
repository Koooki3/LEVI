"""``GET /api/levi/live/stats`` and ``/stats/export``: scope filters, paging,
the three export formats, refusal of odd parameters, no paths or secrets."""

import csv
import io
import json
from pathlib import Path

import pytest
from test_live_service import cfg
from test_live_stats_aggregate import full

from levi.live import api, cli, gating, mirror, stats


@pytest.fixture
def live_stats(client, tmp_path, monkeypatch):
    c = cfg(tmp_path)
    cli.prepare(c)
    monkeypatch.setattr(api, "_workspace", lambda: Path(c.service.workspace))
    monkeypatch.setenv("LEVI_LIVE_HOME", c.service.home)
    rows = [full(f"demo_{n:04d}", n) for n in range(4)]
    rows += [
        full("demo_0000", 0, dataset="g__u", session="s2"),
        full("demo_0001", 1, dataset="g__u", session="s2"),
    ]
    for row in rows:
        stats.record(c.live_dir, row)
    return client, c, rows


def test_the_api_is_disabled_outside_a_live_workspace(client, tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_workspace", lambda: tmp_path)
    assert client.get("/api/levi/live/stats").json() == {"enabled": False}
    assert client.get("/api/levi/live/stats/export").json() == {"enabled": False}


def test_stats_answer_aggregates_sessions_and_a_page_of_episodes(live_stats):
    client, _, _ = live_stats
    body = client.get("/api/levi/live/stats").json()
    assert body["enabled"] and body["schema"] == "levi.live.stats.v1"
    assert body["datasets"] == ["g__t", "g__u"]
    assert body["summary"]["episodes"]["count"] == 6
    assert body["summary"]["model"]["tokens_total"] == 6 * 1800
    assert {r["session"] for r in body["sessions"]} == {"s1", "s2"}
    page = body["episodes"]
    assert page["total"] == 6 and len(page["rows"]) == 6
    assert page["rows"][0]["at"] >= page["rows"][-1]["at"]  # newest first
    # Paging.
    two = client.get("/api/levi/live/stats?limit=2&offset=1").json()["episodes"]
    assert two["total"] == 6 and len(two["rows"]) == 2
    assert two["rows"][0] == page["rows"][1]
    # Filters.
    one = client.get("/api/levi/live/stats?dataset=g__u").json()
    assert one["summary"]["episodes"]["count"] == 2
    assert one["scope"] == {"dataset": "g__u", "session": None, "since": None}
    assert one["datasets"] == ["g__t", "g__u"]  # the choices, not the scope
    s1 = client.get("/api/levi/live/stats?session=s1").json()
    assert s1["summary"]["episodes"]["count"] == 4 and len(s1["sessions"]) == 1
    nothing = client.get("/api/levi/live/stats?dataset=g__x").json()
    assert nothing["summary"]["episodes"]["count"] == 0
    assert nothing["episodes"]["rows"] == []
    late = client.get("/api/levi/live/stats?since=150").json()
    assert all(r["at"] >= 150 for r in late["episodes"]["rows"])


def test_a_session_end_the_state_knows_counts_for_in_session(live_stats):
    client, c, _ = live_stats
    base = client.get("/api/levi/live/stats?session=s1").json()
    # Episodes 0..3 end at 100..130 and ask 4 s later: 3 of 4 in session.
    assert base["summary"]["in_session"]["count"] == 3
    # The dataset's state also lists an episode of the session that has no
    # record yet (it ended at 160): the last episode now counts as inside too.
    path = mirror.state_path(c, "g__t")
    mirror.jsonio.write(
        path,
        {
            "name": "g__t",
            "demos": {
                "demo_0004": {
                    "state": "mirrored",
                    "run_id": "s1",
                    "completed_at": 160.0,
                }
            },
        },
    )
    got = client.get("/api/levi/live/stats?session=s1").json()
    assert got["summary"]["in_session"]["count"] == 4
    assert got["summary"]["in_session"]["ratio"] == 1.0


def test_gate_history_gives_the_closed_time(live_stats):
    client, c, _ = live_stats
    gating.record_transition(
        c.live_dir, {"at": 90.0, "to": {"open": True, "code": "ok"}}
    )
    gating.record_transition(
        c.live_dir, {"at": 105.0, "to": {"open": False, "code": "episode_running"}}
    )
    gating.record_transition(
        c.live_dir, {"at": 112.0, "to": {"open": True, "code": "ok"}}
    )
    window = client.get("/api/levi/live/stats?session=s1").json()["summary"]["gpu"][
        "gate_window"
    ]
    assert window["closed_s"] == 7.0 and window["closures"] == 1


def test_exports_are_csv_json_and_markdown(live_stats):
    client, _, _ = live_stats
    csv_answer = client.get("/api/levi/live/stats/export?format=csv&session=s1")
    assert csv_answer.status_code == 200
    assert csv_answer.headers["content-type"].startswith("text/csv")
    assert 'filename="live-stats-s1.csv"' in csv_answer.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(csv_answer.text)))
    assert len(rows) == 4 and rows[0]["demo"] == "demo_0003"  # newest first
    assert rows[0]["total_tokens"] == "1800" and rows[0]["in_session"] == "False"
    # json carries every episode, whatever the page size.
    data = client.get("/api/levi/live/stats/export?format=json").json()
    assert data["episodes"]["total"] == 6 and len(data["episodes"]["rows"]) == 6
    md = client.get("/api/levi/live/stats/export?format=md&dataset=g__u")
    assert md.headers["content-type"].startswith("text/markdown")
    assert md.text.startswith("# Live annotation statistics")
    assert "demo_0001" in md.text and "demo_0003" not in md.text
    zh = client.get("/api/levi/live/stats/export?format=md&lang=zh").text
    assert "后台实时标注统计" in zh and "实时倍率" in zh
    default = client.get("/api/levi/live/stats/export")
    assert default.headers["content-type"].startswith("application/json")
    assert default.headers["content-disposition"].endswith('live-stats.json"')


@pytest.mark.parametrize(
    "query",
    [
        "format=xml",
        "dataset=../x",
        "dataset=a%2Fb",
        "session=a%20b",
        "since=-1",
        "since=1e12",
    ],
)
def test_odd_parameters_are_refused(live_stats, query):
    client, *_ = live_stats
    assert client.get(f"/api/levi/live/stats/export?{query}").status_code in (400, 422)
    if not query.startswith("format"):
        assert client.get(f"/api/levi/live/stats?{query}").status_code in (400, 422)


def test_nothing_secret_or_absolute_leaves_the_service(live_stats):
    client, c, _ = live_stats
    # A failed demo's free-text reason may name a path: it is not copied.
    row = full("demo_0009", 9, result__state="failed")
    row["result"]["reason"] = f"cannot read {c.workspace}/captures/x/video.mp4"
    stats.record(c.live_dir, row)
    texts = [
        client.get("/api/levi/live/stats").text,
        client.get("/api/levi/live/stats/export?format=csv").text,
        client.get("/api/levi/live/stats/export?format=md").text,
        client.get("/api/levi/live/stats/export?format=json").text,
    ]
    for text in texts:
        assert str(c.workspace) not in text and "/home/" not in text
        assert "human.key" not in text and "api_key" not in text.lower()
        assert "demo_0009" in text


def test_damaged_lines_in_the_file_do_not_break_the_answer(live_stats):
    client, c, _ = live_stats
    path = c.live_dir / "stats.jsonl"
    path.write_bytes(path.read_bytes() + b"garbage\n\xff\n[1]\n" + b'{"demo": "to')
    body = client.get("/api/levi/live/stats").json()
    assert body["summary"]["episodes"]["count"] == 6
    json.dumps(body, allow_nan=False)


def test_a_workspace_without_records_answers_empty_not_error(
    client, tmp_path, monkeypatch
):
    c = cfg(tmp_path)
    cli.prepare(c)
    monkeypatch.setattr(api, "_workspace", lambda: Path(c.service.workspace))
    body = client.get("/api/levi/live/stats").json()
    assert body["enabled"] and body["summary"]["episodes"]["count"] == 0
    assert body["sessions"] == [] and body["episodes"]["rows"] == []
    assert client.get("/api/levi/live/stats/export?format=csv").text.count("\n") == 1


def test_removed_episodes_are_hidden_by_default_and_can_be_included(live_stats):
    client, c, _ = live_stats
    mirror.jsonio.write(
        mirror.state_path(c, "g__t"),
        {
            "name": "g__t",
            "demos": {
                "demo_0001": {
                    "state": "done",
                    "run_id": "s1",
                    "excluded": {"at": 1.0, "by": "person", "reason": ""},
                },
            },
        },
    )
    base = client.get("/api/levi/live/stats?dataset=g__t").json()
    assert base["include_excluded"] is False and base["excluded_demos"] == 1
    assert base["summary"]["episodes"]["count"] == 3
    assert "demo_0001" not in {r["demo"] for r in base["episodes"]["rows"]}
    both = client.get("/api/levi/live/stats?dataset=g__t&include_excluded=true").json()
    assert both["include_excluded"] is True and both["excluded_demos"] == 1
    assert both["summary"]["episodes"]["count"] == 4
    assert both["summary"]["episodes"]["excluded"] == 1
    row = next(r for r in both["episodes"]["rows"] if r["demo"] == "demo_0001")
    assert row["excluded"] is True
    # The exports follow the same switch.
    csv_default = client.get("/api/levi/live/stats/export?format=csv").text
    rows = list(csv.DictReader(io.StringIO(csv_default)))
    assert not [r for r in rows if r["dataset"] == "g__t" and r["demo"] == "demo_0001"]
    rows = list(
        csv.DictReader(
            io.StringIO(
                client.get(
                    "/api/levi/live/stats/export?format=csv&include_excluded=true"
                ).text
            )
        )
    )
    assert [r for r in rows if r["dataset"] == "g__t" and r["demo"] == "demo_0001"]


def test_a_dataset_name_with_a_root_mark_works_in_filters_exports_and_urls(
    client, tmp_path, monkeypatch
):
    c = cfg(tmp_path)
    cli.prepare(c)
    monkeypatch.setattr(api, "_workspace", lambda: Path(c.service.workspace))
    name = "pi05__stack__at__models"
    for n in range(2):
        stats.record(c.live_dir, full(f"demo_{n:04d}", n, dataset=name, session="s1"))
    body = client.get(f"/api/levi/live/stats?dataset={name}").json()
    assert body["datasets"] == [name] and body["summary"]["episodes"]["count"] == 2
    export = client.get(f"/api/levi/live/stats/export?format=csv&dataset={name}")
    assert f'filename="live-stats-{name}.csv"' in export.headers["content-disposition"]
    # A name with characters the sanitiser would have replaced is encoded, not
    # trusted: a slash or a leading dot is refused.
    assert client.get("/api/levi/live/stats?dataset=%2E%2E").status_code == 400
    assert client.get("/api/levi/live/stats?dataset=a%2Fb").status_code == 400
    assert client.get("/api/levi/live/stats?dataset=a%20b").status_code == 200
