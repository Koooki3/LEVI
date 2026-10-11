"""``levi live analysis`` (CLI) and ``GET /api/levi/live/analysis``: the
output is the same summary, the route only reads, odd parameters are
refused, no path or secret is in the answer."""

import json
from pathlib import Path

from analysis_helpers import (
    operator,
    put_changes,
    put_state,
    record,
    row,
    seg,
    segments_block,
    workspace,
)

from levi.live import analysis, api, backfill, cli


def _populate(c):
    put_changes(
        c,
        "ch1",
        [seg(0, "approach", 0, 2, "success"), seg(0, "grasp", 2, 4, "failure")],
    )
    root = Path(c.watch.roots[0])
    source = root / "g/t"
    (source / "demo_0000").mkdir(parents=True)
    (source / "demo_0000/metadata.json").write_text(
        json.dumps({"eval": {"run_id": "s1"}})
    )
    session_dir = root / ".eval_sessions"
    session_dir.mkdir()
    (session_dir / "g-t.json").write_text(
        json.dumps(
            {
                "schema": "levi.eval.session.v1",
                "group": "g",
                "task_folder": "t",
                "session_id": "s1",
                "run_id": "s1",
                "state": "finished",
            }
        )
    )
    put_state(
        c,
        "g__t",
        "g",
        "t",
        {"demo_0000": row(0, changeset="ch1", operator="failure", verdict="failure")},
        source=str(source),
    )


def _args(c, tmp_path, *more):
    from levi.live.config import render

    path = tmp_path / "private-live.toml"
    path.write_text(render(c))
    return [
        "analysis",
        "--config",
        str(path),
        "--workspace",
        str(c.workspace),
        "--no-sync",
        *more,
    ]


def test_the_cli_reads_the_summary_after_separate_sync(tmp_path, capsys):
    c = workspace(tmp_path)
    _populate(c)
    assert analysis.sync(c)["written"] == 1
    assert cli.main(_args(c, tmp_path, "--format", "json")) == 0
    captured = capsys.readouterr()
    found = json.loads(captured.out)
    assert found["schema"] == analysis.SUMMARY_SCHEMA
    assert found["cells"][0]["landing"]["operator"]["counts"] == {"approach": 1}
    assert captured.err == ""
    assert len(analysis.read(c.live_dir)) == 1
    # a second run writes nothing new
    assert cli.main(_args(c, tmp_path, "--format", "json")) == 0
    assert capsys.readouterr().err == ""
    assert len(analysis.read(c.live_dir)) == 1


def test_no_sync_reads_only_what_is_on_file(tmp_path, capsys):
    c = workspace(tmp_path)
    _populate(c)
    assert cli.main(_args(c, tmp_path, "--no-sync", "--format", "json")) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["all"]["episodes"] == 0
    assert analysis.read(c.live_dir) == []


def test_the_cli_filters_and_renders_both_languages(tmp_path, capsys):
    c = workspace(tmp_path)
    _populate(c)
    assert analysis.sync(c)["written"] == 1
    assert cli.main(_args(c, tmp_path, "--group", "g", "--task", "t")) == 0
    en = capsys.readouterr().out
    assert en.startswith("# Evaluation analysis") and "g / t" in en
    assert cli.main(_args(c, tmp_path, "--group", "other", "--lang", "zh")) == 0
    assert "没有分析记录" in capsys.readouterr().out


def test_the_cli_writes_a_file_atomically(tmp_path, capsys):
    c = workspace(tmp_path)
    _populate(c)
    target = tmp_path / "out" / "a.md"
    assert cli.main(_args(c, tmp_path, "--out", str(target))) == 0
    assert target.read_text().startswith("# Evaluation analysis")
    assert not target.with_name("a.md.partial").exists()


def test_a_bad_since_is_a_usage_error(tmp_path, capsys):
    c = workspace(tmp_path)
    assert cli.main(_args(c, tmp_path, "--since", "yesterday")) == 2


