"""The labelling queue seen from outside (the HTTP route, ``levi live queue``,
``levi live init``, ``levi live doctor``) and the live pipeline's link to the
labelling engine (``[pipeline.workflow]``, capability drift, the ``capability``
record). Temporary workspaces and a fake model; no GPU, no service."""

# ruff: noqa: F811
import json
import tomllib
from types import SimpleNamespace

import pytest
from conftest import client  # noqa: F401
from test_live_exclude import audit_lines, env, live_api, mirror_only  # noqa: F401
from test_live_pipeline import NAME, _ReviewOnlyWorker, review_only

from levi.live import capability, cli, jsonio, mirror
from levi.live import config as live_config
from levi.live import queue as lq

URL = f"/api/levi/live/datasets/{NAME}/labelling"


# --- the HTTP route -------------------------------------------------------------


def test_a_person_sets_the_mode_and_it_is_audited_once(live_api, monkeypatch):
    client, e = live_api
    mirror_only(e, 0)
    monkeypatch.setenv("LEVI_UI_TOKEN", "test-ui-token")
    person = {"x-levi-ui-token": "test-ui-token"}
    body = {"mode": "paused", "request_id": "r-1"}
    done = client.post(URL, json=body, headers=person)
    assert done.status_code == 200
    value = done.json()
    assert (
        value["mode"] == "paused" and value["changed"] and value["previous"] == "auto"
    )
    assert lq.mode_of(e.config, NAME) == "paused"
    # The same call again (a retry) changes and audits nothing more.
    again = client.post(URL, json=body, headers=person).json()
    assert again["replayed"] and not again["changed"]
    lines = [x for x in audit_lines(e) if x["tool"] == "labelling.set"]
    assert len(lines) == 1 and lines[0]["mode"] == "paused"
    # Another meaning for the same id is a conflict.
    clash = client.post(URL, json={"mode": "off", "request_id": "r-1"}, headers=person)
    assert clash.status_code == 409
    assert lq.mode_of(e.config, NAME) == "paused"


def test_only_a_person_may_change_a_mode(live_api, monkeypatch):
    client, e = live_api
    mirror_only(e, 0)
    monkeypatch.setenv("LEVI_UI_TOKEN", "test-ui-token")
    monkeypatch.setenv("LEVI_AGENT_TOKEN", "test-scoped-token")
    body = {"mode": "history", "request_id": "x"}
    agent = {"Authorization": "Bearer test-scoped-token"}
    person = {"x-levi-ui-token": "test-ui-token"}
    assert client.post(URL, json=body).status_code == 401
    assert client.post(URL, json=body, headers=agent).status_code == 403
    assert client.post(URL, json=body, headers={**agent, **person}).status_code == 403
    assert not lq.path(e.config).exists() or lq.mode_of(e.config, NAME) == "auto"
    assert [x for x in audit_lines(e) if x["tool"] == "labelling.set"] == []


def test_bad_requests_and_unknown_datasets_are_refused(live_api, monkeypatch):
    client, e = live_api
    mirror_only(e, 0)
    monkeypatch.setenv("LEVI_UI_TOKEN", "test-ui-token")
    person = {"x-levi-ui-token": "test-ui-token"}
    bad = client.post(
        URL, json={"mode": "sometimes", "request_id": "a"}, headers=person
    )
    assert bad.status_code == 422
    missing = client.post(URL, json={"mode": "auto"}, headers=person)
    assert missing.status_code == 422
    unknown = client.post(
        "/api/levi/live/datasets/nothing_here/labelling",
        json={"mode": "auto", "request_id": "a"},
        headers=person,
    )
    assert unknown.status_code == 404
    assert not lq.path(e.config).exists()


def test_a_damaged_registry_is_refused_and_left_alone(live_api, monkeypatch):
    client, e = live_api
    mirror_only(e, 0)
    monkeypatch.setenv("LEVI_UI_TOKEN", "test-ui-token")
    person = {"x-levi-ui-token": "test-ui-token"}
    lq.path(e.config).write_text("{oops")
    answer = client.post(URL, json={"mode": "auto", "request_id": "a"}, headers=person)
    assert answer.status_code == 409
    assert lq.path(e.config).read_text() == "{oops"


