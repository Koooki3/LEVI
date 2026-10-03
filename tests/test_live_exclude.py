"""Taking an episode out of a live dataset and putting it back: the state it
leaves, what the worker and the counts do with it, the review runs, the
audit, and who may do it. Fake model server and temporary workspaces only:
no GPU, no live service, no robot."""

import json
import threading
import time
from pathlib import Path

import pytest
from test_live_pipeline import NAME, Env

from levi.live import api, auto, cli, exclusion, jsonio, mirror, worker


@pytest.fixture
def env(tmp_path, demo_template):
    made = []

    def make(**fake_options):
        made.append(Env(tmp_path, demo_template, **fake_options))
        return made[-1]

    yield make
    for item in made:
        item.close()


def mirror_only(e, *numbers):
    """Mirror finished demos without labelling them: rows in state
    ``mirrored``, the capture in place."""
    for n in numbers:
        e.rollouts.write(n)
    scan = next(t for t in mirror.Scanner(e.config).scan() if t.name == NAME)
    mirror.mirror_dataset(e.config, e.state(), scan.ready)


def audit_lines(e):
    path = e.ws / "live/audit.jsonl"
    return (
        [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
    )


# --- the data model ----------------------------------------------------------------------


def test_an_old_state_file_without_the_key_is_simply_not_excluded(env):
    e = env()
    mirror_only(e, 0, 1)
    state = e.state()
    assert all("excluded" not in r for r in state["demos"].values())
    assert exclusion.excluded_count(state) == 0
    assert mirror.counts(state)["mirrored"] == 2


def test_excluding_adds_one_key_and_restoring_gives_the_row_back_as_it_was(env):
    e = env()
    mirror_only(e, 0, 1)
    before = e.state()
    result = exclusion.exclude(
        e.config, NAME, ["demo_0000"], "  the arm hit\nthe table  ", now=123.0
    )
    assert result["changed"] == ["demo_0000"] and result["unchanged"] == []
    after = e.state()
    row = dict(after["demos"]["demo_0000"])
    assert row.pop("excluded") == {
        "at": 123.0,
        "by": "person",
        "reason": "the arm hit the table",
    }
    # Nothing else moved: the row, the other row and every other key.
    assert row == before["demos"]["demo_0000"]
    assert after["demos"]["demo_0001"] == before["demos"]["demo_0001"]
    assert {k: v for k, v in after.items() if k != "demos"} == {
        k: v for k, v in before.items() if k != "demos"
    }
    assert mirror.counts(after)["mirrored"] == 1
    assert result["counts"]["mirrored"] == 1 and result["excluded_count"] == 1
    back = exclusion.restore(e.config, NAME, ["demo_0000"])
    assert back["changed"] == ["demo_0000"] and back["excluded_count"] == 0
    assert e.state()["demos"] == before["demos"]
    assert mirror.counts(e.state())["mirrored"] == 2


def test_the_reason_is_optional_and_bounded(env):
    e = env()
    mirror_only(e, 0)
    exclusion.exclude(e.config, NAME, ["demo_0000"], "x" * 1000)
    assert len(e.state()["demos"]["demo_0000"]["excluded"]["reason"]) == 300
    exclusion.restore(e.config, NAME, ["demo_0000"])
    exclusion.exclude(e.config, NAME, ["demo_0000"])
    assert e.state()["demos"]["demo_0000"]["excluded"]["reason"] == ""


def test_both_directions_are_idempotent_and_leave_the_first_record(env):
    e = env()
    mirror_only(e, 0)
    first = exclusion.exclude(e.config, NAME, ["demo_0000"], "one", now=1.0)
    again = exclusion.exclude(e.config, NAME, ["demo_0000"], "two", now=2.0)
    assert first["changed"] == ["demo_0000"]
    assert again["changed"] == [] and again["unchanged"] == ["demo_0000"]
    assert e.state()["demos"]["demo_0000"]["excluded"] == {
        "at": 1.0,
        "by": "person",
        "reason": "one",
    }
    exclusion.restore(e.config, NAME, ["demo_0000"])
    nothing = exclusion.restore(e.config, NAME, ["demo_0000"])
    assert nothing["changed"] == [] and nothing["unchanged"] == ["demo_0000"]
    # Only the two real changes were audited.
    assert [x["tool"] for x in audit_lines(e)] == ["episode.exclude", "episode.restore"]


def test_a_request_is_all_or_nothing_and_says_why(env):
    e = env()
    mirror_only(e, 0, 1)
    jsonio.update(
        mirror.state_path(e.config, NAME),
        lambda v: v["demos"].update(
            demo_0002={"state": "rejected", "reason": "unusable video"}
        ),
    )
    before = e.state()
    with pytest.raises(exclusion.Unknown) as unknown:
        exclusion.exclude(e.config, NAME, ["demo_0000", "demo_0099"])
    assert unknown.value.demos == ["demo_0099"]
    with pytest.raises(exclusion.NotPart) as part:
        exclusion.exclude(e.config, NAME, ["demo_0000", "demo_0002"])
    assert part.value.demos == ["demo_0002"]
    jsonio.update(
        mirror.state_path(e.config, NAME),
        lambda v: v.update(current={"demos": ["demo_0001"], "done": []}),
    )
    before = e.state()
    with pytest.raises(exclusion.Busy) as busy:
        exclusion.exclude(e.config, NAME, ["demo_0000", "demo_0001"])
    assert busy.value.demos == ["demo_0001"]
    with pytest.raises(exclusion.Unknown):
        exclusion.restore(e.config, NAME, ["demo_0099"])
    assert e.state() == before  # not one of them was removed
    assert audit_lines(e) == []


# --- the worker --------------------------------------------------------------------------


def test_the_worker_does_not_label_an_excluded_episode_and_the_numbers_leave_it_out(
    env,
):
    e = env()
    mirror_only(e, 0, 1, 2, 3)
    exclusion.exclude(e.config, NAME, ["demo_0003"], "robot reflex")
    ctl = e.run()
    state = e.state()
    states = {d: r["state"] for d, r in state["demos"].items()}
    assert states == {
        "demo_0000": "done",
        "demo_0001": "done",
        "demo_0002": "done",
        "demo_0003": "mirrored",  # never touched
    }
    assert state["demos"]["demo_0003"]["attempts"] == 0
    assert state["demos"]["demo_0003"]["excluded"]["reason"] == "robot reflex"
    episodes = {
        p["episode_index"]
        for c in e.records("changes")
        if c["status"] == "committed"
        for p in c["proposals"]
    }
    assert episodes == {0, 1, 2}
    # The capture keeps its mirror: nothing is deleted.
    assert (Path(state["capture"]) / "demo_0003").is_dir()
    row = ctl.status()["datasets"][NAME]
    assert (row["episodes"], row["done"], row["pending"], row["excluded"]) == (
        3,
        3,
        0,
        1,
    )
    # Nothing waits, so the service is idle rather than "pending" for ever.
    assert row["state"] == "idle" and ctl.status()["queue_depth"] == 0


def test_restoring_makes_it_wait_again_and_the_next_batch_labels_it(env):
    e = env()
    mirror_only(e, 0, 1)
    exclusion.exclude(e.config, NAME, ["demo_0001"])
    e.run()
    assert e.state()["demos"]["demo_0001"]["state"] == "mirrored"
    exclusion.restore(e.config, NAME, ["demo_0001"])
    ctl = e.controller()
    ctl._refresh(time.time(), True)
    assert ctl.status()["datasets"][NAME]["pending"] == 1
    e.run()
    assert {r["state"] for r in e.state()["demos"].values()} == {"done"}


def test_an_annotated_episode_keeps_its_annotations_but_stops_counting(env):
    e = env()
    e.rollouts.write(0)
    e.rollouts.write(1)
    e.run()
    atoms = e.atoms(1)
    exclusion.exclude(e.config, NAME, ["demo_0001"])
    ctl = e.controller()
    ctl._refresh(time.time(), True)
    row = ctl.status()["datasets"][NAME]
    assert (row["episodes"], row["done"], row["excluded"]) == (1, 1, 1)
    assert e.atoms(1) == atoms  # the committed segments stay in LEVI
    assert e.state()["demos"]["demo_0001"]["verdict"]["outcome"] == "success"


def test_the_filter_passes_over_a_demo_excluded_after_the_batch_was_chosen(
    env,
):
    """The window between choosing a batch and saving it: the click wins, the
    worker leaves the row exactly as it was."""
    e = env()
    mirror_only(e, 0, 1)

    class Stub(worker.Worker):
        def __init__(self, config, name):
            self.config, self.name = config, name

        def committed_here(self, batch):
            return set()

        def human_annotated(self, episode):
            return False

    exclusion.exclude(e.config, NAME, ["demo_0000"])
    before = e.state()["demos"]["demo_0000"]
    keep = Stub(e.config, NAME).filter_demos(
        ["demo_0000", "demo_0001"], {"demo_0000": 0, "demo_0001": 1}, {}
    )
    assert keep == ["demo_0001"]
    assert e.state()["demos"]["demo_0000"] == before


def test_excluding_while_a_batch_runs_is_refused_for_its_episodes_only(env):
    e = env(delay=0.3)
    mirror_only(e, 0, 1, 2, 3)
    e.config.watch.batch_max_episodes = 2
    ctl = e.controller()
    thread = threading.Thread(target=lambda: ctl.run(once=True, max_seconds=240))
    thread.start()
    try:
        deadline = time.time() + 90
        while time.time() < deadline and not (e.state() or {}).get("current"):
            time.sleep(0.05)
        batch = e.state()["current"]["demos"]
        assert batch == ["demo_0000", "demo_0001"]
        with pytest.raises(exclusion.Busy):
            exclusion.exclude(e.config, NAME, ["demo_0000"])
        # The waiting ones are free to go, whatever the worker is doing.
        exclusion.exclude(e.config, NAME, ["demo_0003"], "while a batch ran")
    finally:
        e.fake.delay = 0
        thread.join(240)
    state = e.state()
    states = {d: r["state"] for d, r in state["demos"].items()}
    assert (
        states["demo_0003"] == "mirrored" and "excluded" in state["demos"]["demo_0003"]
    )
    assert states["demo_0000"] == states["demo_0001"] == states["demo_0002"] == "done"
    assert "excluded" not in state["demos"]["demo_0000"]


def test_a_source_that_changed_is_not_mirrored_again_while_the_episode_is_out(env):
    e = env()
    mirror_only(e, 0)
    exclusion.exclude(e.config, NAME, ["demo_0000"])
    jsonio.update(
        mirror.state_path(e.config, NAME),
        lambda v: v["demos"]["demo_0000"].update(source_changed=time.time()),
    )
    capture = Path(e.state()["capture"]) / "demo_0000"
    inode = capture.stat().st_ino
    assert mirror.refresh_changed(e.config, NAME) == []
    assert capture.stat().st_ino == inode
    ctl = e.controller()
    ctl._refresh(time.time(), True)
    assert ctl.status()["datasets"][NAME]["source_changed"] == 0


# --- review runs ---------------------------------------------------------------------------


def run_status(e, run_id):
    return next(r["status"] for r in e.records("runs") if r["id"] == run_id)


def test_a_review_run_is_not_cancelled_it_only_stops_counting(env):
    """Cancelling would let the cleanup delete its frozen input, and a restored
    episode could then never be accepted. The run is left alone."""
    e = env()
    e.rollouts.write(0)
    e.rollouts.write(1)
    e.run()
    state = e.state()
    (run_id,) = state["review_runs"]
    assert state["review_runs_open"] == 1
    base = e.ws / f"outputs/LEVI/workbench/agent/datasets/{NAME}/runs"
    assert (base / run_id / "input").is_dir()

    def counted():
        ctl = e.controller()
        ctl._refresh(time.time(), True)
        return ctl.status()["datasets"][NAME]["review_runs_open"]

    # One of two out: the run is still the other one's.
    one = exclusion.exclude(e.config, NAME, ["demo_0000"])
    assert one["review_hidden"] == [] and one["review_runs_open"] == 1
    assert counted() == 1
    # Both out: nothing is left to review, so it no longer counts...
    both = exclusion.exclude(e.config, NAME, ["demo_0001"])
    assert both["review_hidden"] == [run_id] and both["review_runs_open"] == 0
    assert counted() == 0
    # ...but it is untouched: open, with its input, in the dataset state.
    assert run_status(e, run_id) == "waiting_for_review"
    assert (base / run_id / "input").is_dir()
    state = e.state()
    assert state["review_runs"] == [run_id]
    assert state["demos"]["demo_0001"]["verdict"]["run_id"] == run_id
    assert audit_lines(e)[-1]["tool"] == "episode.exclude"
    assert "runs_cancelled" not in audit_lines(e)[-1]
    # Restoring brings the count back and the run is as it was.
    back = exclusion.restore(e.config, NAME, ["demo_0000", "demo_0001"])
    assert back["review_runs_open"] == 1 and back["review_hidden"] == []
    assert counted() == 1 and run_status(e, run_id) == "waiting_for_review"
    assert e.state()["demos"]["demo_0000"]["state"] == "done"
    # Every count is back too, and no run anywhere was cancelled.
    assert back["counts"]["done"] == 2 and back["excluded_count"] == 0
    assert not [r for r in e.records("runs") if r["status"] == "cancelled"]


def test_the_exclusion_makes_no_call_into_the_run_store(env, monkeypatch):
    """Under the state file's lock it validates and writes marks only."""
    e = env()
    e.rollouts.write(0)
    e.run()
    from levi.agent import store

    def forbidden(*args, **kwargs):
        raise AssertionError("the run store was touched")

    for name in ("mutate", "get", "list"):
        monkeypatch.setattr(store.Store, name, forbidden)
    exclusion.exclude(e.config, NAME, ["demo_0000"])
    exclusion.restore(e.config, NAME, ["demo_0000"])


def test_an_old_file_without_a_run_list_keeps_its_stored_count(env):
    e = env()
    mirror_only(e, 0)
    jsonio.update(
        mirror.state_path(e.config, NAME),
        lambda v: v.update(review_runs_open=2),
    )
    assert exclusion.open_review_count(e.state()) == 2


def test_a_refresh_leaves_the_mirror_of_an_episode_removed_meanwhile(env, monkeypatch):
    """The state read before the lock says "not excluded"; the check under the
    lock must see the removal and keep the mirror as it is."""
    e = env()
    mirror_only(e, 0)
    jsonio.update(
        mirror.state_path(e.config, NAME),
        lambda v: v["demos"]["demo_0000"].update(source_changed=time.time()),
    )
    capture = Path(e.state()["capture"]) / "demo_0000"
    inode = capture.stat().st_ino
    real_load = mirror.load_state
    first = {"done": False}

    def stale_then_remove(config, name):
        value = real_load(config, name)
        if not first["done"]:
            first["done"] = True
            snapshot = json.loads(json.dumps(value))
            exclusion.exclude(config, name, ["demo_0000"])  # the click lands now
            return snapshot
        return value

    monkeypatch.setattr(mirror, "load_state", stale_then_remove)
    assert mirror.refresh_changed(e.config, NAME) == []
    assert capture.stat().st_ino == inode
    assert exclusion.is_excluded(e.state()["demos"]["demo_0000"])


# --- the audit -------------------------------------------------------------------------------


def test_every_change_is_an_audit_line_without_a_secret(env):
    e = env()
    mirror_only(e, 0, 1)
    exclusion.exclude(e.config, NAME, ["demo_0000", "demo_0001"], "bad takes", now=5.0)
    exclusion.restore(e.config, NAME, ["demo_0001"], now=6.0)
    lines = audit_lines(e)
    assert [(x["tool"], x["demo"]) for x in lines] == [
        ("episode.exclude", "demo_0000"),
        ("episode.exclude", "demo_0001"),
        ("episode.restore", "demo_0001"),
    ]
    first = lines[0]
    assert first["time"] == 5.0 and first["dataset"] == NAME
    assert first["reason"] == "bad takes" and first["actor"] == "person"
    assert first["decision"] == "completed" and first["principal"] == "local-human"
    assert "reason" not in lines[2] and lines[2]["time"] == 6.0
    text = (e.ws / "live/audit.jsonl").read_text().lower()
    assert "token" not in text and "key" not in text


def test_an_audit_log_that_cannot_be_written_does_not_undo_the_change(env, monkeypatch):
    e = env()
    mirror_only(e, 0)

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(jsonio, "append_line", broken)
    exclusion.exclude(e.config, NAME, ["demo_0000"])
    assert exclusion.is_excluded(e.state()["demos"]["demo_0000"])


# --- who may do it -----------------------------------------------------------------------------


def test_the_automatic_approver_has_no_such_call():
    for tool in (exclusion.TOOL_EXCLUDE, exclusion.TOOL_RESTORE):
        assert tool not in auto.ALLOWED


@pytest.fixture
def live_api(client, env, monkeypatch):
    e = env()
    monkeypatch.setattr(api, "_workspace", lambda: e.ws)
    monkeypatch.setenv("LEVI_LIVE_HOME", e.config.service.home)
    return client, e


ROUTES = [
    ("/exclude", {"demos": ["demo_0000"], "reason": "x"}),
    ("/restore", {"demos": ["demo_0000"]}),
    ("/demos/demo_0000/exclude", {"reason": "x"}),
    ("/demos/demo_0000/restore", None),
]


def test_removing_and_restoring_need_the_ui_token_and_refuse_an_agent(
    live_api, monkeypatch
):
    client, e = live_api
    mirror_only(e, 0)
    monkeypatch.setenv("LEVI_UI_TOKEN", "test-ui-token")
    monkeypatch.setenv("LEVI_AGENT_TOKEN", "test-scoped-token")
    person = {"x-levi-ui-token": "test-ui-token"}
    agent = {"Authorization": "Bearer test-scoped-token"}
    both = {**agent, **person}
    for suffix, body in ROUTES:
        url = f"/api/levi/live/datasets/{NAME}{suffix}"
        assert client.post(url, json=body).status_code == 401, url
        assert client.post(url, json=body, headers=agent).status_code == 403, url
        # Even carrying the UI token as well: an agent credential is refused.
        assert client.post(url, json=body, headers=both).status_code == 403, url
    assert not exclusion.is_excluded(e.state()["demos"]["demo_0000"])
    assert audit_lines(e) == []
    url = f"/api/levi/live/datasets/{NAME}/exclude"
    assert client.post(url, json=ROUTES[0][1], headers=person).status_code == 200
    assert exclusion.is_excluded(e.state()["demos"]["demo_0000"])


def test_an_agent_cannot_reach_it_through_its_own_capabilities(live_api, monkeypatch):
    client, _ = live_api
    monkeypatch.setenv("LEVI_UI_TOKEN", "test-ui-token")
    monkeypatch.setenv("LEVI_AGENT_TOKEN", "test-scoped-token")
    agent = {"Authorization": "Bearer test-scoped-token"}
    listed = client.get("/api/levi/agent/v1/capabilities", headers=agent)
    assert listed.status_code == 200
    text = listed.text.lower()
    assert "exclude" not in text and "episode.restore" not in text
    call = client.post(
        "/api/levi/agent/v1/tools",
        headers=agent,
        json={"name": "episode.exclude", "arguments": {"demos": ["demo_0000"]}},
    )
    assert call.status_code >= 400


def test_the_routes_do_nothing_outside_a_live_workspace(client, tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_workspace", lambda: tmp_path)
    for suffix, body in ROUTES:
        url = f"/api/levi/live/datasets/{NAME}{suffix}"
        assert client.post(url, json=body).status_code == 404


# --- the API ---------------------------------------------------------------------------------


def test_the_api_removes_lists_and_restores_with_the_new_counts(live_api):
    client, e = live_api
    mirror_only(e, 0, 1, 2)
    base = f"/api/levi/live/datasets/{NAME}"
    answer = client.post(
        f"{base}/exclude", json={"demos": ["demo_0001", "demo_0002"], "reason": "r"}
    ).json()
    assert answer["changed"] == ["demo_0001", "demo_0002"]
    assert answer["counts"]["mirrored"] == 1 and answer["excluded_count"] == 2
    detail = client.get(base).json()
    assert [d["demo"] for d in detail["demos"]] == ["demo_0000"]
    assert [d["demo"] for d in detail["excluded_demos"]] == ["demo_0002", "demo_0001"]
    assert detail["excluded_demos"][0]["excluded"]["reason"] == "r"
    assert detail["total_demos"] == 1 and detail["excluded_count"] == 2
    assert detail["counts"]["mirrored"] == 1
    # The same click twice changes nothing.
    again = client.post(f"{base}/demos/demo_0001/exclude").json()
    assert again["changed"] == [] and again["unchanged"] == ["demo_0001"]
    back = client.post(f"{base}/demos/demo_0001/restore").json()
    assert back["changed"] == ["demo_0001"] and back["excluded_count"] == 1
    nothing = client.post(f"{base}/restore", json={"demos": ["demo_0001"]}).json()
    assert nothing["changed"] == [] and nothing["unchanged"] == ["demo_0001"]
    detail = client.get(base).json()
    assert [d["demo"] for d in detail["demos"]] == ["demo_0001", "demo_0000"]
    # The audit view shows it, newest first.
    shown = client.get("/api/levi/live/audit").json()["audit"]
    assert shown[0]["tool"] == "episode.restore"
    assert {x["tool"] for x in shown} == {"episode.exclude", "episode.restore"}


def test_the_api_names_what_it_refused(live_api):
    client, e = live_api
    mirror_only(e, 0, 1)
    jsonio.update(
        mirror.state_path(e.config, NAME),
        lambda v: v.update(current={"demos": ["demo_0001"], "done": []}),
    )
    base = f"/api/levi/live/datasets/{NAME}"
    missing = client.post(f"{base}/exclude", json={"demos": ["demo_0042"]})
    assert missing.status_code == 404 and "demo_0042" in missing.json()["detail"]
    busy = client.post(f"{base}/demos/demo_0001/exclude")
    assert busy.status_code == 409
    assert "being labelled" in busy.json()["detail"]
    assert (
        client.post(
            "/api/levi/live/datasets/nope/exclude", json={"demos": ["a"]}
        ).status_code
        == 404
    )
    assert client.post(f"{base}/exclude", json={"demos": []}).status_code == 422
    assert client.post(f"{base}/exclude", json={"demos": ["../x"]}).status_code == 404
    assert (
        client.post(f"{base}/restore", json={"demos": ["demo_0042"]}).status_code == 404
    )
    assert not exclusion.excluded_count(e.state())


def test_the_api_hides_a_review_run_without_cancelling_it(live_api):
    client, e = live_api
    e.rollouts.write(0)
    e.rollouts.write(1)
    e.run()
    (run_id,) = e.state()["review_runs"]
    base = f"/api/levi/live/datasets/{NAME}"
    done = client.post(f"{base}/exclude", json={"demos": ["demo_0000", "demo_0001"]})
    body = done.json()
    assert body["review_hidden"] == [run_id] and body["review_runs_open"] == 0
    assert "cancelled_runs" not in body
    assert run_status(e, run_id) == "waiting_for_review"
    assert client.get(base).json()["review_runs"] == []
    back = client.post(f"{base}/restore", json={"demos": ["demo_0000", "demo_0001"]})
    assert back.json()["review_runs_open"] == 1
    assert client.get(base).json()["review_runs"] == [run_id]


def test_the_list_of_removed_episodes_is_not_cut_at_the_page_limit(
    live_api, monkeypatch
):
    client, e = live_api
    mirror_only(e, 0, 1, 2)
    monkeypatch.setattr(api, "MAX_DEMOS", 1)
    base = f"/api/levi/live/datasets/{NAME}"
    client.post(f"{base}/exclude", json={"demos": ["demo_0000", "demo_0001"]})
    detail = client.get(base).json()
    assert [d["demo"] for d in detail["excluded_demos"]] == ["demo_0001", "demo_0000"]
    assert detail["excluded_count"] == 2 and len(detail["demos"]) == 1


def request_with(headers):
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        }
    )


