"""Finished-session and live-pipeline deletion, using only temporary data."""

import contextlib
import json
import os
import shutil
import socket
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from levi import dataset_management as data_management
from levi.live import config as live_config
from levi.live import jsonio, management, mirror, sessions


@pytest.fixture
def live(tmp_path):
    config = live_config.Config()
    config.service.workspace = str(tmp_path / "workspace")
    config.service.home = str(tmp_path / "home")
    config.watch.roots = [str(tmp_path / "rollouts")]
    config.watch.backlog = "process"
    config.watch.settle_s = 0
    root = Path(config.watch.roots[0])
    group, task = "model", "pick_the_object"
    name = mirror.dataset_name(group, task)
    source = root / group / task
    demo = source / "demo_0000"
    demo.mkdir(parents=True)
    (demo / ".complete").write_text("")
    (demo / "metadata.json").write_text(json.dumps({"stopped_at": "done"}))
    (demo / "video.mp4").write_bytes(b"source video")
    capture = config.captures_dir / name
    (capture / "demo_0000").mkdir(parents=True)
    os.link(demo / "video.mp4", capture / "demo_0000/video.mp4")
    view = config.workspace / "outputs/LEVI/workbench/views" / name
    (view / "meta").mkdir(parents=True)
    (view / "meta/info.json").write_text('{"total_episodes": 1}')
    state = mirror.empty_state(config, (str(root), group, task), 0.0, name)
    state["demos"] = {
        "demo_0000": {"state": "done", "run_id": "run-1", "episode_index": 0}
    }
    jsonio.write(mirror.state_path(config, name), state)
    catalogue = config.workspace / "outputs/LEVI/workbench/datasets.json"
    jsonio.write(
        catalogue,
        {
            name: {"name": name, "path": str(capture), "view": str(view)},
            f"{name}--experiment": {
                "name": f"{name}--experiment",
                "base": name,
                "path": str(capture),
                "view": str(view),
            },
            "other": {"name": "other", "path": str(tmp_path / "other")},
        },
    )
    return {
        "config": config,
        "root": root,
        "group": group,
        "task": task,
        "name": name,
        "source": source,
        "capture": capture,
        "view": view,
        "catalogue": catalogue,
    }


def session(
    live, *, state="finished", session_id="s-1", pid=None, host=None, updated_at=None
):
    now = time.time()
    value = {
        "schema": "levi.eval.session.v1",
        "state": state,
        "group": live["group"],
        "task_folder": live["task"],
        "started_at": now - 100,
        "updated_at": updated_at or now - 30,
        "pid": os.getpid() if pid is None else pid,
        "host": socket.gethostname() if host is None else host,
        "run_id": "run-1",
        "levi": {"enabled": True},
    }
    if session_id is not None:
        value["session_id"] = session_id
    path = live["root"] / sessions.SESSION_DIR / f"{live['group']}__{live['task']}.json"
    jsonio.write(path, value)
    identity = {
        "root": str(live["root"]),
        "group": live["group"],
        "task_folder": live["task"],
        "session_id": session_id or "",
    }
    return identity, path, value


@contextlib.contextmanager
def error(code, detail):
    with pytest.raises(data_management.ManagementError, match=detail) as caught:
        yield
    assert caught.value.status_code == code