def test_the_queue_view_lists_modes_and_the_status_rows(live_api, monkeypatch):
    client, e = live_api
    mirror_only(e, 0)
    lq.set_mode(e.config, NAME, "history")
    jsonio.write(
        e.config.status_file,
        {
            "queue": [
                {
                    "dataset": NAME,
                    "mode": "history",
                    "waiting": 1,
                    "state": "queued",
                    "reason": "",
                }
            ]
        },
    )
    value = client.get("/api/levi/live/queue").json()
    assert value["enabled"] and value["datasets"][NAME]["mode"] == "history"
    assert value["queue"][0]["state"] == "queued" and value["default_mode"] == "auto"


# --- the commands -------------------------------------------------------------------


def test_the_queue_command_lists_and_sets(env, capsys):
    e = env()
    mirror_only(e, 0)
    base = ["--workspace", str(e.ws)]
    assert cli.main(["queue", "list", *base]) == 0
    assert "no dataset is registered yet" in capsys.readouterr().out
    assert cli.main(["queue", "set", NAME, "history", *base, "--request-id", "c1"]) == 0
    assert f"{NAME}: auto -> history" in capsys.readouterr().out
    assert cli.main(["queue", "set", NAME, "history", *base, "--request-id", "c1"]) == 0
    assert "already history (replayed)" in capsys.readouterr().out
    assert cli.main(["queue", "list", *base, "--json"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["datasets"][NAME]["mode"] == "history"
    assert listing["datasets"][NAME]["by"] == "cli"
    assert cli.main(["queue", "set", "nothing_here", "auto", *base]) == 2
    assert "no live dataset" in capsys.readouterr().err
    assert (
        cli.main(["queue", "set", NAME, "history", *base, "--request-id", "other"]) == 0
    )
    assert cli.main(["queue", "set", NAME, "off", *base, "--request-id", "c1"]) == 2
    capsys.readouterr()
    lq.path(e.config).write_text("nope")
    assert cli.main(["queue", "set", NAME, "auto", *base]) == 1
    assert "queue.json" in capsys.readouterr().err
    assert lq.path(e.config).read_text() == "nope"
    assert cli.main(["queue", "list", *base]) == 1


def test_init_writes_the_queue_and_yield_tables_and_a_plain_render_does_not(
    tmp_path, capsys
):
    target = tmp_path / "fresh"
    assert cli.main(["init", "--workspace", str(target)]) == 0
    text = (target / "live.toml").read_text()
    data = tomllib.loads(text)
    assert (
        data["pipeline"]["background"] is True and data["pipeline"]["temporal"] is True
    )
    assert data["gpu"]["yield"]["enabled"] is True
    assert data["queue"]["default_mode"] == "auto"
    assert "workflow" not in data["pipeline"]  # an empty table is never written
    assert live_config.from_dict(data).queue == live_config.Queue()
    capsys.readouterr()


def test_the_status_text_names_the_yield_and_every_queue_row():
    value = {
        "state": "gpu_wait",
        "workspace": "/w",
        "gpu": {},
        "yield": {
            "enabled": True,
            "active": True,
            "since": 1.0,
            "reasons": [{"code": "deployment", "text": "a policy server is listening"}],
            "resume_at": None,
        },
        "queue": [
            {
                "dataset": "a",
                "mode": "auto",
                "waiting": 3,
                "state": "yielding",
                "reason": "a policy server is listening",
            }
        ],
    }
    text = cli.format_status(value, True)
    assert "YIELDING the GPU" in text and "deployment: a policy server" in text
    assert "queue      a: auto, 3 waiting, yielding" in text
    value["yield"]["active"] = False
    assert "not yielding" in cli.format_status(value, True)
    value["yield"]["enabled"] = False
    assert "yield      off" in cli.format_status(value, True)


# --- [pipeline.workflow] ------------------------------------------------------------


def parsed(text):
    return live_config.from_dict(tomllib.loads(text))


def test_extra_workflow_options_are_checked_by_the_planner_s_own_model():
    c = parsed(
        '[pipeline.workflow]\nevent_intelligence = { mode = "candidates", max_windows = 2 }'
    )
    assert c.pipeline.workflow["event_intelligence"]["max_windows"] == 2
    for text, word in (
        ("[pipeline.workflow]\nnonsense = 1", "nonsense"),
        ('[pipeline.workflow]\nevent_intelligence = { mode = "magic" }', "mode"),
        (
            "[pipeline.workflow]\nevent_intelligence = { max_windows = 99 }",
            "max_windows",
        ),
        ('[pipeline.workflow]\nkind = "review"', "set by the live pipeline"),
        ('[pipeline.workflow]\nrefine = "auto"', "pipeline.refine"),
    ):
        with pytest.raises(ValueError) as err:
            parsed(text)
        assert word in str(err.value)


def test_an_empty_table_changes_nothing_and_a_set_one_survives_a_round_trip():
    plain = live_config.render()
    assert "[pipeline.workflow]" not in plain
    assert plain == live_config.render(live_config.Config())
    c = parsed(
        '[pipeline.workflow]\nevent_intelligence = { mode = "candidates", sources = ["gripper", "still"] }'
    )
    text = live_config.render(c)
    assert "[pipeline.workflow]" in text
    again = live_config.from_dict(tomllib.loads(text))
    assert again.pipeline.workflow == c.pipeline.workflow


def test_the_worker_merges_the_extra_options_into_the_temporal_workflow():
    from levi.live.worker import Worker

    class Stop(Exception):
        pass

    def worker(config):
        w = Worker.__new__(Worker)
        w.config, w.name = config, NAME
        w.repo_id, w.provider_spec = "local/x", {"name": "p", "max_images": 128}
        w.judged_task = lambda state=None: ("put the plate away", None)
        w.check_stop = lambda: None
        w.progress = lambda *a, **k: None
        seen = []

        def plan(context):
            seen.append(context)
            raise Stop

        w.plan = plan
        return w, seen

    batch = {"demos": ["demo_0000"], "temporal": {}}
    for extra, expected in (
        ({}, None),
        ({"event_intelligence": {"mode": "candidates"}}, {"mode": "candidates"}),
    ):
        c = live_config.Config()
        c.pipeline.workflow = extra
        w, seen = worker(c)
        with pytest.raises(Stop):
            w.temporal(batch, {"demo_0000": 1}, {"demo_0000": 10.0})
        flow = seen[0]["workflow"]
        assert flow.get("event_intelligence") == expected
        # What the worker owns is the worker's, whatever the table says.
        assert flow["kind"] == "temporal" and flow["require_human_pilot"] is False
    # And the plan accepts it: the very model runs.plan validates with.
    capability.workflow_model().model_validate(flow)


# --- capability drift ------------------------------------------------------------------


def test_drift_lists_new_options_the_pipeline_does_not_know_and_only_those():
    c = live_config.Config()
    names = [d["option"] for d in capability.drift(c)]
    assert names == ["event_intelligence"]
    c.pipeline.workflow = {"event_intelligence": {"mode": "candidates"}}
    assert capability.drift(c) == []
    # An option an upgrade adds shows up on its own.
    fields = capability.options()
    assert {"kind", "refine", "event_intelligence"} <= set(fields)
    original = capability.options
    capability.options = lambda: {**original(), "brand_new": 5}
    try:
        assert [d["option"] for d in capability.drift(c)] == ["brand_new"]
    finally:
        capability.options = original


def test_the_doctor_says_so_and_changes_nothing(env):
    e = env()
    before = {p.name: p.read_bytes() for p in e.ws.glob("live.toml")}
    report = cli.diagnose(e.config)
    assert [d["option"] for d in report["capability_drift"]] == ["event_intelligence"]
    assert any(
        "capability drift" in n and "event_intelligence" in n for n in report["notes"]
    )
    assert not any("capability drift" in w for w in report["warnings"])
    assert {p.name: p.read_bytes() for p in e.ws.glob("live.toml")} == before
    e.config.pipeline.workflow = {"event_intelligence": {"mode": "candidates"}}
    again = cli.diagnose(e.config)
    assert again["capability_drift"] == []


# --- what an automatic result records ------------------------------------------------


def test_the_stamp_has_commit_plan_spec_and_event_intelligence():
    run = {
        "plan": {"digest": "abc123"},
        "context": {"workflow": {"event_intelligence": {"mode": "candidates"}}},
        "event_algorithm": {"version": 1, "sha256": "f" * 64},
    }
    got = capability.stamp(run, kind="temporal", at=5.0)
    assert got["schema"] == capability.SCHEMA and got["at"] == 5.0
    assert got["plan_digest"] == "abc123" and got["spec"] is None
    assert got["event_intelligence"] == {
        "mode": "candidates",
        "algorithm_sha256": "f" * 64,
    }
    assert set(got["levi"]) == {"commit", "version"}
    commit = got["levi"]["commit"]
    assert commit is None or len(commit) == 40
    review = capability.stamp({}, kind="review", spec=("generic-release", 3))
    assert review["spec"] == "generic-release@3" and review["plan_digest"] is None
    assert review["event_intelligence"] == {"mode": "off", "algorithm_sha256": None}
    assert capability.of({"capability": got}) == got
    assert capability.of({"capability": {"schema": "other"}}) is None


def test_old_results_without_the_record_are_read_as_having_none():
    for part in (None, {}, {"run_id": "r"}, {"capability": "junk"}, "text"):
        assert capability.of(part) is None


def test_the_two_stages_of_one_episode_share_one_record():
    time_run = {
        "plan": {"digest": "t1"},
        "context": {"workflow": {"event_intelligence": {"mode": "candidates"}}},
        "event_algorithm": {"sha256": "b" * 64},
    }
    first = capability.combine(
        None, capability.stamp(time_run, kind="temporal", at=1.0)
    )
    both = capability.combine(
        first,
        capability.stamp(
            {"plan": {"digest": "r1"}}, kind="review", spec=("s", 2), at=2.0
        ),
    )
    assert both["plans"] == {"temporal": "t1", "review": "r1"}
    assert both["plan_digest"] == "r1" and both["spec"] == "s@2"
    # The review has no event intelligence of its own: the segments' stays.
    assert both["event_intelligence"]["algorithm_sha256"] == "b" * 64
    assert both["at"] == 2.0 and both["schema"] == capability.SCHEMA
    # A review first, the segments later.
    review_first = capability.combine(
        None, capability.stamp({"plan": {"digest": "r"}}, kind="review", at=1.0)
    )
    later = capability.combine(
        review_first, capability.stamp(time_run, kind="temporal", at=2.0)
    )
    assert later["event_intelligence"]["mode"] == "candidates"


def test_the_analysis_hook_runs_after_a_finished_or_failed_batch_only(
    tmp_path, monkeypatch
):
    import sys
    import types

    from levi.live import worker

    calls = []
    fake = types.ModuleType("levi.live.analysis")
    fake.sync_quiet = lambda config, name=None: calls.append(name)
    c = live_config.Config()
    # Absent module (an older checkout): nothing happens, nothing fails.
    monkeypatch.setitem(sys.modules, "levi.live.analysis", None)
    assert worker.after_batch(c, "d", worker.OK) is False
    monkeypatch.setitem(sys.modules, "levi.live.analysis", fake)
    for code in (worker.OK, worker.ERROR):
        assert worker.after_batch(c, "d", code) is True
    # Stopped for the GPU, waiting for a person or the model, nothing to do:
    # not called (the labelling may be yielding).
    for code in (
        worker.PREEMPTED,
        worker.AWAIT_HUMAN,
        worker.NEED_MODEL,
        worker.NOTHING,
        worker.DATASET_BUSY,
    ):
        assert worker.after_batch(c, "d", code) is False
    assert calls == ["d", "d"]

    # A module that raises does not fail the worker.
    def boom(config, name=None):
        raise RuntimeError("x")

    fake.sync_quiet = boom
    assert worker.after_batch(c, "d", worker.OK) is True


def test_a_review_verdict_carries_the_record_and_old_verdicts_do_not(tmp_path):
    c = live_config.Config()
    c.service.workspace = str(tmp_path / "ws")
    c.service.home = str(tmp_path / "home")
    c.pipeline.cleanup = False
    review_only(SimpleNamespace(config=c))
    jsonio.write(
        mirror.state_path(c, NAME),
        {
            "name": NAME,
            "task_text": "put the eggplant on the plate",
            "demos": {
                "demo_0000": {"state": "mirrored", "attempts": 0},
                "demo_0009": {"state": "done", "verdict": {"outcome": "success"}},
            },
        },
    )
    success = {
        "events": [{"valid": True}],
        "outcome": "success",
        "basis": {"valid_events": 1},
        "spec": {"id": "generic-release", "version": 3},
    }
    probe = _ReviewOnlyWorker(c, records=[success])
    assert probe.batch() is not None
    rows = mirror.load_state(c, NAME)["demos"]
    verdict = rows["demo_0000"]["verdict"]
    assert verdict["outcome"] == "success" and verdict["spec_version"] == 3
    assert "capability" not in verdict  # top level of the episode's record
    got = capability.of(rows["demo_0000"])
    assert got["spec"] == "generic-release@3" and "review" in got["plans"]
    assert len(json.dumps(got)) < capability.MAX_BYTES
    # A verdict made before the record existed reads as none, and the
    # statistics built from either come out the same.
    assert capability.of(rows["demo_0009"]) is None
    for demo in ("demo_0000", "demo_0009"):
        record = probe.worker.stats_row(
            demo, 3, rows[demo], [], first=False, frames=None, waking={}
        )
        assert record["result"]["state"] == "done"
        # The statistics row carries the record at its top level too.
        assert ("capability" in record) == (demo == "demo_0000")


def test_a_temporal_result_carries_the_record(tmp_path):
    from levi.live.worker import Worker

    c = live_config.Config()
    c.service.workspace = str(tmp_path / "ws")
    c.service.home = str(tmp_path / "home")
    jsonio.write(
        mirror.state_path(c, NAME),
        {
            "name": NAME,
            "demos": {
                "demo_0000": {"state": "mirrored", "attempts": 0},
                "demo_0001": {"state": "mirrored", "attempts": 0},
            },
        },
    )
    w = Worker.__new__(Worker)
    w.config, w.name = c, NAME
    w.store = SimpleNamespace(
        get=lambda kind, ident: {
            "provenance": {"reviewer_type": "auto"},
            "proposals": [{"episode_index": 4}, {"episode_index": 4}],
        }
    )
    run = {
        "id": "run-9",
        "completed": [4],
        "failed": [],
        "plan": {"digest": "d1"},
        "context": {"workflow": {"event_intelligence": {"mode": "candidates"}}},
        "event_algorithm": {"sha256": "a" * 64},
    }
    batch = {"done": []}
    w.record_temporal(["demo_0000"], {"demo_0000": 4}, run, "cs-1", batch)
    row = mirror.load_state(c, NAME)["demos"]["demo_0000"]
    assert row["state"] == "annotating" and row["temporal"]["segments"] == 2
    assert "capability" not in row["temporal"]
    got = capability.of(row)
    assert got["plans"] == {"temporal": "d1"} and got["plan_digest"] == "d1"
    assert got["event_intelligence"] == {
        "mode": "candidates",
        "algorithm_sha256": "a" * 64,
    }
    assert len(json.dumps(got)) < capability.MAX_BYTES
    # An episode the run did not complete gets no record.
    other = mirror.load_state(c, NAME)["demos"]["demo_0001"]
    assert "temporal" not in other