def test_the_person_check_works_by_itself_when_the_middleware_is_bypassed(
    env, monkeypatch
):
    """The route's own check, with no service middleware in front of it."""
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    e = env()
    mirror_only(e, 0)
    monkeypatch.setattr(api, "_workspace", lambda: e.ws)
    monkeypatch.setenv("LEVI_UI_TOKEN", "test-ui-token")
    # The function alone.
    api._person(request_with({"x-levi-ui-token": "test-ui-token"}))
    for headers, code in (
        ({}, 401),
        ({"x-levi-ui-token": "wrong"}, 401),
        ({"authorization": "Bearer anything"}, 403),
        ({"authorization": "bearer x", "x-levi-ui-token": "test-ui-token"}, 403),
    ):
        with pytest.raises(HTTPException) as refused:
            api._person(request_with(headers))
        assert refused.value.status_code == code, headers
    # And mounted without the service's middleware.
    app = FastAPI()
    app.include_router(api.router)
    bare = TestClient(app)
    url = f"/api/levi/live/datasets/{NAME}/exclude"
    body = {"demos": ["demo_0000"]}
    assert bare.post(url, json=body).status_code == 401
    assert (
        bare.post(url, json=body, headers={"Authorization": "Bearer t"}).status_code
        == 403
    )
    assert not exclusion.excluded_count(e.state())
    ok = bare.post(url, json=body, headers={"x-levi-ui-token": "test-ui-token"})
    assert ok.status_code == 200 and exclusion.excluded_count(e.state()) == 1
    # Without a configured token (a development run) the Bearer rule still holds.
    monkeypatch.delenv("LEVI_UI_TOKEN")
    assert (
        bare.post(
            f"/api/levi/live/datasets/{NAME}/restore",
            json=body,
            headers={"Authorization": "Bearer t"},
        ).status_code
        == 403
    )