def test_the_cli_in_a_workspace_that_is_not_live_leaves_no_file(tmp_path, capsys):
    c = workspace(tmp_path)
    for child in list(c.live_dir.iterdir()):
        child.unlink()
    c.live_dir.rmdir()
    assert cli.main(_args(c, tmp_path, "--format", "json")) == 2
    assert capsys.readouterr().out == ""
    assert not c.live_dir.exists()


def test_a_store_that_cannot_be_read_is_waited_for_not_recorded(tmp_path):
    c = workspace(tmp_path)
    put_state(
        c, "g__t", "g", "t", {"demo_0000": row(0, changeset="ch1", operator="failure")}
    )
    store = Path(c.workspace) / backfill.STORE
    store.parent.mkdir(parents=True)
    store.write_bytes(b"this is not a database" * 100)
    out = analysis.sync(c)
    assert out["unreadable"] == 1 and out["written"] == 0
    assert analysis.read(c.live_dir) == []
    store.unlink()
    put_changes(c, "ch1", [seg(0, "approach", 0, 1)])
    assert analysis.sync(c)["written"] == 1


# --- the route --------------------------------------------------------------------------


def _client(client, c, monkeypatch):
    monkeypatch.setattr(api, "_workspace", lambda: Path(c.service.workspace))
    monkeypatch.setattr(api, "_result_config", lambda: c)
    monkeypatch.setenv("LEVI_LIVE_HOME", c.service.home)
    cli.prepare(c)
    root = Path(c.watch.roots[0])
    source = root / "g/t"
    (source / "demo_0000").mkdir(parents=True)
    (source / "demo_0000/metadata.json").write_text(
        json.dumps({"eval": {"run_id": "s1"}})
    )
    (root / ".eval_sessions").mkdir()
    (root / ".eval_sessions/g-t.json").write_text(
        json.dumps(
            {
                "schema": "levi.eval.session.v1",
                "group": "g",
                "task_folder": "t",
                "session_id": "s1",
                "run_id": "s1",
                "state": "finished",
            }
        )
    )
    put_state(
        c,
        "g__t",
        "g",
        "t",
        {"demo_0000": {"state": "done", "run_id": "s1", "episode_index": 0}},
        root=str(root),
        source=str(source),
    )
    return client


def test_the_route_is_disabled_outside_a_live_workspace(client, tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_workspace", lambda: tmp_path)
    monkeypatch.setattr(api, "_result_config", lambda: None)
    api._REASON.set("not_live")
    assert client.get("/api/levi/live/analysis").json() == {
        "enabled": False,
        "reason": "not_live",
    }


def test_the_route_answers_the_summary_and_writes_nothing(
    client, tmp_path, monkeypatch
):
    c = workspace(tmp_path)
    c = _client(client, c, monkeypatch) and c
    analysis.append(
        c.live_dir,
        record(
            operator=operator("failure"),
            segments=segments_block(("approach", 0, 2, "success")),
        ),
    )
    before = analysis.path(c.live_dir).read_bytes()
    body = client.get("/api/levi/live/analysis").json()
    assert body["enabled"] and body["schema"] == analysis.SUMMARY_SCHEMA
    assert body["review"] == "auto"
    assert body["cells"][0]["group"] == "g"
    only = client.get("/api/levi/live/analysis?group=nope").json()
    assert only["cells"] == []
    assert analysis.path(c.live_dir).read_bytes() == before
    text = json.dumps(body)
    assert str(tmp_path) not in text


def test_the_route_refuses_odd_parameters(client, tmp_path, monkeypatch):
    c = workspace(tmp_path)
    _client(client, c, monkeypatch)
    assert client.get("/api/levi/live/analysis?since=yesterday").status_code == 400
    assert client.get("/api/levi/live/analysis?group=" + "x" * 300).status_code == 400
    assert client.get("/api/levi/live/analysis?stages=" + "a," * 60).status_code == 400
    ok = client.get("/api/levi/live/analysis?stages=open,pour")
    assert ok.status_code == 200 and ok.json()["stages"] == ["open", "pour"]