@pytest.fixture
def live_http(live, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from levi.live import api

    monkeypatch.setenv("LEVI_UI_TOKEN", "test-management-ui")
    monkeypatch.setattr(api, "_config", lambda: live["config"])
    monkeypatch.setattr(api, "_own", lambda: live["config"].workspace)
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        yield client


def test_session_http_requires_a_person_and_hides_deleted_record(live, live_http):
    identity, path, _ = session(live)
    endpoint = "/api/levi/live/sessions"
    before = path.read_bytes()
    assert len(live_http.get(endpoint).json()["sessions"]) == 1
    assert live_http.post(endpoint + "/deletion-plan", json=identity).status_code == 401
    headers = {"x-levi-ui-token": "test-management-ui"}
    assert (
        live_http.post(
            endpoint + "/deletion-plan",
            json=identity,
            headers={**headers, "authorization": "Bearer test-agent"},
        ).status_code
        == 403
    )
    plan = live_http.post(endpoint + "/deletion-plan", json=identity, headers=headers)
    assert plan.status_code == 200
    deleted = live_http.request(
        "DELETE",
        endpoint,
        json={**identity, "confirmation": plan.json()["confirmation"]},
        headers=headers,
    )
    assert deleted.status_code == 200 and deleted.json()["deleted"]
    assert live_http.get(endpoint).json()["sessions"] == []
    assert path.read_bytes() == before


def test_pipeline_http_delete_clears_list_and_blocks_old_routes(live, live_http):
    endpoint = "/api/levi/live/datasets/" + live["name"]
    headers = {"x-levi-ui-token": "test-management-ui"}
    plan = live_http.post(endpoint + "/deletion-plan", json={}, headers=headers)
    assert plan.status_code == 200
    removed = live_http.request(
        "DELETE",
        endpoint,
        json={"confirmation": plan.json()["confirmation"]},
        headers=headers,
    )
    assert removed.status_code == 200 and removed.json()["deleted"]
    assert (
        live["name"] not in live_http.get("/api/levi/live/datasets").json()["datasets"]
    )
    assert live_http.post(endpoint + "/prepare", headers=headers).status_code == 404
    assert (
        live["source"].is_dir() and live["capture"].is_dir() and live["view"].is_dir()
    )


def test_active_pipeline_http_returns_conflict_without_deleting(live, live_http):
    session(live, state="running")
    endpoint = "/api/levi/live/datasets/" + live["name"]
    response = live_http.post(
        endpoint + "/deletion-plan",
        json={},
        headers={"x-levi-ui-token": "test-management-ui"},
    )
    assert response.status_code == 409
    assert not management.archived(live["config"], live["name"])


def test_session_removal_writes_only_a_local_tombstone_and_is_idempotent(live):
    identity, path, _ = session(live)
    before = path.read_bytes()
    plan = management.session_plan(live["config"], identity)
    assert (plan["files"], plan["bytes"], plan["paths"]) == (0, 0, [])
    result = management.delete_session(live["config"], identity, plan["confirmation"])
    assert result["deleted"] and path.read_bytes() == before
    found = next(iter(sessions.read_sessions([live["root"]]).values()))
    assert management.session_deleted(live["config"], found)
    plan = management.session_plan(live["config"], identity)
    management.delete_session(live["config"], identity, plan["confirmation"])
    assert len(management.removed_sessions(live["config"])) == 1
    assert (
        live["source"].exists() and live["capture"].exists() and live["view"].exists()
    )


@pytest.mark.parametrize("change", ["updated_at", "session_id", "state"])
def test_new_or_resumed_session_becomes_visible_after_a_tombstone(live, change):
    identity, path, value = session(live)
    plan = management.session_plan(live["config"], identity)
    management.delete_session(live["config"], identity, plan["confirmation"])
    value[change] = {
        "updated_at": time.time(),
        "session_id": "s-2",
        "state": "standby",
    }[change]
    jsonio.write(path, value)
    found = sessions._session(str(path), path.name, value, time.time())
    found.root = str(live["root"])
    assert not management.session_deleted(live["config"], found)


def test_legacy_session_without_an_id_can_be_removed_and_resumed(live):
    identity, path, value = session(live, session_id=None)
    plan = management.session_plan(live["config"], identity)
    management.delete_session(live["config"], identity, plan["confirmation"])
    found = sessions._session(str(path), path.name, value, time.time())
    found.root = str(live["root"])
    assert management.session_deleted(live["config"], found)
    value["updated_at"] = time.time()
    jsonio.write(path, value)
    found.updated_at = value["updated_at"]
    assert not management.session_deleted(live["config"], found)


@pytest.mark.parametrize("state", ["running", "standby", "fault", "waiting_reset"])
def test_session_with_a_live_client_is_refused_even_when_not_inferencing(live, state):
    identity, _, _ = session(live, state=state)
    with error(409, "may still be running"):
        management.session_plan(live["config"], identity)


@pytest.mark.parametrize(
    "fields", [{"pid": 0}, {"pid": 99999999, "host": "other-host"}]
)
def test_unknown_or_remote_client_cannot_be_treated_as_stopped(live, fields):
    identity, _, _ = session(
        live, state="standby", updated_at=time.time() - 7200, **fields
    )
    with error(409, "may still be running"):
        management.session_plan(live["config"], identity)


def test_confirmed_dead_local_client_can_be_removed(live):
    identity, _, _ = session(live, state="running", pid=99999999)
    plan = management.session_plan(live["config"], identity)
    assert management.delete_session(live["config"], identity, plan["confirmation"])[
        "deleted"
    ]


def test_dead_client_is_confirmed_even_if_the_session_record_is_fresh(live):
    identity, _, _ = session(
        live, state="running", pid=99999999, updated_at=time.time()
    )
    assert management.session_plan(live["config"], identity)["confirmation"]


def test_session_before_registration_uses_its_future_pipeline_lock(live):
    identity, _, _ = session(live)
    cfg, name = live["config"], live["name"]
    mirror.state_path(cfg, name).unlink()
    with data_management.lock_for(cfg.workspace, name), error(409, "being updated"):
        management.session_plan(cfg, identity)


def test_current_label_batch_refuses_session_and_pipeline_deletion(live):
    identity, _, _ = session(live)
    path = mirror.state_path(live["config"], live["name"])
    value = jsonio.read(path)
    value["current"] = {"demos": ["demo_0000"], "run_id": "annotation-1"}
    jsonio.write(path, value)
    with error(409, "being labelled"):
        management.session_plan(live["config"], identity)
    with error(409, "being labelled"):
        management.dataset_plan(live["config"], live["name"])


@pytest.mark.parametrize("identity_change", ["root", "session_id", "task_folder"])
def test_session_identity_and_root_allowlist_are_validated(
    live, identity_change, tmp_path
):
    identity, _, _ = session(live)
    identity[identity_change] = {
        "root": str(tmp_path / "elsewhere"),
        "session_id": "s-2",
        "task_folder": "../private",
    }[identity_change]
    with pytest.raises(data_management.ManagementError):
        management.session_plan(live["config"], identity)


def test_replaced_session_after_preview_cannot_be_deleted(live):
    identity, path, value = session(live)
    plan = management.session_plan(live["config"], identity)
    value["updated_at"] = time.time()
    jsonio.write(path, value)
    with error(409, "changed since"):
        management.delete_session(live["config"], identity, plan["confirmation"])
    assert management.removed_sessions(live["config"]) == []


def test_record_only_pipeline_delete_retains_data_but_drops_registration(live):
    cfg, name = live["config"], live["name"]
    scanner = mirror.Scanner(cfg)
    assert scanner.scan()
    plan = management.dataset_plan(cfg, name)
    assert plan["source_retained"] and plan["annotations_retained"]
    assert plan["files"] == 0 and plan["paths"] == []
    result = management.delete_dataset(cfg, name, plan["confirmation"])
    assert result["deleted"] and management.archived(cfg, name)
    assert (
        live["capture"].exists() and live["view"].exists() and live["source"].exists()
    )
    assert jsonio.read(live["catalogue"]) == {
        "other": {"name": "other", "path": str(live["source"].parents[2] / "other")}
    }
    assert mirror.list_states(cfg) == {}
    assert name in mirror.list_states(cfg, include_archived=True)
    assert scanner.scan() == [] and mirror.Scanner(cfg).scan() == []
    assert mirror.waiting_demos(mirror.load_state(cfg, name), 3) == []
    assert all(
        count == 0 for count in mirror.counts(mirror.load_state(cfg, name)).values()
    )


def test_pipeline_physical_delete_unlinks_only_its_mirror_and_view(live):
    cfg, name = live["config"], live["name"]
    source_before = (live["source"] / "demo_0000/video.mp4").read_bytes()
    plan = management.dataset_plan(cfg, name, delete_files=True)
    assert plan["files"] == 2 and plan["bytes"] > 0
    assert set(plan["paths"]) == {str(live["capture"]), str(live["view"])}
    result = management.delete_dataset(
        cfg, name, plan["confirmation"], delete_files=True
    )
    assert result["deleted"] and not result["cleanup_pending"]
    assert not live["capture"].exists() and not live["view"].exists()
    assert (live["source"] / "demo_0000/video.mp4").read_bytes() == source_before
    assert not list(cfg.live_dir.glob(".delete-*"))


def test_changing_file_delete_scope_requires_a_new_preview(live):
    cfg, name = live["config"], live["name"]
    plan = management.dataset_plan(cfg, name)
    with error(409, "changed since"):
        management.delete_dataset(cfg, name, plan["confirmation"], delete_files=True)
    assert live["capture"].exists() and not management.archived(cfg, name)


@pytest.mark.parametrize("change", ["file", "state", "source", "catalogue"])
def test_pipeline_changes_after_preview_invalidate_its_confirmation(live, change):
    cfg, name = live["config"], live["name"]
    plan = management.dataset_plan(cfg, name, delete_files=True)
    if change == "file":
        (live["view"] / "meta/info.json").write_text('{"total_episodes": 2}')
    elif change == "state":
        path = mirror.state_path(cfg, name)
        value = jsonio.read(path)
        value["last_error"] = "new result"
        jsonio.write(path, value)
    elif change == "source":
        (live["source"] / "demo_0000/metadata.json").write_text('{"stopped_at": "new"}')
    else:
        value = jsonio.read(live["catalogue"])
        value[name]["view_status"] = "new"
        jsonio.write(live["catalogue"], value)
    with error(409, "changed since"):
        management.delete_dataset(cfg, name, plan["confirmation"], delete_files=True)
    assert live["capture"].exists() and live["view"].exists()
    assert not management.archived(cfg, name)


@pytest.mark.parametrize("location", ["captures", "view", "state", "session"])
def test_symbolic_links_are_refused_without_changing_their_targets(
    live, location, tmp_path
):
    cfg, name = live["config"], live["name"]
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("untouched")
    if location == "captures":
        (live["capture"] / "external").symlink_to(outside, target_is_directory=True)
    elif location == "view":
        (live["view"] / "external").symlink_to(outside, target_is_directory=True)
    elif location == "state":
        state = mirror.state_path(cfg, name)
        external = outside / "state.json"
        state.rename(external)
        state.symlink_to(external)
    else:
        _, path, _ = session(live)
        external = outside / "session.json"
        path.rename(external)
        path.symlink_to(external)
    with pytest.raises(data_management.ManagementError, match="symbolic"):
        management.dataset_plan(cfg, name, delete_files=True)
    assert (outside / "keep").read_text() == "untouched"


def test_busy_conversion_started_after_preview_refuses_deletion(live):
    cfg, name = live["config"], live["name"]
    plan = management.dataset_plan(cfg, name, delete_files=True)
    jsonio.write(
        cfg.workspace / "outputs/LEVI/workbench/jobs/new.json",
        {
            "status": "running",
            "source": str(live["capture"]),
        },
    )
    with error(409, "job uses this dataset"):
        management.delete_dataset(cfg, name, plan["confirmation"], delete_files=True)
    assert live["capture"].exists() and not management.archived(cfg, name)


def test_a_label_batch_started_after_preview_refuses_deletion(live):
    cfg, name = live["config"], live["name"]
    plan = management.dataset_plan(cfg, name, delete_files=True)
    path = mirror.state_path(cfg, name)
    value = jsonio.read(path)
    value["current"] = {"demos": ["demo_0000"]}
    jsonio.write(path, value)
    with error(409, "being labelled"):
        management.delete_dataset(cfg, name, plan["confirmation"], delete_files=True)
    assert live["capture"].exists() and not management.archived(cfg, name)


def test_a_product_workspace_is_never_accepted_as_a_live_deletion_target(
    live, monkeypatch
):
    monkeypatch.setattr(
        management.locate, "protected_workspaces", lambda: [live["config"].workspace]
    )
    with error(409, "product workspace"):
        management.dataset_plan(live["config"], live["name"])


def test_shared_management_lock_refuses_a_concurrent_view_or_label_worker(live):
    cfg, name = live["config"], live["name"]
    with data_management.lock_for(cfg.workspace, name), error(409, "being updated"):
        management.dataset_plan(cfg, name)


def test_live_partial_capture_is_refused_but_old_abandoned_capture_is_allowed(live):
    cfg, name = live["config"], live["name"]
    unfinished = live["source"] / "demo_0001"
    unfinished.mkdir()
    (unfinished / "metadata.json").write_text("{}")
    (unfinished / "side_camera_raw.avi").write_bytes(b"partial")
    with error(409, "still being written"):
        management.dataset_plan(cfg, name)
    for path in [unfinished, *unfinished.iterdir()]:
        old = time.time() - 7200
        os.utime(path, (old, old))
    assert management.dataset_plan(cfg, name)["confirmation"]


def test_catalogue_commit_failure_restores_staged_directories_and_original_records(
    live, monkeypatch
):
    cfg, name = live["config"], live["name"]
    plan = management.dataset_plan(cfg, name, delete_files=True)
    catalogue_before = jsonio.read(live["catalogue"])
    original = jsonio.write
    failed = False

    def fail_once(path, value, **kwargs):
        nonlocal failed
        if Path(path) == live["catalogue"] and not failed:
            failed = True
            raise OSError("simulated catalogue disk error")
        return original(path, value, **kwargs)

    monkeypatch.setattr(jsonio, "write", fail_once)
    with pytest.raises(OSError, match="simulated"):
        management.delete_dataset(cfg, name, plan["confirmation"], delete_files=True)
    assert not management.archived(cfg, name)
    assert live["capture"].is_dir() and live["view"].is_dir()
    assert jsonio.read(live["catalogue"]) == catalogue_before
    assert not list(cfg.live_dir.glob(".delete-*"))


def test_deleted_demo_and_archived_pipeline_cannot_be_mirrored_again(live):
    cfg, name = live["config"], live["name"]
    path = mirror.state_path(cfg, name)
    value = jsonio.read(path)
    value["demos"]["demo_0000"].update({"state": "mirrored", "deleted": True})
    jsonio.write(path, value)
    assert mirror.waiting_demos(mirror.load_state(cfg, name), 3) == []
    assert mirror.counts(mirror.load_state(cfg, name))["mirrored"] == 0
    assert mirror.mirror_dataset(cfg, value, ["demo_0000"]) == {}
    assert jsonio.read(path)["demos"]["demo_0000"]["deleted"]
    value["archived"] = True
    jsonio.write(path, value)
    assert mirror.mirror_dataset(cfg, value, ["demo_0001"]) == {}
    assert mirror.Scanner(cfg).scan() == []


def completed_demo(live, number, completed_at, *, marker=True, content=b"new video"):
    """A valid, stat-only capture; no real camera, video decoder or model."""
    demo = live["source"] / f"demo_{number:04d}"
    demo.mkdir(parents=True, exist_ok=True)
    jsonio.write(
        demo / "metadata.json",
        {
            "stopped_at": completed_at,
            "media_storage": {"video_frames_match_csv": True},
            "eval": {"run_id": f"new-run-{number}"},
        },
    )
    (demo / "events.csv").write_text("event\nepisode_end\n")
    # Replacing a source demo must leave an archived hard link untouched.
    temp = demo / ".new-video"
    temp.write_bytes(content)
    temp.replace(demo / "video.mp4")
    if marker:
        (demo / ".complete").write_text("")
    else:
        (demo / ".complete").unlink(missing_ok=True)
    for path in [*demo.iterdir(), demo]:
        os.utime(path, (completed_at, completed_at))
    return demo


def archive_pipeline(live, monkeypatch, *, name=None, delete_files=False, at=None):
    name = name or live["name"]
    with monkeypatch.context() as clock:
        if at is not None:
            clock.setattr(management, "time", SimpleNamespace(time=lambda: at))
        plan = management.dataset_plan(live["config"], name, delete_files=delete_files)
        removed = management.delete_dataset(
            live["config"], name, plan["confirmation"], delete_files=delete_files
        )
    assert removed["deleted"]
    return mirror.load_state(live["config"], name)


@pytest.mark.parametrize("delete_files", [False, True])
def test_archived_task_without_new_completion_does_not_create_a_successor(
    live, monkeypatch, delete_files
):
    completed_demo(live, 0, time.time() - 100)
    scanner = mirror.Scanner(live["config"])
    scanner.scan()
    archived = archive_pipeline(live, monkeypatch, delete_files=delete_files)
    assert scanner.scan() == []
    assert mirror.Scanner(live["config"]).scan() == []
    assert (
        mirror.resolve_name(live["config"], live["root"], live["group"], live["task"])
        == live["name"]
    )
    assert mirror.list_states(live["config"]) == {}
    assert mirror.list_states(live["config"], include_archived=True) == {
        live["name"]: archived
    }
    assert live["capture"].exists() is not delete_files
    assert live["view"].exists() is not delete_files
    assert live["source"].is_dir()


@pytest.mark.parametrize("delete_files", [False, True])
def test_new_completion_after_removal_has_its_own_pipeline_and_capture(
    live, monkeypatch, delete_files
):
    cfg = live["config"]
    old = completed_demo(live, 0, time.time() - 100)
    archive = archive_pipeline(live, monkeypatch, delete_files=delete_files)
    at = archive["archived_at"]
    # A recent metadata edit must not drag the old completion into the new view.
    os.utime(old / "metadata.json", (at + 1, at + 1))
    completed_demo(live, 1, at + 2)
    task = mirror.Scanner(cfg).scan(now=at + 10)[0]
    assert task.name == mirror.generation_name(live["name"], 2)
    assert task.ready == ["demo_0001"] and task.backlog == 1
    state = mirror.load_state(cfg, task.name)
    assert state["generation_base"] == live["name"]
    assert state["previous_generation"] == live["name"]
    assert state["archive_cutoff"] == at
    assert state["capture"] != str(live["capture"])
    # Even a caller supplying historical names cannot mirror them into this generation.
    result = mirror.mirror_dataset(cfg, state, ["demo_0000", "demo_0001"], now=at + 10)
    assert set(result) == {"demo_0001"}
    capture = Path(state["capture"])
    assert (capture / "demo_0001/video.mp4").read_bytes() == b"new video"
    assert not (capture / "demo_0000").exists()
    assert set(mirror.list_states(cfg)) == {task.name}
    assert mirror.load_state(cfg, live["name"]) == archive
    assert live["name"] not in jsonio.read(live["catalogue"])


def test_metadata_changes_alone_do_not_reopen_an_archived_task(live, monkeypatch):
    old = completed_demo(live, 0, time.time() - 100)
    archive = archive_pipeline(live, monkeypatch)
    meta = jsonio.read(old / "metadata.json")
    meta["eval"]["run_id"] = "edited-historical-metadata"
    jsonio.write(old / "metadata.json", meta)
    os.utime(old, None)
    assert mirror.Scanner(live["config"]).scan() == []
    assert set(mirror.list_states(live["config"], include_archived=True)) == {
        live["name"]
    }
    assert mirror.load_state(live["config"], live["name"]) == archive


def test_unmarked_captures_use_stopped_at_rather_than_later_file_mtimes(
    live, monkeypatch
):
    now = time.time()
    old = completed_demo(live, 0, now - 400, marker=False)
    archive = archive_pipeline(live, monkeypatch, at=now - 300)
    os.utime(old / "metadata.json", (now, now))
    assert mirror.Scanner(live["config"]).scan(now=now) == []
    completed_demo(live, 1, now - 100, marker=False)
    task = mirror.Scanner(live["config"]).scan(now=now)[0]
    assert task.ready == ["demo_0001"]
    state = mirror.load_state(live["config"], task.name)
    assert state["archive_cutoff"] == archive["archived_at"]
    assert set(
        mirror.mirror_dataset(
            live["config"], state, ["demo_0000", "demo_0001"], now=now
        )
    ) == {"demo_0001"}


def test_replaced_demo_number_does_not_overwrite_the_archived_mirror(live, monkeypatch):
    archived = archive_pipeline(live, monkeypatch)
    before = (live["capture"] / "demo_0000/video.mp4").read_bytes()
    shutil.rmtree(live["source"] / "demo_0000")
    completed_demo(live, 0, archived["archived_at"] + 1, content=b"replacement video")
    task = mirror.Scanner(live["config"]).scan(now=archived["archived_at"] + 10)[0]
    assert task.ready == ["demo_0000"]
    state = mirror.load_state(live["config"], task.name)
    mirror.mirror_dataset(live["config"], state, task.ready)
    assert (Path(state["capture"]) / "demo_0000/video.mp4").read_bytes() == (
        b"replacement video"
    )
    assert (live["capture"] / "demo_0000/video.mp4").read_bytes() == before
    assert mirror.load_state(live["config"], live["name"]) == archived


def test_generation_identity_and_cutoff_survive_restart_and_another_removal(
    live, monkeypatch
):
    cfg = live["config"]
    now = time.time()
    completed_demo(live, 0, now - 100)
    first = archive_pipeline(live, monkeypatch, at=now - 30)
    completed_demo(live, 1, now - 20)
    scanner = mirror.Scanner(cfg)
    task = scanner.scan(now=now)[0]
    successor = task.name
    state = mirror.load_state(cfg, successor)
    mirror.mirror_dataset(cfg, state, task.ready, now=now)
    restarted = mirror.Scanner(cfg)
    assert restarted.scan(now=now)[0].name == successor
    key = (str(live["root"]), live["group"], live["task"])
    assert restarted.name_of(key) == restarted.known_name(key) == successor
    assert mirror.resolve_name(cfg, *key) == successor
    cfg.watch.backlog = "ignore"
    assert mirror.Scanner(cfg).scan(now=now)[0].backlog == 1
    cfg.watch.backlog = "process"
    assert mirror.Scanner(cfg).scan(now=now)[0].ready == []
    second = archive_pipeline(live, monkeypatch, name=successor, at=now - 10)
    assert scanner.scan(now=now) == []
    assert restarted.scan(now=now) == []
    assert mirror.resolve_name(cfg, *key) == successor
    completed_demo(live, 2, now - 5)
    third = restarted.scan(now=now)[0]
    assert third.name == mirror.generation_name(live["name"], 3)
    assert third.ready == ["demo_0002"] and third.backlog == 2
    state = mirror.load_state(cfg, third.name)
    assert state["archive_cutoff"] == second["archived_at"]
    assert mirror.Scanner(cfg).scan(now=now)[0].name == third.name
    assert set(mirror.list_states(cfg)) == {third.name}
    assert mirror.load_state(cfg, live["name"]) == first
    assert mirror.load_state(cfg, successor) == second


def test_archived_generations_under_different_roots_stay_separate(
    live, monkeypatch, tmp_path
):
    cfg = live["config"]
    now = time.time()
    second_root = tmp_path / "second" / live["root"].name
    second = {
        **live,
        "root": second_root,
        "source": second_root / live["group"] / live["task"],
    }
    cfg.watch.roots.append(str(second_root))
    completed_demo(live, 0, now - 100, content=b"root one")
    completed_demo(second, 0, now - 100, content=b"root two")
    names = {task.key[0]: task.name for task in mirror.Scanner(cfg).scan(now=now)}
    second["name"] = names[str(second_root)]
    assert second["name"] != live["name"]
    archive_pipeline(live, monkeypatch, at=now - 30)
    # A still active second root must keep its original name.
    assert (
        mirror.resolve_name(cfg, second_root, live["group"], live["task"])
        == (second["name"])
    )
    archive_pipeline(second, monkeypatch, at=now - 30)
    completed_demo(live, 1, now - 20, content=b"root one new")
    completed_demo(second, 1, now - 20, content=b"root two new")
    cfg.watch.roots.reverse()
    tasks = mirror.Scanner(cfg).scan(now=now)
    actual = {task.key[0]: task.name for task in tasks}
    assert actual == {
        str(live["root"]): mirror.generation_name(live["name"], 2),
        str(second_root): mirror.generation_name(second["name"], 2),
    }
    for task in tasks:
        assert task.ready == ["demo_0001"]
        state = mirror.load_state(cfg, task.name)
        assert state["root"] == task.key[0]
        mirror.mirror_dataset(cfg, state, task.ready, now=now)
    assert {
        task.key[0]: task.name for task in mirror.Scanner(cfg).scan(now=now)
    } == actual
    assert set(mirror.list_states(cfg)) == set(actual.values())
    assert {
        (
            Path(mirror.load_state(cfg, name)["capture"]) / "demo_0001/video.mp4"
        ).read_bytes()
        for name in actual.values()
    } == {b"root one new", b"root two new"}


def test_pending_or_rejected_new_demo_does_not_create_a_pipeline(live, monkeypatch):
    archive = archive_pipeline(live, monkeypatch)
    demo = completed_demo(live, 1, archive["archived_at"] + 1)
    meta = jsonio.read(demo / "metadata.json")
    meta["stopped_at"] = None
    jsonio.write(demo / "metadata.json", meta)
    scanner = mirror.Scanner(live["config"])
    assert scanner.scan(now=archive["archived_at"] + 10) == []
    meta["stopped_at"] = archive["archived_at"] + 1
    meta["media_storage"]["video_frames_match_csv"] = False
    jsonio.write(demo / "metadata.json", meta)
    assert scanner.scan(now=archive["archived_at"] + 10) == []
    meta["media_storage"]["video_frames_match_csv"] = True
    jsonio.write(demo / "metadata.json", meta)
    assert scanner.scan(now=archive["archived_at"] + 10)[0].ready == ["demo_0001"]


def test_completion_at_the_archive_cutoff_is_not_a_new_generation(live, monkeypatch):
    archive = archive_pipeline(live, monkeypatch)
    completed_demo(live, 1, archive["archived_at"])
    assert mirror.Scanner(live["config"]).scan(now=archive["archived_at"] + 10) == []


def test_generation_name_collision_never_replaces_another_tasks_state(
    live, monkeypatch, tmp_path
):
    cfg = live["config"]
    archive = archive_pipeline(live, monkeypatch)
    occupied_name = mirror.generation_name(live["name"], 2)
    occupied = mirror.empty_state(
        cfg, (str(tmp_path / "another-root"), "another", "task"), 0.0, occupied_name
    )
    jsonio.write(mirror.state_path(cfg, occupied_name), occupied)
    completed_demo(live, 1, archive["archived_at"] + 1)
    task = mirror.Scanner(cfg).scan(now=archive["archived_at"] + 10)[0]
    assert task.name == mirror.generation_name(live["name"], 3)
    assert mirror.load_state(cfg, occupied_name) == occupied
    assert (
        mirror.Scanner(cfg).scan(now=archive["archived_at"] + 10)[0].name == task.name
    )


def test_long_generation_names_are_stable_and_accepted_by_the_live_api():
    from levi.live import api

    base = "model__" + "task_" * 25 + "unique-end"
    first = mirror.generation_name(base, 2)
    assert first == mirror.generation_name(base, 2)
    assert api.NAME.fullmatch(first)
    assert len(first) <= mirror.NAME_MAX
    assert first != mirror.generation_name(base + "other", 2)
    assert first != mirror.generation_name(base, 3)


def test_two_scanners_create_only_one_successor(live, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    archive = archive_pipeline(live, monkeypatch)
    completed_demo(live, 1, archive["archived_at"] + 1)
    start = Barrier(2)

    def scan():
        scanner = mirror.Scanner(live["config"])
        start.wait(timeout=5)
        return scanner.scan(now=archive["archived_at"] + 10)[0]

    with ThreadPoolExecutor(max_workers=2) as workers:
        tasks = list(workers.map(lambda _: scan(), range(2)))
    expected = mirror.generation_name(live["name"], 2)
    assert [task.name for task in tasks] == [expected, expected]
    assert all(task.ready == ["demo_0001"] for task in tasks)
    assert set(mirror.list_states(live["config"], include_archived=True)) == {
        live["name"],
        expected,
    }


def test_a_shortened_generation_name_is_found_again_after_restart(live, monkeypatch):
    cfg = live["config"]
    long = {**live, "group": "g" * 30, "task": "t" * 140}
    long["name"] = mirror.dataset_name(long["group"], long["task"])
    long["source"] = long["root"] / long["group"] / long["task"]
    completed_demo(long, 0, time.time() - 100)
    key = (str(long["root"]), long["group"], long["task"])
    jsonio.write(
        mirror.state_path(cfg, long["name"]),
        mirror.empty_state(cfg, key, 0.0, long["name"]),
    )
    archive = archive_pipeline(long, monkeypatch)
    completed_demo(long, 1, archive["archived_at"] + 1)
    expected = mirror.generation_name(long["name"], 2)
    assert len(expected) <= mirror.NAME_MAX
    first = next(
        task
        for task in mirror.Scanner(cfg).scan(now=archive["archived_at"] + 10)
        if task.key == key
    )
    assert first.name == expected and first.ready == ["demo_0001"]
    assert mirror.Scanner(cfg).name_of(key) == expected
    assert mirror.resolve_name(cfg, *key) == expected


@pytest.mark.parametrize("cached_empty_task", [False, True])
def test_a_generation_created_first_does_not_claim_a_real_task_with_its_name(
    live, monkeypatch, cached_empty_task
):
    cfg = live["config"]
    now = time.time()
    generation = mirror.generation_name(live["name"], 2)
    literal = {**live, "task": live["task"] + "__generation_000002"}
    literal["source"] = live["root"] / literal["group"] / literal["task"]
    assert mirror.dataset_name(literal["group"], literal["task"]) == generation
    original_key = (str(live["root"]), live["group"], live["task"])
    literal_key = (str(literal["root"]), literal["group"], literal["task"])
    scanner = mirror.Scanner(cfg)
    if cached_empty_task:
        literal["source"].mkdir(parents=True)
        assert scanner.name_of(literal_key) == generation
    archive_pipeline(live, monkeypatch, at=now - 30)
    completed_demo(live, 1, now - 20, content=b"generated pipeline video")
    generated_task = next(
        task for task in scanner.scan(now=now) if task.key == original_key
    )
    assert generated_task.name == generation
    generated_state = mirror.load_state(cfg, generation)
    mirror.mirror_dataset(cfg, generated_state, generated_task.ready, now=now)
    completed_demo(literal, 1, now - 10, content=b"literal task video")
    tasks = {task.key: task for task in scanner.scan(now=now)}
    literal_task = tasks[literal_key]
    assert literal_task.name != generation
    assert literal_task.ready == ["demo_0001"]
    assert tasks[original_key].name == generation
    literal_state = mirror.load_state(cfg, literal_task.name)
    assert literal_state["source"] == str(literal["source"])
    assert generated_state["source"] == str(live["source"])
    assert literal_state["capture"] != generated_state["capture"]
    mirror.mirror_dataset(cfg, literal_state, literal_task.ready, now=now)
    assert (
        Path(generated_state["capture"]) / "demo_0001/video.mp4"
    ).read_bytes() == b"generated pipeline video"
    assert (
        Path(literal_state["capture"]) / "demo_0001/video.mp4"
    ).read_bytes() == b"literal task video"
    expected = {original_key: generation, literal_key: literal_task.name}
    assert {
        task.key: task.name for task in mirror.Scanner(cfg).scan(now=now)
    } == expected
    for key, name in expected.items():
        assert mirror.resolve_name(cfg, *key) == name
        assert scanner.name_of(key) == scanner.known_name(key) == name


@pytest.mark.parametrize(
    "missing",
    [("root",), ("root", "group", "task_folder"), ("group", "task_folder", "source")],
)
def test_resolve_name_keeps_a_genuine_legacy_tasks_identity(live, missing):
    cfg = live["config"]
    state = jsonio.read(mirror.state_path(cfg, live["name"]))
    for field in missing:
        state.pop(field)
    jsonio.write(mirror.state_path(cfg, live["name"]), state)
    assert (
        mirror.resolve_name(cfg, live["root"], live["group"], live["task"])
        == live["name"]
    )


@pytest.mark.parametrize("field", ["group", "task_folder", "source"])
def test_resolve_name_does_not_reuse_an_explicitly_different_tasks_state(live, field):
    cfg = live["config"]
    state = jsonio.read(mirror.state_path(cfg, live["name"]))
    state[field] = (
        str(live["source"] / "different-task") if field == "source" else "different"
    )
    jsonio.write(mirror.state_path(cfg, live["name"]), state)
    assert (
        mirror.resolve_name(cfg, live["root"], live["group"], live["task"])
        != live["name"]
    )


def test_resolve_name_checks_identity_even_after_all_original_candidates_are_taken(
    live,
):
    from levi.live import api

    cfg = live["config"]
    key = (str(live["root"]), live["group"], live["task"])
    occupied = {}
    for number in range(3):
        name = mirror.resolve_name(cfg, *key)
        assert name not in occupied
        state = mirror.empty_state(
            cfg, (key[0], key[1], f"different_task_{number}"), 0.0, name
        )
        jsonio.write(mirror.state_path(cfg, name), state)
        occupied[name] = state
    available = mirror.resolve_name(cfg, *key)
    assert available not in occupied
    assert api.NAME.fullmatch(available)
    jsonio.write(
        mirror.state_path(cfg, available), mirror.empty_state(cfg, key, 0.0, available)
    )
    assert (
        mirror.resolve_name(cfg, *key) == mirror.Scanner(cfg).name_of(key) == available
    )
    assert all(
        mirror.load_state(cfg, name) == state for name, state in occupied.items()
    )


def test_two_empty_tasks_do_not_claim_one_name_in_the_same_root(live):
    cfg = live["config"]
    first_key = (str(live["root"]), "model__sub", "task")
    second_key = (first_key[0], "model", "sub__task")
    assert mirror.dataset_name(*first_key[1:]) == mirror.dataset_name(*second_key[1:])
    scanner = mirror.Scanner(cfg)
    first = scanner.name_of(first_key)
    second = scanner.name_of(second_key)
    assert first != second
    assert scanner.name_of(first_key) == first
    assert scanner.name_of(second_key) == second