# --- dataset names with a suffix ---------------------------------------------------------------


def renamed(e, new):
    """The dataset's state under another name (what a clash of two roots
    gives it: ``<name>__at__<root mark>``)."""
    state = e.state()
    state["name"] = new
    jsonio.write(mirror.state_path(e.config, new), state)


@pytest.mark.parametrize(
    "new",
    [
        NAME + mirror.ROOT_MARK + "root-2",
        NAME + mirror.ROOT_MARK + "a.b_c-1f2e3d",
        # Not the service's own style: the check does not depend on it.
        NAME + "@root-2",
        "x+y=z@r",
    ],
)
def test_a_dataset_name_with_a_suffix_works_everywhere(live_api, new):
    from urllib.parse import quote

    client, e = live_api
    mirror_only(e, 0, 1)
    renamed(e, new)
    base = f"/api/levi/live/datasets/{quote(new, safe='')}"
    assert client.get(base).json()["name"] == new
    done = client.post(f"{base}/exclude", json={"demos": ["demo_0000"], "reason": "r"})
    assert done.status_code == 200 and done.json()["dataset"] == new
    assert exclusion.is_excluded(mirror.load_state(e.config, new)["demos"]["demo_0000"])
    # The other dataset of that task is untouched.
    assert not exclusion.excluded_count(e.state())
    assert client.get(base).json()["excluded_count"] == 1
    # An unencoded @ in the URL is the same name.
    plain = f"/api/levi/live/datasets/{new}"
    assert (
        client.post(f"{plain}/restore", json={"demos": ["demo_0000"]}).status_code
        == 200
    )
    assert not exclusion.excluded_count(mirror.load_state(e.config, new))
    assert audit_lines(e)[0]["dataset"] == new


def test_a_dataset_name_cannot_name_another_path(live_api):
    client, e = live_api
    mirror_only(e, 0)
    for bad in ("..", ".hidden", "a%2Fb", "..%2Fx", "a%5Cb", "a%00b", "a%0Ab"):
        url = f"/api/levi/live/datasets/{bad}"
        assert client.get(url).status_code == 404, bad
        assert (
            client.post(f"{url}/exclude", json={"demos": ["demo_0000"]}).status_code
            == 404
        ), bad


def test_the_pool_reads_a_suffixed_dataset_by_its_own_name(live_api):
    from levi.pool import exclusions

    _, e = live_api
    mirror_only(e, 0)
    suffixed = NAME + mirror.ROOT_MARK + "root-2"
    renamed(e, suffixed)
    exclusion.exclude(e.config, suffixed, ["demo_0000"])
    found = exclusions.workspace_exclusions(e.ws)
    assert any("__at__root-2" in key for key in found)
    assert {v["dataset"] for v in found.values()} == {suffixed}


# --- levi live exclude (the command line) ----------------------------------------------------------


@pytest.fixture
def served(client, env, monkeypatch):
    """The real service app (its middleware included) on a Unix socket, with
    the person's key file where the command looks for it."""
    import shutil
    import tempfile

    import uvicorn

    from levi import service

    e = env()
    mirror_only(e, 0, 1, 2)
    monkeypatch.setattr(api, "_workspace", lambda: e.ws)
    # A short path: a Unix socket's name is limited to about 100 bytes.
    root = Path(tempfile.mkdtemp(prefix="lvx", dir="/tmp"))
    root.chmod(0o700)
    (root / "human.key").write_text("test-human-key")
    monkeypatch.setattr(cli, "core_dir", lambda config: root)
    monkeypatch.setenv("LEVI_UI_TOKEN", "test-human-key")
    monkeypatch.setenv("LEVI_AGENT_TOKEN", "test-scoped-token")
    server = uvicorn.Server(
        uvicorn.Config(
            service.app, uds=str(root / "api.sock"), lifespan="off", log_level="warning"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    yield e, root
    server.should_exit = True
    thread.join(30)
    shutil.rmtree(root, ignore_errors=True)


def run_cli(e, *args):
    return cli.main(["exclude", *args, "--workspace", str(e.ws)])


def test_the_command_removes_and_restores_through_the_core(served, capsys):
    e, _ = served
    code = run_cli(e, NAME, "demo_0000", "demo_0002", "--reason", "pre-test takes")
    out = capsys.readouterr()
    assert code == 0 and "2 episode(s) removed" in out.out
    assert "test-human-key" not in out.out + out.err
    state = e.state()
    assert state["demos"]["demo_0000"]["excluded"]["reason"] == "pre-test takes"
    assert exclusion.is_excluded(state["demos"]["demo_0002"])
    assert not exclusion.is_excluded(state["demos"]["demo_0001"])
    assert [x["tool"] for x in audit_lines(e)] == ["episode.exclude"] * 2
    # The same again changes nothing and says so.
    assert run_cli(e, NAME, "demo_0000") == 0
    assert "1 already as asked" in capsys.readouterr().out
    assert len(audit_lines(e)) == 2
    assert run_cli(e, NAME, "demo_0000", "--restore") == 0
    assert "1 episode(s) restored" in capsys.readouterr().out
    assert not exclusion.is_excluded(e.state()["demos"]["demo_0000"])


def test_the_command_reports_what_the_core_refused(served, capsys):
    e, _ = served
    jsonio.update(
        mirror.state_path(e.config, NAME),
        lambda v: v.update(current={"demos": ["demo_0001"], "done": []}),
    )
    assert run_cli(e, NAME, "demo_0000", "demo_0001") == 1
    err = capsys.readouterr().err
    assert "HTTP 409" in err and "being labelled" in err
    assert run_cli(e, NAME, "demo_0042") == 1
    err = capsys.readouterr().err
    assert "HTTP 404" in err and "demo_0042" in err
    assert run_cli(e, "nope", "demo_0000") == 1
    assert not exclusion.excluded_count(e.state())  # all or nothing


def test_the_command_needs_the_persons_key_and_an_agents_is_refused(served, capsys):
    e, root = served
    # Not the core's key: the middleware turns it away.
    (root / "human.key").write_text("someone-elses-key")
    assert run_cli(e, NAME, "demo_0000") == 1
    err = capsys.readouterr().err
    assert "HTTP 401" in err and "someone-elses-key" not in err
    # An agent's scoped token is not the person's: refused by name.
    (root / "human.key").write_text("test-scoped-token")
    assert run_cli(e, NAME, "demo_0000") == 1
    assert "HTTP 401" in capsys.readouterr().err
    assert not exclusion.excluded_count(e.state())
    # No key file, no core: a plain message and exit 2, nothing sent.
    (root / "human.key").unlink()
    assert run_cli(e, NAME, "demo_0000") == 2
    assert "cannot read the person's key" in capsys.readouterr().err
    (root / "human.key").write_text("test-human-key")
    (root / "api.sock").unlink()
    assert run_cli(e, NAME, "demo_0000") == 2
    assert "core is not running" in capsys.readouterr().err
    assert not exclusion.excluded_count(e.state())


def test_the_command_takes_a_suffixed_dataset_name(served, capsys):
    e, _ = served
    suffixed = NAME + mirror.ROOT_MARK + "root-2"
    renamed(e, suffixed)
    assert run_cli(e, suffixed, "demo_0001") == 0
    assert "removed" in capsys.readouterr().out
    assert exclusion.is_excluded(
        mirror.load_state(e.config, suffixed)["demos"]["demo_0001"]
    )
    assert not exclusion.excluded_count(e.state())


# --- the statistics record --------------------------------------------------------------------


def test_the_stats_record_says_whether_the_episode_is_removed(env):
    e = env()
    mirror_only(e, 0)

    class Stub(worker.Worker):
        def __init__(self, config, name):
            self.config, self.name = config, name
            self.lengths, self.gated, self.store = {}, (0, 0.0), None
            self.provider_spec = {"name": "p", "model": "m"}

    stub = Stub(e.config, NAME)

    def record():
        row = e.state()["demos"]["demo_0000"]
        return stub.stats_row("demo_0000", 0, row, [], first=True, frames=20, waking={})

    assert record()["excluded"] is False
    exclusion.exclude(e.config, NAME, ["demo_0000"])
    assert record()["excluded"] is True
    exclusion.restore(e.config, NAME, ["demo_0000"])
    assert record()["excluded"] is False
